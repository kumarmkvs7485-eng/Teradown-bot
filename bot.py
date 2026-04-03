"""
bot.py  —  TeraBox Downloader Bot
          Optimised for Termux / Vivo V9 (4 GB RAM)
          python-telegram-bot v20
"""
import os
import time
import logging
import asyncio
import logging.handlers
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
from telegram.error import TelegramError, RetryAfter, TimedOut

import database as db
from config import (
    BOT_TOKEN, ADMIN_IDS, UPI_ID, UPI_NAME, PLANS,
    FREE_DAILY_LIMIT, QR_DIR, SUSPICIOUS_THRESHOLD,
    SPAM_RATE_LIMIT, WELCOME_MSG, HELP_MSG,
    MAX_CONCURRENT_DOWNLOADS,
)
from downloader import (
    normalize_terabox_url, download_video,
    decompress_file, format_size,
)
from payment import generate_upi_qr, hash_screenshot, verify_payment
from scheduler import start_scheduler, stop_scheduler

# ─── Logging (file + console) ────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
log_handler = logging.handlers.RotatingFileHandler(
    "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ─── Rate limiter ─────────────────────────────────────────────────────────────
_rate_store: dict[int, list[float]] = defaultdict(list)

def _rate_limited(user_id: int) -> bool:
    now    = time.time()
    window = [t for t in _rate_store[user_id] if now - t < 60]
    _rate_store[user_id] = window
    if len(window) >= SPAM_RATE_LIMIT:
        return True
    _rate_store[user_id].append(now)
    return False

# ─── Download semaphore (memory guard) ───────────────────────────────────────
_dl_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# ─── Pending payment state ────────────────────────────────────────────────────
_pending: dict[int, dict] = {}

# ─── Helpers ─────────────────────────────────────────────────────────────────
async def _admin_alert(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"🚨 *Admin Alert*\n\n{text}",
                                   parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass

def _sub_status_text(user_id: int) -> str:
    sub = db.get_active_subscription(user_id)
    if sub:
        end       = datetime.strptime(sub["end_date"], "%Y-%m-%d %H:%M:%S")
        remaining = end - datetime.now()
        h  = int(remaining.total_seconds() // 3600)
        m  = int((remaining.total_seconds() % 3600) // 60)
        return (
            f"✅ *Active:* {sub['plan_name']}\n"
            f"⏳ Expires in: *{h}h {m}m*\n"
            f"📅 Until: {end.strftime('%d %b %Y  %I:%M %p')}"
        )
    free = db.get_free_downloads_today(user_id)
    left = max(0, FREE_DAILY_LIMIT - free)
    return (
        f"🆓 *Free Tier* — {left}/{FREE_DAILY_LIMIT} download(s) left today\n"
        f"🔄 Resets at *3:30 AM IST*"
    )

def _plans_kb(show_trial: bool = True):
    rows = []
    for key, plan in PLANS.items():
        if key == "trial" and not show_trial:
            continue
        rows.append([InlineKeyboardButton(
            f"{plan['name']}  ₹{plan['price']}", callback_data=f"plan:{key}"
        )])
    return InlineKeyboardMarkup(rows)

async def _safe_edit(msg, text: str, **kw):
    try:
        await msg.edit_text(text, **kw)
    except TelegramError:
        pass

# ─────────────────────────────────────────────────────────────────────────────
#   COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_new = db.get_user(u.id) is None
    db.upsert_user(u.id, u.username, u.full_name, u.language_code or "en")

    if db.is_banned(u.id):
        await update.message.reply_text(
            "🚫 Your account has been suspended.\n"
            "Contact admin if you believe this is a mistake."
        )
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 View Plans", callback_data="show_plans"),
         InlineKeyboardButton("📊 My Status",  callback_data="my_status")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")],
    ])

    if is_new:
        greeting = (
            f"👋 Hey *{u.first_name}*! Welcome to *TeraBox Downloader Bot*! 🎉\n\n"
            + WELCOME_MSG[WELCOME_MSG.index("\n") + 1:]  # skip first line
        )
        await update.message.reply_text(greeting, parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=kb)
        await _admin_alert(ctx.bot,
            f"🆕 New user joined!\n👤 {u.full_name} (@{u.username})\n🆔 `{u.id}`")
    else:
        await update.message.reply_text(
            f"👋 Welcome back, *{u.first_name}*!\n\n{_sub_status_text(u.id)}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )

async def cmd_help(update: Update, _ctx):
    await update.message.reply_text(HELP_MSG, parse_mode=ParseMode.MARKDOWN)

async def cmd_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.upsert_user(u.id, u.username, u.full_name)
    trial_used = db.has_used_trial(u.id)
    lines = ["💎 *Subscription Plans*\n"]
    for key, plan in PLANS.items():
        tag = " _(used)_" if key == "trial" and trial_used else ""
        lines.append(f"• {plan['name']} — *₹{plan['price']}*{tag}")
    lines += [
        "",
        "💳 Payment via UPI only.",
        "📤 After paying, send the screenshot here.",
        "Bot verifies & activates your plan automatically! ✨",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=_plans_kb(show_trial=not trial_used),
    )

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_plans(update, ctx)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.upsert_user(u.id, u.username, u.full_name)
    s = db.get_user_full_status(u.id)
    usr = s.get("user", {})
    recent = s.get("recent_downloads", [])

    lines = [
        "📊 *Your Account Status*\n",
        f"👤 *Name:*     {usr.get('full_name','N/A')}",
        f"🆔 *User ID:*  `{u.id}`",
        f"🏷️ *Username:* @{usr.get('username','N/A')}",
        f"📅 *Joined:*   {(usr.get('first_seen',''))[:10]}",
        f"⏱ *Last Active:* {(usr.get('last_active',''))[:16]}",
        "",
        "📦 *Subscription:*",
        _sub_status_text(u.id),
        "",
        f"⬇️ *Total Downloads:* {usr.get('total_downloads',0)}",
        f"💳 *Payments Made:*   {s.get('total_payments',0)}",
        f"📜 *Plans Bought:*    {s.get('total_subs',0)}",
    ]
    if recent:
        lines.append("\n📥 *Recent Downloads:*")
        for dl in recent:
            lines.append(f"  • {dl['filename'][:35]}  ({format_size(dl['original_size'])})")

    if usr.get("is_suspicious"):
        lines.append("\n⚠️ _Account flagged. Contact support._")
    if usr.get("is_banned"):
        lines.append("\n🚫 _Account suspended._")

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Subscribe", callback_data="show_plans"),
             InlineKeyboardButton("🔙 Home", callback_data="back_home")],
        ])
    )

# ─── Admin Commands ───────────────────────────────────────────────────────────
def _admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("🚫 Admin only.")
            return
        await func(update, ctx)
    return wrapper

@_admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats = db.get_bot_stats()
    reports = db.get_pending_reports(5)
    lines = [
        "🤖 *Bot Dashboard*\n",
        f"👥 Total Users:      {stats['total_users']}",
        f"💎 Active Subs:      {stats['active_subs']}",
        f"⬇️ Total Downloads:  {stats['total_downloads']}",
        f"📥 Today Downloads:  {stats['today_downloads']}",
        f"💰 Total Revenue:    ₹{stats['total_revenue']:.0f}",
        f"📋 Pending Reports:  {stats['pending_reports']}",
    ]
    if reports:
        lines.append("\n📋 *Latest Reports:*")
        for r in reports:
            lines.append(
                f"\n#{r['id']} | User `{r['user_id']}` | "
                f"{r['report_type']}\n_{r['details'][:120]}_"
            )
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        ])
    )

@_admin_only
async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/approve <user_id> <plan_key> [txn_id]`\n\n"
            "Plan keys: " + ", ".join(PLANS.keys()),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    plan_key = args[1]
    if plan_key not in PLANS:
        await update.message.reply_text(f"Unknown plan. Valid: {', '.join(PLANS.keys())}")
        return
    txn_id = args[2] if len(args) > 2 else f"ADMIN_{int(time.time())}"
    act = db.activate_subscription(target, plan_key, txn_id, activated_by="admin")
    end_str = act["end_date"].strftime("%d %b %Y, %I:%M %p")
    await update.message.reply_text(
        f"✅ Activated *{PLANS[plan_key]['name']}* for `{target}`\nExpires: {end_str}",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        await ctx.bot.send_message(
            target,
            f"🎉 *Subscription Activated!*\n\n"
            f"📦 Plan: *{PLANS[plan_key]['name']}*\n"
            f"📅 Valid Until: *{end_str}*\n\n"
            f"Send a TeraBox link to start downloading! 🚀",
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError:
        pass

@_admin_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/ban <user_id> [reason]`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    target = int(ctx.args[0])
    reason = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "Admin action"
    db.ban_user(target, reason)
    await update.message.reply_text(f"🚫 User `{target}` banned.", parse_mode=ParseMode.MARKDOWN)
    try:
        await ctx.bot.send_message(
            target,
            "🚫 Your account has been suspended for violating our Terms of Service.\n"
            "Contact admin if you believe this is a mistake.",
        )
    except TelegramError:
        pass

@_admin_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/unban <user_id>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    target = int(ctx.args[0])
    db.unban_user(target)
    await update.message.reply_text(f"✅ User `{target}` unbanned.",
                                    parse_mode=ParseMode.MARKDOWN)

@_admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/broadcast Your message here`", parse_mode=ParseMode.MARKDOWN
        )
        return
    msg    = " ".join(ctx.args)
    uids   = db.get_all_user_ids()
    sent   = 0
    failed = 0
    status = await update.message.reply_text(
        f"📢 Broadcasting to {len(uids)} users…"
    )
    for uid in uids:
        try:
            await ctx.bot.send_message(
                uid,
                f"📢 *Announcement*\n\n{msg}",
                parse_mode=ParseMode.MARKDOWN,
            )
            sent += 1
            await asyncio.sleep(0.05)  # ~20 msgs/sec to avoid flood
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramError:
            failed += 1
    await status.edit_text(
        f"📢 Broadcast done!\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )

@_admin_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_admin(update, ctx)

@_admin_only
async def cmd_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/resolve <report_id>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    db.resolve_report(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Report #{ctx.args[0]} resolved.")

# ─────────────────────────────────────────────────────────────────────────────
#   INLINE BUTTONS
# ─────────────────────────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    user = update.effective_user
    data = q.data

    if data == "show_plans":
        trial_used = db.has_used_trial(user.id)
        lines = ["💎 *Choose a Plan*\n"]
        for key, plan in PLANS.items():
            tag = " _(used)_" if key == "trial" and trial_used else ""
            lines.append(f"• {plan['name']} — *₹{plan['price']}*{tag}")
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=_plans_kb(show_trial=not trial_used),
        )

    elif data == "show_help":
        await q.edit_message_text(HELP_MSG, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("🔙 Back", callback_data="back_home")]
                                  ]))

    elif data == "my_status":
        s   = db.get_user_full_status(user.id)
        usr = s.get("user", {})
        lines = [
            "📊 *Your Status*\n",
            f"🆔 ID: `{user.id}`",
            f"⬇️ Downloads: {usr.get('total_downloads',0)}",
            "",
            "📦 *Subscription:*",
            _sub_status_text(user.id),
        ]
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Subscribe", callback_data="show_plans"),
                 InlineKeyboardButton("🔙 Back", callback_data="back_home")],
            ])
        )

    elif data == "back_home":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 View Plans", callback_data="show_plans"),
             InlineKeyboardButton("📊 My Status",  callback_data="my_status")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")],
        ])
        await q.edit_message_text(
            f"👋 Welcome back, *{user.first_name}*!\n\n{_sub_status_text(user.id)}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )

    elif data.startswith("plan:"):
        await _initiate_payment(q, user, data.split(":")[1], ctx)

    elif data.startswith("confirm_pay:"):
        await _send_payment_qr(q, user, data.split(":")[1], ctx)

    elif data == "cancel_pay":
        _pending.pop(user.id, None)
        await q.edit_message_text(
            "❌ Payment cancelled.\nUse /subscribe to try again."
        )

    elif data == "admin_broadcast":
        if user.id in ADMIN_IDS:
            await q.edit_message_text(
                "📢 *Broadcast*\n\nUse:\n`/broadcast Your message here`",
                parse_mode=ParseMode.MARKDOWN,
            )

# ─── Payment Flow ─────────────────────────────────────────────────────────────
async def _initiate_payment(q, user, plan_key: str, ctx):
    if plan_key not in PLANS:
        await q.edit_message_text("❌ Invalid plan.")
        return
    plan = PLANS[plan_key]

    if plan.get("one_time") and db.has_used_trial(user.id):
        await q.edit_message_text(
            "⚠️ You've already used the ₹2 Trial offer.\n\nChoose another plan:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_plans_kb(show_trial=False),
        )
        return

    sub = db.get_active_subscription(user.id)
    extend_note = ""
    if sub:
        end = datetime.strptime(sub["end_date"], "%Y-%m-%d %H:%M:%S")
        extend_note = (
            f"\n\n⚠️ You have an active plan until *{end.strftime('%d %b')}*.\n"
            f"This will extend from that date."
        )

    await q.edit_message_text(
        f"💳 *{plan['name']}*\n\n"
        f"💰 Amount: *₹{plan['price']}*\n"
        f"📅 Duration: *{plan['days']} day(s)*"
        f"{extend_note}\n\n"
        f"Tap *Proceed* to get your payment QR code.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Proceed — ₹{plan['price']}",
                                  callback_data=f"confirm_pay:{plan_key}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")],
        ])
    )

async def _send_payment_qr(q, user, plan_key: str, ctx):
    plan   = PLANS[plan_key]
    amount = float(plan["price"])

    _pending[user.id] = {
        "plan_key": plan_key,
        "amount":   amount,
        "step":     "awaiting_screenshot",
    }

    qr_path = os.path.join(QR_DIR, f"upi_{user.id}_{plan_key}.png")
    try:
        generate_upi_qr(UPI_ID, UPI_NAME, amount, plan["name"], qr_path)
    except Exception as e:
        logger.error(f"QR error: {e}")
        await q.edit_message_text("❌ Could not generate QR. Please try /subscribe again.")
        return

    caption = (
        f"📲 *Pay ₹{amount:.0f} via UPI*\n\n"
        f"📌 UPI ID: `{UPI_ID}`\n"
        f"👤 Name: *{UPI_NAME}*\n"
        f"💰 Amount: *₹{amount:.0f}*\n"
        f"📦 Plan: *{plan['name']}*\n\n"
        f"*After payment, send the screenshot here.*\n\n"
        f"Screenshot must clearly show:\n"
        f"  ✅ Transaction / UTR ID\n"
        f"  ✅ Amount (₹{amount:.0f})\n"
        f"  ✅ Date & Time\n\n"
        f"_Activates automatically after verification!_ 🚀"
    )

    try:
        await ctx.bot.send_photo(
            chat_id=user.id,
            photo=open(qr_path, "rb"),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")]
            ])
        )
    finally:
        try:
            os.remove(qr_path)
        except OSError:
            pass

# ─────────────────────────────────────────────────────────────────────────────
#   PHOTO HANDLER (payment screenshots)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.full_name)

    if db.is_banned(user.id):
        return
    if _rate_limited(user.id):
        await update.message.reply_text("⏱ Slow down! Wait a moment.")
        return

    pending = _pending.get(user.id)
    if not pending or pending.get("step") != "awaiting_screenshot":
        await update.message.reply_text(
            "ℹ️ To submit payment, first choose a plan with /subscribe."
        )
        return

    plan_key = pending["plan_key"]
    amount   = pending["amount"]
    plan     = PLANS[plan_key]

    proc_msg = await update.message.reply_text(
        "🔍 *Verifying your payment…* Please wait.",
        parse_mode=ParseMode.MARKDOWN,
    )

    photo_file  = await update.message.photo[-1].get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())
    img_hash    = hash_screenshot(image_bytes)
    hash_exists = db.screenshot_hash_exists(img_hash)

    result = verify_payment(image_bytes, amount, img_hash, hash_exists)

    if result["approved"]:
        txn_id = result["transaction_id"] or f"MANUAL_{int(time.time())}"
        db.save_payment(user.id, plan_key, amount, txn_id, img_hash, "approved",
                        result["ocr_info"].get("confidence", ""))
        act = db.activate_subscription(user.id, plan_key, txn_id)
        db.reset_failed_payment(user.id)
        _pending.pop(user.id, None)

        end_str = act["end_date"].strftime("%d %b %Y, %I:%M %p")
        await _safe_edit(
            proc_msg,
            f"🎉 *Payment Verified! Plan Activated!*\n\n"
            f"📦 Plan: *{plan['name']}*\n"
            f"💰 Paid: ₹{amount:.0f}\n"
            f"🧾 Txn ID: `{txn_id}`\n"
            f"📅 Valid Until: *{end_str}*\n\n"
            f"✅ Now send any TeraBox link to download! 🚀",
            parse_mode=ParseMode.MARKDOWN,
        )

        if result.get("needs_manual_review"):
            db.create_report(user.id, "payment_manual_review",
                             f"plan={plan_key} txn={txn_id} conf={result['ocr_info'].get('confidence')}")
            await _admin_alert(
                ctx.bot,
                f"⚠️ Payment needs manual check (auto-approved)\n"
                f"👤 {user.full_name} (@{user.username}) `{user.id}`\n"
                f"📦 {plan['name']}  ₹{amount}\n"
                f"🧾 Txn: `{txn_id}`\n"
                f"Reason: {result['reason']}"
            )

    else:
        failed_count = db.increment_failed_payment(user.id)
        db.save_payment(user.id, plan_key, amount,
                        result.get("transaction_id") or "FAILED",
                        img_hash, "rejected",
                        result["ocr_info"].get("confidence", ""))

        reason_msgs = {
            "duplicate_screenshot":       "⚠️ This screenshot was already submitted.",
            "low_confidence_screenshot":  "⚠️ Screenshot is unclear. Please send a cleaner, brighter screenshot.",
        }
        if "amount_mismatch" in result["reason"]:
            reason_text = f"⚠️ {result['reason']}."
        elif "suspicious_content" in result["reason"]:
            reason_text = "⚠️ Screenshot appears invalid or shows a failed transaction."
        else:
            reason_text = reason_msgs.get(result["reason"],
                                          f"⚠️ Verification failed: {result['reason']}")

        await _safe_edit(
            proc_msg,
            f"❌ *Payment Not Verified*\n\n{reason_text}\n\n"
            f"Ensure screenshot shows:\n"
            f"  ✅ Transaction ID / UTR\n"
            f"  ✅ Amount (₹{amount:.0f})\n"
            f"  ✅ Date & Time visible\n\n"
            f"_For manual help, contact admin with your Txn ID._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")]
            ])
        )

        if failed_count >= SUSPICIOUS_THRESHOLD or result.get("needs_manual_review"):
            db.mark_suspicious(user.id, f"failed_payment x{failed_count}: {result['reason']}")
            db.create_report(user.id, "suspicious_payment",
                             f"attempts={failed_count} reason={result['reason']}")
            await _admin_alert(
                ctx.bot,
                f"🚨 *Suspicious Payment Activity*\n"
                f"👤 {user.full_name} (@{user.username}) `{user.id}`\n"
                f"📦 {plan['name']}  ₹{amount}\n"
                f"❌ Failed attempts: {failed_count}\n"
                f"Reason: {result['reason']}"
            )
            await ctx.bot.send_message(
                user.id,
                "⚠️ *Security Notice*\n\n"
                "Multiple failed payment attempts detected on your account.\n\n"
                "Submitting fake or duplicate screenshots violates our Terms of Service "
                "and may result in account suspension and reporting to relevant authorities.\n\n"
                "If this was accidental, contact admin with your correct Txn ID.",
                parse_mode=ParseMode.MARKDOWN,
            )

# ─────────────────────────────────────────────────────────────────────────────
#   TEXT HANDLER (TeraBox links + greetings)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.full_name)

    if db.is_banned(user.id):
        await update.message.reply_text("🚫 Account suspended.")
        return
    if _rate_limited(user.id):
        await update.message.reply_text("⏱ You're sending messages too fast. Wait a moment.")
        return

    text = (update.message.text or "").strip()
    url  = normalize_terabox_url(text)

    if not url:
        greetings = ("hi", "hello", "hey", "hlo", "hii", "helo", "namaskar", "namaste")
        if any(text.lower().startswith(g) for g in greetings):
            await update.message.reply_text(
                f"👋 Hey *{user.first_name}*! Great to see you!\n\n"
                f"{_sub_status_text(user.id)}\n\n"
                f"Paste a TeraBox link to download a video! 🎬",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 View Plans", callback_data="show_plans"),
                     InlineKeyboardButton("📊 My Status",  callback_data="my_status")],
                ])
            )
        elif text.startswith("/"):
            await update.message.reply_text("❓ Unknown command. Use /help for available commands.")
        else:
            await update.message.reply_text(
                "🔗 Send me a *TeraBox video link* to download it!\n\n"
                "Example:\n`https://www.terabox.com/s/xxxxxxx`\n\n"
                "Use /help for more info.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── Access check ─────────────────────────────────────────────
    sub        = db.get_active_subscription(user.id)
    free_today = db.get_free_downloads_today(user.id)

    if not sub and free_today >= FREE_DAILY_LIMIT:
        await update.message.reply_text(
            "🆓 *Free limit reached for today!*\n\n"
            f"You've used your {FREE_DAILY_LIMIT} free download(s).\n"
            "Resets at *3:30 AM IST*.\n\n"
            "Upgrade for unlimited access! Plans start at just *₹2* 🚀",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Subscribe — from ₹2", callback_data="show_plans")]
            ])
        )
        return

    # ── Download ──────────────────────────────────────────────────
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    # Animated progress messages
    progress_texts = [
        "⏳ Connecting to TeraBox…",
        "📡 Fetching video info…",
        "⬇️ Downloading… 0%",
    ]
    status_msg = await update.message.reply_text(
        progress_texts[0], parse_mode=ParseMode.MARKDOWN
    )
    await asyncio.sleep(0.8)
    await _safe_edit(status_msg, progress_texts[1])

    last_pct    = [0]
    last_edited = [time.time()]

    async def update_progress(pct, received, total):
        now = time.time()
        if pct != last_pct[0] and (now - last_edited[0]) > 3:
            size_text = format_size(received)
            total_text = format_size(total) if total else "?"
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            await _safe_edit(
                status_msg,
                f"⬇️ *Downloading…*\n\n"
                f"`[{bar}]` {pct}%\n"
                f"{size_text} / {total_text}",
                parse_mode=ParseMode.MARKDOWN,
            )
            last_pct[0]    = pct
            last_edited[0] = now

    loop = asyncio.get_event_loop()

    async with _dl_semaphore:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: download_video(url, user.id,
                                       lambda p, r, t: asyncio.run_coroutine_threadsafe(
                                           update_progress(p, r, t), loop
                                       ))
            )
        except Exception as e:
            logger.error(f"Download exception: {e}")
            await _safe_edit(status_msg,
                "❌ Download failed. The link may be invalid or expired.\n"
                "Try again or use a different link.")
            return

    if not result:
        await _safe_edit(status_msg,
            "❌ Could not fetch this TeraBox video.\n"
            "Make sure the link is *public* and valid.",
            parse_mode=ParseMode.MARKDOWN)
        return

    if result.get("error") == "too_large":
        size_mb = result["size"] / 1024 / 1024
        await _safe_edit(status_msg,
            f"⚠️ File too large (*{size_mb:.1f} MB*).\n"
            "Telegram limits uploads to 50 MB.",
            parse_mode=ParseMode.MARKDOWN)
        return

    gz_path  = result["compressed_path"]
    filename = result["filename"]
    orig_sz  = result["original_size"]
    comp_sz  = result["compressed_size"]

    dl_id = db.log_download(user.id, url, filename, gz_path, orig_sz, comp_sz)

    if not sub:
        db.increment_free_download(user.id)

    # Decompress
    await _safe_edit(status_msg, "📦 *Preparing file…*", parse_mode=ParseMode.MARKDOWN)
    try:
        send_path = await loop.run_in_executor(None, lambda: decompress_file(gz_path))
    except Exception as e:
        logger.error(f"Decompress error: {e}")
        await _safe_edit(status_msg, "❌ Failed to prepare file. Please try again.")
        return

    await _safe_edit(status_msg, "📤 *Uploading to Telegram…*", parse_mode=ParseMode.MARKDOWN)

    caption = (
        f"🎬 *{filename}*\n"
        f"📦 {format_size(orig_sz)}\n"
        f"🗑 _Auto-deleted in 1 hour_"
    )

    try:
        with open(send_path, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                filename=filename,
            )
        db.mark_file_sent(dl_id)
        await status_msg.delete()
    except (TelegramError, TimedOut) as e:
        logger.warning(f"Video upload failed, trying document: {e}")
        try:
            with open(send_path, "rb") as vf:
                await update.message.reply_document(
                    document=vf,
                    caption=caption + "\n_(Sent as file — open with video player)_",
                    parse_mode=ParseMode.MARKDOWN,
                    filename=filename,
                    read_timeout=120,
                    write_timeout=120,
                )
            db.mark_file_sent(dl_id)
            await status_msg.delete()
        except TelegramError as e2:
            logger.error(f"Document upload failed: {e2}")
            await _safe_edit(status_msg,
                "❌ Upload failed. File may be too large or Telegram is slow.\n"
                "Please try again in a moment.")
    finally:
        if os.path.exists(send_path):
            os.remove(send_path)

    # Nudge free users
    if not sub:
        free_today = db.get_free_downloads_today(user.id)
        if free_today >= FREE_DAILY_LIMIT:
            await update.message.reply_text(
                "ℹ️ *Free limit used for today.*\n\n"
                "Upgrade to Premium for unlimited downloads!\n"
                "Plans start from just *₹2* 🚀",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 Subscribe — from ₹2", callback_data="show_plans")]
                ])
            )

# ─── Error Handler ────────────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {ctx.error}", exc_info=ctx.error)
    if isinstance(ctx.error, RetryAfter):
        logger.warning(f"Rate limited by Telegram for {ctx.error.retry_after}s")

# ─────────────────────────────────────────────────────────────────────────────
#   MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    db.init_db()
    start_scheduler()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(120)
        .pool_timeout(30)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("plans",      cmd_plans))
    app.add_handler(CommandHandler("subscribe",  cmd_subscribe))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("admin",      cmd_admin))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("approve",    cmd_approve))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    app.add_handler(CommandHandler("resolve",    cmd_resolve))

    # Inline buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Photos
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info("🤖 TeraBox Bot starting on Termux…")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )

    stop_scheduler()

if __name__ == "__main__":
    main()
