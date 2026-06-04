import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SQUARE_PROD_ACCESS_TOKEN = os.getenv("SQUARE_PROD_ACCESS_TOKEN", "")
SQUARE_PROD_LOCATION_ID = os.getenv("SQUARE_PROD_LOCATION_ID", "")
SQUARE_ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN", "")
SQUARE_LOCATION_ID = os.getenv("SQUARE_LOCATION_ID", "")
SQUARE_ENVIRONMENT = os.getenv("SQUARE_ENVIRONMENT", "sandbox")

# Telegram Bot — Invoice (FireHorse)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Telegram Bot — Tasting Room (separate bot)
TELEGRAM_TASTINGROOM_BOT_TOKEN = os.getenv("TELEGRAM_TASTINGROOM_BOT_TOKEN", "")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID", "")


def _csv_env(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


TELEGRAM_TASTINGROOM_AUTHORIZED_CHAT_IDS = _csv_env(
    "TELEGRAM_TASTINGROOM_AUTHORIZED_CHAT_IDS",
    TELEGRAM_APPROVAL_CHAT_ID,
)
TELEGRAM_TASTINGROOM_AUTHORIZED_USER_IDS = _csv_env("TELEGRAM_TASTINGROOM_AUTHORIZED_USER_IDS")

# Gmail OAuth (base64-encoded token.json; run scripts/google_auth.py to generate)
GMAIL_TOKEN_JSON_B64 = os.getenv("GMAIL_TOKEN_JSON_B64", "")
GMAIL_INTAKE_LABEL = os.getenv("GMAIL_INTAKE_LABEL", "To Invoice")
GMAIL_TASTING_LABEL = os.getenv("GMAIL_TASTING_LABEL", "Tasting Requests")
GMAIL_TASTING_SOURCE_LABELS = [
    label.strip()
    for label in os.getenv("GMAIL_TASTING_SOURCE_LABELS", "INNOVATUS,Tasting Room/Inbox,Tasting Requests").split(",")
    if label.strip()
]
GMAIL_TASTING_ROOT_LABEL = os.getenv("GMAIL_TASTING_ROOT_LABEL", "Tasting Room")
GMAIL_TASTING_PROCESSED_LABEL = os.getenv("GMAIL_TASTING_PROCESSED_LABEL", "Tasting Room/Processed")
GMAIL_TASTING_QUERY = os.getenv(
    "GMAIL_TASTING_QUERY",
    'newer:2026/05/01 (from:josh@thecavesatsodacanyon.com OR '
    'from:invoicing@messaging.squareup.com OR '
    'from:form-submission@squarespace.info OR subject:"Form Submission - Wine tasting Booking" OR '
    'subject:"Availability Check" OR subject:"new invoice was created" OR subject:"invoice was paid")',
)
GMAIL_TASTING_POLL_SECONDS = int(os.getenv("GMAIL_TASTING_POLL_SECONDS", "60"))
JOSH_EMAIL = os.getenv("JOSH_EMAIL", "josh@thecavesatsodacanyon.com")
TASTINGROOM_SAFE_MODE = os.getenv("TASTINGROOM_SAFE_MODE", "true").lower() in ("1", "true", "yes", "on")
TASTINGROOM_TEST_RECIPIENT = os.getenv("TASTINGROOM_TEST_RECIPIENT", os.getenv("GOOGLE_ACCOUNT_EMAIL", ""))
GOOGLE_AUTHORIZED_ACCOUNTS = [
    email.strip().lower()
    for email in os.getenv(
        "GOOGLE_AUTHORIZED_ACCOUNTS",
        "lisa@innovatuswine.com,cecil.park@winefornia.com",
    ).split(",")
    if email.strip()
]

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
# Postgres connection string for LangGraph checkpointer.
# Use pgBouncer port 6543 (NOT 5432 — that's blocked externally).
# Set in Fly secrets:
#   flyctl secrets set POSTGRES_CONNECTION_STRING="postgresql://postgres:[PW]@db.zlbixpklvejcuxifqzjk.supabase.co:6543/postgres"
# sslmode=require is added automatically if missing.
POSTGRES_CONNECTION_STRING = os.getenv("POSTGRES_CONNECTION_STRING", "")

# Mem0 (persistent user memory across sessions)
MEM0_API_KEY = os.getenv("MEM0_API_KEY", "")

SHIPPING_WAIVER_THRESHOLD_CENTS = 150_000  # $1,500

# Production mode — when True, unsafe fallbacks (MemorySaver, etc.) are disabled.
# Set PRODUCTION_MODE=true in production; leave unset or false for local dev.
PRODUCTION_MODE = os.getenv("PRODUCTION_MODE", "false").lower() in ("1", "true", "yes", "on")

# Patch auto-apply — when True, low/medium patches are applied automatically.
# DISABLED by default. Enable only in dev/staging with explicit review.
PATCH_AUTO_APPLY = os.getenv("PATCH_AUTO_APPLY", "false").lower() in ("1", "true", "yes", "on")
