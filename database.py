"""
database.py — SQLite with WAL mode, full user/payment/download tracking.
"""
import sqlite3, logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from config import DATABASE_PATH, PLANS, FILE_DELETE_HOURS

logger = logging.getLogger(__name__)

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id                 INTEGER PRIMARY KEY,
            username                TEXT,
            full_name               TEXT,
            lang                    TEXT DEFAULT 'en',
            first_seen              TEXT DEFAULT (datetime('now','localtime')),
            last_active             TEXT DEFAULT (datetime('now','localtime')),
            is_banned               INTEGER DEFAULT 0,
            is_suspicious           INTEGER DEFAULT 0,
            total_downloads         INTEGER DEFAULT 0,
            free_today              INTEGER DEFAULT 0,
            free_reset_date         TEXT    DEFAULT '',
            failed_payments         INTEGER DEFAULT 0,
            referred_by             INTEGER DEFAULT 0,
            referral_count          INTEGER DEFAULT 0,
            notes                   TEXT    DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            plan_key        TEXT,
            plan_name       TEXT,
            price           REAL,
            start_date      TEXT,
            end_date        TEXT,
            is_active       INTEGER DEFAULT 1,
            txn_id          TEXT,
            activated_by    TEXT DEFAULT 'auto'
        );
        CREATE TABLE IF NOT EXISTS payments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            plan_key        TEXT,
            amount          REAL,
            txn_id          TEXT,
            img_hash        TEXT,
            submitted_at    TEXT DEFAULT (datetime('now','localtime')),
            status          TEXT DEFAULT 'pending',
            confidence      TEXT DEFAULT '',
            notes           TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS downloads (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            url             TEXT,
            filename        TEXT,
            compressed_path TEXT,
            original_size   INTEGER DEFAULT 0,
            compressed_size INTEGER DEFAULT 0,
            downloaded_at   TEXT DEFAULT (datetime('now','localtime')),
            sent_at         TEXT,
            delete_at       TEXT,
            is_deleted      INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS admin_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            rtype       TEXT,
            details     TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            resolved    INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS i1 ON subscriptions(user_id, is_active, end_date);
        CREATE INDEX IF NOT EXISTS i2 ON payments(img_hash);
        CREATE INDEX IF NOT EXISTS i3 ON downloads(delete_at, is_deleted);
        CREATE INDEX IF NOT EXISTS i4 ON admin_reports(resolved);
        """)
    logger.info("DB ready.")

# ── Users ─────────────────────────────────────────────────────────────────────
def upsert_user(uid, username=None, full_name=None, lang="en"):
    with get_db() as c:
        c.execute("""
            INSERT INTO users(user_id,username,full_name,lang) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, full_name=excluded.full_name,
              lang=excluded.lang, last_active=datetime('now','localtime')
        """, (uid, username, full_name, lang))

def get_user(uid) -> dict | None:
    with get_db() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None

def is_banned(uid) -> bool:
    with get_db() as c:
        r = c.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        return bool(r["is_banned"]) if r else False

def get_all_user_ids() -> list[int]:
    with get_db() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()]

def mark_suspicious(uid, note=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db() as c:
        c.execute("UPDATE users SET is_suspicious=1, notes=notes||? WHERE user_id=?",
                  (f"[{now}] {note}\n", uid))

def ban_user(uid, reason=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db() as c:
        c.execute("UPDATE users SET is_banned=1, notes=notes||? WHERE user_id=?",
                  (f"[BANNED {now}] {reason}\n", uid))

def unban_user(uid):
    with get_db() as c:
        c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))

def inc_failed_payment(uid) -> int:
    with get_db() as c:
        c.execute("UPDATE users SET failed_payments=failed_payments+1 WHERE user_id=?", (uid,))
        r = c.execute("SELECT failed_payments FROM users WHERE user_id=?", (uid,)).fetchone()
        return r[0] if r else 0

def reset_failed_payment(uid):
    with get_db() as c:
        c.execute("UPDATE users SET failed_payments=0 WHERE user_id=?", (uid,))

# ── Free tier ─────────────────────────────────────────────────────────────────
def get_free_today(uid) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as c:
        r = c.execute("SELECT free_today, free_reset_date FROM users WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return 0
        if r["free_reset_date"] != today:
            c.execute("UPDATE users SET free_today=0, free_reset_date=? WHERE user_id=?", (today, uid))
            return 0
        return r["free_today"]

def inc_free_download(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as c:
        c.execute("""UPDATE users SET free_today=free_today+1, free_reset_date=?,
                     total_downloads=total_downloads+1 WHERE user_id=?""", (today, uid))

def reset_all_free():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as c:
        c.execute("UPDATE users SET free_today=0, free_reset_date=?", (today,))

# ── Subscriptions ─────────────────────────────────────────────────────────────
def get_active_sub(uid) -> dict | None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as c:
        r = c.execute("""SELECT * FROM subscriptions
            WHERE user_id=? AND is_active=1 AND end_date>?
            ORDER BY end_date DESC LIMIT 1""", (uid, now)).fetchone()
        return dict(r) if r else None

def has_used_trial(uid) -> bool:
    with get_db() as c:
        return c.execute(
            "SELECT id FROM subscriptions WHERE user_id=? AND plan_key='trial'", (uid,)
        ).fetchone() is not None

def activate_sub(uid, plan_key, txn_id, by="auto") -> dict:
    plan = PLANS[plan_key]
    now  = datetime.now()
    # Extend from existing expiry if active
    with get_db() as c:
        ex = c.execute("""SELECT end_date FROM subscriptions
            WHERE user_id=? AND is_active=1 AND end_date>?
            ORDER BY end_date DESC LIMIT 1""",
            (uid, now.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    base = datetime.strptime(ex["end_date"], "%Y-%m-%d %H:%M:%S") if ex else now
    end  = base + timedelta(days=plan["days"])
    with get_db() as c:
        c.execute("""INSERT INTO subscriptions
            (user_id,plan_key,plan_name,price,start_date,end_date,txn_id,activated_by)
            VALUES(?,?,?,?,?,?,?,?)""",
            (uid, plan_key, plan["name"], plan["price"],
             now.strftime("%Y-%m-%d %H:%M:%S"),
             end.strftime("%Y-%m-%d %H:%M:%S"), txn_id, by))
        c.execute("UPDATE users SET total_downloads=total_downloads WHERE user_id=?", (uid,))
    return {"plan": plan, "end_date": end}

def deactivate_expired():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as c:
        c.execute("UPDATE subscriptions SET is_active=0 WHERE end_date<=? AND is_active=1", (now,))

# ── Payments ──────────────────────────────────────────────────────────────────
def hash_exists(h: str) -> bool:
    with get_db() as c:
        return c.execute(
            "SELECT id FROM payments WHERE img_hash=? AND status='approved'", (h,)
        ).fetchone() is not None

def save_payment(uid, plan_key, amount, txn_id, img_hash, status, confidence=""):
    with get_db() as c:
        cur = c.execute("""INSERT INTO payments
            (user_id,plan_key,amount,txn_id,img_hash,status,confidence)
            VALUES(?,?,?,?,?,?,?)""",
            (uid, plan_key, amount, txn_id, img_hash, status, confidence))
        return cur.lastrowid

# ── Downloads ─────────────────────────────────────────────────────────────────
def log_download(uid, url, filename, compressed_path, orig_size, comp_size=0) -> int:
    delete_at = (datetime.now() + timedelta(hours=FILE_DELETE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as c:
        cur = c.execute("""INSERT INTO downloads
            (user_id,url,filename,compressed_path,original_size,compressed_size,delete_at)
            VALUES(?,?,?,?,?,?,?)""",
            (uid, url, filename, compressed_path, orig_size, comp_size, delete_at))
        c.execute("UPDATE users SET total_downloads=total_downloads+1 WHERE user_id=?", (uid,))
        return cur.lastrowid

def mark_sent(dl_id):
    with get_db() as c:
        c.execute("UPDATE downloads SET sent_at=datetime('now','localtime') WHERE id=?", (dl_id,))

def get_files_to_delete() -> list[dict]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM downloads WHERE delete_at<=? AND is_deleted=0", (now,)
        ).fetchall()]

def mark_deleted(dl_id):
    with get_db() as c:
        c.execute("UPDATE downloads SET is_deleted=1 WHERE id=?", (dl_id,))

# ── Reports ───────────────────────────────────────────────────────────────────
def create_report(uid, rtype, details):
    with get_db() as c:
        c.execute("INSERT INTO admin_reports(user_id,rtype,details) VALUES(?,?,?)",
                  (uid, rtype, details))

def get_reports(limit=20) -> list[dict]:
    with get_db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM admin_reports WHERE resolved=0 ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()]

def resolve_report(rid):
    with get_db() as c:
        c.execute("UPDATE admin_reports SET resolved=1 WHERE id=?", (rid,))

# ── Full status ───────────────────────────────────────────────────────────────
def full_status(uid) -> dict:
    user = get_user(uid)
    if not user:
        return {}
    sub   = get_active_sub(uid)
    freed = get_free_today(uid)
    with get_db() as c:
        tp = c.execute("SELECT COUNT(*) FROM payments WHERE user_id=? AND status='approved'", (uid,)).fetchone()[0]
        ts = c.execute("SELECT COUNT(*) FROM subscriptions WHERE user_id=?", (uid,)).fetchone()[0]
        rd = c.execute("""SELECT filename,original_size,downloaded_at FROM downloads
                          WHERE user_id=? ORDER BY downloaded_at DESC LIMIT 5""", (uid,)).fetchall()
    return {"user": user, "sub": sub, "free_today": freed,
            "total_payments": tp, "total_subs": ts,
            "recent": [dict(r) for r in rd]}

# ── Bot-wide stats ────────────────────────────────────────────────────────────
def bot_stats() -> dict:
    with get_db() as c:
        tu  = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        as_ = c.execute("SELECT COUNT(*) FROM subscriptions WHERE is_active=1 AND end_date>datetime('now')").fetchone()[0]
        td  = c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        tod = c.execute("SELECT COUNT(*) FROM downloads WHERE date(downloaded_at)=date('now','localtime')").fetchone()[0]
        rev = c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved'").fetchone()[0]
        pr  = c.execute("SELECT COUNT(*) FROM admin_reports WHERE resolved=0").fetchone()[0]
        nu  = c.execute("SELECT COUNT(*) FROM users WHERE date(first_seen)=date('now','localtime')").fetchone()[0]
    return {"total_users": tu, "active_subs": as_, "total_dl": td,
            "today_dl": tod, "revenue": rev, "pending_reports": pr, "new_today": nu}
