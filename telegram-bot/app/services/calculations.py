"""IOB and COB: Nightscout's numbers first, the bot's own only as a backup.

The backup models in iob_engine follow Nightscout's, so a Nightscout outage doesn't make
the numbers jump. Only the patient has a Nightscout.
"""
from app.config import settings
from app.services import nightscout
from app.services.iob_engine import calculate_cob, calculate_iob


def _snapshot(telegram_id: int):
    return nightscout.fresh_calculated() if telegram_id == settings.PATIENT_TELEGRAM_ID else None


def using_nightscout(telegram_id: int) -> bool:
    return _snapshot(telegram_id) is not None


def insulin_on_board(telegram_id: int, injections: list, dia_hours: float) -> float:
    """Nightscout's IOB, unless the bot knows of more.

    A dose logged a moment ago hasn't reached Nightscout yet. This number is subtracted
    from dose suggestions, so being low would suggest stacking insulin on that dose.
    """
    local = calculate_iob(injections, dia_hours)
    snapshot = _snapshot(telegram_id)
    return round(max(snapshot.iob, local), 2) if snapshot else local


def carbs_on_board(telegram_id: int, meals: list) -> float:
    """Nightscout's COB, plus meals that haven't reached it yet."""
    snapshot = _snapshot(telegram_id)
    if not snapshot or snapshot.cob is None:
        return calculate_cob(meals, nightscout.carbs_per_hour)
    unsynced = [meal for meal in meals if meal.ns_id is None]
    return round(snapshot.cob + calculate_cob(unsynced, nightscout.carbs_per_hour), 2)
