import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.future import select

from app.config import settings
from app.database import User, async_session_local
from app.services.ai_service import ai_service

from .chat import run_chat_assistant_flow
from .common import (
    DirectLogStates,
    FoodLogStates,
    InsulinLogStates,
    ReminderSettingsStates,
    FOOD_ANALYSIS_HINTS,
    animated_status,
    get_insulin_type_keyboard,
    get_dashboard_inline_keyboard,
    get_status_info,
)
from .food import (
    handle_calories_edit_value,
    handle_carbs_edit_value,
    handle_fat_edit_value,
    handle_food_clarification_answer,
    handle_food_custom_datetime,
    handle_food_custom_time,
    handle_protein_edit_value,
    handle_sugar_edit_value,
    show_food_analysis_results,
)
from .glucose_insulin import (
    handle_custom_insulin_value,
    handle_direct_insulin_datetime,
    handle_direct_insulin_time,
    handle_direct_sugar_datetime,
    handle_direct_sugar_time,
    handle_insulin_custom_datetime,
    handle_insulin_custom_time,
    handle_insulin_dosage_value,
    handle_insulin_today_time,
    try_handle_direct_entry_text,
)
from .onboarding import start_clinical_onboarding
from .reminders import handle_reminder_end_hour, handle_reminder_interval, handle_reminder_start_hour, trigger_emergency_flow

logger = logging.getLogger(__name__)
dispatch_router = Router()


@dispatch_router.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: Message, state: FSMContext):
    """Processes either manual metrics logging or sends plain descriptions to OpenAI."""
    text = message.text.strip().lower()

    # Catches anyone who hasn't finished clinical setup and lands here with no active FSM state.
    # Handlers for the setup steps intercept messages sent during an active step before this.
    async with async_session_local() as session:
        async with session.begin():
            gate_user_res = await session.execute(select(User).filter(User.telegram_id == message.from_user.id))
            gate_user = gate_user_res.scalars().first()
            if not gate_user:
                gate_user = User(telegram_id=message.from_user.id, icr=10.0, isf=3.0, target_bg=5.5, dia_hours=4.0)
                session.add(gate_user)
    if not gate_user.clinical_setup_complete:
        await start_clinical_onboarding(message, state)
        return

    # A pending "save this?" proposal (food and/or glucose) that wasn't confirmed by now means
    # the user moved on without tapping it — silently drop it so a stale draft can't get
    # attached later.
    pending_data = await state.get_data()
    if pending_data.get("temp_food") or pending_data.get("temp_glucose") is not None:
        await state.update_data(temp_food=None, temp_glucose=None)

    if text in ["статус", "🔍 статус", "status", "check status"]:
        status_text, reply_markup = await get_status_info(message.from_user.id)
        await message.answer(status_text, reply_markup=reply_markup)
        return

    if text in ["екстренно", "🚨 екстренно", "emergency"]:
        await trigger_emergency_flow(message.chat.id)
        return

    if text in ["записати інсулін", "💉 записати інсулін", "інсулін без їжі", "inject insulin"]:
        await state.set_state(InsulinLogStates.waiting_for_type)
        await message.answer("💉 <b>Виберіть тип інсуліну:</b>", reply_markup=get_insulin_type_keyboard())
        return

    if text in ["додати страву", "🥗 додати страву", "add food", "log food"]:
        await state.set_state(FoodLogStates.waiting_for_food_input)
        await message.answer("🥗 <b>Надішліть фото або опис страви</b>, і я проаналізую її поживну цінність:")
        return

    # The Mini App labels stay so a keyboard Telegram already drew still works.
    if text in ["дашборд", "📊 дашборд", "dashboard", "міні-додаток", "📊 міні-додаток"]:
        if settings.DASHBOARD_URL:
            await message.answer("📊 Натисніть кнопку нижче:", reply_markup=get_dashboard_inline_keyboard())
        else:
            await message.answer("📊 Посилання на дашборд ще не налаштовано.")
        return

    # Handle editing states
    current_state = await state.get_state()
    if current_state == FoodLogStates.waiting_for_carbs_edit:
        await handle_carbs_edit_value(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_protein_edit:
        await handle_protein_edit_value(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_fat_edit:
        await handle_fat_edit_value(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_calories_edit:
        await handle_calories_edit_value(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_sugar_edit:
        await handle_sugar_edit_value(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_custom_insulin:
        await handle_custom_insulin_value(message, state)
        return
    elif current_state == InsulinLogStates.waiting_for_dosage:
        await handle_insulin_dosage_value(message, state)
        return
    elif current_state == ReminderSettingsStates.waiting_for_start_hour:
        await handle_reminder_start_hour(message, state)
        return
    elif current_state == ReminderSettingsStates.waiting_for_end_hour:
        await handle_reminder_end_hour(message, state)
        return
    elif current_state == ReminderSettingsStates.waiting_for_interval:
        await handle_reminder_interval(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_custom_time:
        await handle_food_custom_time(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_custom_datetime:
        await handle_food_custom_datetime(message, state)
        return
    elif current_state == InsulinLogStates.waiting_for_custom_time:
        await handle_insulin_custom_time(message, state)
        return
    elif current_state == InsulinLogStates.waiting_for_today_time:
        await handle_insulin_today_time(message, state)
        return
    elif current_state == InsulinLogStates.waiting_for_custom_datetime:
        await handle_insulin_custom_datetime(message, state)
        return
    elif current_state == DirectLogStates.waiting_for_sugar_time:
        await handle_direct_sugar_time(message, state)
        return
    elif current_state == DirectLogStates.waiting_for_sugar_datetime:
        await handle_direct_sugar_datetime(message, state)
        return
    elif current_state == DirectLogStates.waiting_for_insulin_time:
        await handle_direct_insulin_time(message, state)
        return
    elif current_state == DirectLogStates.waiting_for_insulin_datetime:
        await handle_direct_insulin_datetime(message, state)
        return
    elif current_state == FoodLogStates.waiting_for_food_input:
        # Reset state so they don't get stuck in food logging state after one food log
        await state.set_state(None)

        async with async_session_local() as session:
            async with session.begin():
                user_res = await session.execute(select(User).filter(User.telegram_id == message.from_user.id))
                user = user_res.scalars().first()
                if not user:
                    user = User(telegram_id=message.from_user.id, icr=10.0, isf=3.0, target_bg=5.5, dia_hours=4.0)
                    session.add(user)
        progress_msg = await message.answer("🔄 <i>ШІ аналізує опис страви... Будь ласка, зачекайте...</i>")
        try:
            async with animated_status(message.chat.id, progress_msg, FOOD_ANALYSIS_HINTS):
                analysis = await ai_service.analyze_food(text_description=message.text)
            await progress_msg.delete()

            if analysis.needs_clarification and analysis.clarification_question:
                await state.update_data(clarification_text=message.text, clarification_photo_b64=None)
                await state.set_state(FoodLogStates.waiting_for_clarification)
                await message.answer(
                    f"❓ <b>Уточнення:</b> {analysis.clarification_question}\n"
                    f"<i>(або напишіть будь-яку відповідь щоб продовжити з оцінкою ШІ)</i>"
                )
            else:
                await state.update_data(
                    carbs=analysis.carbohydrates,
                    protein=analysis.protein,
                    fat=analysis.fat,
                    calories=analysis.calories,
                    sugar=analysis.sugar,
                    desc=", ".join(analysis.ingredients).replace(":", ""),
                    confidence=analysis.confidence_score,
                    data_source=analysis.data_source,
                    portion=analysis.portion_estimate
                )
                await show_food_analysis_results(message, state)
        except Exception as e:
            logger.error(f"Error analyzing text: {e}")
            await progress_msg.edit_text("❌ Помилка розпізнавання тексту. Спробуйте сформулювати опис простіше.")
        return
    elif current_state == FoodLogStates.waiting_for_clarification:
        await handle_food_clarification_answer(message, state)
        return

    if await try_handle_direct_entry_text(message, state, text):
        return

    # General chat assistant flow (not in any food or edit states)
    await run_chat_assistant_flow(message, state)
