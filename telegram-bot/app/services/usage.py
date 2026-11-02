import logging
from contextvars import ContextVar
from typing import Optional

from app.database import async_session_local, AiUsage

logger = logging.getLogger(__name__)

# Set once per incoming Telegram update (see app/bot/middleware.py) so the AI service layer
# can attribute cost to a user without threading telegram_id through every one of its methods.
current_telegram_id: ContextVar[Optional[int]] = ContextVar("current_telegram_id", default=None)

# Per-1M-token pricing (USD), input/output. Keyed by exact model ID string so this stays
# correct whichever models are configured.
# ponytail: hardcoded price list, update when switching models or on a provider price change.
MODEL_PRICING = {
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
}

async def record_usage(model: str, input_tokens: int, output_tokens: int, call_type: str) -> None:
    """Logs one AI API call's cost against the current user, for /admin."""
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
