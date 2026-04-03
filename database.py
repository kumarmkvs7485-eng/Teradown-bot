"""
database.py  —  SQLite layer with WAL mode, full user tracking,
                 subscriptions, payments, downloads, admin reports.
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from config import DATABASE_PATH, PLANS

logger = logging.getLogger(__name__)

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")   # 8 MB cache
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
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id                  INTEGER PRIMARY KEY,
            username                 TEXT,
            full_name                TEXT,
            language_code            TEXT DEFAULT 'en',
            first_seen               TEXT DEFAULT (datetime('now','localtime')),
            last_active              TEXT DEFAULT (datetime('now','localtime')),
            is_banned                INTEGER DEFAULT 0,
            is_suspicious            INTEGER DEFAULT 0,
            total_downloads          INTEGER DEFAULT 0,
            free_downloads_today     INTEGER DEFAULT 0,
            free_reset_date          TEXT    DEFAULT '',
            failed_payment_attempts  INTEGER DEFAULT 0,
            notes                    TEXT    DEFAULT ''
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
            transaction_id  TEXT,
            activated_by    TEXT DEFAULT 'auto',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS payments (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            plan_key         TEXT,
            amount           REAL,
            transaction_id   TEXT,
            screenshot_hash  TEXT,
            submitted_at     TEXT DEFAULT (datetime('now','localtime')),
            status           TEXT DEFAULT 'pending',
            verified_at      TEXT,
            confidence       TEXT DEFAULT '',
            notes            TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            url              TEXT,
            filename         TEXT,
            compressed_path  TEXT,
            original_size    INTEGER DEFAULT 0,
            compressed_size  INTEGER DEFAULT 0,
            downloaded_at    TEXT DEFAULT (datetime('now','localtime')),
            sent_at          TEXT,
            delete_at        TEXT,
            is_deleted       INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS admin_reports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            report_type  TEXT,
            details      TEXT,
            created_at   TEXT DEFAULT (datetime('now','localtime')),
            resolved     INTEGER DEFAULT 0,
            resolved_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS broadcast_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message     TEXT,
            sent_by     INTEGER,
            sent_at     TEXT DEFAULT (datetime('now','localtime')),
            recipients  INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_users_uid      ON users(user_id);
        CREATE INDEX IF NOT EXISTS idx_subs_uid       ON subscriptions(user_id);
        CREATE INDEX IF NOT EXISTS idx_subs_active    ON subscriptions(is_active, end_date);
        CREATE INDEX IF NOT EXISTS idx_pay_uid        ON payments(user_id);
        CREATE INDEX IF NOT EXISTS idx_pay_hash       ON payments(screenshot_hash);
        CREATE INDEX IF NOT EXISTS idx_dl_uid         ON downloads(user_id);
        CREATE INDEX IF NOT EXISTS idx_dl_delete      ON downloads(delete_at, is_deleted);
        CREATE INDEX IF NOT EXISTS idx_reports_unres  ON admin_reports(resolved);
        """)
    logger.info("Database initialised.")

# ─── User ─────────────────────────────────────────────────────────────────────
def upsert_user(user_id: int, username: str = None, full_name: str = None,
                language_code: str = "en"):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, full_name, language_code)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username      = excluded.username,
                full_name     = excluded.full_name,
                language_code = excluded.language_code,
                last_active   = datetime('now','localtime')
        """, (user_id, username, full_name, language_code))

def get_user(user_id: int) -> dict | None:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None

def is_banned(user_id: int) -> bool:
    with get_db() as conn:
        r = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(r["is_banned"]) if r else False

def get_all_user_ids() -> list[int]:
    with get_db() as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
        return [r["user_id"] for r in rows]

def mark_suspicious(user_id: int, note: str = ""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db() as conn:
        conn.execute("""
            UPDATE users SET is_suspicious=1, notes=notes||?
            WHERE user_id=?
        """, (f"[{now}] {note}\n", user_id))

def ban_user(user_id: int, reason: str = ""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db() as conn:
        conn.execute("""
            UPDATE users SET is_banned=1, notes=notes||?
            WHERE user_id=?
        """, (f"[BANNED {now}] {reason}\n", user_id))

def unban_user(user_id: int):
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))

def increment_failed_payment(user_id: int) -> int:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET failed_payment_attempts=failed_payment_attempts+1 WHERE user_id=?",
            (user_id,)
        )
        r = conn.execute(
            "SELECT failed_payment_attempts FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return r["failed_payment_attempts"] if r else 0

def reset_failed_payment(user_id: int):
    with get_db() as conn:
        conn.execute("UPDATE users SET failed_payment_attempts=0 WHERE user_id=?", (user_id,))

# ─── Free Tier ────────────────────────────────────────────────────────────────
def get_free_downloads_today(user_id: int) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        r = conn.execute(
            "SELECT free_downloads_today, free_reset_date FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if not r:
            return 0
        if r["free_reset_date"] != today:
            conn.execute(
                "UPDATE users SET free_downloads_today=0, free_reset_date=? WHERE user_id=?",
                (today, user_id)
            )
            return 0
        return r["free_downloads_today"]

def increment_free_download(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute("""
            UPDATE users SET
                free_downloads_today=free_downloads_today+1,
                free_reset_date=?,
                total_downloads=total_downloads+1
            WHERE user_id=?
        """, (today, user_id))

def reset_all_free_downloads():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute("UPDATE users SET free_downloads_today=0, free_reset_date=?", (today,))
    logger.info("Free download counters reset.")

# ─── Subscriptions ────────────────────────────────────────────────────────────
def get_active_subscription(user_id: int) -> dict | None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        r = conn.execute("""
            SELECT * FROM subscriptions
            WHERE user_id=? AND is_active=1 AND end_date > ?
            ORDER BY end_date DESC LIMIT 1
        """, (user_id, now)).fetchone()
        return dict(r) if r else None

def has_used_trial(user_id: int) -> bool:
    with get_db() as conn:
        r = conn.execute(
            "SELECT id FROM subscriptions WHERE user_id=? AND plan_key='trial'",
            (user_id,)
        ).fetchone()
        return r is not None

def activate_subscription(user_id: int, plan_key: str,
                           transaction_id: str, activated_by: str = "auto") -> dict:
    plan = PLANS[plan_key]
    now  = datetime.now()

    # Extend from existing end if active
    with get_db() as conn:
        existing = conn.execute("""
            SELECT end_date FROM subscriptions
            WHERE user_id=? AND is_active=1 AND end_date > ?
            ORDER BY end_date DESC LIMIT 1
        """, (user_id, now.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()

    if existing:
        base = datetime.strptime(existing["end_date"], "%Y-%m-%d %H:%M:%S")
    else:
        base = now

    end = base + timedelta(days=plan["days"])

    with get_db() as conn:
        conn.execute("""
            INSERT INTO subscriptions
            (user_id, plan_key, plan_name, price, start_date, end_date, transaction_id, activated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, plan_key, plan["name"], plan["price"],
            now.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
            transaction_id, activated_by,
        ))
    return {"plan": plan, "end_date": end}

def deactivate_expired_subscriptions():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "UPDATE subscriptions SET is_active=0 WHERE end_date <= ? AND is_active=1", (now,)
        )

# ─── Payments ─────────────────────────────────────────────────────────────────
def screenshot_hash_exists(h: str) -> bool:
    with get_db() as conn:
        r = conn.execute(
            "SELECT id FROM payments WHERE screenshot_hash=? AND status='approved'", (h,)
        ).fetchone()
        return r is not None

def save_payment(user_id: int, plan_key: str, amount: float,
                 transaction_id: str, screenshot_hash: str,
                 status: str = "pending", confidence: str = "") -> int:
    with get_db() as conn:
        c = conn.execute("""
            INSERT INTO payments
            (user_id, plan_key, amount, transaction_id, screenshot_hash, status, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, plan_key, amount, transaction_id, screenshot_hash, status, confidence))
        return c.lastrowid

def update_payment_status(payment_id: int, status: str, notes: str = ""):
    with get_db() as conn:
        conn.execute("""
            UPDATE payments SET status=?, verified_at=datetime('now','localtime'), notes=?
            WHERE id=?
        """, (status, notes, payment_id))

# ─── Downloads ────────────────────────────────────────────────────────────────
def log_download(user_id: int, url: str, filename: str,
                 compressed_path: str, original_size: int,
                 compressed_size: int = 0) -> int:
    from config import FILE_DELETE_HOURS
    delete_at = (datetime.now() + timedelta(hours=FILE_DELETE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        c = conn.execute("""
            INSERT INTO downloads
            (user_id, url, filename, compressed_path, original_size, compressed_size, delete_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, url, filename, compressed_path, original_size, compressed_size, delete_at))
        conn.execute(
            "UPDATE users SET total_downloads=total_downloads+1 WHERE user_id=?", (user_id,)
        )
        return c.lastrowid

def mark_file_sent(dl_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE downloads SET sent_at=datetime('now','localtime') WHERE id=?", (dl_id,)
        )

def get_files_to_delete() -> list[dict]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE delete_at <= ? AND is_deleted=0", (now,)
        ).fetchall()
        return [dict(r) for r in rows]

def mark_file_deleted(dl_id: int):
    with get_db() as conn:
        conn.execute("UPDATE downloads SET is_deleted=1 WHERE id=?", (dl_id,))

# ─── Admin Reports ────────────────────────────────────────────────────────────
def create_report(user_id: int, report_type: str, details: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admin_reports (user_id, report_type, details) VALUES (?, ?, ?)",
            (user_id, report_type, details)
        )

def get_pending_reports(limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM admin_reports WHERE resolved=0 ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

def resolve_report(report_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE admin_reports SET resolved=1, resolved_at=datetime('now','localtime') WHERE id=?",
            (report_id,)
        )

# ─── Full User Status ─────────────────────────────────────────────────────────
def get_user_full_status(user_id: int) -> dict:
    user = get_user(user_id)
    if not user:
        return {}
    sub   = get_active_subscription(user_id)
    freed = get_free_downloads_today(user_id)
    with get_db() as conn:
        total_pay = conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE user_id=? AND status='approved'",
            (user_id,)
        ).fetchone()["c"]
        total_subs = conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE user_id=?", (user_id,)
        ).fetchone()["c"]
        recent_dl = conn.execute("""
            SELECT filename, original_size, downloaded_at FROM downloads
            WHERE user_id=? ORDER BY downloaded_at DESC LIMIT 5
        """, (user_id,)).fetchall()
    return {
        "user":         user,
        "subscription": sub,
        "free_today":   freed,
        "total_payments": total_pay,
        "total_subs":   total_subs,
        "recent_downloads": [dict(r) for r in recent_dl],
    }

# ─── Bot Stats (admin) ────────────────────────────────────────────────────────
def get_bot_stats() -> dict:
    with get_db() as conn:
        total_users  = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        active_subs  = conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE is_active=1 AND end_date > datetime('now')"
        ).fetchone()["c"]
        total_dl     = conn.execute("SELECT COUNT(*) AS c FROM downloads").fetchone()["c"]
        today_dl     = conn.execute(
            "SELECT COUNT(*) AS c FROM downloads WHERE date(downloaded_at)=date('now','localtime')"
        ).fetchone()["c"]
        total_rev    = conn.execute(
            "SELECT SUM(amount) AS s FROM payments WHERE status='approved'"
        ).fetchone()["s"] or 0
        pending_rep  = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_reports WHERE resolved=0"
        ).fetchone()["c"]
    return {
        "total_users":    total_users,
        "active_subs":    active_subs,
        "total_downloads": total_dl,
        "today_downloads": today_dl,
        "total_revenue":   total_rev,
        "pending_reports": pending_rep,
    }
