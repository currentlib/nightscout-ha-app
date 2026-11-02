import datetime

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func
from sqlalchemy.future import select

from app.config import settings
from app.database import AiUsage, User, async_session_local
from app.services.tiers import TIER_LIMITS, effective_tier

admin_router = Router()


@admin_router.message(Command("admin"))
async def admin_stats_handler(message: Message, command: CommandObject):
    """
    Admin-only. Bare '/admin': cost/usage breakdown across all users.
    '/admin settier <telegram_id> <tier>': change a user's tier.
    '/admin gettier <telegram_id>': look up a user's current tier.
    Not listed in the bot's command menu.
    """
    if message.from_user.id not in settings.ADMIN_USER_IDS:
        await message.answer("🚫 Ця команда доступна лише адміністраторам.")
        return

    args = (command.args or "").strip().split()

    if args and args[0].lower() == "settier":
        if len(args) != 3:
            await message.answer(
                "Використання: <code>/admin settier &lt;telegram_id&gt; &lt;tier&gt;</code>\n"
                f"Тарифи: {', '.join(TIER_LIMITS.keys())}"
            )
            return
        _, target_id_str, new_tier = args
        new_tier = new_tier.lower()
        if new_tier not in TIER_LIMITS:
            await message.answer(f"❌ Невідомий тариф «{new_tier}». Доступні: {', '.join(TIER_LIMITS.keys())}")
            return
        try:
            target_id = int(target_id_str)
        except ValueError:
            await message.answer("❌ telegram_id має бути числом.")
            return

        async with async_session_local() as session:
            async with session.begin():
                result = await session.execute(select(User).filter(User.telegram_id == target_id))
                target_user = result.scalars().first()
                if not target_user:
                    await message.answer(f"❌ Користувача <code>{target_id}</code> не знайдено.")
                    return
                target_user.tier = new_tier
                session.add(target_user)

        await message.answer(f"✅ Тариф користувача <code>{target_id}</code> змінено на <b>{new_tier}</b>.")
        return

    if args and args[0].lower() == "gettier":
        if len(args) != 2:
            await message.answer("Використання: <code>/admin gettier &lt;telegram_id&gt;</code>")
            return
        try:
            target_id = int(args[1])
        except ValueError:
            await message.answer("❌ telegram_id має бути числом.")
            return
        async with async_session_local() as session:
            result = await session.execute(select(User).filter(User.telegram_id == target_id))
            target_user = result.scalars().first()
        if not target_user:
            await message.answer(f"❌ Користувача <code>{target_id}</code> не знайдено.")
            return
        await message.answer(
            f"<code>{target_id}</code>: тариф <b>{target_user.tier}</b>, "
            f"ефективний тариф зараз <b>{effective_tier(target_user)}</b>"
            + (f" (пробний до {target_user.trial_expires_at})" if target_user.trial_expires_at else "")
        )
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

        top_ids = [tid for tid, _, _ in per_user_rows]
        tier_by_id = {}
        if top_ids:
            tier_rows = (await session.execute(
                select(User.telegram_id, User.tier).filter(User.telegram_id.in_(top_ids))
            )).all()
            tier_by_id = dict(tier_rows)

        per_type_rows = (await session.execute(
            select(AiUsage.call_type, func.sum(AiUsage.cost_usd), func.count(AiUsage.id))
            .filter(AiUsage.timestamp >= month_start)
            .group_by(AiUsage.call_type)
            .order_by(func.sum(AiUsage.cost_usd).desc())
        )).all()

    per_user_lines = "\n".join(
        f"  <code>{tid}</code> [{tier_by_id.get(tid, '?')}]: ${cost:.3f} ({cnt} викликів)"
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
        f"📋 <b>Витрати за типом виклику (цей місяць):</b>\n{per_type_lines}\n\n"
        f"⚙️ Керування: <code>/admin settier &lt;id&gt; &lt;tier&gt;</code>, "
        f"<code>/admin gettier &lt;id&gt;</code>"
    )
    await message.answer(text)
