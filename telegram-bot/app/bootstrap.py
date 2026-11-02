"""Home Assistant app startup. Runs before app.config is imported, as root.

Turns the app's options into environment variables, imports an existing database once,
then gives up root. Does nothing outside Home Assistant, so local development keeps
using .env.
"""
import json
import logging
import os
import pwd
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

DATA = Path("/data")
OPTIONS = DATA / "options.json"
DATABASE = DATA / "diabetes.db"
IMPORT_FROM = Path("/config/diabetes.db")
USER = "bot"

# Option name -> environment variable read by app.config.
ENV = {
    "bot_token": "BOT_TOKEN",
    "openai_api_key": "OPENAI_API_KEY",
    "usda_api_key": "USDA_API_KEY",
    "telegram_user_ids": "TELEGRAM_WHITELIST_USER_IDS",
    "admin_user_ids": "ADMIN_USER_IDS",
    "patient_telegram_id": "PATIENT_TELEGRAM_ID",
    "nightscout_url": "NIGHTSCOUT_URL",
    "nightscout_token": "NIGHTSCOUT_TOKEN",
    "dashboard_url": "DASHBOARD_URL",
    "alert_urgent_low": "ALERT_URGENT_LOW",
    "alert_low": "ALERT_LOW",
    "alert_high": "ALERT_HIGH",
    "alert_predicted_low": "ALERT_PREDICTED_LOW",
    "alert_predicted_high": "ALERT_PREDICTED_HIGH",
    "alert_no_data_minutes": "ALERT_NO_DATA_MINUTES",
    "alert_repeat_minutes": "ALERT_REPEAT_MINUTES",
    "alert_recipients": "ALERT_RECIPIENTS",
    "ai_model_heavy": "MODEL_HEAVY",
    "ai_model_light": "MODEL_LIGHT",
}


def environment(options: dict) -> dict:
    """Environment for the given options. Lists are JSON, which is what pydantic-settings reads."""
    env = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{DATABASE}",
        "BACKUP_DIR": str(DATA / "backups"),
    }
    for option, variable in ENV.items():
        value = options.get(option)
        if value is not None:
            env[variable] = json.dumps(value) if isinstance(value, list) else str(value)
    # The patient can always use the bot, whether or not they're listed.
    allowed = [options.get("patient_telegram_id"), *options.get("telegram_user_ids", [])]
    env["TELEGRAM_WHITELIST_USER_IDS"] = json.dumps([uid for uid in allowed if uid])
    return env


def import_database() -> None:
    """Takes a database someone placed in the app's config folder, once.

    The app has to start once for that folder to exist, so a database can already be here.
    It's kept next to the new one, not deleted.
    """
    if not IMPORT_FROM.exists():
        return
    if DATABASE.exists():
        DATABASE.rename(DATABASE.with_suffix(".db.before-import"))
    shutil.copy2(IMPORT_FROM, DATABASE)
    IMPORT_FROM.rename(IMPORT_FROM.with_suffix(".db.imported"))
    logger.info("Imported the existing database")


def drop_root() -> None:
    account = pwd.getpwnam(USER)
    for folder, _, files in os.walk(DATA):
        os.chown(folder, account.pw_uid, account.pw_gid)
        for name in files:
            os.chown(Path(folder) / name, account.pw_uid, account.pw_gid)
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    os.environ["HOME"] = str(DATA)


def prepare() -> None:
    if not OPTIONS.exists():
        return
    os.environ.update(environment(json.loads(OPTIONS.read_text())))
    import_database()
    drop_root()
