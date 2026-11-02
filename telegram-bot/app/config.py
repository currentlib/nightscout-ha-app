from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Telegram Bot Settings
    BOT_TOKEN: str = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ123456789"
    TELEGRAM_WHITELIST_USER_IDS: list[int] = [0]  # List of authorized Telegram User IDs
    ADMIN_USER_IDS: list[int] = []  # Subset of whitelisted users allowed to run /admin
    # Whitelist-only. When True, WhitelistMiddleware stops blocking non-whitelisted users.
    PUBLIC_SIGNUP_ENABLED: bool = False

    # Database Settings
    # Use SQLite + aiosqlite as default for async compatibility
    DATABASE_URL: str = "sqlite+aiosqlite:///./diabetes.db"
    # Local daily SQLite snapshot (VACUUM INTO) — see app/services/backup.py.
    # Off-server push (S3/GCS/etc.) is not implemented yet; add once a provider is chosen.
    BACKUP_DIR: str = "./backups"
    BACKUP_RETENTION_DAYS: int = 7

    # Link behind the bot's dashboard button, for example a Home Assistant dashboard.
    DASHBOARD_URL: str = ""

    # Nightscout. Sync and CGM features stay off until all three are set.
    NIGHTSCOUT_URL: str = ""
    NIGHTSCOUT_TOKEN: str = ""  # an access token, or the API secret
    PATIENT_TELEGRAM_ID: int = 0  # whose data this Nightscout holds
    TIMEZONE: str = Field(default="Europe/Kyiv", validation_alias="TZ")

    # Alerts, in mmol/L. Recipients: [{"chat_id": 1, "urgent_low": true, "low": true,
    # "high": true, "predicted": true, "no_data": true}]. Empty means the patient, for all.
    ALERT_URGENT_LOW: float = 3.1
    ALERT_LOW: float = 3.9
    ALERT_HIGH: float = 13.9
    ALERT_PREDICTED_LOW: float = 4.0
    ALERT_PREDICTED_HIGH: float = 10.0
    ALERT_NO_DATA_MINUTES: int = 30
    ALERT_REPEAT_MINUTES: int = 30
    ALERT_RECIPIENTS: list[dict] = []

    # OpenAI settings
    OPENAI_API_KEY: str = "YOUR_DEFAULT_OPENAI_API_KEY"
    # USDA FoodData Central lookup for dish/nutrition grounding. "DEMO_KEY" works with no signup
    # but is rate-limited; get a free key (1,000 req/hour) at https://api.nal.usda.gov/fdc/api-key-signup/
    USDA_API_KEY: str = "DEMO_KEY"
    # Model used per tier, split by call weight: "heavy" = vision food analysis + chat,
    # "light" = metrics extraction + timing hints. Independently configurable per tier so
    # e.g. Regular can run cheap-everywhere while Unlimited runs the flagship for heavy calls
    # only. See app/services/tiers.py::TIER_MODELS. Free tier has no AI access, no models needed.
    MODEL_REGULAR_HEAVY: str = "gpt-5.6-luna"
    MODEL_REGULAR_LIGHT: str = "gpt-5.6-luna"
    MODEL_SUPER_HEAVY: str = "gpt-5.6-terra"
    MODEL_SUPER_LIGHT: str = "gpt-5.6-luna"
    MODEL_UNLIMITED_HEAVY: str = "gpt-5.6-terra"
    MODEL_UNLIMITED_LIGHT: str = "gpt-5.6-luna"

    # Context & Token Optimization Settings
    CHAT_HISTORY_LIMIT: int = 15
    CHAT_CONTEXT_DB_LOOKBACK_HOURS: int = 24
    CHAT_CONTEXT_LOGS_LIMIT: int = 15

    # Emergency Settings
    EMERGENCY_TIMER_MINUTES: int = 15

    # App Settings
    USE_AI_TIMING_HINTS: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True
    )

settings = Settings()
