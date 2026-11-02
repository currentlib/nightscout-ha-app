import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete
from sqlalchemy.future import select

from app.database import ChatMessage, InsulinInjection, LogEntry, User, async_session_local

from .common import get_start_keyboard, get_status_info

status_router = Router()


@status_router.message(Command("status"))
async def status_command_handler(message: Message):
    """Handles the /status command."""
    telegram_id = message.from_user.id
    status_text, reply_markup = await get_status_info(telegram_id)
    await message.answer(status_text, reply_markup=reply_markup)


@status_router.message(Command("clear_chat"))
async def clear_chat_command_handler(message: Message):
    """Clears the persistent chat history for the user."""
    telegram_id = message.from_user.id
    async with async_session_local() as session:
        async with session.begin():
            await session.execute(
                delete(ChatMessage).filter(ChatMessage.telegram_id == telegram_id)
            )
    await message.answer("🧹 Історію чату успішно очищено! Новий діалог розпочато.")


@status_router.callback_query(F.data == "check_status")
async def check_status_callback(callback_query: CallbackQuery):
    """Handles the 🔍 Статус callback button."""
    telegram_id = callback_query.from_user.id
    status_text, reply_markup = await get_status_info(telegram_id)
    try:
        await callback_query.message.edit_text(status_text, reply_markup=reply_markup)
    except Exception:
        await callback_query.answer("Дані актуальні!")
        return
    await callback_query.answer()


@status_router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback_query: CallbackQuery):
    """Returns to the main menu keyboard."""
    telegram_id = callback_query.from_user.id
    welcome_text = (
        f"Ласкаво просимо до <b>Бот-помічника контролю діабету</b>.\n\n"
        f"Надішліть фото або опис страви для ШІ-аналізу, або надішніть значення цукру (наприклад, <code>цукор 6.2</code>).\n\n"
        f"Використовуйте кнопки або меню нижче для швидкого перегляду статусу організму."
    )
    try:
        await callback_query.message.edit_text(welcome_text, reply_markup=get_start_keyboard(telegram_id))
    except Exception:
        pass
    await callback_query.answer()


@status_router.message(Command("baselines"))
async def baselines_handler(message: Message):
    text = (
        "📊 <b>Інструкція з розрахунку клінічних показників</b>\n\n"
        "Клінічні показники є основою для розрахунку доз інсуліну. Їх можна оцінити за правилами для ультракороткого інсуліну:\n\n"
        "1️⃣ <b>Вуглеводний коефіцієнт (ICR)</b> — кількість грамів вуглеводів, яку компенсує 1 ОД інсуліну.\n"
        "• <b>Розрахунок (Правило 500):</b> <code>ICR = 500 / Добова доза інсуліну (TDD)</code>\n"
        "• <i>Приклад:</i> Якщо ваша загальна добова доза (базальний + болюс) дорівнює 50 ОД, то <code>ICR = 500 / 50 = 10</code> (1 ОД на 10г вуглеводів).\n\n"
        "2️⃣ <b>Фактор чутливості до інсуліну (ISF)</b> — на скільки ммоль/л знижує цукор 1 ОД інсуліну.\n"
        "• <b>Розрахунок (Правило 100 для ммоль/л):</b> <code>ISF = 100 / Добова доза інсуліну (TDD)</code>\n"
        "• <i>Приклад:</i> При добовій дозі 50 ОД, <code>ISF = 100 / 50 = 2.0</code> (1 ОД знижує цукор на 2.0 ммоль/л).\n\n"
        "3️⃣ <b>Тривалість дії інсуліну (DIA)</b>\n"
        "• Для сучасних аналогів інсуліну (Новорапід, Хумалог, Апідра) зазвичай становить <b>3-5 годин</b> (дефолт: 4 години).\n\n"
        "👉 <i>Усі ці показники можна змінити в Nightscout: Profile Editor.</i>"
    )
    await message.answer(text)


@status_router.message(Command("analyze"))
async def analyze_handler(message: Message):
    telegram_id = message.from_user.id
    cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=7)

    async with async_session_local() as session:
        # Fetch user profile
        user_res = await session.execute(select(User).filter(User.telegram_id == telegram_id))
        user = user_res.scalars().first()
        if not user:
            await message.answer("❌ Будь ласка, спочатку запустіть бот через /start.")
            return

        # Fetch last 7 days of logs
        logs_res = await session.execute(
            select(LogEntry).filter(
                LogEntry.telegram_id == telegram_id,
                LogEntry.timestamp >= cutoff
            ).order_by(LogEntry.timestamp.asc())
        )
        logs = logs_res.scalars().all()

        # Fetch last 7 days of injections
        inj_res = await session.execute(
            select(InsulinInjection).filter(
                InsulinInjection.telegram_id == telegram_id,
                InsulinInjection.timestamp >= cutoff
            ).order_by(InsulinInjection.timestamp.asc())
        )
        injections = inj_res.scalars().all()

    # Analytical math
    post_meal_bgs = []
    correction_bgs = []

    # Map meals and look for post-meal blood sugars (1.5h to 3h after)
    meals = [l for l in logs if l.carbs is not None]
    for meal in meals:
        meal_time = meal.timestamp.replace(tzinfo=None)
        # find closest BG reading in range [meal_time + 1.5h, meal_time + 3h]
        for l in logs:
            if l.glucose_level is not None:
                log_time = l.timestamp.replace(tzinfo=None)
                diff = log_time - meal_time
                if datetime.timedelta(hours=1.5) <= diff <= datetime.timedelta(hours=3.0):
                    post_meal_bgs.append(l.glucose_level)

    # Map correction injections and check BG 3h to 4.5h after
    for inj in injections:
        inj_time = inj.timestamp.replace(tzinfo=None)
        # check if it is correction (no meal within 30 min of injection)
        is_correction = True
        for meal in meals:
            meal_t = meal.timestamp.replace(tzinfo=None)
            if abs(meal_t - inj_time) <= datetime.timedelta(minutes=30):
                is_correction = False
                break

        if is_correction:
            # Look for subsequent BG readings 3h to 4.5h later
            for l in logs:
                if l.glucose_level is not None:
                    log_time = l.timestamp.replace(tzinfo=None)
                    diff = log_time - inj_time
                    if datetime.timedelta(hours=3.0) <= diff <= datetime.timedelta(hours=4.5):
                        correction_bgs.append(l.glucose_level)

    # Build report
    report = "📊 <b>Аналіз залежності та коригування показників (за 7 днів):</b>\n\n"
    suggestions = []

    if post_meal_bgs:
        avg_post_meal = sum(post_meal_bgs) / len(post_meal_bgs)
        report += f"🍽️ <b>Після їжі (через 2 години):</b>\n"
        report += f"• Проаналізовано вимірів: {len(post_meal_bgs)}\n"
        report += f"• Середній цукор: {avg_post_meal:.1f} ммоль/л\n"

        if avg_post_meal > 8.5:
            suggested_icr = round(user.icr * 0.9, 1)
            suggestions.append(
                f"📈 <b>Коригування ICR:</b> Цукор після їжі в середньому високий ({avg_post_meal:.1f} ммоль/л). "
                f"Рекомендуємо <b>зменшити ICR з {user.icr} до {suggested_icr}</b> (щоб колоти більше інсуліну на кожні 10г вуглеводів)."
            )
        elif avg_post_meal < 4.8:
            suggested_icr = round(user.icr * 1.1, 1)
            suggestions.append(
                f"📉 <b>Коригування ICR:</b> Цукор після їжі в середньому низький ({avg_post_meal:.1f} ммоль/л). "
                f"Рекомендуємо <b>збільшити ICR з {user.icr} до {suggested_icr}</b> (щоб зменшити дозу інсуліну на їжу)."
            )
        else:
            report += "✅ Ваш вуглеводний коефіцієнт (ICR) підібрано чудово!\n"
        report += "\n"
    else:
        report += "🍽️ <i>Недостатньо вимірів цукру через 2 години після їжі для аналізу ICR.</i>\n\n"

    if correction_bgs:
        avg_post_correction = sum(correction_bgs) / len(correction_bgs)
        report += f"💉 <b>Після корекції високого цукру (через 3-4 години):</b>\n"
        report += f"• Проаналізовано вимірів: {len(correction_bgs)}\n"
        report += f"• Середній цукор: {avg_post_correction:.1f} ммоль/л\n"

        if avg_post_correction > 7.5:
            suggested_isf = round(user.isf * 0.9, 1)
            suggestions.append(
                f"📈 <b>Коригування ISF:</b> Корекційні дози недостатньо знижують цукор (середній: {avg_post_correction:.1f} ммоль/л). "
                f"Рекомендуємо <b>зменшити ISF з {user.isf} до {suggested_isf}</b> (це збільшить дозу інсуліну для корекції високих значень)."
            )
        elif avg_post_correction < 4.2:
            suggested_isf = round(user.isf * 1.1, 1)
            suggestions.append(
                f"📉 <b>Коригування ISF:</b> Корекційні дози знижують цукор занадто сильно (середній: {avg_post_correction:.1f} ммоль/л). "
                f"Рекомендуємо <b>збільшити ISF з {user.isf} до {suggested_isf}</b> (це зменшить дозу інсуліну для корекції, щоб уникнути гіпоглікемії)."
            )
        else:
            report += "✅ Ваш фактор чутливості (ISF) підібрано чудово!\n"
        report += "\n"
    else:
        report += "💉 <i>Недостатньо вимірів через 3-4 години після коригувальних доз для аналізу ISF.</i>\n\n"

    if suggestions:
        report += "💡 <b>Рекомендації:</b>\n" + "\n\n".join(suggestions)
    else:
        report += "👍 Всі ваші клінічні налаштування працюють збалансовано! Змін не потрібно."

    await message.answer(report)
