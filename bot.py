"""
bot.py — Downloader Hut Telegram Bot
  Full-featured, Termux-optimised, all restrictions removed.
  python-telegram-bot v20
"""
import os, time, logging, asyncio, logging.handlers
from collections import defaultdict
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.error import TelegramError, RetryAfter, TimedOut, BadRequest

import database as db
from config import (
    BOT_TOKEN, ADMIN_IDS, UPI_ID, UPI_NAME, PLANS, BOT_NAME,
    FREE_DAILY_LIMIT, QR_DIR, SUSPICIOUS_THRESHOLD, SPAM_RATE_LIMIT,
    MAX_CONCURRENT_DOWNLOADS, PAYMENT_VERIFY_MODE, WELCOME_MSG, HELP_MSG,
)
from downloader import (
    is_terabox_url, download_video, decompress_file,
    format_size, cleanup_user_dir, install_ytdlp,
)
from payment import generate_upi_qr, hash_screenshot, verify_payment
from scheduler import start_scheduler, stop_scheduler

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.handlers.RotatingFileHandler(
            "logs/bot.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────
_rate: dict[int, list[float]] = defaultdict(list)
def _limited(uid: int) -> bool:
    now = time.time()
    w   = [t for t in _rate[uid] if now - t < 60]
    _rate[uid] = w
    if len(w) >= SPAM_RATE_LIMIT:
        return True
    _rate[uid].append(now)
    return False

# ── Download semaphore ────────────────────────────────────────────────────────
_dl_sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# ── Pending payments ──────────────────────────────────────────────────────────
_pending: dict[int, dict] = {}

# ── Active downloads (for /cancel) ───────────────────────────────────────────
_active_dl: dict[int, bool] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────
async def _alert(bot: Bot, text: str, parse_md: bool = True):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid, f"🔔 *Admin Alert*\n\n{text}",
                parse_mode=ParseMode.MARKDOWN if parse_md else None
            )
        except TelegramError:
            pass

async def _edit(msg, text: str, kb=None, md=True):
    try:
        await msg.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN if md else None,
            reply_markup=kb,
        )
    except (TelegramError, BadRequest):
        pass

def _sub_line(uid: int) -> str:
    sub = db.get_active_sub(uid)
    if sub:
        end = datetime.strptime(sub["end_date"], "%Y-%m-%d %H:%M:%S")
        td  = end - datetime.now()
        h   = int(td.total_seconds() // 3600)
        m   = int((td.total_seconds() % 3600) // 60)
        return f"✅ *{sub['plan_name']}* — expires in *{h}h {m}m*"
    free = db.get_free_today(uid)
    left = max(0, FREE_DAILY_LIMIT - free)
    return f"🆓 Free: *{left}/{FREE_DAILY_LIMIT}* download left today"

def _plans_kb(trial_ok=True):
    rows = []
    for k, p in PLANS.items():
        if k == "trial" and not trial_ok:
            continue
        rows.append([InlineKeyboardButton(
            f"{p['name']}  ₹{p['price']}", callback_data=f"plan:{k}"
        )])
    return InlineKeyboardMarkup(rows)

def _home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Plans",    callback_data="show_plans"),
         InlineKeyboardButton("📊 Status",   callback_data="my_status")],
        [InlineKeyboardButton("❓ Help",      callback_data="show_help"),
         InlineKeyboardButton("🔗 Share Bot", callback_data="share_bot")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
#   COMMANDS
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    new = db.get_user(u.id) is None
    db.upsert_user(u.id, u.username, u.full_name, u.language_code or "en")

    if db.is_banned(u.id):
        await update.message.reply_text("🚫 Account suspended. Contact admin.")
        return

    # Referral tracking
    args = ctx.args
    if args and args[0].startswith("ref_") and new:
        try:
            ref_id = int(args[0][4:])
            if ref_id != u.id:
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE users SET referral_count=referral_count+1 WHERE user_id=?",
                        (ref_id,)
                    )
                try:
                    await ctx.bot.send_message(
                        ref_id,
                        f"🎉 Someone joined via your referral link!\n"
                        f"👤 {u.full_name}"
                    )
                except TelegramError:
                    pass
        except ValueError:
            pass

    msg = WELCOME_MSG.format(bot_name=BOT_NAME)
    greeting = (
        f"👋 *Hey {u.first_name}! Welcome to {BOT_NAME}!* 🎉\n\n"
        + msg
    ) if new else (
        f"👋 *Welcome back, {u.first_name}!*\n\n{_sub_line(u.id)}"
    )

    await update.message.reply_text(
        greeting, parse_mode=ParseMode.MARKDOWN, reply_markup=_home_kb()
    )

    if new:
        await _alert(ctx.bot,
            f"🆕 New user!\n👤 *{u.full_name}* (@{u.username})\n🆔 `{u.id}`")

async def cmd_help(update: Update, _ctx):
    await update.message.reply_text(HELP_MSG, parse_mode=ParseMode.MARKDOWN)

async def cmd_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.upsert_user(u.id, u.username, u.full_name)
    trial_ok = not db.has_used_trial(u.id)
    lines = ["💎 *Subscription Plans*\n"]
    for k, p in PLANS.items():
        tag = " _(one-time offer)_" if k == "trial" else ""
        used = " ~~used~~" if k == "trial" and not trial_ok else ""
        lines.append(f"• {p['name']} — *₹{p['price']}*{tag}{used}")
    lines += ["", "💳 Pay via UPI → send screenshot → *instant activation* ✨"]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=_plans_kb(trial_ok),
    )

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_plans(update, ctx)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.upsert_user(u.id, u.username, u.full_name)
    s   = db.full_status(u.id)
    usr = s.get("user", {})
    lines = [
        f"📊 *{BOT_NAME} — Your Account*\n",
        f"👤 *{usr.get('full_name','?')}*  (@{usr.get('username','?')})",
        f"🆔 ID: `{u.id}`",
        f"📅 Joined: {str(usr.get('first_seen',''))[:10]}",
        f"⏱ Last active: {str(usr.get('last_active',''))[:16]}",
        "",
        f"📦 *Subscription:*\n{_sub_line(u.id)}",
        "",
        f"⬇️ Total downloads: *{usr.get('total_downloads',0)}*",
        f"💳 Payments made: *{s.get('total_payments',0)}*",
        f"🏷 Plans bought: *{s.get('total_subs',0)}*",
    ]
    recent = s.get("recent", [])
    if recent:
        lines.append("\n📥 *Recent downloads:*")
        for r in recent:
            lines.append(f"  • `{r['filename'][:38]}`")
    if usr.get("is_suspicious"):
        lines.append("\n⚠️ _Account flagged — contact support_")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Subscribe", callback_data="show_plans"),
             InlineKeyboardButton("🏠 Home",      callback_data="back_home")],
        ])
    )

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _active_dl.get(uid):
        _active_dl[uid] = False
        await update.message.reply_text("⏹ Download cancelled.")
    elif _pending.get(uid):
        _pending.pop(uid, None)
        await update.message.reply_text("❌ Payment cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel right now.")

# ── Admin decorator ───────────────────────────────────────────────────────────
def admin_only(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("🚫 Admin only.")
            return
        await fn(update, ctx)
    return wrap

@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = db.bot_stats()
    lines = [
        f"🤖 *{BOT_NAME} — Dashboard*\n",
        f"👥 Total users:      *{st['total_users']}*",
        f"🆕 New today:        *{st['new_today']}*",
        f"💎 Active subs:      *{st['active_subs']}*",
        f"⬇️ Total downloads:  *{st['total_dl']}*",
        f"📥 Downloads today:  *{st['today_dl']}*",
        f"💰 Total revenue:    *₹{st['revenue']:.0f}*",
        f"📋 Pending reports:  *{st['pending_reports']}*",
    ]
    rpts = db.get_reports(3)
    if rpts:
        lines.append("\n📋 *Latest Reports:*")
        for r in rpts:
            lines.append(f"\n`#{r['id']}` User `{r['user_id']}` — {r['rtype']}\n_{r['details'][:100]}_")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_bc")],
            [InlineKeyboardButton("📋 All Reports", callback_data="admin_reports")],
        ])
    )

@admin_only
async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/approve <user_id> <plan_key> [txn_id]`\n"
            "Keys: " + " · ".join(PLANS.keys()),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        tid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID."); return
    pk = args[1]
    if pk not in PLANS:
        await update.message.reply_text(f"❌ Unknown plan. Keys: {', '.join(PLANS.keys())}"); return
    txn  = args[2] if len(args) > 2 else f"ADMIN_{int(time.time())}"
    act  = db.activate_sub(tid, pk, txn, "admin")
    ends = act["end_date"].strftime("%d %b %Y, %I:%M %p")
    await update.message.reply_text(
        f"✅ Activated *{PLANS[pk]['name']}* for `{tid}`\nExpires: {ends}",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        await ctx.bot.send_message(
            tid,
            f"🎉 *Subscription Activated!*\n\n"
            f"📦 *{PLANS[pk]['name']}*\n📅 Until: *{ends}*\n\n"
            f"Send a TeraBox link to start downloading! 🚀",
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError: pass

@admin_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/ban <user_id> [reason]`", parse_mode=ParseMode.MARKDOWN); return
    db.ban_user(int(ctx.args[0]), " ".join(ctx.args[1:]) or "Admin action")
    await update.message.reply_text(f"🚫 Banned `{ctx.args[0]}`", parse_mode=ParseMode.MARKDOWN)
    try:
        await ctx.bot.send_message(int(ctx.args[0]),
            "🚫 Your account has been suspended. Contact admin if this is a mistake.")
    except TelegramError: pass

@admin_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/unban <user_id>`"); return
    db.unban_user(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Unbanned `{ctx.args[0]}`", parse_mode=ParseMode.MARKDOWN)

@admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/broadcast Your message`", parse_mode=ParseMode.MARKDOWN); return
    msg   = " ".join(ctx.args)
    uids  = db.get_all_user_ids()
    sent  = failed = 0
    prog  = await update.message.reply_text(f"📢 Sending to {len(uids)} users…")
    for uid in uids:
        try:
            await ctx.bot.send_message(uid, f"📢 *{BOT_NAME}*\n\n{msg}",
                                        parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.05)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError:
            failed += 1
    await prog.edit_text(f"📢 Done! ✅ {sent} sent  ❌ {failed} failed")

@admin_only
async def cmd_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/resolve <report_id>`"); return
    db.resolve_report(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Report #{ctx.args[0]} resolved.")

@admin_only
async def cmd_give(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Give a user free downloads or a plan."""
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/give <user_id> <plan_key>`", parse_mode=ParseMode.MARKDOWN); return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID."); return
    pk = ctx.args[1]
    if pk not in PLANS:
        await update.message.reply_text(f"❌ Unknown plan."); return
    act  = db.activate_sub(tid, pk, f"GIFT_{int(time.time())}", "admin_gift")
    ends = act["end_date"].strftime("%d %b %Y, %I:%M %p")
    await update.message.reply_text(
        f"🎁 Gifted *{PLANS[pk]['name']}* to `{tid}`\nExpires: {ends}",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        await ctx.bot.send_message(
            tid,
            f"🎁 *You've received a gift subscription!*\n\n"
            f"📦 *{PLANS[pk]['name']}*\n📅 Until: *{ends}*",
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError: pass

@admin_only
async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Look up any user."""
    if not ctx.args:
        await update.message.reply_text("Usage: `/lookup <user_id>`"); return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID."); return
    s   = db.full_status(tid)
    if not s:
        await update.message.reply_text("User not found."); return
    usr = s["user"]
    sub = s["sub"]
    lines = [
        f"🔍 *User Lookup: `{tid}`*\n",
        f"👤 {usr.get('full_name','?')} (@{usr.get('username','?')})",
        f"📅 Joined: {str(usr.get('first_seen',''))[:10]}",
        f"⬇️ Downloads: {usr.get('total_downloads',0)}",
        f"💳 Payments: {s['total_payments']}",
        f"🚫 Banned: {'Yes' if usr.get('is_banned') else 'No'}",
        f"⚠️ Suspicious: {'Yes' if usr.get('is_suspicious') else 'No'}",
    ]
    if sub:
        lines.append(f"💎 Active sub: {sub['plan_name']} until {sub['end_date'][:16]}")
    else:
        lines.append("💎 No active subscription")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
#   INLINE BUTTONS
# ─────────────────────────────────────────────────────────────────────────────
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = update.effective_user
    data = q.data
    await q.answer()

    if data == "show_plans":
        trial_ok = not db.has_used_trial(user.id)
        lines = ["💎 *Subscription Plans*\n"]
        for k, p in PLANS.items():
            used = " _(used)_" if k == "trial" and not trial_ok else ""
            lines.append(f"• {p['name']} — *₹{p['price']}*{used}")
        await _edit(q.message, "\n".join(lines), _plans_kb(trial_ok))

    elif data == "show_help":
        await _edit(q.message, HELP_MSG,
                    InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="back_home")]]))

    elif data == "my_status":
        s   = db.full_status(user.id)
        usr = s.get("user", {})
        lines = [
            f"📊 *Your Status*\n",
            f"🆔 ID: `{user.id}`",
            f"⬇️ Downloads: *{usr.get('total_downloads',0)}*",
            "",
            f"📦 *Subscription:*\n{_sub_line(user.id)}",
        ]
        await _edit(q.message, "\n".join(lines),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("💎 Subscribe", callback_data="show_plans"),
                         InlineKeyboardButton("🏠 Home",      callback_data="back_home")]
                    ]))

    elif data == "back_home":
        await _edit(q.message,
                    f"👋 *Welcome back, {user.first_name}!*\n\n{_sub_line(user.id)}",
                    _home_kb())

    elif data == "share_bot":
        bot_info = await ctx.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
        await _edit(q.message,
                    f"🔗 *Share your referral link:*\n\n`{link}`\n\n"
                    f"Every new user you bring in is tracked — bonuses coming soon! 🎁",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="back_home")]]))

    elif data.startswith("plan:"):
        await _begin_payment(q, user, data.split(":")[1], ctx)

    elif data.startswith("confirm_pay:"):
        await _send_qr(q, user, data.split(":")[1], ctx)

    elif data == "cancel_pay":
        _pending.pop(user.id, None)
        await _edit(q.message, "❌ Payment cancelled.\n\nUse /subscribe anytime.")

    elif data == "admin_bc":
        if user.id in ADMIN_IDS:
            await _edit(q.message, "📢 *Broadcast*\n\nUse: `/broadcast Your message`",
                        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back", callback_data="back_home")]]))

    elif data == "admin_reports":
        if user.id in ADMIN_IDS:
            rpts = db.get_reports(10)
            if not rpts:
                await _edit(q.message, "✅ No pending reports!")
                return
            lines = ["📋 *Pending Reports*\n"]
            for r in rpts:
                lines.append(f"`#{r['id']}` • `{r['user_id']}` • {r['rtype']}\n_{r['details'][:80]}_\n")
            await _edit(q.message, "\n".join(lines),
                        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back", callback_data="back_home")]]))

# ── Payment flow ──────────────────────────────────────────────────────────────
async def _begin_payment(q, user, plan_key, ctx):
    if plan_key not in PLANS:
        await _edit(q.message, "❌ Invalid plan."); return
    plan = PLANS[plan_key]

    if plan.get("one_time") and db.has_used_trial(user.id):
        await _edit(q.message,
                    "⚠️ You've already used the ₹2 Trial offer.\n\nChoose another plan:",
                    _plans_kb(trial_ok=False)); return

    sub = db.get_active_sub(user.id)
    note = ""
    if sub:
        end  = datetime.strptime(sub["end_date"], "%Y-%m-%d %H:%M:%S")
        note = f"\n\n_ℹ️ Extends from {end.strftime('%d %b')} (your current expiry)_"

    await _edit(q.message,
                f"💳 *{plan['name']}*\n\n"
                f"💰 Amount: *₹{plan['price']}*\n"
                f"📅 Duration: *{plan['days']} day(s)*"
                f"{note}\n\n"
                f"Tap *Proceed* to get the payment QR code.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Proceed — ₹{plan['price']}",
                                          callback_data=f"confirm_pay:{plan_key}")],
                    [I
