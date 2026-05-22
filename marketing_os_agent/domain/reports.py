from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from ..clients.claude import ClaudeClient
from ..clients.email_client import EmailClient
from ..clients.slack import SlackClient
from ..config import Settings
from ..models import Campaign, OPEN_STATUSES, Task
from ..persistence import Persistence
from .formatting import campaign_brief_line, format_friday_roundup_email, section, task_line
from .owner_mapping import OwnerResolver


logger = logging.getLogger(__name__)

MONDAY_SECTION_TITLES = (
    "Open tasks due this week",
    "Not completed last week",
    "Moved to this week",
)

FRIDAY_ROLLOVER_SECTION = "Not completed, needs rollover"
FRIDAY_ROLLOVER_STATUSES = OPEN_STATUSES


class ReportService:
    def __init__(
        self,
        settings: Settings,
        db: Persistence,
        slack: SlackClient,
        claude: ClaudeClient,
        email: EmailClient,
        owner_resolver: OwnerResolver,
    ) -> None:
        self.settings = settings
        self.db = db
        self.slack = slack
        self.claude = claude
        self.email = email
        self.owner_resolver = owner_resolver

    def monday_push(self, tasks: list[Task], now: datetime) -> dict[str, list[Task]]:
        run_id = self.db.log_run_start("monday_push")
        try:
            start, end = week_bounds(now.date())
            grouped_sections: dict[str, dict[str, list[Task]]] = {}
            for task in tasks:
                if task.status not in OPEN_STATUSES or not task.deadline:
                    continue
                owner_sections = grouped_sections.setdefault(
                    task.owner_name,
                    {title: [] for title in MONDAY_SECTION_TITLES},
                )
                if start <= task.deadline <= end:
                    owner_sections["Open tasks due this week"].append(task)
                if task.deadline < start:
                    owner_sections["Not completed last week"].append(task)
                if _moved_to_week(task, start, end):
                    owner_sections["Moved to this week"].append(task)

            grouped = {
                owner_name: _unique_section_tasks(owner_sections)
                for owner_name, owner_sections in grouped_sections.items()
                if any(owner_sections.values())
            }

            for owner_name, owner_tasks in sorted(grouped.items()):
                owner = owner_tasks[0].owner
                slack_user_id = self.owner_resolver.resolve_slack_user(owner)
                lines = _monday_owner_lines(grouped_sections[owner_name])
                message = self.claude.draft_monday_owner_message(owner_name, lines)
                if slack_user_id:
                    self.slack.dm_user(slack_user_id, message)
                else:
                    self._alert_tim_owner_unreachable(owner_name, "Monday push")
            summary_lines = _monday_channel_summary(grouped_sections)
            self.slack.post_message(
                self.settings.slack_marketing_ops_channel_id,
                section("Monday Marketing Task Push", summary_lines),
            )
            self.db.log_run_complete(run_id, "completed", {"owners": len(grouped)})
            logger.info("scheduled_job_completed", extra={"job": "monday_push", "owners": len(grouped)})
            return dict(grouped)
        except Exception as exc:
            self.db.log_run_complete(run_id, "failed", {"error": str(exc)})
            logger.exception("scheduled_job_failed", extra={"job": "monday_push"})
            raise

    def friday_roundup(self, tasks: list[Task], now: datetime) -> str:
        run_id = self.db.log_run_start("friday_roundup")
        try:
            start, end = week_bounds(now.date())
            next_start = end + timedelta(days=1)
            next_end = next_start + timedelta(days=6)
            sections = self.build_friday_sections(tasks, start, end, next_start, next_end)
            body = self.claude.draft_friday_roundup(sections)
            self.slack.post_message(self.settings.slack_marketing_ops_channel_id, body)
            recipients = [email for email in [self.settings.tim_email, self.settings.vadim_email] if email]
            email_text, email_html = format_friday_roundup_email(sections, start, end)
            self.email.send_email("Friday Marketing Roundup", email_text, recipients, html_body=email_html)
            self.db.log_run_complete(run_id, "completed", {"sections": {k: len(v) for k, v in sections.items()}})
            logger.info("scheduled_job_completed", extra={"job": "friday_roundup"})
            return body
        except Exception as exc:
            self.db.log_run_complete(run_id, "failed", {"error": str(exc)})
            logger.exception("scheduled_job_failed", extra={"job": "friday_roundup"})
            raise

    def build_friday_sections(
        self,
        tasks: list[Task],
        week_start: date,
        week_end: date,
        next_start: date,
        next_end: date,
    ) -> dict[str, list[str]]:
        sections = {
            "Completed": [],
            "Delayed, with new deadline and reason": [],
            "Blocked": [],
            FRIDAY_ROLLOVER_SECTION: [],
            "Canceled": [],
            "Coming next week": [],
        }
        for task in tasks:
            in_week = task.deadline is not None and week_start <= task.deadline <= week_end
            if task.status == "Completed" and in_week:
                sections["Completed"].append(task_line(task))
            if task.status == "Delayed":
                reason = task.notes_issues or "reason missing"
                sections["Delayed, with new deadline and reason"].append(f"{task_line(task)} | reason: {reason}")
            if task.status == "Blocked":
                sections["Blocked"].append(f"{task_line(task)} | needs: {task.needs_from_others or 'not specified'}")
            if task.status in FRIDAY_ROLLOVER_STATUSES and task.deadline and task.deadline <= week_end:
                sections[FRIDAY_ROLLOVER_SECTION].append(_task_line_with_original_deadline(task))
            if task.status == "Canceled" and in_week:
                sections["Canceled"].append(task_line(task))
            if task.status in {"Not Started", "In Progress", "Needs Review"} and task.deadline and next_start <= task.deadline <= next_end:
                sections["Coming next week"].append(task_line(task))
        return sections

    def monthly_kickoff(self, campaigns: list[Campaign], now: datetime) -> str:
        run_id = self.db.log_run_start("monthly_kickoff")
        try:
            message = section(f"Monthly Marketing Kickoff: {now:%B %Y}", [campaign_brief_line(c) for c in campaigns])
            self.slack.post_message(self.settings.slack_marketing_ops_channel_id, message)
            self.db.log_run_complete(run_id, "completed", {"campaigns": len(campaigns)})
            logger.info("scheduled_job_completed", extra={"job": "monthly_kickoff", "campaigns": len(campaigns)})
            return message
        except Exception as exc:
            self.db.log_run_complete(run_id, "failed", {"error": str(exc)})
            logger.exception("scheduled_job_failed", extra={"job": "monthly_kickoff"})
            raise

    def quarterly_kickoff(self, campaigns: list[Campaign], now: datetime) -> str:
        run_id = self.db.log_run_start("quarterly_kickoff")
        try:
            quarter = ((now.month - 1) // 3) + 1
            message = section(f"Quarterly Marketing Kickoff: Q{quarter} {now.year}", [campaign_brief_line(c) for c in campaigns])
            self.slack.post_message(self.settings.slack_marketing_ops_channel_id, message)
            self.db.log_run_complete(run_id, "completed", {"campaigns": len(campaigns)})
            logger.info("scheduled_job_completed", extra={"job": "quarterly_kickoff", "campaigns": len(campaigns)})
            return message
        except Exception as exc:
            self.db.log_run_complete(run_id, "failed", {"error": str(exc)})
            logger.exception("scheduled_job_failed", extra={"job": "quarterly_kickoff"})
            raise

    def _alert_tim_owner_unreachable(self, owner_name: str, context: str) -> None:
        message = f"Agent cannot reach owner {owner_name} for {context}; no Slack user mapping is configured."
        if self.settings.slack_tim_user_id:
            self.slack.dm_user(self.settings.slack_tim_user_id, message)
            logger.info("tim_escalation_sent", extra={"subject": "owner_unreachable", "owner": owner_name})
        else:
            logger.warning("owner_unreachable_tim_missing", extra={"owner": owner_name, "context": context})


def week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def month_bounds(today: date) -> tuple[date, date]:
    start = date(today.year, today.month, 1)
    if today.month == 12:
        end = date(today.year, 12, 31)
    else:
        end = date(today.year, today.month + 1, 1) - timedelta(days=1)
    return start, end


def quarter_bounds(today: date) -> tuple[date, date]:
    quarter_start_month = ((today.month - 1) // 3) * 3 + 1
    start = date(today.year, quarter_start_month, 1)
    end_month = quarter_start_month + 2
    if end_month == 12:
        end = date(today.year, 12, 31)
    else:
        end = date(today.year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def select_campaigns_starting_between(campaigns: list[Campaign], start: date, end: date) -> list[Campaign]:
    return [campaign for campaign in campaigns if campaign.start_date and start <= campaign.start_date <= end]


def _moved_to_week(task: Task, start: date, end: date) -> bool:
    return bool(task.deadline and task.original_deadline and task.original_deadline < start <= task.deadline <= end)


def _task_line_with_original_deadline(task: Task) -> str:
    line = task_line(task)
    if task.original_deadline and task.deadline and task.original_deadline != task.deadline:
        line = f"{line} | original due {task.original_deadline.isoformat()}"
    return line


def _unique_section_tasks(sections: dict[str, list[Task]]) -> list[Task]:
    seen: set[str] = set()
    unique: list[Task] = []
    for title in MONDAY_SECTION_TITLES:
        for task in sections[title]:
            if task.id in seen:
                continue
            seen.add(task.id)
            unique.append(task)
    return unique


def _monday_owner_lines(sections: dict[str, list[Task]]) -> list[str]:
    lines: list[str] = []
    for title in MONDAY_SECTION_TITLES:
        lines.append(title)
        tasks = sections[title]
        lines.extend([_task_line_with_original_deadline(task) for task in tasks] or ["- None"])
    return lines


def _monday_channel_summary(grouped_sections: dict[str, dict[str, list[Task]]]) -> list[str]:
    if not grouped_sections:
        return ["- None"]

    lines: list[str] = []
    for owner_name, sections in sorted(grouped_sections.items()):
        if not any(sections.values()):
            continue
        lines.append(
            "- "
            f"{owner_name}: "
            f"{len(sections['Open tasks due this week'])} due this week, "
            f"{len(sections['Not completed last week'])} not completed last week, "
            f"{len(sections['Moved to this week'])} moved to this week"
        )

    carryover = [
        f"- {task.owner_name}: {_task_line_with_original_deadline(task).removeprefix('- ')}"
        for sections in grouped_sections.values()
        for task in sections["Not completed last week"]
    ]
    moved = [
        f"- {task.owner_name}: {_task_line_with_original_deadline(task).removeprefix('- ')}"
        for sections in grouped_sections.values()
        for task in sections["Moved to this week"]
    ]

    if carryover:
        lines.extend(["", "Not completed last week"])
        lines.extend(carryover)
    if moved:
        lines.extend(["", "Moved to this week"])
        lines.extend(moved)
    return lines or ["- None"]
