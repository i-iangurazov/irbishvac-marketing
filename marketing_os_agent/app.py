from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clients.claude import ClaudeClient
from .clients.email_client import EmailClient
from .clients.notion import NotionClient
from .clients.servicetitan import ServiceTitanClient, ServiceTitanJob
from .clients.slack import SlackClient
from .config import HealthReport, Settings
from .domain.campaign_health import CampaignHealthService
from .domain.owner_mapping import OwnerResolver
from .domain.pm_audit import PM_AUDIT_TEST_MESSAGE, PMAuditService, PMAuditSummary
from .domain.formatting import format_friday_roundup_email
from .domain.reports import ReportService, month_bounds, quarter_bounds, week_bounds
from .domain.service_titan_audit import (
    ServiceTitanAuditLoop,
    ServiceTitanAuditService,
    ServiceTitanAuditSummary,
    ServiceTitanWeeklySummary,
    ServiceTitanWeeklySummaryService,
)
from .domain.service_titan_discovery import ServiceTitanScopeDiscovery, ServiceTitanScopeDiscoverySummary
from .domain.service_titan_rules import RESULT_FAIL, RULESET_HVAC, RULESET_PLUMBING, RULESET_SALES, RuleResult, active_service_titan_rules
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


def _mask_channel(value: str) -> str:
    if not value:
        return "<missing>"
    suffix = value[-4:] if len(value) >= 4 else value
    return f"***{suffix}"


def _json_valid(raw: str, *, expected: str) -> bool:
    raw = raw.strip()
    if not raw:
        return True
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if expected == "list":
        return isinstance(parsed, list)
    if expected == "object":
        return isinstance(parsed, dict)
    return True


def _safe_json_dict(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
        self.service_titan_weekly_summary = ServiceTitanWeeklySummaryService(settings, self.db, self.slack)
        self.service_titan_scope_discovery = ServiceTitanScopeDiscovery(settings, self.service_titan)
        self.pm_audit = PMAuditService(settings, self.service_titan, self.slack)

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
        self._log_service_titan_startup_config()
        self._log_service_titan_weekly_summary_config()
        self._log_pm_audit_config()

        scheduler = Scheduler(self.settings, self.db)
        scheduler.register(ScheduledJob("monday_push", monday_8am, self.run_monday_push))
        scheduler.register(ScheduledJob("friday_roundup", friday_4pm, self.run_friday_roundup))
        scheduler.register(ScheduledJob("monthly_kickoff", first_day_9am, self.run_monthly_kickoff))
        scheduler.register(ScheduledJob("quarterly_kickoff", first_day_quarter_9am, self.run_quarterly_kickoff))
        scheduler.register(ScheduledJob("campaign_health_scan", daily_7am, self.run_campaign_health_scan))
        if self.settings.service_titan_weekly_summary_enabled:
            scheduler.register(
                ScheduledJob(
                    "servicetitan_weekly_summary",
                    self.service_titan_weekly_summary.should_run_at,
                    self.run_service_titan_weekly_summary,
                )
            )
        if self.settings.pm_audit_enabled and self.settings.pm_audit_schedule_enabled:
            scheduler.register(ScheduledJob("pm_audit", self.should_run_pm_audit_at, self.run_pm_audit_scheduled))

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
        if self.settings.pm_audit_enabled and self.settings.pm_audit_run_on_startup:
            threads.append(
                threading.Thread(
                    target=self._pm_audit_startup_loop,
                    args=(stop_event,),
                    name="pm-audit-startup",
                    daemon=True,
                )
            )
        for thread in threads:
            thread.start()
        stop_event.wait()
        http_server.shutdown()
        for thread in threads:
            thread.join(timeout=5)

    def _log_service_titan_startup_config(self) -> None:
        if not self.settings.service_titan_audit_enabled:
            logger.info("servicetitan_continuous_audit_disabled")
            return
        rules = active_service_titan_rules(self.settings)
        enabled_rulesets = sorted({rule.ruleset for rule in rules})
        channel = self.settings.slack_alert_channel_id
        logger.info(
            "servicetitan_continuous_audit_enabled",
            extra={
                "dry_run": self.settings.service_titan_audit_dry_run,
                "backfill_alerts": self.settings.service_titan_audit_backfill_alerts,
                "poll_interval_seconds": self.settings.service_titan_audit_poll_interval_seconds,
                "startup_delay_seconds": self.settings.service_titan_audit_startup_delay_seconds,
                "lookback_minutes": self.settings.service_titan_audit_lookback_minutes,
                "overlap_seconds": self.settings.service_titan_audit_overlap_seconds,
                "max_alerts_per_cycle": self.settings.service_titan_audit_max_alerts_per_cycle,
                "enabled_rulesets": enabled_rulesets,
                "disabled_rules": self.settings.service_titan_disabled_rule_ids,
                "sales_enabled": self.settings.sales_comfort_advisor_audit_enabled,
                "hvac_service_enabled": self.settings.hvac_service_audit_enabled,
                "plumbing_service_enabled": self.settings.plumbing_service_audit_enabled,
                "technician_compliance_enabled": self.settings.technician_compliance_enabled,
                "dispatcher_audit_enabled": self.settings.dispatcher_audit_enabled,
                "slack_channel_configured": bool(channel),
            },
        )

    def _log_pm_audit_config(self) -> None:
        if not self.settings.pm_audit_enabled:
            logger.info("pm_audit_skipped_disabled")
            return
        channel = self.settings.pm_audit_slack_channel_id
        if self.settings.pm_audit_schedule_enabled:
            logger.info(
                "pm_audit_scheduler_enabled",
                extra={
                    "dry_run": self.settings.pm_audit_dry_run,
                    "run_hour": self.settings.pm_audit_run_hour,
                    "run_minute": self.settings.pm_audit_run_minute,
                    "weekdays_only": self.settings.pm_audit_weekdays_only,
                    "enabled_rules": self.settings.pm_audit_enabled_rule_ids,
                    "slack_channel": channel or "<missing>",
                },
            )
        else:
            logger.info("pm_audit_scheduler_disabled", extra={"enabled": self.settings.pm_audit_enabled})
        if self.settings.pm_audit_run_on_startup:
            logger.info(
                "pm_audit_startup_run_enabled",
                extra={
                    "dry_run": self.settings.pm_audit_dry_run,
                    "enabled_rules": self.settings.pm_audit_enabled_rule_ids,
                    "slack_channel": channel or "<missing>",
                },
            )

    def _log_service_titan_weekly_summary_config(self) -> None:
        if not self.settings.service_titan_weekly_summary_enabled:
            logger.info("servicetitan_weekly_summary_disabled")
            return
        logger.info(
            "servicetitan_weekly_summary_enabled",
            extra={
                "day": self.settings.service_titan_weekly_summary_day,
                "hour": self.settings.service_titan_weekly_summary_hour,
                "lookback_days": self.settings.service_titan_weekly_summary_lookback_days,
                "dry_run": self.settings.service_titan_audit_dry_run,
                "slack_channel_configured": bool(self.settings.slack_alert_channel_id),
            },
        )

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

    def run_service_titan_scope_discovery(self) -> ServiceTitanScopeDiscoverySummary:
        return self.service_titan_scope_discovery.run_once()

    def run_service_titan_weekly_summary(self, now: datetime | None = None, *, force: bool = False) -> ServiceTitanWeeklySummary:
        return self.service_titan_weekly_summary.run_once(now, require_enabled=not force)

    def run_pm_audit_once(self, now: datetime | None = None) -> PMAuditSummary:
        return self.pm_audit.run_once(now)

    def should_run_pm_audit_at(self, now: datetime) -> bool:
        if not self.settings.pm_audit_enabled or not self.settings.pm_audit_schedule_enabled:
            return False
        if self.settings.pm_audit_weekdays_only and now.weekday() >= 5:
            return False
        return now.hour == self.settings.pm_audit_run_hour and now.minute == self.settings.pm_audit_run_minute

    def run_pm_audit_scheduled(self, now: datetime | None = None) -> PMAuditSummary | None:
        return self.run_pm_audit_automatic("scheduled", now)

    def run_pm_audit_automatic(self, trigger: str, now: datetime | None = None) -> PMAuditSummary | None:
        if not self.settings.pm_audit_enabled:
            logger.info("pm_audit_skipped_disabled", extra={"trigger": trigger})
            return None
        local_now = self._pm_audit_local_now(now)
        run_date = local_now.date().isoformat()
        marker_key = "pm_audit_auto_last_run_date"
        if self.db.get_kv(marker_key) == run_date:
            logger.info("pm_audit_skipped_already_ran_today", extra={"trigger": trigger, "date": run_date})
            return None
        self.db.set_kv(marker_key, run_date)
        logger.info(
            "pm_audit_started",
            extra={
                "trigger": trigger,
                "dry_run": self.settings.pm_audit_dry_run,
                "slack_channel": self.settings.pm_audit_slack_channel_id or "<missing>",
                "enabled_rules": self.settings.pm_audit_enabled_rule_ids,
            },
        )
        summary = self.run_pm_audit_once(local_now.astimezone(timezone.utc))
        if self.settings.pm_audit_dry_run:
            logger.info("pm_audit_skipped_dry_run", extra={"trigger": trigger, "failures": summary.fail_count})
        elif summary.fail_count == 0:
            logger.info("pm_audit_skipped_no_failures", extra={"trigger": trigger, "projects": summary.projects_evaluated})
        logger.info(
            "pm_audit_finished",
            extra={
                "trigger": trigger,
                "status": summary.status,
                "projects": summary.projects_evaluated,
                "fails": summary.fail_count,
                "skips": summary.skip_count,
                "slack_sent": summary.alerts_sent,
            },
        )
        return summary

    def _pm_audit_local_now(self, now: datetime | None = None) -> datetime:
        tz = ZoneInfo(self.settings.timezone)
        return (now or datetime.now(tz)).astimezone(tz)

    def _pm_audit_startup_loop(self, stop_event: threading.Event) -> None:
        if stop_event.wait(5):
            return
        try:
            self.run_pm_audit_automatic("startup")
        except Exception:
            logger.exception("pm_audit_startup_failed")

    def pm_audit_slack_test_text(self) -> tuple[bool, str]:
        send = self.settings.pm_audit_test_send
        channel = self.settings.pm_audit_slack_channel_id
        lines = [
            "PM Audit Slack test diagnostics",
            f"- PM_AUDIT_TEST_SEND: {send}",
            f"- PM_AUDIT_DRY_RUN: {self.settings.pm_audit_dry_run}",
            "- calls ServiceTitan: false",
            "- uses live ServiceTitan audit channel fallback: false",
            f"- PM_AUDIT_SLACK_CHANNEL_ID present: {bool(channel)}",
            f"- SLACK_BOT_TOKEN present: {bool(self.settings.slack_bot_token)}",
            "- payload:",
            PM_AUDIT_TEST_MESSAGE,
        ]
        ok = bool(self.settings.slack_bot_token and channel)
        if not send:
            lines.append("- Slack send: skipped because PM_AUDIT_TEST_SEND=false")
            lines.append("- No Slack messages were sent.")
            return ok, "\n".join(lines)
        if not self.settings.slack_bot_token or not channel:
            lines.append("- Slack send: not attempted because Slack token or PM test channel is missing")
            return False, "\n".join(lines)
        ts = self.slack.post_message(channel, PM_AUDIT_TEST_MESSAGE)
        if ts:
            lines.append(f"- Slack send: sent to PM_AUDIT_SLACK_CHANNEL_ID (ts={ts})")
            return True, "\n".join(lines)
        lines.append("- Slack send: failed. Check bot token, PM channel ID, and whether the bot is invited to the channel.")
        return False, "\n".join(lines)

    def notifications_test_text(self) -> tuple[bool, str]:
        send = self.settings.notifications_test_send
        channel = self.settings.slack_alert_channel_id or self.settings.slack_marketing_ops_channel_id
        lines = [
            "Notification diagnostics",
            f"- NOTIFICATIONS_TEST_SEND: {send}",
            f"- SERVICE_TITAN_AUDIT_DRY_RUN: {self.settings.service_titan_audit_dry_run}",
            f"- SLACK_BOT_TOKEN present: {bool(self.settings.slack_bot_token)}",
            f"- SLACK_ALERT_CHANNEL_ID present: {bool(self.settings.slack_alert_channel_id)}",
            f"- SLACK_MARKETING_OPS_CHANNEL_ID present: {bool(self.settings.slack_marketing_ops_channel_id)}",
            f"- effective Slack channel: {channel or '<missing>'}",
            "- ServiceTitan audit email alerts: not implemented",
        ]
        ok = bool(self.settings.slack_bot_token and channel)
        if self.settings.slack_bot_token:
            auth = self.slack.auth_test()
            if auth:
                lines.append(f"- Slack auth.test: ok (team={auth.get('team') or 'unknown'}, bot/user={auth.get('user') or auth.get('bot_id') or 'unknown'})")
            else:
                lines.append("- Slack auth.test: failed")
                ok = False
        else:
            lines.append("- Slack auth.test: skipped because SLACK_BOT_TOKEN is missing")

        if not send:
            lines.append("- Slack test message: skipped because NOTIFICATIONS_TEST_SEND=false")
            lines.append("- No Slack/email messages were sent.")
            return ok, "\n".join(lines)

        if not self.settings.slack_bot_token or not channel:
            lines.append("- Slack test message: not sent because Slack token or channel is missing")
            return False, "\n".join(lines)

        ts = self.slack.post_message(channel, "[TEST] Marketing OS Agent notification test. If you see this, Slack alert delivery works.")
        if ts:
            lines.append(f"- Slack test message: sent (ts={ts})")
            return ok, "\n".join(lines)
        lines.append("- Slack test message: failed. Check bot token, channel ID, and whether the bot is invited to the channel.")
        return False, "\n".join(lines)

    def email_test_text(self, recipients: list[str] | None = None) -> tuple[bool, str]:
        send = self.settings.notifications_test_send
        target_recipients = self._clean_email_recipients(recipients)
        missing = self.settings.missing_email_credentials()
        lines = [
            "Email diagnostics",
            "- Email subsystem: implemented via SMTP EmailClient",
            "- ServiceTitan audit email alerts: not implemented",
            f"- NOTIFICATIONS_TEST_SEND: {send}",
            f"- SMTP_HOST present: {bool(self.settings.smtp_host)}",
            f"- SMTP_USER present: {bool(self.settings.smtp_user)}",
            f"- SMTP_PASS present: {bool(self.settings.smtp_pass)}",
            f"- EMAIL_FROM present: {bool(self.settings.email_from)}",
            f"- recipients: {', '.join(target_recipients) if target_recipients else '<missing>'}",
        ]
        if missing:
            lines.append("- missing email config: " + ", ".join(missing))
        if not send:
            lines.append("- Email test message: skipped because NOTIFICATIONS_TEST_SEND=false")
            lines.append("- No email was sent.")
            return (not missing and bool(target_recipients)), "\n".join(lines)
        sent, sent_recipients = self.send_test_email(target_recipients)
        if sent:
            lines.append("- Email test message: sent to " + ", ".join(sent_recipients))
            return True, "\n".join(lines)
        lines.append("- Email test message: failed. Check SMTP_* and EMAIL_FROM, then inspect email_failure logs.")
        return False, "\n".join(lines)

    def service_titan_alert_test_text(self) -> tuple[bool, str]:
        send = self.settings.notifications_test_send
        channel = self.settings.slack_alert_channel_id
        job = ServiceTitanJob(
            job_id="synthetic-notification-test",
            job_number="TEST-SERVICETITAN-ALERT",
            status="Completed",
            modified_on=datetime.now(timezone.utc),
            completed_on=datetime.now(timezone.utc),
            technician_name="Test Technician",
            dispatcher_name="Test Dispatcher",
            invoice_total=0.0,
            present_fields={"status"},
        )
        result = RuleResult(
            rule_id="synthetic_servicetitan_alert_test",
            ruleset="ServiceTitan Notification Test",
            severity="test",
            title="[TEST] Synthetic ServiceTitan audit alert",
            description="Development-only test alert used to verify Slack routing and formatting.",
            status=RESULT_FAIL,
            explanation="[TEST] Synthetic ServiceTitan violation. No customer or ServiceTitan data was used.",
            recommended_action="No action required. Use this only to verify alert delivery.",
            required_fields=(),
            violation_key="servicetitan:synthetic-notification-test:no-appointment:synthetic_servicetitan_alert_test:unknown",
            metadata={},
            handbook_source="notification diagnostics",
            recommended_alert_recipient="slack audit channel",
            delivery="test",
        )
        payload = self.service_titan_audit._alert_text(job, result)
        lines = [
            "Synthetic ServiceTitan alert diagnostics",
            f"- NOTIFICATIONS_TEST_SEND: {send}",
            f"- SERVICE_TITAN_AUDIT_DRY_RUN: {self.settings.service_titan_audit_dry_run}",
            "- calls ServiceTitan: false",
            "- writes violation/dedupe records: false",
            f"- effective Slack channel: {channel or '<missing>'}",
            f"- would_send: {bool(self.settings.slack_bot_token and channel)}",
            "- alert payload:",
            payload,
        ]
        ok = bool(self.settings.slack_bot_token and channel)
        if not send:
            lines.append("- Slack send: skipped because NOTIFICATIONS_TEST_SEND=false")
            return ok, "\n".join(lines)
        if not self.settings.slack_bot_token or not channel:
            lines.append("- Slack send: not attempted because Slack token or channel is missing")
            return False, "\n".join(lines)
        ts = self.slack.post_message(channel, payload)
        if ts:
            lines.append(f"- Slack send: sent (ts={ts})")
            return True, "\n".join(lines)
        lines.append("- Slack send: failed. Check bot token, channel ID, and whether the bot is invited to the channel.")
        return False, "\n".join(lines)

    def service_titan_runtime_diagnostics_text(self) -> str:
        rules = active_service_titan_rules(self.settings)
        sales_rules = [rule for rule in rules if rule.ruleset == RULESET_SALES]
        hvac_rules = [rule for rule in rules if rule.ruleset == RULESET_HVAC]
        plumbing_rules = [rule for rule in rules if rule.ruleset == RULESET_PLUMBING]
        active_rule_ids = {rule.rule_id for rule in rules}
        checkpoint = self.db.get_kv("servicetitan_audit_last_processed")
        run_logs = self.db.get_recent_run_logs("servicetitan_audit", limit=5)
        violations = self.db.get_service_titan_violation_summary()
        disabled_raw = os.getenv("SERVICE_TITAN_DISABLED_RULE_IDS_JSON", "")
        scope_raw = os.getenv("SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON", "")
        channel = self.settings.slack_alert_channel_id or self.settings.slack_marketing_ops_channel_id

        lines = [
            "ServiceTitan runtime diagnostics",
            "- sanitized: true",
            "- customer names, addresses, phone numbers, emails, raw notes, client secrets, Slack tokens, and access tokens are not printed",
            "- runtime config:",
            f"  - SERVICE_TITAN_AUDIT_ENABLED: {self.settings.service_titan_audit_enabled}",
            f"  - SERVICE_TITAN_AUDIT_DRY_RUN: {self.settings.service_titan_audit_dry_run}",
            f"  - SERVICE_TITAN_AUDIT_BACKFILL_ALERTS: {self.settings.service_titan_audit_backfill_alerts}",
            f"  - SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE: {self.settings.service_titan_audit_ignore_checkpoint_once}",
            f"  - SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS: {self.settings.service_titan_audit_poll_interval_seconds}",
            f"  - SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS: {self.settings.service_titan_audit_startup_delay_seconds}",
            f"  - SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES: {self.settings.service_titan_audit_lookback_minutes}",
            f"  - SERVICE_TITAN_AUDIT_OVERLAP_SECONDS: {self.settings.service_titan_audit_overlap_seconds}",
            f"  - SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE: {self.settings.service_titan_audit_max_alerts_per_cycle}",
            f"  - SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED: {self.settings.service_titan_weekly_summary_enabled}",
            f"  - SERVICE_TITAN_WEEKLY_SUMMARY_DAY: {self.settings.service_titan_weekly_summary_day}",
            f"  - SERVICE_TITAN_WEEKLY_SUMMARY_HOUR: {self.settings.service_titan_weekly_summary_hour}",
            f"  - SERVICE_TITAN_WEEKLY_SUMMARY_LOOKBACK_DAYS: {self.settings.service_titan_weekly_summary_lookback_days}",
            f"  - SALES_COMFORT_ADVISOR_AUDIT_ENABLED: {self.settings.sales_comfort_advisor_audit_enabled}",
            f"  - HVAC_SERVICE_AUDIT_ENABLED: {self.settings.hvac_service_audit_enabled}",
            f"  - PLUMBING_SERVICE_AUDIT_ENABLED: {self.settings.plumbing_service_audit_enabled}",
            f"  - TECHNICIAN_COMPLIANCE_ENABLED: {self.settings.technician_compliance_enabled}",
            f"  - DISPATCHER_AUDIT_ENABLED: {self.settings.dispatcher_audit_enabled}",
            f"  - SERVICE_TITAN_DISABLED_RULE_IDS_JSON valid: {_json_valid(disabled_raw, expected='list')}",
            f"  - SERVICE_TITAN_DISABLED_RULE_IDS_JSON parsed: {json.dumps(self.settings.service_titan_disabled_rule_ids, sort_keys=True)}",
            f"  - SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON valid: {_json_valid(scope_raw, expected='object')}",
            f"  - SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON parsed: {json.dumps(self.settings.service_titan_rule_scope_config, sort_keys=True, separators=(',', ':'))}",
            f"  - SLACK_BOT_TOKEN present: {bool(self.settings.slack_bot_token)}",
            f"  - SLACK_ALERT_CHANNEL_ID: {_mask_channel(self.settings.slack_alert_channel_id)}",
            f"  - effective Slack audit channel: {_mask_channel(channel)}",
            f"  - continuous audit expected running: {self.settings.service_titan_audit_enabled and not self.settings.missing_service_titan_credentials()}",
            "- rules:",
            f"  - active ServiceTitan rules: {len(rules)}",
            f"  - active Sales rules: {', '.join(rule.rule_id for rule in sales_rules) if sales_rules else '<none>'}",
            f"  - active HVAC Service rules: {', '.join(rule.rule_id for rule in hvac_rules) if hvac_rules else '<none>'}",
            f"  - active Plumbing Service rules: {', '.join(rule.rule_id for rule in plumbing_rules) if plumbing_rules else '<none>'}",
            f"  - sales_options_fewer_than_three active: {'sales_options_fewer_than_three' in active_rule_ids}",
            f"  - sales_arrival_after_first_half active: {'sales_arrival_after_first_half' in active_rule_ids}",
            f"  - sales_photos_missing active: {'sales_photos_missing' in active_rule_ids}",
            f"  - hvac_options_fewer_than_three active: {'hvac_options_fewer_than_three' in active_rule_ids}",
            f"  - hvac_payment_missing_on_completed_job active: {'hvac_payment_missing_on_completed_job' in active_rule_ids}",
            f"  - hvac_diagnosis_form_missing active: {'hvac_diagnosis_form_missing' in active_rule_ids}",
            f"  - hvac_required_photos_missing active: {'hvac_required_photos_missing' in active_rule_ids}",
            f"  - hvac_arrival_outside_window active: {'hvac_arrival_outside_window' in active_rule_ids}",
            f"  - plumbing_options_fewer_than_three active: {'plumbing_options_fewer_than_three' in active_rule_ids}",
            f"  - plumbing_payment_missing_on_completed_job active: {'plumbing_payment_missing_on_completed_job' in active_rule_ids}",
            f"  - plumbing_diagnosis_form_missing active: {'plumbing_diagnosis_form_missing' in active_rule_ids}",
            f"  - plumbing_required_photos_missing active: {'plumbing_required_photos_missing' in active_rule_ids}",
            f"  - plumbing_arrival_outside_window active: {'plumbing_arrival_outside_window' in active_rule_ids}",
            "- checkpoint:",
            f"  - servicetitan_audit_last_processed: {checkpoint or '<none>'}",
            f"  - first-run baseline likely on next live cycle: {not checkpoint and not self.settings.service_titan_audit_dry_run and not self.settings.service_titan_audit_backfill_alerts}",
            "- recent audit cycles:",
        ]
        if run_logs:
            for row in run_logs:
                details = _safe_json_dict(row.get("details_json"))
                lines.append(
                    "  - "
                    + f"id={row.get('id')} status={row.get('status')} started_at={row.get('started_at')} completed_at={row.get('completed_at')} "
                    + f"dry_run={details.get('dry_run', '<unknown>')} jobs={details.get('jobs_seen', '<unknown>')} "
                    + f"checkpoint_ignored={details.get('checkpoint_ignored', '<unknown>')} "
                    + f"sales_fail={details.get('sales_fail', '<unknown>')} alerts_sent={details.get('alerts_sent', '<unknown>')} "
                    + f"hvac_fail={details.get('hvac_fail', '<unknown>')} "
                    + f"plumbing_fail={details.get('plumbing_fail', '<unknown>')} "
                    + f"alerts_would_send={details.get('alerts_would_send', '<unknown>')} deduped={details.get('alerts_skipped_dedupe', '<unknown>')} "
                    + f"limited={details.get('alerts_skipped_limit', '<unknown>')} failed={details.get('alerts_failed', '<unknown>')} "
                    + f"errors={details.get('errors', '<unknown>')}"
                )
        else:
            lines.append("  - <none>")

        totals = violations.get("totals", {})
        lines.extend(
            [
                "- durable violation summary:",
                f"  - total: {int(totals.get('total') or 0)}",
                f"  - open: {int(totals.get('open_count') or 0)}",
                f"  - resolved: {int(totals.get('resolved_count') or 0)}",
                f"  - alert_sent: {int(totals.get('alert_sent_count') or 0)}",
                f"  - open_unsent_retryable: {int(totals.get('open_unsent_count') or 0)}",
                "  - by rule:",
            ]
        )
        by_rule = violations.get("by_rule", [])
        if by_rule:
            for row in by_rule:
                lines.append(
                    "    - "
                    + f"{row.get('rule_id')}: total={int(row.get('total') or 0)} open={int(row.get('open_count') or 0)} "
                    + f"alert_sent={int(row.get('alert_sent_count') or 0)} open_unsent={int(row.get('open_unsent_count') or 0)} "
                    + f"last_seen_at={row.get('last_seen_at') or '<none>'}"
                )
        else:
            lines.append("    - <none>")
        lines.append("  - latest:")
        latest = violations.get("latest", [])
        if latest:
            for row in latest:
                lines.append(
                    "    - "
                    + f"job_id={row.get('service_titan_job_id')} appointment_id={row.get('appointment_id') or '<none>'} "
                    + f"rule_id={row.get('rule_id')} status={row.get('status')} "
                    + f"first_detected_at={row.get('first_detected_at')} last_seen_at={row.get('last_seen_at')} "
                    + f"alert_sent_at={row.get('alert_sent_at') or '<none>'}"
                )
        else:
            lines.append("    - <none>")
        return "\n".join(lines)

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
        cleaned_recipients = self._clean_email_recipients(recipients)
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

    def _clean_email_recipients(self, recipients: list[str] | None = None) -> list[str]:
        target_recipients = recipients or [self.settings.tim_email, self.settings.vadim_email]
        cleaned_recipients: list[str] = []
        for item in target_recipients:
            cleaned_recipients.extend(part.strip() for part in item.split(",") if part.strip())
        return list(dict.fromkeys(cleaned_recipients))

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
