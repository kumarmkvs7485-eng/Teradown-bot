"""
scheduler.py  —  Background tasks: file cleanup, free-tier reset, subscription expiry
"""
import os
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import database as db
from config import FREE_RESET_HOUR, FREE_RESET_MINUTE

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

async def delete_expired_files():
    files = db.get_files_to_delete()
    removed = 0
    for f in files:
        path = f.get("compressed_path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                logger.warning(f"Could not delete {path}: {e}")
        db.mark_file_deleted(f["id"])
    if removed:
        logger.info(f"Auto-deleted {removed} expired file(s).")

async def reset_free_downloads():
    db.reset_all_free_downloads()
    logger.info("Free daily downloads reset.")

async def expire_subscriptions():
    db.deactivate_expired_subscriptions()

def start_scheduler():
    scheduler.add_job(delete_expired_files, "interval", minutes=10, id="file_cleanup", replace_existing=True)
    scheduler.add_job(
        reset_free_downloads,
        CronTrigger(hour=FREE_RESET_HOUR, minute=FREE_RESET_MINUTE, timezone="Asia/Kolkata"),
        id="free_reset", replace_existing=True,
    )
    scheduler.add_job(expire_subscriptions, "interval", minutes=15, id="sub_expiry", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started.")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
