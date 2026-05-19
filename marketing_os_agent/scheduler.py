from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .persistence import Persistence


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    should_run: object
    run: object


class Scheduler:
    def __init__(self, settings: Settings, db: Persistence) -> None:
        self.settings = settings
        self.db = db
        self.tz = ZoneInfo(settings.timezone)
        self.jobs: list[ScheduledJob] = []

    def register(self, job: ScheduledJob) -> None:
        self.jobs.append(job)

    def run_loop(self, stop_event: threading.Event) -> None:
        logger.info("scheduler_started", extra={"timezone": self.settings.timezone, "jobs": [job.name for job in self.jobs]})
        while not stop_event.is_set():
            now = datetime.now(self.tz)
            for job in self.jobs:
                try:
                    if job.should_run(now) and self._mark_run_once(job.name, now):
                        logger.info("scheduled_job_started", extra={"job": job.name})
                        job.run(now)
                except Exception:
                    logger.exception("scheduled_job_unhandled_failure", extra={"job": job.name})
            stop_event.wait(60)
        logger.info("scheduler_stopped")

    def _mark_run_once(self, job_name: str, now: datetime) -> bool:
        key = f"schedule:{job_name}:{now:%Y-%m-%d-%H-%M}"
        return self.db.mark_dedupe(key, "scheduled_job")


def monday_8am(now: datetime) -> bool:
    return now.weekday() == 0 and now.hour == 8 and now.minute == 0


def friday_4pm(now: datetime) -> bool:
    return now.weekday() == 4 and now.hour == 16 and now.minute == 0


def first_day_9am(now: datetime) -> bool:
    return now.day == 1 and now.hour == 9 and now.minute == 0


def first_day_quarter_9am(now: datetime) -> bool:
    return now.month in {1, 4, 7, 10} and first_day_9am(now)


def daily_7am(now: datetime) -> bool:
    return now.hour == 7 and now.minute == 0


class PollingLoop:
    def __init__(self, settings: Settings, poll_once: object) -> None:
        self.settings = settings
        self.poll_once = poll_once

    def run_loop(self, stop_event: threading.Event) -> None:
        logger.info("notion_polling_started", extra={"interval_seconds": self.settings.poll_interval_seconds})
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                logger.exception("notion_polling_cycle_failed")
            elapsed = time.monotonic() - started
            wait_seconds = max(1, self.settings.poll_interval_seconds - elapsed)
            stop_event.wait(wait_seconds)
        logger.info("notion_polling_stopped")

