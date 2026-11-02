import datetime
import logging
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import select, func

from app.config import settings
from app.database import async_session_local, AiUsage
from app.services.tiers import TIER_LIMITS, current_tier
from app.bot.bot import bot

logger = logging.getLogger(__name__)

# Set once per incoming Telegram update (see app/bot/middleware.py) so the AI service layer
# can attribute cost to a user without threading telegram_id through every one of its methods.
current_telegram_id: ContextVar[Optional[int]] = ContextVar("current_telegram_id", default=None)

# Per-1M-token pricing (USD), input/output. Keyed by exact model ID string so this stays
# correct regardless of which models each tier is configured to use (see app/services/tiers.py).
# ponytail: hardcoded price list, update when switching models or on a provider price change.
MODEL_PRICING = {
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
}

# Nudge the user once their today's spend crosses each of these fractions of their tier's
# daily cap. Checked by comparing spend before/after this call against every boundary, so one
# expensive call that jumps past several thresholds at once still notifies for each in order.
NOTIFY_THRESHOLDS = (0.25, 0.5, 0.75, 1.0)


async def record_usage(model: str, input_tokens: int, output_tokens: int, call_type: str) -> None:
    """Logs one AI API call's cost against the current user, then nudges them if this call
    pushed today's spend across a 25% increment of their tier's daily cap."""
    input_price, output_price = MODEL_PRICING.get(model, (0.0, 0.0))
    if model not in MODEL_PRICING:
        logger.warning(f"No pricing entry for model '{model}' — recording usage with cost_usd=0.")
    cost_usd = (input_tokens * input_price + output_tokens * output_price) / 1_000_000

    telegram_id = current_telegram_id.get()
    try:
        async with async_session_local() as session:
            async with session.begin():
                session.add(AiUsage(
                    telegram_id=telegram_id,
                    call_type=call_type,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd
                ))
    except Exception as e:
        logger.error(f"Failed to record AI usage: {e}")
        return

    await _notify_if_threshold_crossed(telegram_id, cost_usd)


async def _notify_if_threshold_crossed(telegram_id: Optional[int], cost_usd: float) -> None:
    """Checks whether this call's cost pushed the user's today's spend past a notify
    threshold for their tier's daily cap, and messages them once per boundary crossed."""
    if telegram_id is None or cost_usd <= 0:
        return

    limit = TIER_LIMITS.get(current_tier.get())
    if not limit:  # None = unlimited (never notify), 0.0 = free tier (can't spend anyway)
        return

    today_start = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    try:
        async with async_session_local() as session:
            spent_after = (await session.execute(
                select(func.sum(AiUsage.cost_usd)).filter(
                    AiUsage.telegram_id == telegram_id,
                    AiUsage.timestamp >= today_start
                )
            )).scalar() or 0.0
    except Exception as e:
        logger.error(f"Failed to check usage threshold for {telegram_id}: {e}")
        return

    spent_before = spent_after - cost_usd
    for fraction in NOTIFY_THRESHOLDS:
        boundary = fraction * limit
        if spent_before < boundary <= spent_after:
            await _send_threshold_notice(telegram_id, fraction, spent_after, limit)


async def _send_threshold_notice(telegram_id: int, fraction: float, spent: float, limit: float) -> None:
    if fraction >= 1.0:
        text = (
            f"🚫 <b>Денний ліміт ШІ вичерпано</b> (${spent:.3f} з ${limit:.2f}).\n"
            f"Ліміт оновиться завтра."
        )
    else:
        text = f"⚠️ Використано {int(fraction * 100)}% денного ліміту ШІ на сьогодні (${spent:.3f} з ${limit:.2f})."
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send usage threshold notice to {telegram_id}: {e}")
