from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clients.claude import ClaudeClient
from .clients.email_client import EmailClient
from .clients.notion import NotionClient
from .clients.servicetitan import ServiceTitanClient
from .clients.slack import SlackClient
from .config import HealthReport, Settings
from .domain.campaign_health import CampaignHealthService
from .domain.owner_mapping import OwnerResolver
from .domain.formatting import format_friday_roundup_email
from .domain.reports import ReportService, month_bounds, quarter_bounds, week_bounds
from .domain.service_titan_audit import ServiceTitanAuditLoop, ServiceTitanAuditService, ServiceTitanAuditSummary
from .domain.task_processor import TaskProcessor
from .http_server import AgentHttpServer
from .models import ValidationReport
from .persistence import Persistence
from .scheduler import (
    PollingLoop,
    ScheduledJob,
    Scheduler,
    daily_7am,
    first_day_9am,
    first_day_quarter_9am,
    friday_4pm,
    monday_8am,
)


logger = logging.getLogger(__name__)


class AgentApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Persistence(settings.sqlite_path)
        self.notion = NotionClient(settings)
        self.service_titan = ServiceTitanClient(settings)
        self.slack = SlackClient(settings)
        self.claude = ClaudeClient(settings)
        self.email = EmailClient(settings)
        self.owner_resolver = OwnerResolver(settings, self.db)
        self.campaign_health = CampaignHealthService(settings, self.db, self.slack)
        self.processor = TaskProcessor(
            settings,
            self.db,
            self.notion,
            self.slack,
            self.claude,
            self.owner_resolver,
            self.campaign_health,
        )
        self.reports = ReportService(settings, self.db, self.slack, self.claude, self.email, self.owner_resolver)
        self.service_titan_audit = ServiceTitanAuditService(settings, self.db, self.service_titan, self.slack)

    def initialize_storage(self) -> None:
        self.db.initialize()
        self.owner_resolver.seed_from_config()

    def run(self, stop_event: threading.Event) -> None:
        self.initialize_storage()
        missing = self.settings.missing_runtime_credentials()
        if missing:
            logger.warning("runtime_credentials_missing", extra={"missing": missing})
        if self.settings.missing_email_credentials():
            logger.warning("email_credentials_missing", extra={"missing": self.settings.missing_email_credentials()})
        if self.settings.missing_service_titan_credentials():
            logger.warning("servicetitan_credentials_missing", extra={"missing": self.settings.missing_service_titan_credentials()})
        self._validate_timezone()

        scheduler = Scheduler(self.settings, self.db)
        scheduler.register(ScheduledJob("monday_push", monday_8am, self.run_monday_push))
        scheduler.register(ScheduledJob("friday_roundup", friday_4pm, self.run_friday_roundup))
        scheduler.register(ScheduledJob("monthly_kickoff", first_day_9am, self.run_monthly_kickoff))
        scheduler.register(ScheduledJob("quarterly_kickoff", first_day_quarter_9am, self.run_quarterly_kickoff))
        scheduler.register(ScheduledJob("campaign_health_scan", daily_7am, self.run_campaign_health_scan))

        http_server = AgentHttpServer(
            "0.0.0.0",
            self.settings.port,
            self.health_payload,
            self.ready_payload,
            self.handle_slack_webhook,
        )
        threads = [
            threading.Thread(target=http_server.serve_forever, name="http-server", daemon=True),
            threading.Thread(target=PollingLoop(self.settings, self.poll_once).run_loop, args=(stop_event,), name="notion-polling", daemon=True),
            threading.Thread(target=scheduler.run_loop, args=(stop_event,), name="scheduler", daemon=True),
        ]
        if self.settings.service_titan_audit_enabled:
            threads.append(
                threading.Thread(
                    target=ServiceTitanAuditLoop(self.settings, self.run_service_titan_audit_once).run_loop,
                    args=(stop_event,),
                    name="servicetitan-audit",
                    daemon=True,
                )
            )
        for thread in threads:
            thread.start()
        stop_event.wait()
        http_server.shutdown()
        for thread in threads:
            thread.join(timeout=5)

    def poll_once(self) -> int:
        return self.processor.poll_once()

    def rebuild_task_baseline(self) -> int:
        return self.processor.rebuild_baseline()

    def process_pending_transitions(self) -> int:
        return self.processor.process_pending_transitions()

    def repost_missing_slack_updates(self) -> int:
        return self.processor.repost_missing_slack_updates()

    def run_service_titan_audit_once(self, *, force: bool = False) -> ServiceTitanAuditSummary:
        return self.service_titan_audit.audit_once(require_enabled=not force)

    def validate_notion(self) -> ValidationReport:
        return self.notion.validate_databases()

    def seed_workbooks(self) -> list[str]:
        created = self.notion.seed_missing_workbooks()
        logger.info("workbooks_seeded", extra={"created_count": len(created)})
        return created

    def health_check(self) -> HealthReport:
        checks = {
            "sqlite": self.db.ping(),
            "timezone": self._timezone_ok(),
            "servicetitan_timezone": (not self.settings.service_titan_audit_enabled) or self._timezone_ok(self.settings.service_titan_audit_timezone),
            "notion_config": bool(self.settings.notion_api_key and self.settings.notion_tasks_database_id and self.settings.notion_marketing_calendar_database_id),
            "slack_config": bool(self.settings.slack_bot_token and self.settings.slack_marketing_ops_channel_id),
            "claude_config": bool(self.settings.anthropic_api_key),
            "email_config": not self.settings.missing_email_credentials(),
            "servicetitan_config": not self.settings.missing_service_titan_credentials(),
        }
        messages = []
        for key in self.settings.missing_runtime_credentials():
            messages.append(f"Missing {key}")
        for key in self.settings.missing_email_credentials():
            messages.append(f"Missing {key}")
        for key in self.settings.missing_service_titan_credentials():
            messages.append(f"Missing {key}")
        core_ok = checks["sqlite"] and checks["timezone"] and checks["servicetitan_timezone"] and checks["notion_config"] and checks["slack_config"] and checks["claude_config"]
        return HealthReport(ok=core_ok, checks=checks, messages=messages)

    def smoke_test_text(self) -> str:
        tasks = self.notion.query_all_tasks()
        campaigns = self.notion.query_all_campaigns()
        lines = [
            "Marketing OS smoke test",
            f"- tasks readable: {len(tasks)}",
            f"- campaigns readable: {len(campaigns)}",
        ]
        if tasks:
            task = tasks[0]
            lines.extend(
                [
                    "- first task:",
                    f"  name: {task.name}",
                    f"  owner: {task.owner_name}",
                    f"  status: {task.status}",
                    f"  deadline: {task.deadline_iso}",
                ]
            )
        if campaigns:
            campaign = campaigns[0]
            lines.extend(
                [
                    "- first campaign:",
                    f"  name: {campaign.name}",
                    f"  owner: {campaign.owner_name}",
                    f"  status: {campaign.status}",
                    f"  dates: {campaign.start_date} to {campaign.end_date}",
                    f"  planned spend: {campaign.planned_spend}",
                ]
            )
        lines.extend(
            [
                f"- slack configured: {bool(self.settings.slack_bot_token and self.settings.slack_marketing_ops_channel_id)}",
                f"- claude configured: {bool(self.settings.anthropic_api_key)}",
                f"- email configured: {not self.settings.missing_email_credentials()}",
            ]
        )
        return "\n".join(lines)

    def debug_tasks_text(self) -> str:
        tasks = self.notion.query_all_tasks()
        lines = [
            "Task debug",
            f"- notion tasks readable: {len(tasks)}",
            f"- local baseline task states: {self.db.count_task_states()}",
        ]
        for task in tasks:
            previous = self.db.get_task_state(task.id)
            previous_status = previous.get("status") if previous else "<no local baseline>"
            last_edited = task.last_edited_time.isoformat() if task.last_edited_time else "unknown"
            lines.append(
                f"- {task.name} | notion_status={task.status or '<blank>'} | "
                f"local_status={previous_status} | deadline={task.deadline_iso} | last_edited={last_edited} | id={task.id}"
            )
        return "\n".join(lines)

    def transition_counts_text(self) -> str:
        tasks = {task.id: task for task in self.notion.query_all_tasks()}
        rows = self.db.get_status_transition_counts()
        lines = [
            "Observed transition counts",
            "- Counts include only transitions observed by the service while polling or manual processing.",
            "- Rapid Done -> In Progress -> Done toggles between polls cannot be reconstructed from current Notion state.",
        ]
        if not rows:
            lines.append("- No observed transitions yet.")
            return "\n".join(lines)
        for row in rows:
            task = tasks.get(str(row["task_id"]))
            task_name = task.name if task else str(row["task_id"])
            lines.append(
                f"- {task_name} | to_status={row['to_status']} | count={row['transition_count']} | "
                f"last_processed={row['last_processed_at']}"
            )
        return "\n".join(lines)

    def claude_models_text(self) -> str:
        models = self.claude.list_models()
        lines = ["Claude models available to this API key"]
        if not models:
            lines.append("- No models returned. Check ANTHROPIC_API_KEY or Anthropic account access.")
            return "\n".join(lines)
        lines.extend(f"- {model}" for model in models)
        lines.append("Set CLAUDE_MODEL to one of the IDs above.")
        return "\n".join(lines)

    def send_test_email(self, recipients: list[str] | None = None) -> tuple[bool, list[str]]:
        target_recipients = recipients or [self.settings.tim_email, self.settings.vadim_email]
        cleaned_recipients: list[str] = []
        for item in target_recipients:
            cleaned_recipients.extend(part.strip() for part in item.split(",") if part.strip())
        cleaned_recipients = list(dict.fromkeys(cleaned_recipients))
        if not cleaned_recipients:
            logger.warning("email_test_recipient_missing")
            return False, []

        now = datetime.now(ZoneInfo(self.settings.timezone))
        week_start, week_end = week_bounds(now.date())
        next_start = week_end + timedelta(days=1)
        next_end = next_start + timedelta(days=6)
        sections = self.reports.build_friday_sections(
            self.notion.query_all_tasks(),
            week_start,
            week_end,
            next_start,
            next_end,
        )
        body, html_body = format_friday_roundup_email(sections, week_start, week_end, preview=True)
        sent = self.email.send_email("[Test] Friday Marketing Roundup Preview", body, cleaned_recipients, html_body=html_body)
        logger.info("email_test_completed", extra={"sent": sent, "recipients": cleaned_recipients})
        return sent, cleaned_recipients

    def health_payload(self) -> dict[str, object]:
        return {"ok": True, "service": "marketing-os-agent"}

    def ready_payload(self) -> dict[str, object]:
        report = self.health_check()
        return {"ok": report.ok, "checks": report.checks, "messages": report.messages}

    def handle_slack_webhook(self, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
        signature = headers.get("x-slack-signature", "")
        timestamp = headers.get("x-slack-request-timestamp", "")
        if not self.slack.verify_signature(timestamp, body, signature):
            logger.warning("slack_webhook_signature_invalid")
            return 401, {"ok": False, "error": "invalid_signature"}
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return 400, {"ok": False, "error": "invalid_json"}
        if payload.get("type") == "url_verification":
            return 200, {"challenge": payload.get("challenge")}
        logger.info("slack_webhook_received", extra={"type": payload.get("type")})
        return 200, {"ok": True}

    def run_monday_push(self, now: datetime | None = None) -> None:
        now = self._now(now)
        self.reports.monday_push(self.notion.query_all_tasks(), now)

    def run_friday_roundup(self, now: datetime | None = None) -> None:
        now = self._now(now)
        self.reports.friday_roundup(self.notion.query_all_tasks(), now)

    def run_monthly_kickoff(self, now: datetime | None = None) -> None:
        now = self._now(now)
        start, end = month_bounds(now.date())
        self.reports.monthly_kickoff(self.notion.query_campaigns_starting_between(start, end), now)

    def run_quarterly_kickoff(self, now: datetime | None = None) -> None:
        now = self._now(now)
        start, end = quarter_bounds(now.date())
        self.reports.quarterly_kickoff(self.notion.query_campaigns_starting_between(start, end), now)

    def run_campaign_health_scan(self, now: datetime | None = None) -> None:
        now = self._now(now)
        run_id = self.db.log_run_start("campaign_health_scan")
        try:
            alerts = self.campaign_health.scan(self.notion.query_all_campaigns(), self.notion.query_all_tasks(), now.date())
            self.db.log_run_complete(run_id, "completed", {"alerts": len(alerts)})
            logger.info("scheduled_job_completed", extra={"job": "campaign_health_scan", "alerts": len(alerts)})
        except Exception as exc:
            self.db.log_run_complete(run_id, "failed", {"error": str(exc)})
            logger.exception("scheduled_job_failed", extra={"job": "campaign_health_scan"})
            raise

    def _now(self, now: datetime | None) -> datetime:
        if now is not None:
            return now
        return datetime.now(ZoneInfo(self.settings.timezone))

    def _validate_timezone(self) -> None:
        if not self._timezone_ok():
            raise ValueError(f"Invalid TIMEZONE: {self.settings.timezone}")
        if self.settings.service_titan_audit_enabled and not self._timezone_ok(self.settings.service_titan_audit_timezone):
            raise ValueError(f"Invalid SERVICE_TITAN_AUDIT_TIMEZONE: {self.settings.service_titan_audit_timezone}")

    def _timezone_ok(self, timezone_name: str | None = None) -> bool:
        try:
            ZoneInfo(timezone_name or self.settings.timezone)
            return True
        except ZoneInfoNotFoundError:
            return False
