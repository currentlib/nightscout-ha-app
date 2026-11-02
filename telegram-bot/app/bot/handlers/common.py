import asyncio
import datetime
import logging
import random
import re
from contextlib import asynccontextmanager

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonDefault,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.future import select

from app.bot.bot import bot
from app.config import settings
from app.database import InsulinInjection, LogEntry, User, async_session_local
from app.services import nightscout
from app.services.ai_service import ai_service
from app.services.calculations import carbs_on_board, insulin_on_board, using_nightscout
from app.services.iob_engine import calculate_hypo_carb_target, calculate_prebolus_minutes

logger = logging.getLogger(__name__)

# Callback data keys
EMERGENCY_CALLBACK_DATA = "emergency_low_sugar"


class FoodLogStates(StatesGroup):
    waiting_for_carbs_edit = State()
    waiting_for_protein_edit = State()
    waiting_for_fat_edit = State()
    waiting_for_calories_edit = State()
    waiting_for_sugar_edit = State()
    waiting_for_custom_insulin = State()
    waiting_for_food_input = State()
    waiting_for_custom_time = State()
    waiting_for_custom_datetime = State()
    waiting_for_clarification = State()  # AI asked a question, waiting for user answer


class ReminderSettingsStates(StatesGroup):
    waiting_for_start_hour = State()
    waiting_for_end_hour = State()
    waiting_for_interval = State()


class InsulinLogStates(StatesGroup):
    waiting_for_type = State()
    waiting_for_dosage = State()
    waiting_for_custom_time = State()
    waiting_for_today_time = State()
    waiting_for_custom_datetime = State()


class DirectLogStates(StatesGroup):
    waiting_for_sugar_time = State()
    waiting_for_sugar_datetime = State()
    waiting_for_insulin_time = State()
    waiting_for_insulin_datetime = State()


class OnboardingStates(StatesGroup):
    waiting_for_icr = State()
    waiting_for_isf = State()
    waiting_for_target_bg = State()
    waiting_for_dia_hours = State()
    waiting_for_diabetes_type = State()
    waiting_for_insulin_type = State()
    waiting_for_weight = State()
    waiting_for_activity_level = State()
    waiting_for_gender = State()
    waiting_for_birth_year = State()


def safe_float(val_str: str) -> float:
    """Safely converts string to float supporting both dot and comma separators."""
    return float(val_str.strip().replace(",", "."))


_TG_UNSAFE_LT = re.compile(
    r'<(?!/?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|blockquote)\b)',
    re.IGNORECASE
)

def sanitize_telegram_html(text: str) -> str:
    """
    Escapes any '<' that isn't the start of a Telegram-supported HTML tag, so AI-generated
    text containing a literal comparison like '< 3.9' doesn't break Telegram's HTML parser
    (bot-wide default parse_mode is HTML — see app/bot/bot.py).
    """
    if not text:
        return text
    return _TG_UNSAFE_LT.sub('&lt;', text)


async def _typing_loop(chat_id: int):
    """Resends Telegram's "typing..." indicator every 4s (it auto-clears after ~5s)."""
    while True:
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        await asyncio.sleep(4)


async def _status_text_loop(progress_msg: Message, messages: list, interval: float = 1.8):
    """Cycles a progress message through a shuffled pool of contextual flavor-text lines."""
    pool = random.sample(messages, len(messages))
    i = 0
    while True:
        try:
            await progress_msg.edit_text(pool[i % len(pool)])
        except Exception:
            pass
        i += 1
        await asyncio.sleep(interval)


@asynccontextmanager
async def animated_status(chat_id: int, progress_msg: Message, messages: list):
    """
    Keeps a slow AI call from looking frozen: cycles progress_msg through a pool of
    contextual flavor-text lines (different per call site — analyzing food, calculating a
    dose, or just chatting) and keeps Telegram's native "typing..." indicator alive alongside.
    """
    text_task = asyncio.create_task(_status_text_loop(progress_msg, messages))
    typing_task = asyncio.create_task(_typing_loop(chat_id))
    try:
        yield
    finally:
        text_task.cancel()
        typing_task.cancel()


FOOD_ANALYSIS_HINTS = [
    "🔍 Розпізнаю страву на фото...",
    "🥗 Визначаю інгредієнти...",
    "📏 Оцінюю розмір порції...",
    "🧮 Рахую вуглеводи...",
    "🍽️ Аналізую склад страви...",
    "📊 Порівнюю з базою даних продуктів...",
    "🌐 Шукаю інформацію про калорійність...",
    "⚖️ Оцінюю вагу порції...",
    "🥩 Перевіряю вміст білків і жирів...",
    "🍞 Визначаю тип вуглеводів...",
    "🔬 Придивляюся до деталей страви...",
    "📸 Обробляю зображення...",
    "🧾 Читаю штрихкод...",
    "🍅 Розпізнаю окремі компоненти страви...",
    "📚 Звіряюся з базою Open Food Facts...",
    "🧠 Аналізую фото за допомогою ШІ...",
    "🍚 Оцінюю кількість вуглеводів у порції...",
    "🥣 Визначаю тип страви...",
    "🍬 Перевіряю вміст цукру...",
    "🥕 Розпізнаю овочі та інші інгредієнти...",
    "📝 Формую опис страви...",
    "🔥 Рахую калорійність...",
    "🍇 Визначаю фрукти й ягоди на фото...",
    "🧀 Аналізую молочні продукти, якщо є...",
    "🌾 Перевіряю злакові компоненти...",
    "🍳 Визначаю спосіб приготування...",
    "📐 Оцінюю пропорції інгредієнтів...",
    "🥫 Шукаю відповідний продукт у базі даних...",
    "🧂 Враховую спеції та приправи...",
    "✅ Завершую аналіз страви...",
]

DOSE_CALC_HINTS = [
    "🧮 Розраховую дозу інсуліну...",
    "📊 Аналізую активний інсулін (IOB)...",
    "⚖️ Балансую вуглеводи та інсулін...",
    "🩸 Враховую поточний рівень цукру...",
    "📈 Прогнозую динаміку глюкози...",
    "💉 Визначаю харчовий болюс...",
    "🎯 Рахую корекційну дозу...",
    "🕒 Оцінюю час дії інсуліну...",
    "🧊 Перевіряю ризик гіпоглікемії...",
    "🔥 Перевіряю ризик гіперглікемії...",
    "📉 Аналізую тренд цукру...",
    "🥩 Враховую білки та жири (FPU)...",
    "⏱️ Визначаю оптимальний час ін'єкції...",
    "🧠 Формую рекомендацію ШІ...",
    "📋 Перевіряю ваші клінічні налаштування (ICR, ISF)...",
    "🩹 Оцінюю безпеку дозування...",
    "📐 Рахую співвідношення вуглеводів до інсуліну...",
    "🔍 Аналізую останні показники цукру...",
    "⚠️ Перевіряю ризик накопичення інсуліну...",
    "🍽️ Враховую активні вуглеводи (COB)...",
    "🧾 Формую фінальну рекомендацію...",
    "🩺 Звіряюся з вашим профілем...",
    "🎯 Підбираю оптимальну дозу...",
    "⏳ Майже готово...",
    "🧮 Перераховую з урахуванням активного інсуліну...",
    "💡 Формую пораду щодо часу ін'єкції...",
    "🩸 Аналізую вплив страви на цукор...",
]

CHAT_THINKING_HINTS = [
    "🤔 Думаю над відповіддю...",
    "📖 Переглядаю ваші останні показники...",
    "💬 Формулюю пораду...",
    "🧠 Аналізую ваше запитання...",
    "📋 Перевіряю історію повідомлень...",
    "🩸 Дивлюся на ваш профіль...",
    "🔍 Шукаю відповідь у ваших даних...",
    "📊 Аналізую IOB та COB...",
    "🧾 Перевіряю ваші клінічні налаштування...",
    "💡 Формую рекомендацію...",
    "🕐 Дивлюся на останні записи...",
    "📝 Готую відповідь...",
    "🧬 Враховую ваші довгострокові нотатки...",
    "🩺 Обмірковую пораду...",
    "📈 Перевіряю тенденції цукру...",
    "💭 Формую думку...",
    "🔎 Аналізую контекст розмови...",
    "✍️ Пишу відповідь...",
    "🧮 Перевіряю розрахунки...",
    "📚 Звіряюся з вашою історією...",
    "🤖 Обробляю запит...",
    "💊 Враховую активний інсулін...",
    "🍽️ Перевіряю останні прийоми їжі...",
    "⏳ Ще трохи...",
    "🌡️ Оцінюю ваш стан...",
]


def parse_time_string(time_str: str) -> datetime.datetime:
    """Parses local time string HH:MM (supports separators :, ., -) and converts to naive UTC."""
    m = re.match(r"^([0-9]{1,2})[:\.\-]([0-9]{2})$", time_str.strip())
    if not m:
        raise ValueError("Invalid time format. Use HH:MM")
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Invalid hour or minute")

    kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
    now_kyiv = datetime.datetime.now(kyiv_tz)
    local_dt = now_kyiv.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_dt > now_kyiv:
        local_dt -= datetime.timedelta(days=1)
    return local_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def parse_today_time(time_str: str) -> datetime.datetime:
    return parse_time_string(time_str)


def parse_custom_datetime(datetime_str: str) -> datetime.datetime:
    """Parses custom local datetime string DD.MM HH:MM and converts to naive UTC."""
    m = re.match(r"^([0-9]{1,2})[:\.\-/]([0-9]{1,2})\s+([0-9]{1,2})[:\.\-]([0-9]{2})$", datetime_str.strip())
    if not m:
        raise ValueError("Некоректний формат. Використовуйте ДД.ММ ГГ:ХХ")
    day, month, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

    kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
    now_kyiv = datetime.datetime.now(kyiv_tz)
    year = now_kyiv.year

    try:
        local_dt = datetime.datetime(year, month, day, hour, minute, tzinfo=kyiv_tz)
    except ValueError:
        raise ValueError("Вказано некоректну дату або час")

    if local_dt > now_kyiv:
        if local_dt - now_kyiv > datetime.timedelta(days=1):
            try:
                local_dt = datetime.datetime(year - 1, month, day, hour, minute, tzinfo=kyiv_tz)
            except ValueError:
                pass
    return local_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def parse_direct_entry_text(text: str, prefix_pattern: str) -> tuple[float | None, datetime.datetime | None]:
    """
    Parses prefix value [time/datetime] from direct user message.
    Supports:
    - 'цукор 6.5' -> value=6.5, dt=None
    - 'цукор 6.5 12:30' -> value=6.5, dt=parsed today 12:30
    - 'цукор 6.5 о 12:30' -> value=6.5, dt=parsed today 12:30
    - 'цукор 6.5 14.06 12:30' -> value=6.5, dt=parsed custom datetime
    - 'цукор 6.5 о 14.06 12:30' -> value=6.5, dt=parsed custom datetime
    """
    # Match the prefix and the float value (allowing both dot and comma), then capture the rest
    pattern = rf"^(?:{prefix_pattern})\s*[:\-]?\s*([0-9]+(?:[\.,][0-9]+)?)(.*)$"
    m = re.match(pattern, text.strip(), re.IGNORECASE)
    if not m:
        return None, None

    val = safe_float(m.group(1))
    time_part = m.group(2).strip()

    if not time_part:
        return val, None

    # Remove optional "о", "в", "at"
    time_part = re.sub(r"^(?:о|в|at)\s+", "", time_part, flags=re.IGNORECASE).strip()

    # Try parsing custom datetime first (DD.MM HH:MM)
    try:
        dt = parse_custom_datetime(time_part)
        return val, dt
    except ValueError:
        pass
        pass

    # Try parsing today's time (HH:MM)
    try:
        dt = parse_today_time(time_part)
        return val, dt
    except ValueError:
        pass

    raise ValueError("Некоректний формат дати/часу. Використовуйте ГГ:ХХ або ДД.ММ ГГ:ХХ")


def get_insulin_type_keyboard() -> InlineKeyboardMarkup:
    """Generates inline keyboard to select insulin type."""
    bolus_btn = InlineKeyboardButton(text="💉 Болюс (швидкий)", callback_data="insulin_select_type:bolus")
    basal_btn = InlineKeyboardButton(text="⏳ Базал (довгий)", callback_data="insulin_select_type:basal")
    return InlineKeyboardMarkup(inline_keyboard=[[bolus_btn, basal_btn]])


async def resolve_timing_hint(
    telegram_id: int,
    carbs: float,
    current_glucose: float,
    suggested_dose: float,
    current_iob: float,
    user: User,
    default_hint: str,
    protein: float = 0.0,
    fat: float = 0.0
) -> str:
    """Resolves timing hint using AI if configured, otherwise falls back to the default."""
    if not settings.USE_AI_TIMING_HINTS:
        return default_hint

    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=24)
        async with async_session_local() as session:
            logs_res = await session.execute(
                select(LogEntry)
                .filter(LogEntry.telegram_id == telegram_id, LogEntry.timestamp >= cutoff)
            )
            recent_logs_for_cob = logs_res.scalars().all()
        current_cob = carbs_on_board(telegram_id, recent_logs_for_cob)

        user_profile = {
            "icr": user.icr,
            "isf": user.isf,
            "target_bg": user.target_bg,
            "dia_hours": user.dia_hours,
            "weight": user.weight,
            "diabetes_type": user.diabetes_type,
            "activity_level": user.activity_level,
            "gender": user.gender,
            "birth_year": user.birth_year
        }

        # Precompute the same numbers suggest_bolus() would use for its deterministic fallback —
        # the AI must phrase these, not recalculate them (LLMs shouldn't redo arithmetic that's
        # already correct).
        bg_delta = current_glucose - user.target_bg
        is_severe_hypo = current_glucose < 3.0
        hypo_carb_grams = calculate_hypo_carb_target(user.weight, is_severe_hypo)
        prebolus_minutes = calculate_prebolus_minutes(user.insulin_type, protein, fat, bg_delta, current_glucose)

        ai_hint = await ai_service.generate_timing_hint(
            carbs=carbs,
            current_glucose=current_glucose,
            suggested_dose=suggested_dose,
            current_iob=current_iob,
            current_cob=current_cob,
            user_profile=user_profile,
            protein=protein,
            fat=fat,
            hypo_carb_grams=hypo_carb_grams,
            prebolus_minutes=prebolus_minutes
        )
        if ai_hint:
            return sanitize_telegram_html(ai_hint)
    except Exception as e:
        logger.error(f"Error resolving AI timing hint: {e}")

    return default_hint


def get_start_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Generates the main navigation keyboard."""
    status_btn = InlineKeyboardButton(
        text="🔍 Статус (Цукор та Активний інсулін)",
        callback_data="check_status"
    )
    emergency_btn = InlineKeyboardButton(
        text="🚨 ЕКСТРЕННО: НИЗЬКИЙ ЦУКОР",
        callback_data=EMERGENCY_CALLBACK_DATA
    )
    second_row = [emergency_btn]
    if settings.DASHBOARD_URL:
        second_row.append(InlineKeyboardButton(text="📊 Дашборд", url=settings.DASHBOARD_URL))

    return InlineKeyboardMarkup(inline_keyboard=[[status_btn], second_row])


def get_dashboard_inline_keyboard() -> InlineKeyboardMarkup:
    """Reply-keyboard buttons can't open a link, so tapping one answers with this."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Відкрити дашборд", url=settings.DASHBOARD_URL)]
    ])


async def reset_menu_button_to_default(telegram_id: int) -> None:
    """Cleanup for a reverted experiment: this chat may still have a Menu Button stuck on
    MenuButtonWebApp from when /start briefly set one (see git history around the Mini App
    button fix) — Telegram remembers it per chat until explicitly changed, it doesn't revert
    just because the code stopped calling set_chat_menu_button. Idempotent either way, so
    safe to call unconditionally on /start rather than tracking who was actually affected.
    ponytail: fine to delete this call once confirmed no chat still shows the stuck button."""
    try:
        await bot.set_chat_menu_button(chat_id=telegram_id, menu_button=MenuButtonDefault())
    except Exception as e:
        logger.error(f"Failed to reset menu button for {telegram_id}: {e}")


def get_main_reply_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Generates the main persistent reply keyboard."""
    status_btn = KeyboardButton(text="🔍 Статус")
    dashboard_btn = KeyboardButton(text="📊 Дашборд")
    food_btn = KeyboardButton(text="🥗 Додати страву")
    insulin_btn = KeyboardButton(text="💉 Записати інсулін")
    emergency_btn = KeyboardButton(text="🚨 ЕКСТРЕННО")

    return ReplyKeyboardMarkup(
        keyboard=[
            [status_btn, dashboard_btn],
            [food_btn, insulin_btn],
            [emergency_btn]
        ],
        resize_keyboard=True,
        persistent=True
    )


async def get_status_info(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Generates current blood glucose and active insulin report."""
    async with async_session_local() as session:
        # Fetch user
        result = await session.execute(
            select(User).filter(User.telegram_id == telegram_id)
        )
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

        # Get latest glucose reading
        glucose_result = await session.execute(
            select(LogEntry)
            .filter(LogEntry.telegram_id == telegram_id, LogEntry.glucose_level.isnot(None))
            .order_by(LogEntry.timestamp.desc())
            .limit(1)
        )
        last_glucose_entry = glucose_result.scalars().first()

        # Fetch active injections to calculate IOB (24h is enough to cover any dia_hours window)
        iob_cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=24)
        inj_result = await session.execute(
            select(InsulinInjection).filter(
                InsulinInjection.telegram_id == telegram_id,
                InsulinInjection.timestamp >= iob_cutoff
            )
        )
        injections = inj_result.scalars().all()

    current_iob = insulin_on_board(telegram_id, injections, user.dia_hours)
    source_note = ""
    if nightscout.enabled() and telegram_id == settings.PATIENT_TELEGRAM_ID and not using_nightscout(telegram_id):
        source_note = "⚠️ <i>Nightscout недоступний, IOB розраховано ботом.</i>\n"

    glucose_val, entry_time = nightscout.newest_glucose(
        telegram_id,
        last_glucose_entry.glucose_level if last_glucose_entry else None,
        last_glucose_entry.timestamp if last_glucose_entry else None,
    )
    if glucose_val is not None:
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        diff = now_utc - entry_time

        minutes = int(diff.total_seconds() / 60)
        if minutes < 1:
            time_str = "щойно"
        elif minutes < 60:
            time_str = f"{minutes} хв. тому"
        else:
            hours = minutes // 60
            mins = minutes % 60
            if hours < 24:
                time_str = f"{hours} год. {mins} хв. тому"
            else:
                days = hours // 24
                time_str = f"{days} дн. тому"

        last_glucose_text = f"<code>{glucose_val} ммоль/л</code> ({time_str})"
    else:
        last_glucose_text = "Немає записів рівня цукру."

    try:
        from zoneinfo import ZoneInfo
        kyiv_tz = ZoneInfo("Europe/Kyiv")
    except Exception:
        kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))
    current_time_str = datetime.datetime.now(kyiv_tz).strftime("%d.%m.%Y %H:%M:%S")

    status_text = (
        f"📊 <b>Поточний статус організму:</b>\n\n"
        f"🩸 <b>Рівень цукру:</b> {last_glucose_text}\n"
        f"📊 <b>Активний інсулін (IOB):</b> <code>{current_iob} ОД</code>\n"
        f"{source_note}\n"
        f"🕒 <i>Дані оновлено: {current_time_str}</i>"
    )

    # Inline Keyboard for Status Actions
    refresh_btn = InlineKeyboardButton(
        text="🔄 Оновити",
        callback_data="check_status"
    )
    back_btn = InlineKeyboardButton(
        text="🔙 Головне меню",
        callback_data="back_to_menu"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [refresh_btn],
        [back_btn]
    ])
    return status_text, keyboard
