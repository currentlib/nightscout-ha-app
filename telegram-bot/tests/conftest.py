import os
import tempfile

# A throwaway database and predictable settings, set before app.config is first imported.
os.environ.update(
    BOT_TOKEN="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    DATABASE_URL=f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/test.db",
    NIGHTSCOUT_URL="http://nightscout.invalid",
    NIGHTSCOUT_TOKEN="test-token",
    PATIENT_TELEGRAM_ID="1",
    ALERT_RECIPIENTS="[]",
)
