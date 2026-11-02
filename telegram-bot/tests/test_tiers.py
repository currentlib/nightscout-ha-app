"""Unit tests for the pure functions in app/services/tiers.py.

effective_tier, resolve_heavy_model, resolve_light_model take no DB session and are
covered here. check_ai_budget needs a real DB session (AiUsage query) and is
deliberately NOT covered — out of scope for this pure-logic suite.

Pure functions, no DB, no event loop. Unexecuted in this environment (no Python
interpreter available in this sandbox) — run with `pytest` from the repo root
once a real environment is available.
"""
import datetime
import types

from app.config import settings
from app.services.tiers import effective_tier, resolve_heavy_model, resolve_light_model, current_tier


def _user(tier, trial_expires_at=None):
    return types.SimpleNamespace(tier=tier, trial_expires_at=trial_expires_at)


def test_active_trial_overrides_tier_to_regular():
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    future = now + datetime.timedelta(days=1)
    assert effective_tier(_user(tier="free", trial_expires_at=future)) == "regular"


def test_active_trial_with_tzaware_expiry_still_works():
    future_aware = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    assert effective_tier(_user(tier="free", trial_expires_at=future_aware)) == "regular"


def test_expired_trial_falls_back_to_stored_tier():
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    past = now - datetime.timedelta(days=1)
    assert effective_tier(_user(tier="super", trial_expires_at=past)) == "super"


def test_no_trial_returns_stored_tier():
    assert effective_tier(_user(tier="regular", trial_expires_at=None)) == "regular"


def test_no_trial_and_no_tier_defaults_to_free():
    assert effective_tier(_user(tier=None, trial_expires_at=None)) == "free"


def test_resolve_heavy_model_per_tier():
    for tier, expected in [
        ("regular", settings.MODEL_REGULAR_HEAVY),
        ("super", settings.MODEL_SUPER_HEAVY),
        ("unlimited", settings.MODEL_UNLIMITED_HEAVY),
    ]:
        token = current_tier.set(tier)
        try:
            assert resolve_heavy_model() == expected
        finally:
            current_tier.reset(token)


def test_resolve_light_model_per_tier():
    for tier, expected in [
        ("regular", settings.MODEL_REGULAR_LIGHT),
        ("super", settings.MODEL_SUPER_LIGHT),
        ("unlimited", settings.MODEL_UNLIMITED_LIGHT),
    ]:
        token = current_tier.set(tier)
        try:
            assert resolve_light_model() == expected
        finally:
            current_tier.reset(token)
