import os, logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import database as db
from config import FREE_RESET_HOUR, FREE_RESET_MINUTE

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

async def _delete_files():
    for f in db.get_files_to_delete():
        p = f.get("compressed_path")
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
        # Also try uncompressed version
        if p and p.endswith(".gz"):
            raw = p[:-3]
            if os.path.exists(raw):
                try: os.remove(raw)
                except OSError: pass
        db.mark_deleted(f["id"])

async def _reset_free():
    db.reset_all_free()
    logger.info("Free downloads reset.")

async def _expire_subs():
    db.deactivate_expired()

def start_scheduler():
    scheduler.add_job(_delete_files, "interval", minutes=10, id="del", replace_existing=True)
    scheduler.add_job(_reset_free,
        CronTrigger(hour=FREE_RESET_HOUR, minute=FREE_RESET_MINUTE, timezone="Asia/Kolkata"),
        id="free_reset", replace_existing=True)
    scheduler.add_job(_expire_subs, "interval", minutes=15, id="subs", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started.")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
