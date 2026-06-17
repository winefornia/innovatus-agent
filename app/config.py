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

# Telegram Bot — Invoice (FireHorse). The tasting-room approval flow no longer
# uses Telegram; it runs entirely over Google Chat (see GOOGLE_CHAT_TR_* below).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _csv_env(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]

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
    '(from:josh@thecavesatsodacanyon.com OR '
    'from:invoicing@messaging.squareup.com OR '
    'from:form-submission@squarespace.info OR '
    'from:innovatuswine.com OR '
    'subject:"Form Submission - Wine tasting Booking" OR '
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

# ── Tasting Room approvals over Google Chat ──────────────────────────────────
# A SEPARATE Google Chat app (its own GCP project → its own bot identity),
# pointed at a dedicated route on this same server. All four are config-gated:
# when GOOGLE_CHAT_TR_SPACE is unset the Google Chat path stays dormant and
# approvals keep flowing over Telegram, so the two channels can run in parallel
# during cutover. See feat/tastingroom-google-chat.
#   GOOGLE_CHAT_TR_SPACE              e.g. "spaces/AAAA…" — where cards post
#   GOOGLE_TASTINGROOM_SA_JSON_B64    base64 of the TR project's SA key (posting)
#   GOOGLE_CHAT_TR_AUDIENCE           the TR webhook URL (token aud check)
#   GOOGLE_CHAT_TR_SIGNER_EMAIL       the TR project's gsuiteaddons SA
#   GOOGLE_CHAT_TR_ENDPOINT_URL       URL card buttons call back into
_TR_DEFAULT_URL = "https://winefornia-agent.fly.dev/webhooks/google-chat/tastingroom"
GOOGLE_CHAT_TR_SPACE = os.getenv("GOOGLE_CHAT_TR_SPACE", "")
# Posting key for the tasting-room Chat app (its own GCP project, #275073979299).
GOOGLE_CHAT_TR_SERVICE_ACCOUNT_JSON_B64 = os.getenv("GOOGLE_TASTINGROOM_SA_JSON_B64", "")
GOOGLE_CHAT_TR_AUDIENCE = os.getenv("GOOGLE_CHAT_TR_AUDIENCE", _TR_DEFAULT_URL)
GOOGLE_CHAT_TR_SIGNER_EMAIL = os.getenv(
    "GOOGLE_CHAT_TR_SIGNER_EMAIL",
    "service-275073979299@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
)
GOOGLE_CHAT_TR_ENDPOINT_URL = os.getenv("GOOGLE_CHAT_TR_ENDPOINT_URL", _TR_DEFAULT_URL)
# Only these Google accounts may approve/act on tasting-room cards or commands.
# Empty list = open to any authenticated space member (back-compatible). Covers
# Cecil + both of Lisa's addresses by default.
GOOGLE_CHAT_TR_AUTHORIZED_EMAILS = [
    e.strip().lower() for e in os.getenv(
        "GOOGLE_CHAT_TR_AUTHORIZED_EMAILS",
        "cecil.park@winefornia.com,lisa@innovatuswine.com,lisa@winefornia.com",
    ).split(",") if e.strip()
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

# Activity page auth — set ACTIVITY_API_KEY in production to gate /activity.
# If unset, the page is open (dev-friendly). Pass as ?key=VALUE in the URL.
ACTIVITY_API_KEY = os.getenv("ACTIVITY_API_KEY", "")

# Production mode — when True, unsafe fallbacks (MemorySaver, etc.) are disabled.
# Set PRODUCTION_MODE=true in production; leave unset or false for local dev.
PRODUCTION_MODE = os.getenv("PRODUCTION_MODE", "false").lower() in ("1", "true", "yes", "on")
