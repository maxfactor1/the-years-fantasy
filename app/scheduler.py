import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    from app.services.etl_service import run_hourly_refresh

    scheduler.add_job(
        run_hourly_refresh,
        trigger=IntervalTrigger(hours=1),
        id="hourly_refresh",
        name="Hourly ADP + keeper refresh",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("Scheduler started — hourly refresh job registered")


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
