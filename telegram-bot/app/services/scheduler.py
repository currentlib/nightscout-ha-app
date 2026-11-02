from apscheduler.schedulers.asyncio import AsyncIOScheduler

# We use AsyncIOScheduler since this is an async framework (aiogram)
scheduler = AsyncIOScheduler()
