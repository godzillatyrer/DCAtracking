"""
Background Worker — runs all scanners and trackers on schedule.

This is the background worker entry point for Render deployment.
It runs APScheduler jobs for volume scanning, profile checking,
wallet tracking, exchange flow monitoring, and score recalculation.
"""

import asyncio
import logging
import signal
import sys

from backend.database import Base, engine
from backend.scheduler import setup_scheduler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def main():
    """Main worker entry point."""
    logger.info("Starting BSC Pump Scanner Worker...")

    # Create database tables if they don't exist
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables verified.")

    # Set up scheduler
    scheduler = setup_scheduler()
    scheduler.start()
    logger.info("Scheduler started with all jobs.")

    # Log registered jobs
    for job in scheduler.get_jobs():
        logger.info(f"  Job: {job.name} — interval: {job.trigger}")

    # Keep running
    stop_event = asyncio.Event()

    def shutdown(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        scheduler.shutdown(wait=False)
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Worker running. Press Ctrl+C to stop.")
    await stop_event.wait()
    logger.info("Worker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
