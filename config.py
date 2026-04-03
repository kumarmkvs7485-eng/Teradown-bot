import os
from dotenv import load_dotenv

load_dotenv()

# ─── Core Bot Settings ────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN",  "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS   = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip().isdigit()]

# ─── UPI Payment ──────────────────────────────────────────────────────────────
UPI_ID      = os.getenv("UPI_ID",   "yourname@paytm")
UPI_NAME    = os.getenv("UPI_NAME", "Your Name")

# ─── GitHub ───────────────────────────────────────────────────────────────────
GITHUB_REPO = os.getenv("GITHUB_REPO", "")

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
DATA_DIR      = os.path.join(BASE_DIR, "data")
QR_DIR        = os.path.join(BASE_DIR, "qrcodes")
LOGS_DIR      = os.path.join(BASE_DIR, "logs")
DATABASE_PATH = os.path.join(DATA_DIR, "bot.db")

for d in (DOWNLOADS_DIR, DATA_DIR, QR_DIR, LOGS_DIR):
    os.makedirs(d, exist_ok=True)

# ─── Termux / Mobile Optimisations ───────────────────────────────────────────
MAX_CONCURRENT_DOWNLOADS = 2
CHUNK_SIZE_KB             = 512
MAX_FILE_SIZE_MB          = 50
REQUESTS_POOL_SIZE        = 3

# ─── Timing & Limits ─────────────────────────────────────────────────────────
FILE_DELETE_HOURS    = 1
FREE_DAILY_LIMIT     = 1
FREE_RESET_HOUR      = 3
FREE_RESET_MINUTE    = 30
SPAM_RATE_LIMIT      = 5
SUSPICIOUS_THRESHOLD = 3

# ─── Download Retry ───────────────────────────────────────────────────────────
DOWNLOAD_RETRIES  = 3
RETRY_DELAY_SEC   = 5

# ─── Subscription Plans ───────────────────────────────────────────────────────
PLANS = {
    "trial":     {"name": "⚡ Trial (24hr One-Time)", "price": 2,  "days": 1,  "one_time": True},
    "daily":     {"name": "☀️ Daily",                 "price": 5,  "days": 1,  "one_time": False},
    "weekly":    {"name": "📅 Weekly",                "price": 15, "days": 7,  "one_time": False},
    "biweekly":  {"name": "🗓 Two Weeks",             "price": 19, "days": 14, "one_time": False},
    "triweekly": {"name": "📆 Three Weeks",           "price": 29, "days": 21, "one_time": False},
    "monthly":   {"name": "💎 Monthly",               "price": 39, "days": 30, "one_time": False},
}

WELCOME_MSG = """👋 *Welcome to TeraBox Downloader Bot!*

I can download and send you TeraBox videos directly in Telegram — playable right here! 🎬

🆓 *Free Tier:* 1 download/day _(resets 3:30 AM IST)_
💎 *Premium:* Unlimited downloads

Starting from just *₹2* for 24 hours!

➡️ Paste a TeraBox link to get started.
📋 Use /plans to see all options."""

HELP_MSG = """📖 *Commands*

/start — Welcome & quick menu
/plans — Subscription plans & prices
/subscribe — Start a subscription
/status — Your account & sub info
/help — This message

*How to download:*
Just paste any TeraBox video link. Bot handles the rest!

*Free Tier:* 1 download/day — resets at 3:30 AM IST
*Subscribed:* Unlimited downloads 🚀

*Payment:*
1. Pick a plan → pay via UPI
2. Send screenshot (must show Txn ID + date + amount)
3. Bot verifies & activates instantly

_⚠️ Files auto-deleted after 1 hour (copyright policy)_"""
