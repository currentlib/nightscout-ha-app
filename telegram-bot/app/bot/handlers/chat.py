import asyncio
import datetime
import logging
import random
import re
import time

from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.future import select

from app.config import settings
from app.database import ChatMessage, InsulinInjection, LogEntry, User, async_session_local
from app.services.ai_service import ai_service
from app.services import nightscout
from app.services.calculations import carbs_on_board, insulin_on_board
from app.services.iob_engine import calculate_fpu

from .common import CHAT_THINKING_HINTS, _status_text_loop, _typing_loop, sanitize_telegram_html

logger = logging.getLogger(__name__)

# Guards against overlapping runs for the same user — without this, sending several messages
# while a previous reply is still streaming spawns multiple concurrent typing/flavor-text/edit
# loops against the SAME chat, multiplying the effective call rate and risking a severe,
# long-duration Telegram flood ban (hours, not seconds) rather than a normal transient one.
_active_chat_flows: set[int] = set()


async def run_chat_assistant_flow(message: Message, state: FSMContext) -> None:
    """General AI chat assistant flow (not in any food or edit states): extraction, long-term
    memory, streaming reply, DB save, and per-item pending-confirmation buttons."""
    if message.from_user.id in _active_chat_flows:
        await message.answer("⏳ Будь ласка, зачекайте — попереднє повідомлення ще обробляється.")
        return
    _active_chat_flows.add(message.from_user.id)

    progress_msg = await message.answer(random.choice(CHAT_THINKING_HINTS))
    # Rotating flavor text + native "typing..." indicator for the silent gap before the reply
    # starts streaming in (extraction + any auto food-estimate call happen before the first
    # token arrives). Both cancelled as soon as real content starts streaming into progress_msg.
    text_task = asyncio.create_task(_status_text_loop(progress_msg, CHAT_THINKING_HINTS))
    typing_task = asyncio.create_task(_typing_loop(message.chat.id))
    try:
        telegram_id = message.from_user.id

        # The "2. Retrieve user settings" block below re-fetches the user in its own session;
        # this just makes sure the row exists before extraction runs.
        async with async_session_local() as session:
            async with session.begin():
                user_res = await session.execute(select(User).filter(User.telegram_id == telegram_id))
                if not user_res.scalars().first():
                    session.add(User(telegram_id=telegram_id, icr=10.0, isf=3.0, target_bg=5.5, dia_hours=4.0))

        # 1. Automatic metrics + long-term memory extraction. Runs unconditionally (not gated
        # on digits/keywords) because memory-worthy facts ("не люблю гостре") rarely contain
        # either, and we're about to spend a much larger call on the chat reply anyway.
        extracted_info = ""
        has_temp_food = False
        temp_food_summary = ""
        has_temp_glucose = False

        extracted = await ai_service.extract_metrics_from_text(message.text)
        if extracted:
            # Casual mention of eating something right now — run the same estimation the
            # dedicated food-logging flow uses (portion + full macros, web-search grounded
            # when possible) and propose adding it, instead of just logging raw numbers.
            if extracted.mentioned_food:
                try:
                    analysis = await ai_service.analyze_food(text_description=extracted.mentioned_food)
                    temp_food = {
                        "carbs": analysis.carbohydrates,
                        "protein": analysis.protein,
                        "fat": analysis.fat,
                        "calories": analysis.calories,
                        "sugar": analysis.sugar,
                        "desc": ", ".join(analysis.ingredients).replace(":", "") or extracted.mentioned_food
                    }
                    await state.update_data(temp_food=temp_food)
                    has_temp_food = True
                    temp_food_summary = (
                        f"{analysis.portion_estimate}. Вугл: {analysis.carbohydrates}г, "
                        f"Білки: {analysis.protein}г, Жири: {analysis.fat}г, Калорії: {analysis.calories}ккал"
                    )
                except Exception as e:
                    logger.error(f"Error auto-estimating mentioned food '{extracted.mentioned_food}': {e}")

            # A glucose reading mentioned in passing alongside other stuff (food, chit-chat) is
            # proposed for confirmation instead of auto-saved — same pattern as mentioned_food,
            # and combined into the same confirm button when both appear in one message.
            if extracted.glucose_level is not None:
                await state.update_data(temp_glucose=extracted.glucose_level)
                has_temp_glucose = True

            if extracted.insulin_dosage is not None:
                async with async_session_local() as session:
                    async with session.begin():
                        new_inj = InsulinInjection(
                            telegram_id=telegram_id,
                            dosage=extracted.insulin_dosage,
                            injection_type="bolus"
                        )
                        session.add(new_inj)
                        extracted_info += f"• 💉 Інсулін: {extracted.insulin_dosage} ОД\n"
                        from app.services.alerts import check_and_trigger_glucose_alert
                        await check_and_trigger_glucose_alert(telegram_id, session)

        # 2. Retrieve user settings, recent history context & chat history from DB
        async with async_session_local() as session:
            user_res = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = user_res.scalars().first()
            if not user:
                # auto register
                user = User(
                    telegram_id=telegram_id,
                    icr=10.0,
                    isf=3.0,
                    target_bg=5.5,
                    dia_hours=4.0
                )
                session.add(user)
                await session.commit()
                # refetch
                user_res = await session.execute(select(User).filter(User.telegram_id == telegram_id))
                user = user_res.scalars().first()

            # Merge any newly extracted long-term fact into persistent memory
            if extracted and extracted.memory_fact and extracted.memory_fact not in (user.memory_notes or ""):
                notes = f"{user.memory_notes}\n- {extracted.memory_fact}" if user.memory_notes else f"- {extracted.memory_fact}"
                # ponytail: naive line-count cap (keeps most recent facts); switch to
                # AI-summarized consolidation if this list grows noisy/redundant over time
                lines = notes.split("\n")[-40:]
                user.memory_notes = "\n".join(lines)
                session.add(user)
                await session.commit()

            user_profile = {
                "icr": user.icr,
                "isf": user.isf,
                "target_bg": user.target_bg,
                "dia_hours": user.dia_hours,
                "weight": user.weight,
                "diabetes_type": user.diabetes_type,
                "activity_level": user.activity_level,
                "gender": user.gender,
                "birth_year": user.birth_year,
                "memory_notes": user.memory_notes
            }

            # A meal the user just had analyzed (photo/text food logging, or a casual chat
            # mention) but hasn't confirmed yet — surface it as chat context so they can ask
            # follow-up questions about it ("should I split the insulin?", "how long before
            # eating should I inject?") without it being saved first. Precompute the FPU
            # (fat/protein delayed-bolus) math server-side rather than leaving it to the model.
            pending_meal_context = None
            state_data = await state.get_data()
            p_carbs = state_data.get("carbs")
            if p_carbs is not None:
                p_protein, p_fat, p_calories, p_sugar = (
                    state_data.get("protein") or 0.0, state_data.get("fat") or 0.0,
                    state_data.get("calories") or 0.0, state_data.get("sugar") or 0.0
                )
                p_desc = state_data.get("desc") or "Страва"
            elif state_data.get("temp_food"):
                tf = state_data["temp_food"]
                p_carbs, p_protein, p_fat, p_calories, p_sugar, p_desc = (
                    tf["carbs"], tf["protein"], tf["fat"], tf["calories"], tf["sugar"], tf["desc"]
                )
            else:
                p_carbs = None

            if p_carbs is not None:
                pending_meal_context = (
                    f"{p_desc} — Вугл: {p_carbs}г, Білки: {p_protein}г, Жири: {p_fat}г, "
                    f"Цукор: {p_sugar}г, Калорії: {p_calories}ккал."
                )
                fpu_info = calculate_fpu(p_protein, p_fat, user.icr)
                if fpu_info["fpu"] > 0:
                    pending_meal_context += (
                        f" FPU-аналіз: {fpu_info['fpu']} FPU → додатковий болюс {fpu_info['dose']} ОД "
                        f"приблизно через {fpu_info['delay_hours']} год. після їжі (компенсація білків/жирів)."
                    )

            # Fetch logs for the configured lookback window (default 24 hours)
            lookback_hours = settings.CHAT_CONTEXT_DB_LOOKBACK_HOURS
            cutoff_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=lookback_hours)
            logs_res = await session.execute(
                select(LogEntry)
                .filter(LogEntry.telegram_id == telegram_id, LogEntry.timestamp >= cutoff_time)
                .order_by(LogEntry.timestamp.desc())
            )
            logs_db = logs_res.scalars().all()

            # Fetch injections for the configured lookback window
            inj_res = await session.execute(
                select(InsulinInjection)
                .filter(InsulinInjection.telegram_id == telegram_id, InsulinInjection.timestamp >= cutoff_time)
                .order_by(InsulinInjection.timestamp.desc())
            )
            inj_db = inj_res.scalars().all()

            # Fetch chat history for context (up to CHAT_HISTORY_LIMIT messages)
            chat_res = await session.execute(
                select(ChatMessage)
                .filter(ChatMessage.telegram_id == telegram_id)
                .order_by(ChatMessage.timestamp.desc())
                .limit(settings.CHAT_HISTORY_LIMIT)
            )
            chat_msgs = chat_res.scalars().all()
            # Reverse to maintain chronological order
            chat_msgs.reverse()
            history = [{"role": msg.role, "content": msg.content} for msg in chat_msgs]

        try:
            from zoneinfo import ZoneInfo
            kyiv_tz = ZoneInfo("Europe/Kyiv")
        except Exception:
            kyiv_tz = datetime.timezone(datetime.timedelta(hours=3))

        current_time_kyiv = datetime.datetime.now(kyiv_tz)
        days_ua = {
            "Monday": "Понеділок",
            "Tuesday": "Вівторок",
            "Wednesday": "Середа",
            "Thursday": "Четвер",
            "Friday": "П'ятниця",
            "Saturday": "Субота",
            "Sunday": "Неділя"
        }
        day_name_en = current_time_kyiv.strftime("%A")
        day_name_ua = days_ua.get(day_name_en, day_name_en)
        user_profile["current_time"] = f"{current_time_kyiv.strftime('%d.%m.%Y %H:%M')}, {day_name_ua}"
        user_profile["current_iob"] = insulin_on_board(telegram_id, inj_db, user.dia_hours)
        if nightscout.latest and telegram_id == settings.PATIENT_TELEGRAM_ID:
            cgm_at = nightscout.latest.at.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz)
            user_profile["current_glucose"] = (
                f"{nightscout.latest.mmol} mmol/L {nightscout.latest.arrow} at {cgm_at.strftime('%H:%M')}"
            )
        user_profile["current_cob"] = carbs_on_board(telegram_id, logs_db)


        recent_logs = []
        for l in logs_db[:settings.CHAT_CONTEXT_LOGS_LIMIT]:
            l_time_local = l.timestamp.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz) if l.timestamp.tzinfo is None else l.timestamp.astimezone(kyiv_tz)
            recent_logs.append({
                "timestamp": l_time_local.strftime("%d.%m %H:%M"),
                "glucose": l.glucose_level,
                "carbs": l.carbs,
                "protein": l.protein,
                "fat": l.fat,
                "calories": l.calories,
                "sugar": l.sugar,
                "desc": l.meal_description or ""
            })

        recent_injections = []
        for i in inj_db[:settings.CHAT_CONTEXT_LOGS_LIMIT]:
            i_time_local = i.timestamp.replace(tzinfo=datetime.timezone.utc).astimezone(kyiv_tz) if i.timestamp.tzinfo is None else i.timestamp.astimezone(kyiv_tz)
            recent_injections.append({
                "timestamp": i_time_local.strftime("%d.%m %H:%M"),
                "dosage": i.dosage,
                "type": i.injection_type
            })

        # 4. Call chat assistant, streaming the reply into progress_msg as it's generated
        # (edits throttled to every ~1.5s — Telegram flood-controls edits that fire more often,
        # and mid-stream text can contain unclosed HTML tags, so intermediate frames are shown
        # as plain text; only the final edit applies full HTML formatting).
        reply_text = ""
        last_edit_at = 0.0
        async for partial in ai_service.chat_with_assistant(
            user_message=message.text,
            history=history,
            user_profile=user_profile,
            recent_logs=recent_logs,
            recent_injections=recent_injections,
            lookback_hours=lookback_hours,
            pending_meal_context=pending_meal_context
        ):
            if not typing_task.done():
                typing_task.cancel()
            if not text_task.done():
                text_task.cancel()
            # <br> cleanup must run before sanitizing (sanitize would otherwise escape the
            # literal "<br>" into "&lt;br>" since "br" isn't a Telegram-supported tag, and the
            # replace below would no longer find it).
            cleaned = partial.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
            reply_text = sanitize_telegram_html(cleaned)
            now = time.monotonic()
            if now - last_edit_at >= 1.5:
                last_edit_at = now
                preview = re.sub(r'<[^>]+>', '', reply_text)
                try:
                    await progress_msg.edit_text(preview)
                except Exception:
                    pass  # transient edit failure (flood control / unchanged text) — next frame will catch up

        # Save conversation turn to DB
        async with async_session_local() as session:
            async with session.begin():
                session.add(ChatMessage(telegram_id=telegram_id, role="user", content=message.text))
                session.add(ChatMessage(telegram_id=telegram_id, role="assistant", content=reply_text))

        # Prepend confirmation of auto-logged metrics if any, and one confirm button PER
        # detected item (not one combined button) — so the user can accept just the glucose,
        # just the food, both, or neither (by ignoring both and sending the next message).
        keyboard = None
        pending_lines = []
        pending_buttons = []
        if has_temp_glucose:
            pending_lines.append(f"🩸 Цукор: {extracted.glucose_level} ммоль/л")
            btn_text = "✅ Так, зберегти" if not has_temp_food else "🩸 Зберегти цукор"
            pending_buttons.append(InlineKeyboardButton(text=btn_text, callback_data="confirm_pending:glucose"))
        if has_temp_food:
            pending_lines.append(f"🥗 Прийом їжі: {temp_food_summary}")
            btn_text = "✅ Так, зберегти" if not has_temp_glucose else "🥗 Зберегти страву"
            pending_buttons.append(InlineKeyboardButton(text=btn_text, callback_data="confirm_pending:food"))

        if pending_buttons:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[pending_buttons])

        prefix = ""
        if extracted_info:
            prefix += f"📝 <b>Автоматично записано показники:</b>\n{extracted_info}\n"
        if pending_lines:
            prefix += "📋 <b>Виявлено:</b>\n" + "\n".join(pending_lines) + "\n<b>Зберегти?</b>\n\n"
        final_text = prefix + reply_text

        try:
            await progress_msg.edit_text(final_text, reply_markup=keyboard)
        except TelegramRetryAfter as e:
            # Flood control, not a malformed-HTML issue — retrying immediately would just hit
            # the same wall again. Give up on this edit silently; the user already saw the
            # streamed preview text, they're just missing the final formatting/buttons.
            logger.error(f"Flood control on final chat reply edit (retry after {e.retry_after}s) — not retrying.")
        except Exception as e:
            logger.error(f"HTML parsing failed for final streamed message: {e}. Falling back to plain text.")
            clean_text = re.sub(r'<[^>]+>', '', final_text)
            try:
                await progress_msg.edit_text(clean_text, reply_markup=keyboard)
            except Exception as e2:
                logger.error(f"Plain-text fallback edit also failed: {e2}")

    except Exception as e:
        logger.error(f"Error in chat assistant flow: {e}")
        try:
            await progress_msg.delete()
        except Exception:
            pass
        if not isinstance(e, TelegramRetryAfter):
            # Don't attempt yet another call to a chat we just got flood-limited on.
            try:
                await message.answer("❌ Помилка обробки повідомлення ШІ. Будь ласка, спробуйте ще раз.")
            except Exception:
                pass
    finally:
        if not typing_task.done():
            typing_task.cancel()
        if not text_task.done():
            text_task.cancel()
        _active_chat_flows.discard(message.from_user.id)
