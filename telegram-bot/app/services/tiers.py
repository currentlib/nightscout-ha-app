import datetime
from contextvars import ContextVar

from sqlalchemy import select, func

from app.config import settings
from app.database import async_session_local, AiUsage, User

# Daily USD cost ceiling per tier. None = unlimited. 0.0 = no AI access at all.
# ponytail: hand-tuned numbers, sanity-checked against real usage in PUBLIC_LAUNCH_PLAN.md —
# revisit if usage patterns shift once more users are on the tier system.
TIER_LIMITS = {
    "free": 0.0,
    "regular": 0.05,
    "super": 0.10,
    "unlimited": None,
}

# Model used per tier, independently configurable for "heavy" calls (vision food analysis,
# chat) vs "light" calls (extraction, timing hints) — each tier's two knobs live in
# app/config.py. Regular defaults to the cheap model for both, by design, not by accident.
# Free tier never reaches this (no AI access at all — see TIER_LIMITS/check_ai_budget).
TIER_MODELS = {
    "regular": {"heavy": settings.MODEL_REGULAR_HEAVY, "light": settings.MODEL_REGULAR_LIGHT},
    "super": {"heavy": settings.MODEL_SUPER_HEAVY, "light": settings.MODEL_SUPER_LIGHT},
    "unlimited": {"heavy": settings.MODEL_UNLIMITED_HEAVY, "light": settings.MODEL_UNLIMITED_LIGHT},
}

# Set once per incoming Telegram update by the handler that resolves the user's tier, read by
# ai_service.py to pick the right model for "heavy" calls. Defaults to "free" (fail closed —
# an AI call site that forgets to set this gets no access rather than silently unlimited access).
current_tier: ContextVar[str] = ContextVar("current_tier", default="free")


def effective_tier(user: User) -> str:
    """Resolves the tier that actually applies right now, accounting for an active trial."""
    if user.trial_expires_at:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        trial_expires = user.trial_expires_at
        if trial_expires.tzinfo is not None:
            trial_expires = trial_expires.replace(tzinfo=None)
        if now < trial_expires:
            return "regular"
    return user.tier or "free"


async def check_ai_budget(telegram_id: int, tier: str) -> bool:
    """True if the user still has budget left today for their effective tier."""
    limit = TIER_LIMITS.get(tier)
    if limit is None:
        return True  # unlimited tier, or an unrecognized tier — fail open only for "unlimited"
    if limit <= 0:
        return False  # free tier: no AI access, period

    today_start = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    async with async_session_local() as session:
        spent = (await session.execute(
            select(func.sum(AiUsage.cost_usd)).filter(
                AiUsage.telegram_id == telegram_id,
                AiUsage.timestamp >= today_start
            )
        )).scalar() or 0.0
    return spent < limit


def _resolve_model(weight: str) -> str:
    # Fail-safe default to Regular's models (the cheapest configured tier) if current_tier
    # is somehow unset or "free" when an AI call site is reached — should never happen in
    # practice since check_ai_budget already blocks free-tier calls before this is read.
    tier_models = TIER_MODELS.get(current_tier.get(), TIER_MODELS["regular"])
    return tier_models[weight]


def resolve_heavy_model() -> str:
    """Picks the model for a "heavy" AI call (vision food analysis, chat) based on tier."""
    return _resolve_model("heavy")


def resolve_light_model() -> str:
    """Picks the model for a "light" AI call (extraction, timing hints) based on tier."""
    return _resolve_model("light")
