from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import OPEN_STATUSES, Task


REMINDER_TYPE_1H = "deadline_1h"


@dataclass(frozen=True)
class ReminderDecision:
    eligible: bool
    reason: str
    deadline_at: datetime | None = None
    reminder_at: datetime | None = None
    reminder_key: str | None = None


def reminder_decision(
    task: Task,
    now: datetime,
    timezone_name: str,
    *,
    minutes_before: int = 60,
    date_only_deadline_hour: int = 17,
) -> ReminderDecision:
    tz = ZoneInfo(timezone_name)
    now_local = _aware_in_timezone(now, tz)
    deadline_at = task_deadline_at(task, tz, date_only_deadline_hour)
    if task.status not in OPEN_STATUSES:
        return ReminderDecision(False, "closed_status", deadline_at)
    if not task.owner:
        return ReminderDecision(False, "missing_owner", deadline_at)
    if not deadline_at:
        return ReminderDecision(False, "missing_deadline")

    reminder_at = deadline_at - timedelta(minutes=max(1, minutes_before))
    reminder_key = task_reminder_key(task, deadline_at, minutes_before)
    if task.last_reminder_sent_at:
        last_sent = _aware_in_timezone(task.last_reminder_sent_at, tz)
        if last_sent >= reminder_at:
            return ReminderDecision(False, "notion_already_sent", deadline_at, reminder_at, reminder_key)
    if now_local < reminder_at:
        return ReminderDecision(False, "too_early", deadline_at, reminder_at, reminder_key)
    if now_local > deadline_at:
        return ReminderDecision(False, "past_deadline", deadline_at, reminder_at, reminder_key)
    return ReminderDecision(True, "eligible", deadline_at, reminder_at, reminder_key)


def task_deadline_at(task: Task, tz: ZoneInfo, date_only_deadline_hour: int = 17) -> datetime | None:
    if task.deadline_at:
        return _aware_in_timezone(task.deadline_at, tz)
    if not task.deadline:
        return None
    hour = min(23, max(0, date_only_deadline_hour))
    return datetime.combine(task.deadline, time(hour=hour), tzinfo=tz)


def task_reminder_key(task: Task, deadline_at: datetime, minutes_before: int) -> str:
    deadline_utc = deadline_at.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return f"task-deadline-reminder:{task.id}:{minutes_before}m:{deadline_utc}"


def reminder_message(task: Task) -> str:
    owner_name = task.owner.name if task.owner and task.owner.name else "there"
    lines = [
        f'Hey {owner_name}, quick reminder: the task "{task.name}" is due in 1 hour.',
        "Please update the status in Notion if it is done, blocked, or needs more time.",
    ]
    if task.url:
        lines.append(task.url)
    return "\n".join(lines)


def _aware_in_timezone(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)
