import asyncio
import datetime
import logging
import random
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.future import select

from app.database import InsulinInjection, LogEntry, User, async_session_local
from app.services.calculations import insulin_on_board
from app.services.iob_engine import suggest_bolus

from .common import (
    DOSE_CALC_HINTS,
    DirectLogStates,
    InsulinLogStates,
    _status_text_loop,
    _typing_loop,
    get_insulin_type_keyboard,
    parse_custom_datetime,
    parse_direct_entry_text,
    parse_today_time,
    resolve_timing_hint,
    safe_float,
)
from .food import show_basket_summary

logger = logging.getLogger(__name__)
glucose_insulin_router = Router()


# --- DIRECT ENTRY VIA PLAIN TEXT (e.g. "цукор 6.5", "болюс 4") ---

async def try_handle_direct_entry_text(message: Message, state: FSMContext, text: str) -> bool:
    """
    Matches "цукор 6.5" / "болюс 4" / "базал 12" / "інсулін 4" typed as plain text (optionally
    with a time/datetime suffix). Returns True if it handled the message (caller should return),
    False if nothing matched (caller should fall through to the next thing, e.g. general chat).
    """
    # Match "цукор 6.5" or "цукор 6.5 о 12:30" or "цукор 6.5 14.06 12:30"
    try:
        if re.match(r"^(?:цукор|blood sugar|sugar)\b", text, re.IGNORECASE):
            glucose, custom_time = parse_direct_entry_text(text, "цукор|blood sugar|sugar")
            if glucose is not None:
                if custom_time:
                    await log_blood_sugar_direct(message, glucose, custom_time)
                else:
                    await prompt_direct_entry_time(message, glucose, "sugar", state)
                return True
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return True

    # Match "болюс 4" or "болюс 4 о 12:30" or "болюс 4 14.06 12:30"
    try:
        if re.match(r"^(?:болюс|bolus)\b", text, re.IGNORECASE):
            dosage, custom_time = parse_direct_entry_text(text, "болюс|bolus")
            if dosage is not None:
                if custom_time:
                    await log_insulin_direct(message, dosage, "bolus", custom_time)
                else:
                    await prompt_direct_entry_time(message, dosage, "bolus", state)
                return True
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return True

    # Match "базал 12" or "базал 12 о 12:30" or "базал 12 14.06 12:30"
    try:
        if re.match(r"^(?:базал|basal)\b", text, re.IGNORECASE):
            dosage, custom_time = parse_direct_entry_text(text, "базал|basal")
            if dosage is not None:
                if custom_time:
                    await log_insulin_direct(message, dosage, "basal", custom_time)
                else:
                    await prompt_direct_entry_time(message, dosage, "basal", state)
                return True
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return True

    # Match "інсулін 4" or "інсулін 4 о 12:30" or "інсулін 4 14.06 12:30"
    try:
        if re.match(r"^(?:інсулін|insulin)\b", text, re.IGNORECASE):
            dosage, custom_time = parse_direct_entry_text(text, "інсулін|insulin")
            if dosage is not None:
                if custom_time:
                    await state.update_data(insulin_custom_time_str=custom_time.isoformat())
                await state.update_data(insulin_dosage=dosage)
                await state.set_state(InsulinLogStates.waiting_for_type)
                await message.answer("💉 <b>Виберіть тип інсуліну для цієї дози:</b>", reply_markup=get_insulin_type_keyboard())
                return True
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return True

    return False


# --- DIRECT METRICS LOGGING ---

async def log_blood_sugar_direct(message: Message, glucose: float, custom_time: datetime.datetime = None, telegram_id: int = None):
    """Handles parsing and logging raw blood sugar checks."""
    if telegram_id is None:
        telegram_id = message.from_user.id

    progress_msg = await message.answer(random.choice(DOSE_CALC_HINTS))
    text_task = asyncio.create_task(_status_text_loop(progress_msg, DOSE_CALC_HINTS))
    typing_task = asyncio.create_task(_typing_loop(message.chat.id))

    async with async_session_local() as session:
        async with session.begin():
            new_log = LogEntry(
                telegram_id=telegram_id,
                glucose_level=glucose
            )
            if custom_time:
                new_log.timestamp = custom_time
            session.add(new_log)

            result = await session.execute(
                select(User).filter(User.telegram_id == telegram_id)
            )
            user = result.scalars().first()

            iob_cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=24)
            inj_result = await session.execute(
                select(InsulinInjection).filter(
                    InsulinInjection.telegram_id == telegram_id,
                    InsulinInjection.timestamp >= iob_cutoff
                )
            )
            injections = inj_result.scalars().all()

            # Check and trigger predictive alert
            from app.services.alerts import check_and_trigger_glucose_alert
            await check_and_trigger_glucose_alert(telegram_id, session)

    current_iob = insulin_on_board(telegram_id, injections, user.dia_hours)

    # Calculate insulin suggestion for glucose correction alone
    suggestion = suggest_bolus(
        carbs=0.0,
        current_glucose=glucose,
        icr=user.icr,
        isf=user.isf,
        target_bg=user.target_bg,
        current_iob=current_iob,
        weight=user.weight,
        insulin_type=user.insulin_type
    )

    suggested_val = suggestion["total_suggested_dose"]

    suggestion["timing_hint"] = await resolve_timing_hint(
        telegram_id=telegram_id,
        carbs=0.0,
        current_glucose=glucose,
        suggested_dose=suggested_val,
        current_iob=current_iob,
        user=user,
        default_hint=suggestion["timing_hint"]
    )

    # Show custom time suffix if logged retroactively
    time_suffix = ""
    if custom_time:
        kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
        local_time_str = custom_time.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz).strftime("%H:%M")
        time_suffix = f" (записано на {local_time_str})"

    response_text = (
        f"🩸 <b>Рівень цукру зафіксовано:</b> {glucose} ммоль/л{time_suffix}\n\n"
        f"📊 <b>Активний інсулін (IOB):</b> {current_iob} ОД\n"
        f"💉 <b>Рекомендована доза для корекції:</b> {suggested_val} ОД\n\n"
        f"👉 {suggestion['timing_hint']}\n\n"
        f"{suggestion['disclaimer']}"
    )

    text_task.cancel()
    typing_task.cancel()
    await progress_msg.delete()

    if suggested_val > 0:
        log_inj_btn = InlineKeyboardButton(
            text=f"💉 Записати введення {suggested_val} ОД",
            callback_data=f"log_insulin:{suggested_val}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[log_inj_btn]])
        await message.answer(response_text, reply_markup=keyboard)
    else:
        await message.answer(response_text)


async def log_insulin_direct(message: Message, dosage: float, injection_type: str = "bolus", custom_time: datetime.datetime = None, telegram_id: int = None):
    """Handles raw dosage inputs."""
    if telegram_id is None:
        telegram_id = message.from_user.id
    type_name = "Болюс (швидкий)" if injection_type == "bolus" else "Базал (довгий)"

    async with async_session_local() as session:
        async with session.begin():
            new_injection = InsulinInjection(
                telegram_id=telegram_id,
                dosage=dosage,
                injection_type=injection_type
            )
            if custom_time:
                new_injection.timestamp = custom_time
            session.add(new_injection)
            logger.info(f"Direct logged insulin injection: {dosage}U ({injection_type})")

            # Check and trigger predictive alert
            from app.services.alerts import check_and_trigger_glucose_alert
            await check_and_trigger_glucose_alert(telegram_id, session)

    time_suffix = ""
    if custom_time:
        kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
        local_time_str = custom_time.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz).strftime("%H:%M")
        time_suffix = f" на {local_time_str}"

    await message.answer(f"✅ <b>Введення інсуліну ({type_name}: {dosage} ОД) успішно зафіксовано в системі{time_suffix}!</b>")


@glucose_insulin_router.callback_query(F.data.startswith("log_insulin:"))
async def log_insulin_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, val_str = callback_query.data.split(":")
    dosage = safe_float(val_str)

    telegram_id = callback_query.from_user.id

    state_data = await state.get_data()
    meal_custom_time_str = state_data.get("meal_custom_time_str")

    custom_timestamp = None
    if meal_custom_time_str:
        try:
            custom_timestamp = datetime.datetime.fromisoformat(meal_custom_time_str)
        except Exception:
            pass
        # Clear it so it doesn't leak into subsequent logs
        await state.update_data(meal_custom_time_str=None)

    async with async_session_local() as session:
        async with session.begin():
            new_injection = InsulinInjection(
                telegram_id=telegram_id,
                dosage=dosage,
                injection_type="bolus"
            )
            if custom_timestamp:
                new_injection.timestamp = custom_timestamp
            session.add(new_injection)

    time_suffix = ""
    if custom_timestamp:
        kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
        local_time_str = custom_timestamp.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz).strftime("%H:%M")
        time_suffix = f" на {local_time_str}"

    await callback_query.message.answer(f"✅ <b>Введення інсуліну ({dosage} ОД) успішно зафіксовано в системі{time_suffix}!</b>")


@glucose_insulin_router.callback_query(F.data == "edit_insulin_prompt")
async def edit_insulin_prompt_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.set_state(InsulinLogStates.waiting_for_type)
    await callback_query.message.answer("💉 <b>Виберіть тип інсуліну:</b>", reply_markup=get_insulin_type_keyboard())


async def handle_custom_insulin_value(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть додатне число:")
        return

    data = await state.get_data()
    meal_custom_time_str = data.get("meal_custom_time_str")
    custom_timestamp = None
    if meal_custom_time_str:
        try:
            custom_timestamp = datetime.datetime.fromisoformat(meal_custom_time_str)
        except Exception:
            pass

    await state.clear()
    await log_insulin_direct(message, val, "bolus", custom_timestamp)


@glucose_insulin_router.callback_query(F.data.startswith("insulin_select_type:"))
async def insulin_select_type_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, ins_type = callback_query.data.split(":")
    await state.update_data(insulin_type=ins_type)

    # Check if we already have a dosage (e.g. from direct "інсулін 4" matching)
    state_data = await state.get_data()
    dosage = state_data.get("insulin_dosage")

    if dosage is not None:
        custom_time_str = state_data.get("insulin_custom_time_str")
        custom_timestamp = None
        if custom_time_str:
            try:
                custom_timestamp = datetime.datetime.fromisoformat(custom_time_str)
            except Exception:
                pass

        # Directly log and finish
        await state.set_state(None)
        await state.update_data(insulin_dosage=None, insulin_type=None, insulin_custom_time_str=None)
        await log_insulin_direct(callback_query.message, dosage, ins_type, custom_timestamp, telegram_id=callback_query.from_user.id)
        # Delete original query message to keep clean
        await callback_query.message.delete()
    else:
        # Ask for dosage
        await state.set_state(InsulinLogStates.waiting_for_dosage)
        type_name = "Болюс (швидкий)" if ins_type == "bolus" else "Базал (довгий)"
        await callback_query.message.edit_text(
            f"💉 Ви вибрали: <b>{type_name}</b>.\n"
            f"Введіть дозу інсуліну в ОД (наприклад, <code>5.5</code>):"
        )


async def handle_insulin_dosage_value(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть додатне число:")
        return

    data = await state.get_data()
    ins_type = data.get("insulin_type", "bolus")

    # Store dosage in state
    await state.update_data(insulin_dosage=val)

    type_name = "Болюс (швидкий)" if ins_type == "bolus" else "Базал (довгий)"

    meal_custom_time_str = data.get("meal_custom_time_str")
    meal_time_display = None
    if meal_custom_time_str:
        try:
            kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
            dt = datetime.datetime.fromisoformat(meal_custom_time_str).replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz)
            meal_time_display = dt.strftime("%d.%m %H:%M")
        except Exception:
            pass

    confirm_text = (
        f"💉 <b>Підтвердження введення інсуліну:</b>\n\n"
        f"• <b>Тип:</b> {type_name}\n"
        f"• <b>Доза:</b> {val} ОД\n"
    )
    if meal_time_display:
        confirm_text += f"• <b>Час прийому їжі:</b> {meal_time_display}\n"

    confirm_text += "\nВиберіть час запису інсуліну:"

    inline_keyboard = []
    if meal_time_display:
        inline_keyboard.append([InlineKeyboardButton(text=f"✅ Записати на {meal_time_display}", callback_data="confirm_insulin_final:meal_time")])

    inline_keyboard.append([InlineKeyboardButton(text="🕒 Поточний час (Зараз)", callback_data="confirm_insulin_final:now")])
    inline_keyboard.append([InlineKeyboardButton(text="📅 Сьогодні (вказати час)", callback_data="confirm_insulin_final:today")])
    inline_keyboard.append([InlineKeyboardButton(text="📆 Інша дата та час", callback_data="confirm_insulin_final:custom")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    await message.answer(confirm_text, reply_markup=keyboard)


@glucose_insulin_router.callback_query(F.data.startswith("confirm_insulin_final:"))
async def confirm_insulin_final_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, choice = callback_query.data.split(":")

    data = await state.get_data()
    dosage = data.get("insulin_dosage")
    ins_type = data.get("insulin_type", "bolus")

    if dosage is None:
        await callback_query.message.answer("❌ Помилка: доза інсуліну не знайдена.")
        await state.clear()
        return

    if choice == "now":
        await state.clear()
        await log_insulin_direct(callback_query.message, dosage, ins_type, telegram_id=callback_query.from_user.id)
        await callback_query.message.delete()
    elif choice == "meal_time":
        meal_custom_time_str = data.get("meal_custom_time_str")
        custom_timestamp = None
        if meal_custom_time_str:
            try:
                custom_timestamp = datetime.datetime.fromisoformat(meal_custom_time_str)
            except Exception:
                pass
        await state.clear()
        await log_insulin_direct(callback_query.message, dosage, ins_type, custom_timestamp, telegram_id=callback_query.from_user.id)
        await callback_query.message.delete()
    elif choice == "today":
        await state.set_state(InsulinLogStates.waiting_for_today_time)
        await callback_query.message.edit_text("🕒 Введіть час введення інсуліну сьогодні у форматі <b>ГГ:ХХ</b> (наприклад, <code>13:45</code>):")
    elif choice == "custom":
        await state.set_state(InsulinLogStates.waiting_for_custom_datetime)
        await callback_query.message.edit_text("📆 Введіть дату та час введення інсуліну у форматі <b>ДД.ММ ГГ:ХХ</b> (наприклад, <code>14.06 13:45</code>):")


async def handle_insulin_today_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        custom_timestamp = parse_today_time(time_str)
        data = await state.get_data()
        dosage = data.get("insulin_dosage")
        ins_type = data.get("insulin_type", "bolus")

        if dosage is None:
            await message.answer("❌ Помилка: дозу не знайдено.")
            await state.clear()
            return

        await state.clear()
        await log_insulin_direct(message, dosage, ins_type, custom_timestamp)
    except ValueError:
        await message.answer("❌ Некоректний формат часу. Будь ласка, введіть час у форматі ГГ:ХХ (наприклад, 13:45):")


async def handle_insulin_custom_datetime(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        custom_timestamp = parse_custom_datetime(time_str)
        data = await state.get_data()
        dosage = data.get("insulin_dosage")
        ins_type = data.get("insulin_type", "bolus")

        if dosage is None:
            await message.answer("❌ Помилка: дозу не знайдено.")
            await state.clear()
            return

        await state.clear()
        await log_insulin_direct(message, dosage, ins_type, custom_timestamp)
    except ValueError as e:
        await message.answer(f"❌ {e}. Будь ласка, введіть дату та час у форматі ДД.ММ ГГ:ХХ (наприклад, 14.06 13:45):")


async def handle_insulin_custom_time(message: Message, state: FSMContext):
    # Backward compatibility handler if called
    await handle_insulin_today_time(message, state)


@glucose_insulin_router.message(Command("insulin"))
async def insulin_command_handler(message: Message, state: FSMContext):
    """Handles the /insulin command."""
    await state.set_state(InsulinLogStates.waiting_for_type)
    await message.answer("💉 <b>Виберіть тип інсуліну:</b>", reply_markup=get_insulin_type_keyboard())


@glucose_insulin_router.callback_query(F.data.startswith("confirm_pending:"))
async def confirm_pending_callback(callback_query: CallbackQuery, state: FSMContext):
    """Saves ONLY the pending item this specific button represents (glucose or food) —
    each is confirmed independently, so accepting one doesn't force-accept the other."""
    await callback_query.answer()
    which = callback_query.data.split(":", 1)[1]
    data = await state.get_data()
    telegram_id = callback_query.from_user.id

    if which == "glucose":
        temp_glucose = data.get("temp_glucose")
        if temp_glucose is None:
            await callback_query.message.answer("❌ Дані застаріли або не знайдені.")
            return
        async with async_session_local() as session:
            async with session.begin():
                session.add(LogEntry(telegram_id=telegram_id, glucose_level=temp_glucose))
                from app.services.alerts import check_and_trigger_glucose_alert
                await check_and_trigger_glucose_alert(telegram_id, session)
        await state.update_data(temp_glucose=None)
        await callback_query.message.answer(f"🩸 Цукор {temp_glucose} ммоль/л збережено!")

    elif which == "food":
        temp_food = data.get("temp_food")
        if not temp_food:
            await callback_query.message.answer("❌ Дані про страву застаріли або не знайдені.")
            return
        basket = data.get("basket", [])
        basket.append(temp_food)
        await state.update_data(basket=basket, temp_food=None)
        await callback_query.message.answer("🥗 Страву додано до тарілки!")
        await show_basket_summary(callback_query.message, state)

    # Keep only the button(s) for whatever is still pending — don't force a decision
    # on the other item just because this one got confirmed.
    remaining = await state.get_data()
    remaining_buttons = []
    if remaining.get("temp_glucose") is not None:
        remaining_buttons.append(InlineKeyboardButton(text="🩸 Зберегти цукор", callback_data="confirm_pending:glucose"))
    if remaining.get("temp_food"):
        remaining_buttons.append(InlineKeyboardButton(text="🥗 Зберегти страву", callback_data="confirm_pending:food"))
    try:
        new_markup = InlineKeyboardMarkup(inline_keyboard=[remaining_buttons]) if remaining_buttons else None
        await callback_query.message.edit_reply_markup(reply_markup=new_markup)
    except Exception:
        pass  # keyboard already gone / message too old to edit — harmless


async def prompt_direct_entry_time(message: Message, value: float, type_key: str, state: FSMContext):
    if type_key == "sugar":
        await state.update_data(direct_glucose=value)
        title = f"🩸 <b>Рівень цукру: {value} ммоль/л</b>"
        prefix = "confirm_direct_sugar"
    elif type_key == "bolus":
        await state.update_data(direct_insulin_dosage=value, direct_insulin_type="bolus")
        title = f"💉 <b>Болюс інсуліну: {value} ОД</b>"
        prefix = "confirm_direct_insulin"
    else:  # basal
        await state.update_data(direct_insulin_dosage=value, direct_insulin_type="basal")
        title = f"⏳ <b>Базал інсуліну: {value} ОД</b>"
        prefix = "confirm_direct_insulin"

    confirm_text = f"{title}\n\nВиберіть час запису:"
    now_btn = InlineKeyboardButton(text="🕒 Поточний час (Зараз)", callback_data=f"{prefix}:now")
    today_btn = InlineKeyboardButton(text="📅 Сьогодні (вказати час)", callback_data=f"{prefix}:today")
    custom_btn = InlineKeyboardButton(text="📆 Інша дата та час", callback_data=f"{prefix}:custom")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[now_btn], [today_btn], [custom_btn]])
    await message.answer(confirm_text, reply_markup=keyboard)


@glucose_insulin_router.callback_query(F.data.startswith("confirm_direct_sugar:"))
async def confirm_direct_sugar_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, choice = callback_query.data.split(":")

    data = await state.get_data()
    glucose = data.get("direct_glucose")
    if glucose is None:
        await callback_query.message.answer("❌ Помилка: значення цукру не знайдено.")
        await state.clear()
        return

    if choice == "now":
        await state.clear()
        await log_blood_sugar_direct(callback_query.message, glucose, telegram_id=callback_query.from_user.id)
        await callback_query.message.delete()
    elif choice == "today":
        await state.set_state(DirectLogStates.waiting_for_sugar_time)
        await callback_query.message.edit_text("🕒 Введіть час вимірювання цукру сьогодні у форматі <b>ГГ:ХХ</b> (наприклад, <code>13:45</code>):")
    elif choice == "custom":
        await state.set_state(DirectLogStates.waiting_for_sugar_datetime)
        await callback_query.message.edit_text("📆 Введіть дату та час вимірювання цукру у форматі <b>ДД.ММ ГГ:ХХ</b> (наприклад, <code>14.06 13:45</code>):")


async def handle_direct_sugar_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        custom_timestamp = parse_today_time(time_str)
        data = await state.get_data()
        glucose = data.get("direct_glucose")
        if glucose is None:
            await message.answer("❌ Помилка: значення цукру не знайдено.")
            await state.clear()
            return
        await state.clear()
        await log_blood_sugar_direct(message, glucose, custom_timestamp, telegram_id=message.from_user.id)
    except ValueError:
        await message.answer("❌ Некоректний формат часу. Будь ласка, введіть час у форматі ГГ:ХХ (наприклад, 13:45):")


async def handle_direct_sugar_datetime(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        custom_timestamp = parse_custom_datetime(time_str)
        data = await state.get_data()
        glucose = data.get("direct_glucose")
        if glucose is None:
            await message.answer("❌ Помилка: значення цукру не знайдено.")
            await state.clear()
            return
        await state.clear()
        await log_blood_sugar_direct(message, glucose, custom_timestamp, telegram_id=message.from_user.id)
    except ValueError as e:
        await message.answer(f"❌ {e}. Будь ласка, введіть дату та час у форматі ДД.ММ ГГ:ХХ (наприклад, 14.06 13:45):")


@glucose_insulin_router.callback_query(F.data.startswith("confirm_direct_insulin:"))
async def confirm_direct_insulin_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, choice = callback_query.data.split(":")

    data = await state.get_data()
    dosage = data.get("direct_insulin_dosage")
    ins_type = data.get("direct_insulin_type", "bolus")
    if dosage is None:
        await callback_query.message.answer("❌ Помилка: дозу не знайдено.")
        await state.clear()
        return

    if choice == "now":
        await state.clear()
        await log_insulin_direct(callback_query.message, dosage, ins_type, telegram_id=callback_query.from_user.id)
        await callback_query.message.delete()
    elif choice == "today":
        await state.set_state(DirectLogStates.waiting_for_insulin_time)
        await callback_query.message.edit_text("🕒 Введіть час введення інсуліну сьогодні у форматі <b>ГГ:ХХ</b> (наприклад, <code>13:45</code>):")
    elif choice == "custom":
        await state.set_state(DirectLogStates.waiting_for_insulin_datetime)
        await callback_query.message.edit_text("📆 Введіть дату та час введення інсуліну у форматі <b>ДД.ММ ГГ:ХХ</b> (наприклад, <code>14.06 13:45</code>):")


async def handle_direct_insulin_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        custom_timestamp = parse_today_time(time_str)
        data = await state.get_data()
        dosage = data.get("direct_insulin_dosage")
        ins_type = data.get("direct_insulin_type", "bolus")
        if dosage is None:
            await message.answer("❌ Помилка: дозу не знайдено.")
            await state.clear()
            return
        await state.clear()
        await log_insulin_direct(message, dosage, ins_type, custom_timestamp)
    except ValueError:
        await message.answer("❌ Некоректний формат часу. Будь ласка, введіть час у форматі ГГ:ХХ (наприклад, 13:45):")


async def handle_direct_insulin_datetime(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        custom_timestamp = parse_custom_datetime(time_str)
        data = await state.get_data()
        dosage = data.get("direct_insulin_dosage")
        ins_type = data.get("direct_insulin_type", "bolus")
        if dosage is None:
            await message.answer("❌ Помилка: дозу не знайдено.")
            await state.clear()
            return
        await state.clear()
        await log_insulin_direct(message, dosage, ins_type, custom_timestamp)
    except ValueError as e:
        await message.answer(f"❌ {e}. Будь ласка, введіть дату та час у форматі ДД.ММ ГГ:ХХ (наприклад, 14.06 13:45):")
