import datetime
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func
from sqlalchemy.future import select

from app.bot.bot import bot
from app.config import settings
from app.services import nightscout
from app.database import LogEntry, User, async_session_local
from app.services.scheduler import scheduler

from .common import EMERGENCY_CALLBACK_DATA, ReminderSettingsStates

logger = logging.getLogger(__name__)
reminders_router = Router()


# --- EMERGENCY FLOW ---

async def send_emergency_reminder(chat_id: int):
    """Sends the 15-minute re-test notification."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="⏰ <b>Минуло 15 хвилин з моменту низького цукру. Будь ласка, виміряйте рівень цукру в крові зараз!</b>"
        )
        logger.info(f"Emergency reminder successfully sent to {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send emergency reminder to {chat_id}: {e}")


@reminders_router.callback_query(F.data == EMERGENCY_CALLBACK_DATA)
async def emergency_callback_handler(callback_query: CallbackQuery):
    """Handles the 🚨 EMERGENCY: LOW SUGAR button click."""
    await callback_query.answer()
    await trigger_emergency_flow(callback_query.message.chat.id)


async def trigger_emergency_flow(chat_id: int):
    """Bypasses normal logic to output immediate emergency guidelines and schedule reminder."""
    emergency_instructions = (
        "⚠️ <b>🚨 ВИЯВЛЕНО КРИТИЧНО НИЗЬКИЙ ЦУКОР (ГІПОГЛІКЕМІЯ)</b> ⚠️\n\n"
        "<b>Необхідно терміново вжити заходів:</b>\n"
        "1. <b>Прийміть 15г швидких вуглеводів:</b>\n"
        "   • 120 мл (пів склянки) фруктового соку або звичайної солодкої газованої води\n"
        "   • 3-4 таблетки глюкози\n"
        "   • 1 столова ложка цукру або меду\n"
        "2. Відпочиньте та зачекайте.\n"
        "3. <b>НЕ вводьте інсулін</b> у цей момент.\n\n"
        "⏰ Таймер запущено. Ми нагадаємо вам перевірити рівень цукру через 15 хвилин."
    )

    await bot.send_message(chat_id=chat_id, text=emergency_instructions)

    run_time = datetime.datetime.now() + datetime.timedelta(minutes=settings.EMERGENCY_TIMER_MINUTES)
    scheduler.add_job(
        send_emergency_reminder,
        'date',
        run_date=run_time,
        args=[chat_id],
        id=f"emergency_reminder_{chat_id}_{int(run_time.timestamp())}"
    )
    logger.info(f"Scheduled emergency reminder for {chat_id} at {run_time}")


# --- REMINDERS CONFIGURATION FLOW ---

async def show_reminders_menu(message: Message, telegram_id: int, edit_message_mode: bool = False):
    async with async_session_local() as session:
        result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
        user = result.scalars().first()
        if not user:
            # Register user
            user = User(
                telegram_id=telegram_id,
                icr=10.0,
                isf=3.0,
                target_bg=5.5,
                dia_hours=4.0
            )
            session.add(user)
            await session.commit()
            # refetch
            result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = result.scalars().first()

    status_str = "Увімкнено ✅" if user.reminder_enabled else "Вимкнено ❌"
    alerts_status_str = "Увімкнено ✅" if getattr(user, "predictive_alerts_enabled", True) else "Вимкнено ❌"
    text = (
        f"⏰ <b>Налаштування нагадувань вимірювання цукру:</b>\n\n"
        f"• <b>Нагадування про замір:</b> {status_str}\n"
        f"• <b>Попередження про коливання:</b> {alerts_status_str}\n"
        f"• <b>Період надсилання замірів:</b> з <code>{user.reminder_start_hour}:00</code> до <code>{user.reminder_end_hour}:00</code>\n"
        f"• <b>Інтервал замірів:</b> кожні <code>{user.reminder_interval_hours}</code> год.\n\n"
        f"Виберіть дію для налаштування:"
    )

    toggle_btn = InlineKeyboardButton(
        text="📳 Вимкнути нагадування" if user.reminder_enabled else "🟢 Увімкнути нагадування",
        callback_data="reminders_toggle"
    )
    alerts_btn = InlineKeyboardButton(
        text="🔕 Вимкнути попередження" if getattr(user, "predictive_alerts_enabled", True) else "🔔 Увімкнути попередження",
        callback_data="predictive_alerts_toggle"
    )
    start_btn = InlineKeyboardButton(
        text="🕒 Час початку",
        callback_data="reminders_set_start"
    )
    end_btn = InlineKeyboardButton(
        text="🕒 Час закінчення",
        callback_data="reminders_set_end"
    )
    interval_btn = InlineKeyboardButton(
        text="⏳ Інтервал",
        callback_data="reminders_set_interval"
    )
    back_btn = InlineKeyboardButton(
        text="🔙 Головне меню",
        callback_data="back_to_menu"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [toggle_btn],
        [alerts_btn],
        [start_btn, end_btn],
        [interval_btn],
        [back_btn]
    ])

    if edit_message_mode and isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass
    else:
        await message.answer(text, reply_markup=keyboard)


@reminders_router.message(Command("reminders"))
async def reminders_command_handler(message: Message):
    """Handles /reminders command."""
    await show_reminders_menu(message, message.from_user.id)


@reminders_router.callback_query(F.data == "reminders_toggle")
async def reminders_toggle_callback(callback_query: CallbackQuery):
    await callback_query.answer()
    telegram_id = callback_query.from_user.id
    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = result.scalars().first()
            if user:
                user.reminder_enabled = not user.reminder_enabled

    await show_reminders_menu(callback_query.message, telegram_id, edit_message_mode=True)


@reminders_router.callback_query(F.data == "predictive_alerts_toggle")
async def predictive_alerts_toggle_callback(callback_query: CallbackQuery):
    await callback_query.answer()
    telegram_id = callback_query.from_user.id
    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = result.scalars().first()
            if user:
                user.predictive_alerts_enabled = not getattr(user, "predictive_alerts_enabled", True)

    await show_reminders_menu(callback_query.message, telegram_id, edit_message_mode=True)


@reminders_router.callback_query(F.data == "reminders_set_start")
async def reminders_set_start_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.set_state(ReminderSettingsStates.waiting_for_start_hour)
    await callback_query.message.answer("🕒 Введіть годину початку надсилання нагадувань (ціле число від 0 до 23, наприклад, <code>8</code>):")


@reminders_router.callback_query(F.data == "reminders_set_end")
async def reminders_set_end_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.set_state(ReminderSettingsStates.waiting_for_end_hour)
    await callback_query.message.answer("🕒 Введіть годину закінчення надсилання нагадувань (ціле число від 0 до 23, наприклад, <code>22</code>):")


@reminders_router.callback_query(F.data == "reminders_set_interval")
async def reminders_set_interval_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.set_state(ReminderSettingsStates.waiting_for_interval)
    await callback_query.message.answer("⏳ Введіть інтервал надсилання нагадувань у годинах (ціле число від 1 до 24, наприклад, <code>2</code>):")


async def handle_reminder_start_hour(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if not (0 <= val <= 23): raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть цілу годину від 0 до 23:")
        return

    telegram_id = message.from_user.id
    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = result.scalars().first()
            if user:
                user.reminder_start_hour = val

    await state.set_state(None)
    await show_reminders_menu(message, telegram_id)


async def handle_reminder_end_hour(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if not (0 <= val <= 23): raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть цілу годину від 0 до 23:")
        return

    telegram_id = message.from_user.id
    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = result.scalars().first()
            if user:
                user.reminder_end_hour = val

    await state.set_state(None)
    await show_reminders_menu(message, telegram_id)


async def check_and_send_reminders():
    """Checks all users with active reminders and sends notifications if they are due."""
    logger.info("Running check_and_send_reminders job...")
    try:
        from zoneinfo import ZoneInfo
        kyiv_tz = ZoneInfo("Europe/Kyiv")
    except Exception:
        kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
    now_local = datetime.datetime.now(kyiv_tz)
    current_hour = now_local.hour
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    async with async_session_local() as session:
        # Fetch all users with reminders enabled
        result = await session.execute(
            select(User).filter(User.reminder_enabled == True)
        )
        users = result.scalars().all()

        # Fetch each user's latest glucose reading in one aggregate query instead of
        # querying per-user inside the loop below.
        latest_glucose_res = await session.execute(
            select(LogEntry.telegram_id, func.max(LogEntry.timestamp).label("last_glucose_at"))
            .filter(LogEntry.glucose_level.isnot(None))
            .group_by(LogEntry.telegram_id)
        )
        latest_glucose_by_user = {row.telegram_id: row.last_glucose_at for row in latest_glucose_res}

        for user in users:
            # Check if current hour is within the user's active reminder period
            is_active_hour = False
            if user.reminder_start_hour <= user.reminder_end_hour:
                is_active_hour = user.reminder_start_hour <= current_hour <= user.reminder_end_hour
            else:
                # Overnight range (e.g. 22:00 to 06:00)
                is_active_hour = current_hour >= user.reminder_start_hour or current_hour <= user.reminder_end_hour

            if not is_active_hour:
                continue

            # Check their last glucose reading
            _, last_glucose_at = nightscout.newest_glucose(
                user.telegram_id, None, latest_glucose_by_user.get(user.telegram_id)
            )

            should_remind = False
            if not last_glucose_at:
                # No entry ever recorded, remind them
                should_remind = True
                elapsed_str = "ніколи"
            else:
                elapsed = now_utc - last_glucose_at.replace(tzinfo=None)
                elapsed_hours = elapsed.total_seconds() / 3600.0
                if elapsed_hours >= user.reminder_interval_hours:
                    should_remind = True
                    elapsed_str = f"{int(elapsed_hours)} год. тому"

            if should_remind:
                try:
                    text = (
                        f"⏰ <b>Нагадування контролю діабету!</b>\n\n"
                        f"Будь ласка, виміряйте рівень цукру в крові та запишіть його.\n"
                        f"<i>(Ваш останній запис цукру: {elapsed_str})</i>\n\n"
                        f"Надішліть повідомлення на кшталт <code>цукор 5.8</code> або скористайтеся кнопками нижче."
                    )
                    status_btn = InlineKeyboardButton(
                        text="🔍 Перевірити статус",
                        callback_data="check_status"
                    )
                    food_btn = InlineKeyboardButton(
                        text="🥗 Додати страву",
                        callback_data="add_more_food"
                    )
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[status_btn], [food_btn]])

                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=text,
                        reply_markup=keyboard
                    )
                    logger.info(f"Sent reminder to user {user.telegram_id}")
                except Exception as e:
                    logger.error(f"Failed to send reminder to user {user.telegram_id}: {e}")

        # 2. Predictive Glucose Alerts for all users
        all_users_res = await session.execute(select(User))
        all_users = all_users_res.scalars().all()
        for u in all_users:
            try:
                from app.services.alerts import check_and_trigger_glucose_alert
                await check_and_trigger_glucose_alert(u.telegram_id, session)
            except Exception as ex:
                logger.error(f"Error running background predictive alert for user {u.telegram_id}: {ex}")

        await session.commit()


async def handle_reminder_interval(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if not (1 <= val <= 24): raise ValueError
    except ValueError:
        await message.answer("❌ Будь ласка, вкажіть ціле число годин від 1 до 24:")
        return

    telegram_id = message.from_user.id
    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = result.scalars().first()
            if user:
                user.reminder_interval_hours = val

    await state.set_state(None)
    await show_reminders_menu(message, telegram_id)
