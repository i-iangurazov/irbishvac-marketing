from __future__ import annotations

import logging
from collections import defaultdict
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
            grouped: dict[str, list[Task]] = defaultdict(list)
            for task in tasks:
                if task.status in OPEN_STATUSES and task.deadline and start <= task.deadline <= end:
                    grouped[task.owner_name].append(task)
            for owner_name, owner_tasks in grouped.items():
                owner = owner_tasks[0].owner
                slack_user_id = self.owner_resolver.resolve_slack_user(owner)
                lines = [task_line(task) for task in owner_tasks]
                message = self.claude.draft_monday_owner_message(owner_name, lines)
                if slack_user_id:
                    self.slack.dm_user(slack_user_id, message)
                else:
                    self._alert_tim_owner_unreachable(owner_name, "Monday push")
            summary_lines = [
                f"- {owner}: {len(owner_tasks)} open task(s) due this week"
                for owner, owner_tasks in sorted(grouped.items())
            ]
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
