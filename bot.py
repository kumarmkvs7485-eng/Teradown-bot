"""
bot.py — Downloader Hut Telegram Bot
Termux-optimised | python-telegram-bot v20
"""
import os, time, logging, asyncio, logging.handlers
from collections import defaultdict
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
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
    format_size, cleanup_user_dir, install_ytdlp, get_last_debug,
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

_rate: dict[int, list[float]] = defaultdict(list)
def _limited(uid: int) -> bool:
    now = time.time()
    w = [t for t in _rate[uid] if now-t < 60]
    _rate[uid] = w
    if len(w) >= SPAM_RATE_LIMIT: return True
    _rate[uid].append(now); return False

_dl_sem   = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_pending: dict[int, dict] = {}
_active:  dict[int, bool] = {}

async def _alert(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"🔔 *Admin Alert*\n\n{text}",
                                   parse_mode=ParseMode.MARKDOWN)
        except TelegramError: pass

async def _edit(msg, text, kb=None, md=True):
    try:
        await msg.edit_text(text,
                            parse_mode=ParseMode.MARKDOWN if md else None,
                            reply_markup=kb)
    except (TelegramError, BadRequest): pass

def _sub_line(uid):
    sub = db.get_active_sub(uid)
    if sub:
        end = datetime.strptime(sub["end_date"], "%Y-%m-%d %H:%M:%S")
        td  = end - datetime.now()
        h   = int(td.total_seconds()//3600)
        m   = int((td.total_seconds()%3600)//60)
        return f"✅ *{sub['plan_name']}* — {h}h {m}m left"
    left = max(0, FREE_DAILY_LIMIT - db.get_free_today(uid))
    return f"🆓 Free: *{left}/{FREE_DAILY_LIMIT}* download today"

def _plans_kb(trial_ok=True):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{p['name']}  ₹{p['price']}", callback_data=f"plan:{k}")]
        for k, p in PLANS.items() if not (k=="trial" and not trial_ok)
    ])

def _home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Plans",   callback_data="show_plans"),
         InlineKeyboardButton("📊 Status",  callback_data="my_status")],
        [InlineKeyboardButton("❓ Help",     callback_data="show_help"),
         InlineKeyboardButton("🔗 Share",   callback_data="share_bot")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
#   COMMANDS
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    new = db.get_user(u.id) is None
    db.upsert_user(u.id, u.username, u.full_name, u.language_code or "en")
    if db.is_banned(u.id):
        await update.message.reply_text("🚫 Account suspended."); return

    # referral
    if ctx.args and ctx.args[0].startswith("ref_") and new:
        try:
            rid = int(ctx.args[0][4:])
            if rid != u.id:
                with db.get_db() as c:
                    c.execute("UPDATE users SET referral_count=referral_count+1 WHERE user_id=?", (rid,))
                try: await ctx.bot.send_message(rid, f"🎉 New referral: {u.full_name}")
                except TelegramError: pass
        except ValueError: pass

    msg = WELCOME_MSG.format(bot_name=BOT_NAME)
    if new:
        text = f"👋 *Hey {u.first_name}! Welcome to {BOT_NAME}!* 🎉\n\n{msg}"
        await _alert(ctx.bot, f"🆕 New user!\n👤 *{u.full_name}* (@{u.username})\n🆔 `{u.id}`")
    else:
        text = f"👋 *Welcome back, {u.first_name}!*\n\n{_sub_line(u.id)}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_home_kb())

async def cmd_help(update: Update, _ctx):
    await update.message.reply_text(HELP_MSG, parse_mode=ParseMode.MARKDOWN)

async def cmd_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.upsert_user(u.id, u.username, u.full_name)
    trial_ok = not db.has_used_trial(u.id)
    lines = ["💎 *Subscription Plans*\n"]
    for k, p in PLANS.items():
        used = " _(used)_" if k=="trial" and not trial_ok else ""
        lines.append(f"• {p['name']} — *₹{p['price']}*{used}")
    lines += ["","💳 Pay via UPI → send screenshot → *instant activation* ✨"]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=_plans_kb(trial_ok))

async def cmd_subscribe(u, c): await cmd_plans(u, c)

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
        "", f"📦 *Subscription:*\n{_sub_line(u.id)}",
        "", f"⬇️ Total downloads: *{usr.get('total_downloads',0)}*",
        f"💳 Payments: *{s.get('total_payments',0)}*",
    ]
    recent = s.get("recent",[])
    if recent:
        lines.append("\n📥 *Recent downloads:*")
        for r in recent:
            lines.append(f"  • `{r['filename'][:38]}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Subscribe", callback_data="show_plans"),
             InlineKeyboardButton("🏠 Home",      callback_data="back_home")],
        ]))

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _active.get(uid):
        _active[uid] = False
        await update.message.reply_text("⏹ Download cancelled.")
    elif _pending.get(uid):
        _pending.pop(uid, None)
        await update.message.reply_text("❌ Payment cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")

# ── Admin decorator ───────────────────────────────────────────────────────────
def admin_only(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("🚫 Admin only."); return
        await fn(update, ctx)
    return wrap

@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = db.bot_stats()
    lines = [
        f"🤖 *{BOT_NAME} Dashboard*\n",
        f"👥 Total users:     *{st['total_users']}*",
        f"🆕 New today:       *{st['new_today']}*",
        f"💎 Active subs:     *{st['active_subs']}*",
        f"⬇️ Total DLs:       *{st['total_dl']}*",
        f"📥 Today DLs:       *{st['today_dl']}*",
        f"💰 Revenue:         *₹{st['revenue']:.0f}*",
        f"📋 Reports:         *{st['pending_reports']}*",
    ]
    rpts = db.get_reports(3)
    if rpts:
        lines.append("\n📋 *Latest Reports:*")
        for r in rpts:
            lines.append(f"`#{r['id']}` `{r['user_id']}` — {r['rtype']}\n_{r['details'][:80]}_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Broadcast",   callback_data="admin_bc")],
            [InlineKeyboardButton("📋 Reports",     callback_data="admin_reports")],
        ]))

@admin_only
async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Test a TeraBox URL and see full debug log."""
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage: `/debug <terabox_url>`\n\nShows full download attempt log.",
            parse_mode=ParseMode.MARKDOWN)
        return
    url = args[0]
    if not is_terabox_url(url):
        await update.message.reply_text("❌ Not a TeraBox URL."); return

    msg = await update.message.reply_text("🔍 Testing download pipeline…")
    loop = asyncio.get_event_loop()

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: download_video(url, update.effective_user.id)),
            timeout=120,
        )
    except asyncio.TimeoutError:
        result = None

    debug_text = get_last_debug(update.effective_user.id)

    if result and not result.get("error"):
        # Cleanup test file
        try:
            gz = result.get("compressed_path","")
            if gz and os.path.exists(gz): os.remove(gz)
        except OSError: pass
        status = f"✅ *SUCCESS* — {result['filename']} ({format_size(result['original_size'])})"
    elif result and result.get("error") == "too_large":
        status = f"⚠️ *File too large* ({format_size(result.get('size',0))})"
    else:
        status = "❌ *FAILED* — all methods exhausted"

    # Send debug in chunks (Telegram 4096 char limit)
    full = f"{status}\n\n```\n{debug_text}\n```"
    chunks = [full[i:i+4000] for i in range(0, len(full), 4000)]
    await _edit(msg, chunks[0], md=True)
    for chunk in chunks[1:]:
        await update.message.reply_text(f"```\n{chunk}\n```", parse_mode=ParseMode.MARKDOWN)

@admin_only
async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/approve <user_id> <plan_key> [txn_id]`\nKeys: "+", ".join(PLANS.keys()),
            parse_mode=ParseMode.MARKDOWN); return
    try: tid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID."); return
    pk = args[1]
    if pk not in PLANS:
        await update.message.reply_text(f"❌ Unknown plan."); return
    txn = args[2] if len(args)>2 else f"ADMIN_{int(time.time())}"
    act = db.activate_sub(tid, pk, txn, "admin")
    ends = act["end_date"].strftime("%d %b %Y, %I:%M %p")
    await update.message.reply_text(
        f"✅ *{PLANS[pk]['name']}* activated for `{tid}`\nExpires: {ends}",
        parse_mode=ParseMode.MARKDOWN)
    try:
        await ctx.bot.send_message(tid,
            f"🎉 *Subscription Activated!*\n\n📦 *{PLANS[pk]['name']}*\n📅 Until: *{ends}*\n\n"
            f"Send a TeraBox link to download! 🚀", parse_mode=ParseMode.MARKDOWN)
    except TelegramError: pass

@admin_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/ban <user_id> [reason]`",
                                        parse_mode=ParseMode.MARKDOWN); return
    db.ban_user(int(ctx.args[0]), " ".join(ctx.args[1:]) or "Admin action")
    await update.message.reply_text(f"🚫 Banned `{ctx.args[0]}`",
                                    parse_mode=ParseMode.MARKDOWN)
    try: await ctx.bot.send_message(int(ctx.args[0]),
                                    "🚫 Account suspended. Contact admin.")
    except TelegramError: pass

@admin_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/unban <user_id>`"); return
    db.unban_user(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Unbanned `{ctx.args[0]}`",
                                    parse_mode=ParseMode.MARKDOWN)

@admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/broadcast message`",
                                        parse_mode=ParseMode.MARKDOWN); return
    msg   = " ".join(ctx.args)
    uids  = db.get_all_user_ids()
    sent  = failed = 0
    prog  = await update.message.reply_text(f"📢 Sending to {len(uids)} users…")
    for uid in uids:
        try:
            await ctx.bot.send_message(uid, f"📢 *{BOT_NAME}*\n\n{msg}",
                                        parse_mode=ParseMode.MARKDOWN)
            sent += 1; await asyncio.sleep(0.05)
        except RetryAfter as e: await asyncio.sleep(e.retry_after+1)
        except TelegramError: failed += 1
    await prog.edit_text(f"📢 Done! ✅ {sent}  ❌ {failed}")

@admin_only
async def cmd_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/resolve <id>`"); return
    db.resolve_report(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Report #{ctx.args[0]} resolved.")

@admin_only
async def cmd_give(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/give <user_id> <plan_key>`",
                                        parse_mode=ParseMode.MARKDOWN); return
    try: tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID."); return
    pk = ctx.args[1]
    if pk not in PLANS:
        await update.message.reply_text("❌ Unknown plan."); return
    act  = db.activate_sub(tid, pk, f"GIFT_{int(time.time())}", "admin_gift")
    ends = act["end_date"].strftime("%d %b %Y, %I:%M %p")
    await update.message.reply_text(
        f"🎁 Gifted *{PLANS[pk]['name']}* to `{tid}`\nUntil: {ends}",
        parse_mode=ParseMode.MARKDOWN)
    try:
        await ctx.bot.send_message(tid,
            f"🎁 *Gift Subscription!*\n📦 *{PLANS[pk]['name']}*\n📅 Until: *{ends}*",
            parse_mode=ParseMode.MARKDOWN)
    except TelegramError: pass

@admin_only
async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/lookup <user_id>`"); return
    try: tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID."); return
    s = db.full_status(tid)
    if not s:
        await update.message.reply_text("User not found."); return
    usr = s["user"]; sub = s["sub"]
    lines = [
        f"🔍 *User `{tid}`*\n",
        f"👤 {usr.get('full_name','?')} (@{usr.get('username','?')})",
        f"📅 Joined: {str(usr.get('first_seen',''))[:10]}",
        f"⬇️ Downloads: {usr.get('total_downloads',0)}",
        f"💳 Payments: {s['total_payments']}",
        f"🚫 Banned: {'Yes' if usr.get('is_banned') else 'No'}",
        f"⚠️ Flagged: {'Yes' if usr.get('is_suspicious') else 'No'}",
    ]
    if sub:
        lines.append(f"💎 Active: {sub['plan_name']} until {sub['end_date'][:16]}")
    else:
        lines.append("💎 No active subscription")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

@admin_only
async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send last 50 log lines to admin."""
    try:
        with open("logs/bot.log","r",encoding="utf-8",errors="replace") as f:
            lines = f.readlines()
        last50 = "".join(lines[-50:])
        text   = f"📋 *Last 50 log lines:*\n\n```\n{last50[-3500:]}\n```"
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Could not read logs: {e}")

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
        lines = ["💎 *Choose a Plan*\n"]
        for k, p in PLANS.items():
            used = " _(used)_" if k=="trial" and not trial_ok else ""
            lines.append(f"• {p['name']} — *₹{p['price']}*{used}")
        await _edit(q.message, "\n".join(lines), _plans_kb(trial_ok))

    elif data == "show_help":
        await _edit(q.message, HELP_MSG,
                    InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="back_home")]]))

    elif data == "my_status":
        s   = db.full_status(user.id)
        usr = s.get("user",{})
        lines = [f"📊 *Your Status*\n",
                 f"🆔 `{user.id}`",
                 f"⬇️ Downloads: *{usr.get('total_downloads',0)}*",
                 "", f"📦 *Subscription:*\n{_sub_line(user.id)}"]
        await _edit(q.message, "\n".join(lines),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("💎 Subscribe", callback_data="show_plans"),
                         InlineKeyboardButton("🏠 Home",      callback_data="back_home")]]))

    elif data == "back_home":
        await _edit(q.message,
                    f"👋 *Welcome back, {user.first_name}!*\n\n{_sub_line(user.id)}",
                    _home_kb())

    elif data == "share_bot":
        bot_info = await ctx.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
        await _edit(q.message,
                    f"🔗 *Your referral link:*\n\n`{link}`\n\n_Share with friends!_ 🎁",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="back_home")]]))

    elif data.startswith("plan:"):
        await _begin_payment(q, user, data.split(":")[1], ctx)

    elif data.startswith("confirm_pay:"):
        await _send_qr(q, user, data.split(":")[1], ctx)

    elif data == "cancel_pay":
        _pending.pop(user.id, None)
        await _edit(q.message, "❌ Payment cancelled.\n\nUse /subscribe anytime.")

    elif data == "admin_bc" and user.id in ADMIN_IDS:
        await _edit(q.message, "📢 Use: `/broadcast Your message`",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back", callback_data="back_home")]]))

    elif data == "admin_reports" and user.id in ADMIN_IDS:
        rpts = db.get_reports(10)
        if not rpts:
            await _edit(q.message, "✅ No pending reports!")
            return
        lines = ["📋 *Pending Reports*\n"]
        for r in rpts:
            lines.append(f"`#{r['id']}` `{r['user_id']}` — {r['rtype']}\n_{r['details'][:80]}_\n")
        await _edit(q.message, "\n".join(lines),
                    InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back", callback_data="back_home")]]))

# ── Payment flow ──────────────────────────────────────────────────────────────
async def _begin_payment(q, user, plan_key, ctx):
    if plan_key not in PLANS:
        await _edit(q.message, "❌ Invalid plan."); return
    plan = PLANS[plan_key]
    if plan.get("one_time") and db.has_used_trial(user.id):
        await _edit(q.message, "⚠️ Trial already used.\n\nChoose another plan:",
                    _plans_kb(trial_ok=False)); return
    sub  = db.get_active_sub(user.id)
    note = ""
    if sub:
        end  = datetime.strptime(sub["end_date"], "%Y-%m-%d %H:%M:%S")
        note = f"\n\n_ℹ️ Extends from {end.strftime('%d %b')} (current expiry)_"
    await _edit(q.message,
                f"💳 *{plan['name']}*\n\n💰 *₹{plan['price']}*\n📅 {plan['days']} day(s){note}\n\n"
                f"Tap *Proceed* to get QR code.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Proceed — ₹{plan['price']}",
                                          callback_data=f"confirm_pay:{plan_key}")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")],
                ]))

async def _send_qr(q, user, plan_key, ctx):
    plan   = PLANS[plan_key]
    amount = float(plan["price"])
    _pending[user.id] = {"plan_key": plan_key, "amount": amount,
                          "step": "awaiting_screenshot"}
    qr_path = os.path.join(QR_DIR, f"qr_{user.id}_{plan_key}.png")
    try:
        generate_upi_qr(UPI_ID, UPI_NAME, amount, plan["name"], qr_path)
    except Exception as e:
        logger.error(f"QR error: {e}")
        await _edit(q.message, "❌ QR failed. Try again."); return
    caption = (
        f"📲 *Pay ₹{amount:.0f} via UPI*\n\n"
        f"📌 UPI ID: `{UPI_ID}`\n👤 *{UPI_NAME}*\n"
        f"💰 *₹{amount:.0f}*  |  📦 *{plan['name']}*\n\n"
        f"*Send screenshot after paying.*\n"
        f"Must show: ✅ UTR/Txn ID  ✅ Amount  ✅ Date\n\n"
        f"_Activates in seconds!_ ⚡"
    )
    try:
        await ctx.bot.send_photo(user.id, photo=open(qr_path,"rb"),
                                  caption=caption, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")]]))
    finally:
        try: os.remove(qr_path)
        except OSError: pass

# ─────────────────────────────────────────────────────────────────────────────
#   PHOTO HANDLER
# ─────────────────────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.full_name)
    if db.is_banned(user.id): return
    if _limited(user.id):
        await update.message.reply_text("⏱ Too fast."); return
    pend = _pending.get(user.id)
    if not pend or pend.get("step") != "awaiting_screenshot":
        await update.message.reply_text("ℹ️ No pending payment. Use /subscribe."); return

    plan_key = pend["plan_key"]; amount = pend["amount"]; plan = PLANS[plan_key]
    proc = await update.message.reply_text("🔍 *Verifying payment…*",
                                            parse_mode=ParseMode.MARKDOWN)
    img_bytes = bytes(await (await update.message.photo[-1].get_file()).download_as_bytearray())
    img_hash  = hash_screenshot(img_bytes)
    h_exists  = db.hash_exists(img_hash)
    res = verify_payment(img_bytes, amount, img_hash, h_exists, PAYMENT_VERIFY_MODE)

    if res["approved"]:
        txn = res["transaction_id"] or f"TXN_{int(time.time())}"
        db.save_payment(user.id, plan_key, amount, txn, img_hash, "approved")
        act  = db.activate_sub(user.id, plan_key, txn)
        db.reset_failed_payment(user.id)
        _pending.pop(user.id, None)
        ends = act["end_date"].strftime("%d %b %Y, %I:%M %p")
        await _edit(proc,
            f"🎉 *Payment Verified! Plan Activated!*\n\n"
            f"📦 *{plan['name']}*  💰 ₹{amount:.0f}\n"
            f"🧾 Txn: `{txn}`\n📅 Until: *{ends}*\n\n"
            f"✅ Send a TeraBox link to download now! 🚀")
        await _alert(ctx.bot,
            f"💰 *Payment received!*\n👤 {user.full_name} (@{user.username}) `{user.id}`\n"
            f"📦 {plan['name']}  ₹{amount:.0f}\n🧾 `{txn}`\nStatus: {res['reason']}"
            + ("\n⚠️ _Manual check needed_" if res["needs_manual_review"] else ""))
        if res["needs_manual_review"]:
            db.create_report(user.id, "payment_review",
                             f"plan={plan_key} txn={txn} reason={res['reason']}")
    else:
        fc = db.inc_failed_payment(user.id)
        db.save_payment(user.id, plan_key, amount,
                        res.get("transaction_id") or "FAILED", img_hash, "rejected")
        rmap = {
            "duplicate_screenshot":      "⚠️ Screenshot already used before.",
            "low_confidence_screenshot": "⚠️ Screenshot unclear. Send a brighter/clearer one.",
        }
        rtext = rmap.get(res["reason"],
                         f"⚠️ {res['reason'].replace('_',' ').title()}")
        if "amount_mismatch" in res["reason"]: rtext = f"⚠️ {res['reason']}"
        await _edit(proc,
            f"❌ *Not Verified*\n\n{rtext}\n\n"
            f"Screenshot must show:\n✅ UTR/Txn ID  ✅ Amount ₹{amount:.0f}  ✅ Date\n\n"
            f"_Contact admin with your Txn ID for manual help._",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")]]))
        if res.get("suspicious") or fc >= SUSPICIOUS_THRESHOLD:
            db.mark_suspicious(user.id, f"failed_payment x{fc}: {res['reason']}")
            db.create_report(user.id, "suspicious_payment",
                             f"attempts={fc} reason={res['reason']}")
            await _alert(ctx.bot,
                f"🚨 *Suspicious payment!*\n👤 {user.full_name} `{user.id}`\n"
                f"📦 {plan['name']}  ₹{amount}\n❌ Attempts: {fc}\nReason: {res['reason']}")
            await ctx.bot.send_message(user.id,
                "⚠️ *Security Notice*\n\nMultiple failed payment attempts detected.\n"
                "Contact admin with your correct Transaction ID.",
                parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
#   TEXT HANDLER (links + chat)
# ─────────────────────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.full_name)
    if db.is_banned(user.id):
        await update.message.reply_text("🚫 Account suspended."); return
    if _limited(user.id):
        await update.message.reply_text("⏱ Slow down!"); return

    text = (update.message.text or "").strip()
    url  = is_terabox_url(text)

    if not url:
        greets = ("hi","hello","hey","hlo","hii","namaste","namaskar","sup","yo")
        if any(text.lower().startswith(g) for g in greets):
            await update.message.reply_text(
                f"👋 *Hey {user.first_name}!*\n\n{_sub_line(user.id)}\n\n"
                f"Paste a TeraBox link to download! 🎬",
                parse_mode=ParseMode.MARKDOWN, reply_markup=_home_kb())
        elif text.startswith("/"):
            await update.message.reply_text("❓ Unknown command. Use /help.")
        else:
            await update.message.reply_text(
                "🔗 *Send me a TeraBox video link!*\n\nExample:\n"
                "`https://www.terabox.com/s/xxxxxxx`",
                parse_mode=ParseMode.MARKDOWN)
        return

    # Access check
    sub   = db.get_active_sub(user.id)
    freed = db.get_free_today(user.id)
    if not sub and freed >= FREE_DAILY_LIMIT:
        await update.message.reply_text(
            f"🆓 *Free limit reached!*\n\n"
            f"Used your {FREE_DAILY_LIMIT} free download(s) for today.\n"
            f"⏰ Resets at *3:30 AM IST*\n\nFrom just *₹2!* 🚀",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Subscribe — from ₹2", callback_data="show_plans")]
            ])); return

    if _active.get(user.id):
        await update.message.reply_text(
            "⏳ Download already in progress.\nWait or use /cancel."); return

    # Download
    _active[user.id] = True
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    status = await update.message.reply_text("⏳ *Starting download…*",
                                              parse_mode=ParseMode.MARKDOWN)
    loop       = asyncio.get_event_loop()
    last_edit  = [0.0]; last_pct = [-1]

    def prog_cb(pct, recv, total):
        async def _do():
            now = time.time()
            if pct==last_pct[0] or now-last_edit[0]<4: return
            last_pct[0]=pct; last_edit[0]=now
            bar = "█"*(pct//10)+"░"*(10-pct//10)
            await _edit(status,
                f"⬇️ *Downloading…*\n\n`[{bar}]` {pct}%\n"
                f"📦 {format_size(recv)} / {format_size(total) if total else '?'}")
        asyncio.run_coroutine_threadsafe(_do(), loop)

    async with _dl_sem:
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: download_video(url, user.id, prog_cb)),
                timeout=300,
            )
        except asyncio.TimeoutError:
            result = None
            logger.warning(f"Download timeout for user {user.id}")
        except Exception as e:
            logger.exception(f"Download exception: {e}")
            result = None

    _active.pop(user.id, None)

    if result is None:
        debug = get_last_debug(user.id)
        await _edit(status,
            "❌ *Download failed.*\n\n"
            "Possible causes:\n"
            "• Link is private or expired\n"
            "• TeraBox servers unreachable\n"
            "• File unavailable\n\n"
            "_Try another link or contact admin._")
        # Auto-send debug to admin
        for aid in ADMIN_IDS:
            try:
                await ctx.bot.send_message(aid,
                    f"❌ *Download failed*\n👤 {user.full_name} `{user.id}`\n"
                    f"🔗 `{url[:100]}`\n\n```\n{debug[-1500:]}\n```",
                    parse_mode=ParseMode.MARKDOWN)
            except TelegramError: pass
        return

    if result.get("error") == "too_large":
        mb = result.get("size",0)/1024/1024
        await _edit(status,
            f"⚠️ *File too large* ({mb:.1f} MB)\n\nTelegram limits to 50 MB."); return

    gz_path  = result["compressed_path"]
    filename = result["filename"]
    orig_sz  = result["original_size"]
    comp_sz  = result["compressed_size"]

    dl_id = db.log_download(user.id, url, filename, gz_path, orig_sz, comp_sz)
    if not sub: db.inc_free_download(user.id)

    await _edit(status, "📦 *Preparing file…*")
    try:
        send_path = await loop.run_in_executor(None, lambda: decompress_file(gz_path))
    except Exception as e:
        logger.error(f"Decompress error: {e}")
        await _edit(status, "❌ File prep failed. Try again."); return

    await _edit(status, "📤 *Uploading…*")
    caption = (
        f"🎬 *{filename}*\n"
        f"📦 {format_size(orig_sz)}\n"
        f"🗑 _Auto-deleted in 1 hour_"
    )
    sent = False

    # Try as streamable video first
    try:
        with open(send_path,"rb") as vf:
            await update.message.reply_video(
                video=vf, caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=180, write_timeout=180,
                connect_timeout=30, filename=filename,
            )
        db.mark_sent(dl_id); sent=True
        logger.info(f"Sent as video: {filename}")
    except (TelegramError, TimedOut, BadRequest) as e:
        logger.warning(f"Video send failed ({type(e).__name__}: {e}) — trying document")

    # Fallback: send as document
    if not sent:
        try:
            with open(send_path,"rb") as vf:
                await update.message.reply_document(
                    document=vf, caption=caption+"\n_(Open with video player)_",
                    parse_mode=ParseMode.MARKDOWN,
                    read_timeout=180, write_timeout=180,
                    connect_timeout=30, filename=filename,
                )
            db.mark_sent(dl_id); sent=True
            logger.info(f"Sent as document: {filename}")
        except (TelegramError, TimedOut) as e:
            logger.error(f"Document send failed: {e}")

    if os.path.exists(send_path):
        try: os.remove(send_path)
        except OSError: pass

    if sent:
        try: await status.delete()
        except TelegramError: pass
        await _alert(ctx.bot,
            f"📥 *Download sent*\n"
            f"👤 {user.full_name} (@{user.username})\n"
            f"📁 `{filename}` ({format_size(orig_sz)})\n"
            f"💎 {'Subscribed' if sub else 'Free tier'}")
        if not sub:
            freed = db.get_free_today(user.id)
            if freed >= FREE_DAILY_LIMIT:
                await update.message.reply_text(
                    "ℹ️ *Free limit used.*\nUpgrade for unlimited — from *₹2!* 🚀",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💎 Subscribe", callback_data="show_plans")]]))
    else:
        await _edit(status,
            "❌ *Upload failed.*\n\nFile may be too large or Telegram is busy.\nTry again.")

async def err_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {ctx.error}", exc_info=ctx.error)
    if isinstance(ctx.error, RetryAfter):
        logger.warning(f"Flood wait: {ctx.error.retry_after}s")

# ─────────────────────────────────────────────────────────────────────────────
#   MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    logger.info(f"Starting {BOT_NAME}…")
    install_ytdlp()
    db.init_db()
    start_scheduler()

    app = (Application.builder().token(BOT_TOKEN)
           .connect_timeout(30).read_timeout(60)
           .write_timeout(180).pool_timeout(30).build())

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("plans",     cmd_plans))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("stats",     cmd_admin))
    app.add_handler(CommandHandler("debug",     cmd_debug))
    app.add_handler(CommandHandler("logs",      cmd_logs))
    app.add_handler(CommandHandler("approve",   cmd_approve))
    app.add_handler(CommandHandler("ban",       cmd_ban))
    app.add_handler(CommandHandler("unban",     cmd_unban))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("resolve",   cmd_resolve))
    app.add_handler(CommandHandler("give",      cmd_give))
    app.add_handler(CommandHandler("lookup",    cmd_lookup))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(err_handler)

    logger.info("Bot live ✅")
    app.run_polling(allowed_updates=["message","callback_query"],
                    drop_pending_updates=True)
    stop_scheduler()

if __name__ == "__main__":
    main()
