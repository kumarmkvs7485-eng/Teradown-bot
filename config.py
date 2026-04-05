import os
from dotenv import load_dotenv

load_dotenv()

# ── Core ──────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS   = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip().isdigit()]
BOT_NAME    = os.getenv("BOT_NAME", "Downloader Hut")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# ── UPI ───────────────────────────────────────────────────────────────────────
UPI_ID   = os.getenv("UPI_ID",   "yourname@paytm")
UPI_NAME = os.getenv("UPI_NAME", "Your Name")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
DATA_DIR      = os.path.join(BASE_DIR, "data")
QR_DIR        = os.path.join(BASE_DIR, "qrcodes")
LOGS_DIR      = os.path.join(BASE_DIR, "logs")
DATABASE_PATH = os.path.join(DATA_DIR, "bot.db")

for _d in (DOWNLOADS_DIR, DATA_DIR, QR_DIR, LOGS_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Limits ────────────────────────────────────────────────────────────────────
FILE_DELETE_HOURS        = 1
FREE_DAILY_LIMIT         = 1
FREE_RESET_HOUR          = 3
FREE_RESET_MINUTE        = 30
SPAM_RATE_LIMIT          = 8       # requests per 60s
SUSPICIOUS_THRESHOLD     = 4       # failed payment attempts
MAX_CONCURRENT_DOWNLOADS = 3       # parallel downloads
MAX_FILE_SIZE_MB         = 50      # Telegram Bot API hard limit
CHUNK_SIZE_KB            = 1024    # download chunk = 1MB
DOWNLOAD_RETRIES         = 4
RETRY_DELAY_SEC          = 3

# ── Payment verification ──────────────────────────────────────────────────────
# "auto"   = auto-approve if OCR confidence is medium or high
# "strict" = only approve on high confidence
# "manual" = always send to admin (never auto-approve)
PAYMENT_VERIFY_MODE = "auto"

# ── Subscriptions ─────────────────────────────────────────────────────────────
PLANS = {
    "trial":     {"name": "⚡ Trial (One-Time)",  "price": 2,  "days": 1,  "one_time": True},
    "daily":     {"name": "☀️ Daily",              "price": 5,  "days": 1,  "one_time": False},
    "weekly":    {"name": "📅 Weekly",             "price": 15, "days": 7,  "one_time": False},
    "biweekly":  {"name": "🗓 Two Weeks",          "price": 19, "days": 14, "one_time": False},
    "triweekly": {"name": "📆 Three Weeks",        "price": 29, "days": 21, "one_time": False},
    "monthly":   {"name": "💎 Monthly",            "price": 39, "days": 30, "one_time": False},
}

# ── Messages ──────────────────────────────────────────────────────────────────
WELCOME_MSG = """🎬 *Welcome to {bot_name}!*

Download any TeraBox video straight to Telegram — plays right in the app!

🆓 *Free:* 1 download/day _(resets 3:30 AM)_
💎 *Premium:* Unlimited — from just *₹2*

➡️ Paste a TeraBox link to download now!"""

HELP_MSG = """📖 *Commands*

/start — Home
/plans — Subscription plans
/subscribe — Buy a plan
/status — Your account info
/cancel — Cancel ongoing download
/help — This message

*Supported links:*
• terabox.com  • 1024terabox.com
• teraboxapp.com  • and more

*Payment:* UPI only → send screenshot after paying
*Files auto-delete* after 1 hour 🗑"""
