import datetime
from sqlalchemy import Column, Integer, BigInteger, Float, String, DateTime, JSON, ForeignKey, Boolean, Index, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
from app.config import settings

# Declare Base
Base = declarative_base()

# Configure SQLAlchemy Engine and Session
engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session_local = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True, unique=True, index=True)
    icr = Column(Float, default=10.0, nullable=False)        # Вуглеводний коефіцієнт (Insulin-to-Carbohydrate Ratio)
    isf = Column(Float, default=3.0, nullable=False)         # Фактор чутливості до інсуліну (Insulin Sensitivity Factor, ммоль/л на 1 ОД)
    target_bg = Column(Float, default=5.5, nullable=False)   # Цільовий рівень глюкози (ммоль/л)
    dia_hours = Column(Float, default=4.0, nullable=False)   # Тривалість дії інсуліну (DIA, години)
    
    # Reminder configuration
    reminder_enabled = Column(Boolean, default=False, nullable=False)
    reminder_start_hour = Column(Integer, default=8, nullable=False)
    reminder_end_hour = Column(Integer, default=22, nullable=False)
    reminder_interval_hours = Column(Integer, default=2, nullable=False)
    
    # Alert history tracking
    last_glucose_alert_at = Column(DateTime(timezone=True), nullable=True)
    last_glucose_alert_type = Column(String, nullable=True)
    ns_profile_synced = Column(String, nullable=True)  # ICR|ISF|target|DIA as of the last Nightscout sync
    predictive_alerts_enabled = Column(Boolean, default=True, nullable=False)
    
    # Physical parameters
    weight = Column(Float, nullable=True)
    diabetes_type = Column(String, nullable=True)
    activity_level = Column(String, nullable=True)
    gender = Column(String, nullable=True)
    birth_year = Column(Integer, nullable=True)
    insulin_type = Column(String, nullable=True)  # ultra_rapid | rapid | regular

    # Long-term AI memory: durable facts auto-extracted from chat (allergies, preferences, routines)
    memory_notes = Column(String, nullable=True)

    # No longer used, but it's NOT NULL with no database default in existing files.
    tier = Column(String, default="unlimited", nullable=False)
    clinical_setup_complete = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))

    # Relationships
    logs = relationship("LogEntry", back_populates="user", cascade="all, delete-orphan")
    injections = relationship("InsulinInjection", back_populates="user", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="user", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    role = Column(String, nullable=False)  # "user" or "assistant"
    content = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), index=True)

    __table_args__ = (Index("ix_chat_messages_telegram_id_timestamp", "telegram_id", "timestamp"),)

    user = relationship("User", back_populates="chat_messages")


class LogEntry(Base):
    __tablename__ = "log_entries"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    glucose_level = Column(Float, nullable=True)  # ммоль/л
    carbs = Column(Float, nullable=True)          # грами
    protein = Column(Float, nullable=True)        # грами
    fat = Column(Float, nullable=True)            # грами
    calories = Column(Float, nullable=True)       # ккал
    sugar = Column(Float, nullable=True)          # грами
    meal_description = Column(String, nullable=True)
    confidence_score = Column(Float, nullable=True)
    ingredients = Column(JSON, nullable=True)     # detailed list/breakdown
    ns_id = Column(String, nullable=True, index=True)  # Nightscout treatment _id, null until synced
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), index=True)

    __table_args__ = (Index("ix_log_entries_telegram_id_timestamp", "telegram_id", "timestamp"),)

    user = relationship("User", back_populates="logs")


class InsulinInjection(Base):
    __tablename__ = "insulin_injections"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    dosage = Column(Float, nullable=False)       # Units
    injection_type = Column(String, default="bolus", nullable=False)  # bolus or basal
    ns_id = Column(String, nullable=True, index=True)  # Nightscout treatment _id, null until synced
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), index=True)

    __table_args__ = (Index("ix_insulin_injections_telegram_id_timestamp", "telegram_id", "timestamp"),)

    user = relationship("User", back_populates="injections")


class AiUsage(Base):
    """One row per AI API call — lets cost be broken down per user, per feature, or over time."""
    __tablename__ = "ai_usage"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=True)
    call_type = Column(String, nullable=False)     # e.g. "chat", "food_analysis", "extraction", "timing_hint"
    model = Column(String, nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), index=True)

    __table_args__ = (Index("ix_ai_usage_telegram_id_timestamp", "telegram_id", "timestamp"),)


async def init_db():
    """Initializes tables in database."""
    async with engine.begin() as conn:
        # Create all tables if they don't exist
        await conn.run_sync(Base.metadata.create_all)

        # Create indexes if they don't exist (create_all skips existing tables, so
        # indexes added after the table already existed need to be created explicitly)
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_log_entries_telegram_id_timestamp ON log_entries (telegram_id, timestamp)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_insulin_injections_telegram_id_timestamp ON insulin_injections (telegram_id, timestamp)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_messages_telegram_id_timestamp ON chat_messages (telegram_id, timestamp)"))

        # Check and alter users table to add reminder columns if they are missing
        for statement in (
            "ALTER TABLE log_entries ADD COLUMN ns_id TEXT",
            "ALTER TABLE insulin_injections ADD COLUMN ns_id TEXT",
            "ALTER TABLE users ADD COLUMN ns_profile_synced TEXT",
        ):
            try:
                await conn.execute(text(statement))
            except Exception:
                pass
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_log_entries_ns_id ON log_entries (ns_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_insulin_injections_ns_id ON insulin_injections (ns_id)"))

        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN reminder_enabled BOOLEAN DEFAULT 0 NOT NULL"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN reminder_start_hour INTEGER DEFAULT 8 NOT NULL"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN reminder_end_hour INTEGER DEFAULT 22 NOT NULL"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN reminder_interval_hours INTEGER DEFAULT 2 NOT NULL"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN last_glucose_alert_at TIMESTAMP"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN last_glucose_alert_type TEXT"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN predictive_alerts_enabled BOOLEAN DEFAULT 1 NOT NULL"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN weight FLOAT"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN diabetes_type TEXT"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN activity_level TEXT"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN gender TEXT"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN birth_year INTEGER"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN insulin_type TEXT"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN memory_notes TEXT"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN tier TEXT DEFAULT 'unlimited' NOT NULL"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN clinical_setup_complete BOOLEAN DEFAULT 0 NOT NULL"))
        except Exception:
            pass

        # Someone with logged history is past clinical setup, even if their database is older
        # than this column. Anyone else must finish it, or doses would use default values.
        await conn.execute(text(
            "UPDATE users SET clinical_setup_complete = 1 "
            "WHERE telegram_id IN (SELECT telegram_id FROM log_entries)"
        ))


async def get_db():
    """Dependency generator for database sessions."""
    async with async_session_local() as session:
        yield session
