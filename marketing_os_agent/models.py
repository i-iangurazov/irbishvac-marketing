from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


OPEN_STATUSES = {"Not Started", "In Progress", "Blocked", "Needs Review", "Delayed"}
TERMINAL_STATUSES = {"Completed", "Canceled"}
STATUS_CHANGE_POST_STATUSES = {"Completed", "Delayed", "Blocked", "Canceled"}


def parse_notion_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def parse_notion_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Owner:
    notion_user_id: str
    name: str
    email: str = ""

    @property
    def mapping_keys(self) -> list[str]:
        keys = [self.notion_user_id, self.name, self.email]
        return [key for key in keys if key]


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    owner: Owner | None
    deadline: date | None
    original_deadline: date | None
    status: str
    priority: str
    department: str
    linked_campaign_ids: list[str] = field(default_factory=list)
    linked_campaign_names: list[str] = field(default_factory=list)
    deliverable_link: str = ""
    notes_issues: str = ""
    needs_from_others: str = ""
    created_time: datetime | None = None
    last_edited_time: datetime | None = None
    url: str = ""
    child_task_ids: list[str] = field(default_factory=list)
    dependency_task_ids: list[str] = field(default_factory=list)
    deadline_at: datetime | None = None
    last_reminder_sent_at: datetime | None = None

    @property
    def owner_name(self) -> str:
        return self.owner.name if self.owner else "Unassigned"

    @property
    def owner_notion_user_id(self) -> str:
        return self.owner.notion_user_id if self.owner else ""

    @property
    def owner_email(self) -> str:
        return self.owner.email if self.owner else ""

    @property
    def deadline_iso(self) -> str:
        return self.deadline.isoformat() if self.deadline else "No deadline"

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass(frozen=True)
class Campaign:
    id: str
    name: str
    trade: list[str]
    channel: list[str]
    start_date: date | None
    end_date: date | None
    owner: Owner | None
    status: str
    planned_spend: float | None
    expected_leads: float | None
    expected_roi: float | None
    actual_spend: float | None
    actual_leads: float | None
    actual_roi: float | None
    linked_task_ids: list[str] = field(default_factory=list)
    linked_workbook_ids: list[str] = field(default_factory=list)
    notes: str = ""
    url: str = ""

    @property
    def owner_name(self) -> str:
        return self.owner.name if self.owner else "Unassigned"

    def expected_cpl(self) -> float | None:
        if not self.planned_spend or not self.expected_leads:
            return None
        if self.expected_leads == 0:
            return None
        return self.planned_spend / self.expected_leads

    def actual_cpl(self) -> float | None:
        if not self.actual_spend or not self.actual_leads:
            return None
        if self.actual_leads == 0:
            return None
        return self.actual_spend / self.actual_leads


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def merge(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.details.update(other.details)
        self.ok = self.ok and other.ok

    def to_text(self) -> str:
        lines = ["OK" if self.ok else "NOT OK"]
        for error in self.errors:
            lines.append(f"ERROR: {error}")
        for warning in self.warnings:
            lines.append(f"WARNING: {warning}")
        return "\n".join(lines)
