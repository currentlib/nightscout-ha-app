from aiogram import Router

from .admin import admin_router
from .common import sanitize_telegram_html
from .dispatch import dispatch_router
from .food import food_router
from .glucose_insulin import glucose_insulin_router
from .onboarding import onboarding_router
from .reminders import check_and_send_reminders, reminders_router
from .status import status_router

# Aggregate router: order matters. `dispatch_router` holds the single catch-all
# `@dispatch_router.message(F.text & ~F.text.startswith("/"))` handler (text_handler), which
# matches ANY plain-text message regardless of FSM state. Every other router here has either a
# Command(...) filter, a specific FSM-state filter, or handles callback_query (a different event
# type) — none of those overlap with dispatch_router's catch-all, EXCEPT the onboarding router's
# OnboardingStates-filtered message handlers, which must win over the catch-all when a user is
# mid-onboarding. aiogram matches the first handler whose filter matches, in router-inclusion
# order — so dispatch_router MUST be included last, or a more specific handler registered after
# it would never be reached.
router = Router()
router.include_router(onboarding_router)
router.include_router(food_router)
router.include_router(glucose_insulin_router)
router.include_router(reminders_router)
router.include_router(status_router)
router.include_router(admin_router)
router.include_router(dispatch_router)

__all__ = ["router", "sanitize_telegram_html", "check_and_send_reminders"]
