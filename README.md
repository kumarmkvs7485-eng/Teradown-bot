# 🎬 TeraBox Downloader Bot

A powerful Telegram bot for downloading and delivering TeraBox videos — with subscriptions, UPI payments, auto-verification, and anti-spam. Runs 24/7 on Android via **Termux**.

---

## 📁 Files

```
terabox_bot/
├── bot.py            ← Main bot (all handlers)
├── config.py         ← All settings & plan prices
├── database.py       ← SQLite layer
├── downloader.py     ← TeraBox API + gzip compression
├── payment.py        ← OCR verification + QR generation
├── scheduler.py      ← Background jobs
├── setup_termux.sh   ← One-click Termux installer
├── run.sh            ← Smart launcher with auto-restart
├── stop.sh           ← Graceful shutdown
├── update.sh         ← GitHub pull + hot-reload
├── push.sh           ← Commit & push to GitHub
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 🚀 Termux Setup (Vivo V9)

### Step 1 — Install apps from F-Droid
- **Termux** → https://f-droid.org/en/packages/com.termux/
- **Termux:Boot** → https://f-droid.org/en/packages/com.termux.boot/

Open Termux:Boot app once after installing so it registers as a boot service.

### Step 2 — Get the code
```bash
pkg install git -y
git clone https://github.com/YOUR_USERNAME/terabox-bot.git
cd terabox-bot
```

### Step 3 — Run setup
```bash
bash setup_termux.sh
```

### Step 4 — Add your credentials
```bash
cp .env.example .env
nano .env
```
Fill in BOT_TOKEN, ADMIN_IDS, UPI_ID, UPI_NAME. Save: Ctrl+X → Y → Enter

### Step 5 — Start
```bash
bash run.sh
```

---

## 📱 Keep Bot Running on Vivo V9

1. **Disable battery optimisation for Termux:**
   Settings → Battery → App Battery Usage → Termux → Don't Optimise

2. **Background run (close Termux app safely):**
```bash
nohup bash run.sh > /dev/null 2>&1 &
```

3. **View live logs:**
```bash
tail -f logs/bot.log
```

4. **Auto-starts after reboot** via Termux:Boot (setup_termux.sh configures this)

---

## 🔄 GitHub Workflow

```bash
# First time — connect your repo
git remote add origin https://github.com/YOUR_USER/terabox-bot.git
git push -u origin main

# Pull updates & restart bot
bash update.sh

# Push your changes
bash push.sh "your commit message"
```

---

## 👮 Admin Commands

| Command | Description |
|---|---|
| `/admin` or `/stats` | Dashboard: users, revenue, reports |
| `/approve <user_id> <plan_key>` | Manually activate subscription |
| `/ban <user_id> [reason]` | Ban a user |
| `/unban <user_id>` | Remove ban |
| `/broadcast <message>` | Message all users |
| `/resolve <report_id>` | Dismiss an admin report |

Plan keys: `trial` · `daily` · `weekly` · `biweekly` · `triweekly` · `monthly`

---

## 💳 Plans

| Plan | Price | Duration |
|---|---|---|
| ⚡ Trial (one-time) | ₹2 | 24 hr |
| ☀️ Daily | ₹5 | 1 day |
| 📅 Weekly | ₹15 | 7 days |
| 🗓 Two Weeks | ₹19 | 14 days |
| 📆 Three Weeks | ₹29 | 21 days |
| 💎 Monthly | ₹39 | 30 days |

---

## ⚙️ Customisation (config.py)

```python
FREE_DAILY_LIMIT         = 1    # free downloads/day
FREE_RESET_HOUR          = 3    # reset at 3:30 AM
FREE_RESET_MINUTE        = 30
FILE_DELETE_HOURS        = 1    # auto-delete after 1 hr
SPAM_RATE_LIMIT          = 5    # requests/min
SUSPICIOUS_THRESHOLD     = 3    # failed payments before flag
MAX_CONCURRENT_DOWNLOADS = 2    # parallel downloads
```

---

## ❓ Troubleshooting

**Bot won't start** → check `nano .env` — ensure BOT_TOKEN is set

**OCR not working** → `pkg install tesseract` in Termux

**Downloads failing** → link must be publicly shared; run `bash update.sh` to get latest API fixes

**Phone killing Termux** → disable battery optimisation for Termux in phone settings
