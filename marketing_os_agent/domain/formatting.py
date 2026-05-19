from __future__ import annotations

from datetime import date
from typing import Iterable

from ..models import Campaign, Task


def task_line(task: Task) -> str:
    campaign = f" | Campaign: {', '.join(task.linked_campaign_names or task.linked_campaign_ids)}" if task.linked_campaign_ids else ""
    return f"- {task.name} | {task.status} | due {task.deadline_iso}{campaign}"


def status_update_text(task: Task) -> str:
    lines = [
        f"*Task:* {task.name}",
        f"*Owner:* {task.owner_name}",
        f"*New status:* {task.status}",
        f"*Deadline:* {task.deadline_iso}",
    ]
    if task.linked_campaign_names or task.linked_campaign_ids:
        lines.append(f"*Linked campaign:* {', '.join(task.linked_campaign_names or task.linked_campaign_ids)}")
    if task.status in {"Delayed", "Blocked"} and task.notes_issues:
        lines.append(f"*Reason / notes:* {task.notes_issues}")
    if task.status == "Completed" and task.deliverable_link:
        lines.append(f"*Deliverable:* {task.deliverable_link}")
    if task.url:
        lines.append(f"*Notion:* {task.url}")
    return "\n".join(lines)


def task_status_blocks(task: Task) -> list[dict[str, object]]:
    text = status_update_text(task)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def format_money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def format_cpl(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def campaign_brief_line(campaign: Campaign) -> str:
    return (
        f"- {campaign.name} | owner {campaign.owner_name} | "
        f"{_date(campaign.start_date)} to {_date(campaign.end_date)} | "
        f"channel {', '.join(campaign.channel) or 'n/a'} | "
        f"planned {format_money(campaign.planned_spend)} | "
        f"expected leads {campaign.expected_leads or 'n/a'} | "
        f"expected CPL {format_cpl(campaign.expected_cpl())} | "
        f"expected ROI {format_percent(campaign.expected_roi)}"
    )


def section(title: str, lines: Iterable[str]) -> str:
    line_list = list(lines)
    return f"{title}\n" + "\n".join(line_list or ["- None"])


def _date(value: date | None) -> str:
    return value.isoformat() if value else "n/a"

