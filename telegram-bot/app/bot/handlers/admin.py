import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func
from sqlalchemy.future import select

from app.config import settings
from app.database import AiUsage, User, async_session_local

admin_router = Router()


@admin_router.message(Command("admin"))
async def admin_stats_handler(message: Message):
    """Admin-only AI spending breakdown. Not listed in the bot's command menu."""
    if message.from_user.id not in settings.ADMIN_USER_IDS:
        await message.answer("🚫 Ця команда доступна лише адміністраторам.")
        return

    month_start = datetime.datetime.now(datetime.timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )

    async with async_session_local() as session:
        total_users = (await session.execute(select(func.count(User.telegram_id)))).scalar() or 0

        total_cost, total_calls = (await session.execute(
            select(func.sum(AiUsage.cost_usd), func.count(AiUsage.id))
        )).first()

        month_cost, month_calls = (await session.execute(
            select(func.sum(AiUsage.cost_usd), func.count(AiUsage.id))
            .filter(AiUsage.timestamp >= month_start)
        )).first()

        per_user_rows = (await session.execute(
            select(AiUsage.telegram_id, func.sum(AiUsage.cost_usd), func.count(AiUsage.id))
            .filter(AiUsage.timestamp >= month_start)
            .group_by(AiUsage.telegram_id)
            .order_by(func.sum(AiUsage.cost_usd).desc())
            .limit(10)
        )).all()

        per_type_rows = (await session.execute(
            select(AiUsage.call_type, func.sum(AiUsage.cost_usd), func.count(AiUsage.id))
            .filter(AiUsage.timestamp >= month_start)
            .group_by(AiUsage.call_type)
            .order_by(func.sum(AiUsage.cost_usd).desc())
        )).all()

    per_user_lines = "\n".join(
        f"  <code>{tid}</code>: ${cost:.3f} ({cnt} викликів)"
        for tid, cost, cnt in per_user_rows
    ) or "  Немає даних."

    per_type_lines = "\n".join(
        f"  {ctype}: ${cost:.3f} ({cnt} викликів)" for ctype, cost, cnt in per_type_rows
    ) or "  Немає даних."

    text = (
        f"📊 <b>Адмін-статистика</b>\n\n"
        f"👥 <b>Всього користувачів:</b> {total_users}\n\n"
        f"💰 <b>Витрати за весь час:</b> ${total_cost or 0.0:.3f} ({total_calls or 0} викликів)\n"
        f"💰 <b>Витрати за цей місяць:</b> ${month_cost or 0.0:.3f} ({month_calls or 0} викликів)\n\n"
        f"🏆 <b>Топ-10 користувачів за витратами (цей місяць):</b>\n{per_user_lines}\n\n"
        f"📋 <b>Витрати за типом виклику (цей місяць):</b>\n{per_type_lines}"
    )
    await message.answer(text)
