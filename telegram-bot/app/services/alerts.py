import logging
import datetime
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import User, LogEntry, InsulinInjection
from app.services import nightscout
from app.services.calculations import carbs_on_board, insulin_on_board
from app.services.iob_engine import predict_glucose_timeline
from app.bot.bot import bot

logger = logging.getLogger(__name__)

URGENT_REPEAT = datetime.timedelta(minutes=10)
NO_DATA_REPEAT = datetime.timedelta(hours=1)
KINDS = ("urgent_low", "low", "high", "predicted", "no_data")

# Alert state lives in memory: a restart can repeat one alert, but it can't lose one.
_level: str | None = None
_level_alerted_at: datetime.datetime | None = None
_no_data_alerted_at: datetime.datetime | None = None
_started_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def recipients(kind: str) -> list[int]:
    """Chats for this kind of alert. With none configured, alerts go to the patient."""
    if not settings.ALERT_RECIPIENTS:
        return [settings.PATIENT_TELEGRAM_ID] if settings.PATIENT_TELEGRAM_ID else []
    return [r["chat_id"] for r in settings.ALERT_RECIPIENTS if r.get(kind)]


async def notify(kind: str, text: str, fallback_chat: int | None = None) -> bool:
    chats = recipients(kind) or ([fallback_chat] if fallback_chat and not settings.ALERT_RECIPIENTS else [])
    sent = False
    for chat_id in chats:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            sent = True
        except Exception as e:
            logger.error(f"Failed to send {kind} alert to chat {chat_id}: {e}")
    return sent


def level_for(mmol: float) -> str | None:
    if mmol < settings.ALERT_URGENT_LOW:
        return "urgent_low"
    if mmol < settings.ALERT_LOW:
        return "low"
    if mmol > settings.ALERT_HIGH:
        return "high"
    return None


LEVEL_TEXT = {
    "urgent_low": "🚨 <b>ДУЖЕ НИЗЬКИЙ ЦУКОР!</b>",
    "low": "⚠️ <b>Низький цукор</b>",
    "high": "⚠️ <b>Високий цукор</b>",
}


async def on_new_reading(reading: nightscout.Reading, now: datetime.datetime) -> None:
    """Alerts on the real CGM value, once per level, repeating while it lasts."""
    global _level, _level_alerted_at
    level = level_for(reading.mmol)
    value = f"<b>{reading.mmol} ммоль/л</b> {reading.arrow}".strip()

    if level is None:
        if _level:
            await notify(_level, f"✅ Цукор знову в нормі: {value}")
        _level, _level_alerted_at = None, None
        return

    repeat = URGENT_REPEAT if level == "urgent_low" else datetime.timedelta(minutes=settings.ALERT_REPEAT_MINUTES)
    if level != _level or _level_alerted_at is None or now - _level_alerted_at >= repeat:
        await notify(level, f"{LEVEL_TEXT[level]}\n\n{value}")
        _level_alerted_at = now
    _level = level


async def check_no_data(reading: nightscout.Reading | None, now: datetime.datetime) -> None:
    """Also fires when Nightscout itself is down, because `reading` just stops changing."""
    global _no_data_alerted_at
    last = reading.at if reading else _started_at
    silence = now - last
    if silence < datetime.timedelta(minutes=settings.ALERT_NO_DATA_MINUTES):
        if _no_data_alerted_at:
            await notify("no_data", "✅ Дані про цукор знову надходять.")
        _no_data_alerted_at = None
        return
    if _no_data_alerted_at is None or now - _no_data_alerted_at >= NO_DATA_REPEAT:
        await notify("no_data", f"📡 <b>Немає даних про цукор</b> уже {int(silence.total_seconds() // 60)} хв.")
        _no_data_alerted_at = now


async def check_and_trigger_glucose_alert(telegram_id: int, session: AsyncSession):
    """
    Checks the user's predicted glucose level in 30 minutes.
    If it is outside the configured limits, sends an alert message via Telegram,
    respecting a 30-minute cooldown for the same alert type.
    """
    # 1. Fetch user
    result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
    user = result.scalars().first()
    if not user:
        logger.warning(f"User {telegram_id} not found in database for predictive alert check.")
        return

    # Check if predictive alerts are enabled for this user
    if not getattr(user, "predictive_alerts_enabled", True):
        logger.info(f"Skipping prediction alert for user {telegram_id} because predictive_alerts_enabled is disabled.")
        return

    # 2. Fetch last 24h food logs and metrics
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - datetime.timedelta(hours=24)

    logs_result = await session.execute(
        select(LogEntry).filter(
            LogEntry.telegram_id == telegram_id,
            LogEntry.timestamp >= cutoff
        ).order_by(LogEntry.timestamp.asc())
    )
    logs = logs_result.scalars().all()

    # 3. Fetch last 24h insulin injections
    inj_result = await session.execute(
        select(InsulinInjection).filter(
            InsulinInjection.telegram_id == telegram_id,
            InsulinInjection.timestamp >= cutoff
        ).order_by(InsulinInjection.timestamp.asc())
    )
    injections = inj_result.scalars().all()

    # 4. Get the latest glucose reading, from the CGM when it's newer than a manual one
    glucose_logs = [log for log in logs if log.glucose_level is not None]
    last_manual = glucose_logs[-1] if glucose_logs else None
    latest_glucose, reading_at = nightscout.newest_glucose(
        telegram_id,
        last_manual.glucose_level if last_manual else None,
        last_manual.timestamp if last_manual else None,
    )
    if latest_glucose is None or reading_at < cutoff:
        # We cannot predict glucose timeline without at least one recent reading as baseline
        logger.info(f"Skipping prediction alert for user {telegram_id} because no recent glucose reading is available.")
        return

    # 5. Compute IOB and COB
    current_iob = insulin_on_board(telegram_id, injections, user.dia_hours)
    current_cob = carbs_on_board(telegram_id, logs)

    # 6. Predict glucose timeline
    prediction_timeline = predict_glucose_timeline(
        current_glucose=latest_glucose,
        injections=injections,
        meals=logs,
        isf=user.isf,
        icr=user.icr,
        dia_hours=user.dia_hours,
        carbs_per_hour=nightscout.carbs_per_hour
    )

    if len(prediction_timeline) < 2:
        return

    # Prediction for 30 minutes in the future is at index 1
    predicted_val = prediction_timeline[1]["glucose"]

    alert_type = None
    alert_msg = None

    if predicted_val < settings.ALERT_PREDICTED_LOW:
        alert_type = "hypo"
        alert_msg = (
            f"🚨 <b>Попередження про низький рівень цукру!</b>\n\n"
            f"За нашими розрахунками, через 30 хвилин рівень цукру може знизитися до <b>{predicted_val} ммоль/л</b>.\n"
            f"Рекомендовано вжити швидкі вуглеводи (сік, цукор, фрукти) для запобігання гіпоглікемії."
        )
    elif predicted_val > settings.ALERT_PREDICTED_HIGH:
        alert_type = "hyper"
        alert_msg = (
            f"⚠️ <b>Попередження про високий рівень цукру!</b>\n\n"
            f"За нашими розрахунками, через 30 хвилин рівень цукру може піднятися до <b>{predicted_val} ммоль/л</b>.\n"
            f"Будь ласка, перевірте свій стан та розгляньте введення корекційного болюсу інсуліну за потреби."
        )

    if not alert_type:
        # Normal prediction, clear alert state if it was set
        if user.last_glucose_alert_type:
            user.last_glucose_alert_type = None
            user.last_glucose_alert_at = None
            session.add(user)
        return

    # Check cooldown constraint
    if user.last_glucose_alert_type == alert_type and user.last_glucose_alert_at:
        last_alert_time = user.last_glucose_alert_at
        if last_alert_time.tzinfo is None:
            last_alert_time = last_alert_time.replace(tzinfo=datetime.timezone.utc)

        elapsed = datetime.datetime.now(datetime.timezone.utc) - last_alert_time
        if elapsed.total_seconds() < 1800: # 30 minutes cooldown
            logger.info(f"Skipping alert for user {telegram_id} due to cooldown. Elapsed: {elapsed.total_seconds():.0f}s")
            return

    # Send notification via telegram bot
    if await notify("predicted", alert_msg, fallback_chat=telegram_id):
        logger.info(f"Sent {alert_type} alert for user {telegram_id} (predicted glucose: {predicted_val})")

        # Update user's alert status
        user.last_glucose_alert_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        user.last_glucose_alert_type = alert_type
        session.add(user)
