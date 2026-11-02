import asyncio
import datetime
import logging
import random

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.future import select

from app.bot.bot import bot
from app.database import InsulinInjection, LogEntry, User, async_session_local
from app.services.ai_service import ai_service
from app.services import nightscout
from app.services.calculations import insulin_on_board
from app.services.iob_engine import suggest_bolus

from .common import (
    DOSE_CALC_HINTS,
    FOOD_ANALYSIS_HINTS,
    FoodLogStates,
    _status_text_loop,
    _typing_loop,
    animated_status,
    gate_ai_access,
    parse_custom_datetime,
    parse_today_time,
    resolve_timing_hint,
    safe_float,
)

logger = logging.getLogger(__name__)
food_router = Router()


# --- TEXT & PHOTO PROCESSING FOR FOOD LOGGING ---

@food_router.message(F.photo)
async def photo_food_handler(message: Message, state: FSMContext):
    """Downloads image and sends it to OpenAI Vision for analysis if in food input state."""
    current_state = await state.get_state()
    if current_state != FoodLogStates.waiting_for_food_input:
        await message.answer("🥗 Щоб проаналізувати страву по фото, спочатку натисніть кнопку <b>🥗 Додати страву</b>.")
        return

    # Reset state while processing
    await state.set_state(None)

    async with async_session_local() as session:
        async with session.begin():
            user_res = await session.execute(select(User).filter(User.telegram_id == message.from_user.id))
            user = user_res.scalars().first()
            if not user:
                user = User(telegram_id=message.from_user.id, icr=10.0, isf=3.0, target_bg=5.5, dia_hours=4.0)
                session.add(user)
    if not await gate_ai_access(message, user):
        return

    progress_msg = await message.answer("🔄 <i>ШІ аналізує фото страви... Будь ласка, зачекайте...</i>")

    progress_deleted = False
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_bytes = await bot.download_file(file_info.file_path)
        photo_data = photo_bytes.read()

        async with animated_status(message.chat.id, progress_msg, FOOD_ANALYSIS_HINTS):
            analysis = await ai_service.analyze_food(
                photo_bytes=photo_data,
                text_description=message.caption
            )
        await progress_msg.delete()
        progress_deleted = True

        if analysis.needs_clarification and analysis.clarification_question:
            # Save photo and caption for re-use in clarification follow-up
            import base64 as b64
            await state.update_data(
                clarification_photo_b64=b64.b64encode(photo_data).decode("utf-8"),
                clarification_text=message.caption,
            )
            await state.set_state(FoodLogStates.waiting_for_clarification)
            await message.answer(
                f"❓ <b>Уточнення:</b> {analysis.clarification_question}\n"
                f"<i>(або надішліть будь-яку відповідь, щоб продовжити)</i>"
            )
        else:
            # Normal flow — save analysis to state and show results
            await state.update_data(
                carbs=analysis.carbohydrates,
                protein=analysis.protein,
                fat=analysis.fat,
                calories=analysis.calories,
                sugar=analysis.sugar,
                desc=", ".join(analysis.ingredients).replace(":", ""),
                confidence=analysis.confidence_score,
                data_source=analysis.data_source,
                portion=analysis.portion_estimate
            )
            await show_food_analysis_results(message, state)

    except Exception as e:
        logger.error(f"Error analyzing photo: {e}")
        if progress_deleted:
            await message.answer("❌ Помилка розпізнавання фото. Спробуйте надіслати текстовий опис.")
        else:
            await progress_msg.edit_text("❌ Помилка розпізнавання фото. Спробуйте надіслати текстовий опис.")


async def handle_food_clarification_answer(message: Message, state: FSMContext):
    """
    Called when the user replies to an AI clarification question about their food.
    Re-runs analysis using the original photo/text + the user's answer.
    """
    await state.set_state(None)
    data = await state.get_data()
    original_text = data.get("clarification_text")
    photo_b64 = data.get("clarification_photo_b64")

    async with async_session_local() as session:
        async with session.begin():
            user_res = await session.execute(select(User).filter(User.telegram_id == message.from_user.id))
            user = user_res.scalars().first()
            if not user:
                user = User(telegram_id=message.from_user.id, icr=10.0, isf=3.0, target_bg=5.5, dia_hours=4.0)
                session.add(user)
    if not await gate_ai_access(message, user):
        return

    progress_msg = await message.answer("🔄 <i>Перераховую оцінку з урахуванням вашої відповіді...</i>")
    try:
        import base64 as b64
        photo_bytes = b64.b64decode(photo_b64) if photo_b64 else None

        async with animated_status(message.chat.id, progress_msg, FOOD_ANALYSIS_HINTS):
            analysis = await ai_service.analyze_food_clarified(
                user_answer=message.text,
                original_text=original_text,
                photo_bytes=photo_bytes,
            )
        await progress_msg.delete()
        await state.update_data(
            carbs=analysis.carbohydrates,
            protein=analysis.protein,
            fat=analysis.fat,
            calories=analysis.calories,
            sugar=analysis.sugar,
            desc=", ".join(analysis.ingredients).replace(":", ""),
            confidence=analysis.confidence_score,
            data_source=analysis.data_source,
            portion=analysis.portion_estimate,
        )
        await show_food_analysis_results(message, state)
    except Exception as e:
        logger.error(f"Error in clarification re-analysis: {e}")
        await progress_msg.edit_text("❌ Помилка перерахунку. Спробуйте знову.")


async def show_food_analysis_results(message: Message, state: FSMContext):
    """Presents structured estimates from FSM state data with action keys."""
    data = await state.get_data()
    carbs = data.get("carbs", 0.0)
    protein = data.get("protein", 0.0)
    fat = data.get("fat", 0.0)
    calories = data.get("calories", 0.0)
    sugar = data.get("sugar", 0.0)
    desc = data.get("desc", "Прийом їжі")
    confidence = data.get("confidence", 1.0)
    data_source = data.get("data_source", "ai")
    portion = data.get("portion", "")

    source_labels = {
        "barcode_db": "📦 База даних Open Food Facts (за штрихкодом)",
        "name_db":    "🔍 База даних Open Food Facts (за назвою)",
        "usda":       "🌾 База даних USDA FoodData Central",
        "web_search": "🌐 Пошук в інтернеті (DuckDuckGo)",
        "ai":         "🤖 Оцінка ШІ (Vision)",
    }
    source_label = source_labels.get(data_source, "🤖 Оцінка ШІ")

    portion_line = f"• <b>Порція (оцінка ШІ):</b> <i>{portion}</i>\n" if portion else ""

    response_text = (
        f"🥗 <b>Аналіз страви:</b>\n\n"
        f"{portion_line}"
        f"• <b>Вуглеводи:</b> {carbs}г\n"
        f"• <b>Білки:</b> {protein}г\n"
        f"• <b>Жири:</b> {fat}г\n"
        f"• <b>Цукор:</b> {sugar}г\n"
        f"• <b>Калорії:</b> {calories} ккал\n"
        f"• <b>Опис:</b> <i>{desc}</i>\n"
        f"• <b>Точність:</b> {int(confidence * 100)}%\n"
        f"• <b>Джерело даних:</b> {source_label}\n\n"
        f"Виберіть дію нижче:"
    )

    add_btn = InlineKeyboardButton(
        text="➕ Додати до тарілки",
        callback_data="add_to_basket"
    )
    edit_carbs = InlineKeyboardButton(text="✏️ Вугл.", callback_data="edit_prompt:carbs")
    edit_protein = InlineKeyboardButton(text="✏️ Білки", callback_data="edit_prompt:protein")
    edit_fat = InlineKeyboardButton(text="✏️ Жири", callback_data="edit_prompt:fat")
    edit_calories = InlineKeyboardButton(text="✏️ Калорії", callback_data="edit_prompt:calories")
    edit_sugar = InlineKeyboardButton(text="✏️ Цукор", callback_data="edit_prompt:sugar")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [add_btn],
        [edit_carbs, edit_protein, edit_fat],
        [edit_calories, edit_sugar]
    ])
    await message.answer(response_text, reply_markup=keyboard)


# --- FLOW: EDITING FOOD PARAMETERS ---

@food_router.callback_query(F.data.startswith("edit_prompt:"))
async def edit_prompt_callback(callback_query: CallbackQuery, state: FSMContext):
    """Prompts user to enter custom values for specific food attributes."""
    await callback_query.answer()
    _, field = callback_query.data.split(":")

    if field == "carbs":
        await state.set_state(FoodLogStates.waiting_for_carbs_edit)
        await callback_query.message.answer("✏️ Введіть кількість <b>вуглеводів</b> у грамах (наприклад, <code>45</code>):")
    elif field == "protein":
        await state.set_state(FoodLogStates.waiting_for_protein_edit)
        await callback_query.message.answer("✏️ Введіть кількість <b>білків</b> у грамах (наприклад, <code>20</code>):")
    elif field == "fat":
        await state.set_state(FoodLogStates.waiting_for_fat_edit)
        await callback_query.message.answer("✏️ Введіть кількість <b>жирів</b> у грамах (наприклад, <code>15</code>):")
    elif field == "calories":
        await state.set_state(FoodLogStates.waiting_for_calories_edit)
        await callback_query.message.answer("✏️ Введіть кількість <b>калорій</b> у ккал (наприклад, <code>350</code>):")
    elif field == "sugar":
        await state.set_state(FoodLogStates.waiting_for_sugar_edit)
        await callback_query.message.answer("✏️ Введіть кількість <b>цукру</b> у грамах (наприклад, <code>5</code>):")


async def handle_carbs_edit_value(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть додатне число:")
        return

    await state.update_data(carbs=val)
    await state.set_state(None)
    await show_food_analysis_results(message, state)


async def handle_protein_edit_value(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть додатне число:")
        return

    await state.update_data(protein=val)
    await state.set_state(None)
    await show_food_analysis_results(message, state)


async def handle_fat_edit_value(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть додатне число:")
        return

    await state.update_data(fat=val)
    await state.set_state(None)
    await show_food_analysis_results(message, state)


async def handle_calories_edit_value(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть додатне число:")
        return

    await state.update_data(calories=val)
    await state.set_state(None)
    await show_food_analysis_results(message, state)


async def handle_sugar_edit_value(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть додатне число:")
        return

    await state.update_data(sugar=val)
    await state.set_state(None)
    await show_food_analysis_results(message, state)


# --- FLOW: BASKET OPERATIONS ---

@food_router.callback_query(F.data == "add_to_basket")
async def add_to_basket_callback(callback_query: CallbackQuery, state: FSMContext):
    """Adds currently analyzed food item to the basket."""
    await callback_query.answer()

    data = await state.get_data()
    carbs = data.get("carbs", 0.0)
    protein = data.get("protein", 0.0)
    fat = data.get("fat", 0.0)
    calories = data.get("calories", 0.0)
    sugar = data.get("sugar", 0.0)
    desc = data.get("desc", "Страва")

    basket = data.get("basket", [])
    basket.append({
        "carbs": carbs,
        "protein": protein,
        "fat": fat,
        "calories": calories,
        "sugar": sugar,
        "desc": desc
    })

    # Save basket and clean current item
    await state.update_data(basket=basket)
    await state.update_data(carbs=None, protein=None, fat=None, calories=None, sugar=None, desc=None, confidence=None)

    await show_basket_summary(callback_query.message, state)


async def show_basket_summary(message: Message, state: FSMContext):
    """Renders all items currently inside the food basket."""
    data = await state.get_data()
    basket = data.get("basket", [])

    if not basket:
        await message.answer("🍽️ <b>Ваша тарілка порожня.</b> Надішліть фото або опис страви, щоб додати її.")
        return

    total_carbs = sum(item["carbs"] for item in basket)
    total_protein = sum(item["protein"] for item in basket)
    total_fat = sum(item["fat"] for item in basket)
    total_calories = sum(item["calories"] for item in basket)
    total_sugar = sum(item["sugar"] for item in basket)

    items_text = ""
    for idx, item in enumerate(basket, 1):
        items_text += f"{idx}. <b>{item['desc']}</b> ({item['carbs']}г В, {item['protein']}г Б, {item['fat']}г Ж, {item['sugar']}г Ц, {item['calories']} ккал)\n"

    basket_time_str = data.get("basket_time_str")
    time_prefix = ""
    if basket_time_str:
        time_prefix = f"🕒 <b>Встановлений час прийому:</b> {basket_time_str}\n\n"

    summary_text = (
        f"🍽️ <b>Ваша тарілка містить:</b>\n\n"
        f"{time_prefix}"
        f"{items_text}\n"
        f"📊 <b>Загальні показники:</b>\n"
        f"• <b>Вуглеводи:</b> {total_carbs}г\n"
        f"• <b>Білки:</b> {total_protein}г\n"
        f"• <b>Жири:</b> {total_fat}г\n"
        f"• <b>Цукор:</b> {total_sugar}г\n"
        f"• <b>Калорії:</b> {total_calories} ккал\n\n"
        f"Виберіть дію нижче:"
    )

    calc_btn = InlineKeyboardButton(text="✅ Розрахувати дозу", callback_data="confirm_basket")
    set_time_btn = InlineKeyboardButton(
        text="🕒 Змінити час" if basket_time_str else "🕒 Встановити час",
        callback_data="set_basket_time"
    )
    add_more_btn = InlineKeyboardButton(text="🍽️ Додати ще страву", callback_data="add_more_food")
    clear_btn = InlineKeyboardButton(text="🗑️ Очистити тарілку", callback_data="clear_basket")

    inline_keyboard = [
        [calc_btn],
        [set_time_btn, add_more_btn],
    ]
    # Add individual remove buttons
    for idx, item in enumerate(basket):
        desc_short = item['desc'] if len(item['desc']) <= 15 else item['desc'][:15] + "..."
        inline_keyboard.append([
            InlineKeyboardButton(text=f"❌ Вилучити {idx+1}: {desc_short}", callback_data=f"remove_basket_item:{idx}")
        ])
    inline_keyboard.append([clear_btn])

    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    await message.answer(summary_text, reply_markup=keyboard)


@food_router.callback_query(F.data.startswith("remove_basket_item:"))
async def remove_basket_item_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, index_str = callback_query.data.split(":")
    idx = int(index_str)

    data = await state.get_data()
    basket = data.get("basket", [])
    if 0 <= idx < len(basket):
        removed = basket.pop(idx)
        await state.update_data(basket=basket)
        await callback_query.message.answer(f"❌ <b>{removed['desc']}</b> вилучено з тарілки.")

    # Update basket view
    await show_basket_summary(callback_query.message, state)
    # Delete original message to keep chat clean
    try:
        await callback_query.message.delete()
    except Exception:
        pass


@food_router.callback_query(F.data == "set_basket_time")
async def set_basket_time_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    today_btn = InlineKeyboardButton(text="📅 Сьогодні (вказати час)", callback_data="set_basket_time_type:today")
    custom_btn = InlineKeyboardButton(text="📆 Інша дата та час", callback_data="set_basket_time_type:custom")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[today_btn], [custom_btn]])
    await callback_query.message.answer("🕒 <b>Виберіть варіант часу прийому їжі:</b>", reply_markup=keyboard)


@food_router.callback_query(F.data.startswith("set_basket_time_type:"))
async def set_basket_time_type_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, choice = callback_query.data.split(":")

    if choice == "today":
        await state.set_state(FoodLogStates.waiting_for_custom_time)
        await callback_query.message.edit_text("🕒 Введіть час прийому їжі сьогодні у форматі <b>ГГ:ХХ</b> (наприклад, <code>13:45</code>):")
    elif choice == choice == "custom":
        await state.set_state(FoodLogStates.waiting_for_custom_datetime)
        await callback_query.message.edit_text("📆 Введіть дату та час прийому їжі у форматі <b>ДД.ММ ГГ:ХХ</b> (наприклад, <code>14.06 13:45</code>):")


async def handle_food_custom_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        dt = parse_today_time(time_str)
        kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
        display_str = dt.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz).strftime("%d.%m %H:%M")

        await state.update_data(basket_time_str=display_str, basket_custom_time=dt.isoformat())
        await state.set_state(None)
        await message.answer(f"✅ Встановлено час прийому їжі: <b>{display_str}</b>")
        await show_basket_summary(message, state)
    except ValueError:
        await message.answer("❌ Некоректний формат часу. Будь ласка, введіть час у форматі ГГ:ХХ (наприклад, 13:45):")


async def handle_food_custom_datetime(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        dt = parse_custom_datetime(time_str)
        kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
        display_str = dt.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz).strftime("%d.%m %H:%M")

        await state.update_data(basket_time_str=display_str, basket_custom_time=dt.isoformat())
        await state.set_state(None)
        await message.answer(f"✅ Встановлено дату та час прийому їжі: <b>{display_str}</b>")
        await show_basket_summary(message, state)
    except ValueError as e:
        await message.answer(f"❌ {e}. Будь ласка, введіть дату та час у форматі ДД.ММ ГГ:ХХ (наприклад, 14.06 13:45):")


@food_router.callback_query(F.data == "add_more_food")
async def add_more_food_callback(callback_query: CallbackQuery, state: FSMContext):
    """Prompts user to send another dish details."""
    await callback_query.answer()
    await state.set_state(FoodLogStates.waiting_for_food_input)
    await callback_query.message.answer("📥 Надішліть опис чи фото нової страви, щоб додати її до тарілки:")


@food_router.callback_query(F.data == "clear_basket")
async def clear_basket_callback(callback_query: CallbackQuery, state: FSMContext):
    """Clears the FSM basket database."""
    await callback_query.answer()
    await state.clear()
    await callback_query.message.answer("🗑️ <b>Вашу тарілку очищено!</b>")


@food_router.callback_query(F.data == "confirm_basket")
async def confirm_basket_callback(callback_query: CallbackQuery, state: FSMContext):
    """Confirms all food logs in the basket and suggestions calculations."""
    await callback_query.answer()

    data = await state.get_data()
    basket = data.get("basket", [])

    if not basket:
        await callback_query.message.answer("❌ Тарілка порожня.")
        return

    progress_msg = await callback_query.message.answer(random.choice(DOSE_CALC_HINTS))
    text_task = asyncio.create_task(_status_text_loop(progress_msg, DOSE_CALC_HINTS))
    typing_task = asyncio.create_task(_typing_loop(callback_query.message.chat.id))

    total_carbs = sum(item["carbs"] for item in basket)
    total_protein = sum(item["protein"] for item in basket)
    total_fat = sum(item["fat"] for item in basket)
    total_calories = sum(item["calories"] for item in basket)
    total_sugar = sum(item["sugar"] for item in basket)
    combined_desc = ", ".join(item["desc"] for item in basket)[:100]

    basket_custom_time = data.get("basket_custom_time")
    custom_timestamp = None
    if basket_custom_time:
        try:
            custom_timestamp = datetime.datetime.fromisoformat(basket_custom_time)
        except Exception:
            pass

    # Reset food log FSM states but keep the custom time string for subsequent insulin logging
    await state.update_data(
        basket=[],
        carbs=None,
        protein=None,
        fat=None,
        calories=None,
        sugar=None,
        desc=None,
        confidence=None,
        basket_time_str=None,
        basket_custom_time=None,
        meal_custom_time_str=basket_custom_time  # Save for the next log_insulin callback
    )
    await state.set_state(None)

    telegram_id = callback_query.from_user.id

    # 1. Save entry to DB
    async with async_session_local() as session:
        async with session.begin():
            new_log = LogEntry(
                telegram_id=telegram_id,
                carbs=total_carbs,
                protein=total_protein,
                fat=total_fat,
                calories=total_calories,
                sugar=total_sugar,
                meal_description=combined_desc,
                confidence_score=1.0
            )
            if custom_timestamp:
                new_log.timestamp = custom_timestamp
            session.add(new_log)
            await session.flush()

            # Fetch user profile settings
            result = await session.execute(
                select(User).filter(User.telegram_id == telegram_id)
            )
            user = result.scalars().first()
            if not user:
                text_task.cancel()
                typing_task.cancel()
                await progress_msg.delete()
                await callback_query.message.answer("❌ Профіль не знайдено. Будь ласка, запустіть /start спочатку.")
                return

            # Fetch active injections to calculate IOB (24h is enough to cover any dia_hours window)
            iob_cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=24)
            inj_result = await session.execute(
                select(InsulinInjection).filter(
                    InsulinInjection.telegram_id == telegram_id,
                    InsulinInjection.timestamp >= iob_cutoff
                )
            )
            injections = inj_result.scalars().all()

            # Fetch the user's actual latest glucose reading — the bolus/timing-hint math
            # MUST use this, not target_bg (target_bg is a static clinical setting, not a
            # measurement; using it as a stand-in silently zeroes out the correction term and
            # defeats the hypoglycemia safety check regardless of the user's real glucose state).
            glucose_result = await session.execute(
                select(LogEntry)
                .filter(LogEntry.telegram_id == telegram_id, LogEntry.glucose_level.isnot(None))
                .order_by(LogEntry.timestamp.desc())
                .limit(1)
            )
            last_glucose_entry = glucose_result.scalars().first()
            newest, _ = nightscout.newest_glucose(
                telegram_id,
                last_glucose_entry.glucose_level if last_glucose_entry else None,
                last_glucose_entry.timestamp if last_glucose_entry else None,
            )
            current_glucose = newest if newest is not None else user.target_bg

            # Check and trigger predictive alert
            from app.services.alerts import check_and_trigger_glucose_alert
            await check_and_trigger_glucose_alert(telegram_id, session)

    # 2. Calculate IOB and Dosage Recommendation
    current_iob = insulin_on_board(telegram_id, injections, user.dia_hours)

    suggestion = suggest_bolus(
        carbs=total_carbs,
        current_glucose=current_glucose,
        icr=user.icr,
        isf=user.isf,
        target_bg=user.target_bg,
        current_iob=current_iob,
        protein=total_protein,
        fat=total_fat,
        weight=user.weight,
        insulin_type=user.insulin_type
    )

    suggested_val = suggestion["total_suggested_dose"]

    suggestion["timing_hint"] = await resolve_timing_hint(
        telegram_id=telegram_id,
        carbs=total_carbs,
        current_glucose=current_glucose,
        suggested_dose=suggested_val,
        current_iob=current_iob,
        user=user,
        default_hint=suggestion["timing_hint"],
        protein=total_protein,
        fat=total_fat
    )

    response_msg = (
        f"🍽️ <b>Прийом їжі збережено!</b>\n\n"
        f"📝 <b>Страви:</b> {combined_desc}\n"
        f"• Вуглеводи: {total_carbs}г | Білки: {total_protein}г | Жири: {total_fat}г\n\n"
        f"📊 <b>Активний інсулін в тілі (IOB):</b> {current_iob} ОД\n\n"
        f"💉 <b>Розрахунок дози інсуліну:</b>\n"
        f"• Харчова доза: {suggestion['meal_bolus']} ОД\n"
        f"• Корекція: {suggestion['correction_bolus']} ОД\n"
        f"• <b>Рекомендована доза: {suggested_val} ОД</b>\n\n"
        f"👉 {suggestion['timing_hint']}\n\n"
        f"{suggestion['disclaimer']}"
    )

    # Inline keys: record recommendation or customize dose
    log_inj_btn = InlineKeyboardButton(
        text=f"✅ Записати рекомендовану дозу ({suggested_val} ОД)",
        callback_data=f"log_insulin:{suggested_val}"
    )
    custom_inj_btn = InlineKeyboardButton(
        text="✏️ Записати іншу дозу",
        callback_data="edit_insulin_prompt"
    )
    no_insulin_btn = InlineKeyboardButton(
        text="👌 Окей (без інсуліну)",
        callback_data="food_log_no_insulin"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[log_inj_btn], [custom_inj_btn], [no_insulin_btn]])

    text_task.cancel()
    typing_task.cancel()
    await progress_msg.delete()
    await callback_query.message.answer(response_msg, reply_markup=keyboard)


@food_router.callback_query(F.data == "food_log_no_insulin")
async def food_log_no_insulin_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    # Clear any residual custom time from state
    await state.update_data(meal_custom_time_str=None)
    await callback_query.message.edit_reply_markup(reply_markup=None) # Remove buttons to prevent double-clicks
    await callback_query.message.answer("✅ <b>Прийом їжі збережено без введення інсуліну!</b>\nЗа потреби ви можете додати інсулін пізніше за допомогою кнопки <b>💉 Записати інсулін</b> в меню.")
