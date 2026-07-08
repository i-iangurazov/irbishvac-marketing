from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..clients.servicetitan import ServiceTitanApiError, ServiceTitanClient, ServiceTitanJob
from ..clients.slack import SlackClient
from ..config import Settings
from ..persistence import Persistence
from .pm_audit import (
    _deposit_paid_amount as _pm_deposit_paid_amount,
    _invoice_has_deposit_line as _pm_invoice_has_deposit_line,
    _money_value as _pm_money_value,
)


logger = logging.getLogger(__name__)

INSTALL_PASS = "pass"
INSTALL_FAIL = "fail"
INSTALL_SKIP = "skip"

INSTALL_RULESET = "Installs"
INSTALL_AUDIT_RUN_TYPE = "install_audit"

COMPLETION_FORM_NAMES = (
    "Installation Completion Form",
    "Installation Completion Report",
    "Completion Report",
)
AUTHORIZATION_FORM_NAMES = (
    "Homeowner Authorization Form",
    "Homeowner Authorization",
)
INSTALL_REVIEW_FIELD_NAMES = (
    "Review Requested",
    "Review request",
    "Review Sent",
)
COMPLETE_STATUS_WORDS = ("complete", "completed", "closed", "done")
ACTIVE_STATUS_WORDS = ("working", "in progress", "started", "dispatched", *COMPLETE_STATUS_WORDS)
EXCLUDED_APPOINTMENT_STATUS_WORDS = ("cancel", "rescheduled", "no access")


INSTALL_AUDIT_TEST_MESSAGE = """HIGH - Installs: Job Not Marked Complete
Technician: Test Installer
Appointment: Jul 8, 8:00 AM-4:00 PM
Issue: final install window passed, but job status is still In Progress
Action: mark job Completed or confirm why it is still open
Open in ServiceTitan: https://go.servicetitan.com/#/Job/Index/INSTALL-TEST-1001"""


@dataclass(frozen=True)
class InstallRuleResult:
    rule_id: str
    title: str
    severity: str
    status: str
    issue: str
    action: str
    technician_name: str
    appointment_text: str
    job_id: str
    appointment_id: str
    url: str
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def violation_key(self) -> str:
        tech_key = _normalize_key(self.technician_name or "unassigned")
        appointment_key = _normalize_key(self.appointment_id or self.appointment_text or "no-appointment")
        return f"install_audit:{self.job_id or 'no-job'}:{appointment_key}:{self.rule_id}:{tech_key}"


@dataclass
class InstallAuditSummary:
    status: str = "completed"
    enabled: bool = False
    dry_run: bool = True
    raw_jobs_fetched: int = 0
    jobs_skipped_out_of_scope: int = 0
    jobs_enriched: int = 0
    jobs_scanned: int = 0
    appointments_evaluated: int = 0
    rules_evaluated: int = 0
    pass_count: int = 0
    fail_count: int = 0
    skip_count: int = 0
    alerts_sent: int = 0
    alerts_would_send: int = 0
    alerts_skipped_dedupe: int = 0
    errors: int = 0
    config_errors: list[str] = field(default_factory=list)
    results: list[InstallRuleResult] = field(default_factory=list)
    data_fields: dict[str, bool] = field(default_factory=dict)

    @property
    def failures(self) -> list[InstallRuleResult]:
        return [result for result in self.results if result.status == INSTALL_FAIL]

    def top_fail_rules(self, limit: int = 3) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter(f"{result.rule_id} {result.title}" for result in self.failures)
        return counts.most_common(limit)

    def top_skip_reasons(self, limit: int = 3) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter(result.issue for result in self.results if result.status == INSTALL_SKIP)
        return counts.most_common(limit)

    def to_lines(self) -> list[str]:
        lines = [
            f"Install audit: {self.status}",
            f"- enabled: {self.enabled}",
            f"- dry_run: {self.dry_run}",
            f"- raw jobs fetched: {self.raw_jobs_fetched}",
            f"- out-of-scope jobs skipped: {self.jobs_skipped_out_of_scope}",
            f"- jobs enriched: {self.jobs_enriched}",
            f"- jobs scanned: {self.jobs_scanned}",
            f"- appointments evaluated: {self.appointments_evaluated}",
            f"- rules evaluated: {self.rules_evaluated}",
            f"- pass: {self.pass_count}",
            f"- fail: {self.fail_count}",
            f"- skip: {self.skip_count}",
            f"- Slack alerts sent: {self.alerts_sent}",
            f"- Slack alerts that would send: {self.alerts_would_send}",
            f"- Slack alerts skipped due to dedupe: {self.alerts_skipped_dedupe}",
            f"- errors: {self.errors}",
        ]
        if self.config_errors:
            lines.append("- config errors:")
            lines.extend(f"  - {error}" for error in self.config_errors)
        if self.data_fields:
            lines.append("- data fields:")
            for name, readable in sorted(self.data_fields.items()):
                lines.append(f"  - {name}: {'readable' if readable else 'missing'}")
        top_fail = self.top_fail_rules(3)
        if top_fail:
            lines.append("- top fail rules:")
            lines.extend(f"  - {rule}: {count}" for rule, count in top_fail)
        top_skip = self.top_skip_reasons(3)
        if top_skip:
            lines.append("- top skip reasons:")
            lines.extend(f"  - {reason}: {count}" for reason, count in top_skip)
        if self.failures:
            lines.append("")
            lines.extend(_alert_text(result) for result in self.failures[:5])
        return lines


@dataclass(frozen=True)
class InstallAuditRule:
    rule_id: str
    title: str
    severity: str
    enabled_by_default: bool
    evaluate: Callable[[ServiceTitanJob, Settings, datetime], InstallRuleResult]


class InstallAuditService:
    def __init__(self, settings: Settings, db: Persistence, client: ServiceTitanClient, slack: SlackClient) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.slack = slack

    def run_once(self, now: datetime | None = None, *, require_enabled: bool = True) -> InstallAuditSummary:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        summary = InstallAuditSummary(enabled=self.settings.install_audit_enabled, dry_run=self.settings.install_audit_dry_run)
        if require_enabled and not self.settings.install_audit_enabled:
            summary.status = "disabled"
            logger.info("install_audit_disabled")
            return summary

        missing = self._missing_config()
        if missing:
            summary.status = "config_error"
            summary.config_errors = missing
            summary.data_fields = self._data_field_summary([])
            logger.warning("install_audit_skipped_missing_config", extra={"missing_config": missing})
            return summary

        run_id = self.db.log_run_start(
            INSTALL_AUDIT_RUN_TYPE,
            {
                "dry_run": summary.dry_run,
                "rule_ids": self.settings.install_audit_rule_ids,
                "business_unit_ids_configured": bool(self.settings.install_audit_business_unit_ids),
            },
        )
        try:
            window_start = now - timedelta(days=self.settings.install_audit_lookback_days)
            window_end = now + timedelta(days=self.settings.install_audit_lookahead_days)
            jobs = self.client.query_install_audit_jobs(
                business_unit_ids=set(self.settings.install_audit_business_unit_ids),
                window_start=window_start,
                window_end=window_end,
                max_appointments=self.settings.install_audit_max_appointments,
            )
        except ServiceTitanApiError as exc:
            summary.status = "api_error"
            summary.errors = 1
            self.db.log_run_complete(run_id, "api_error", {"status": exc.status, "error_message": exc.message})
            logger.warning("install_audit_api_error", extra={"status_code": exc.status, "error_message": exc.message})
            return summary
        except Exception as exc:
            summary.status = "api_error"
            summary.errors = 1
            self.db.log_run_complete(run_id, "api_error", {"error_message": str(exc)})
            logger.warning("install_audit_failed", exc_info=True, extra={"error_message": str(exc)})
            return summary

        stats = getattr(self.client, "last_install_audit_stats", {}) or {}
        summary.raw_jobs_fetched = int(stats.get("raw_jobs_fetched", len(jobs)))
        summary.jobs_skipped_out_of_scope = int(stats.get("jobs_skipped_out_of_scope", 0))
        summary.jobs_enriched = int(stats.get("jobs_enriched", len(jobs)))
        scoped_jobs = [_job for _job in jobs if _job_matches_install_scope(_job, self.settings)]
        summary.jobs_scanned = len(scoped_jobs)
        summary.appointments_evaluated = sum(max(1, len(_install_appointments(job))) for job in scoped_jobs)
        summary.data_fields = self._data_field_summary(scoped_jobs)

        rules = active_install_audit_rules(self.settings)
        for job in scoped_jobs:
            for rule in rules:
                if rule.rule_id == "I6":
                    continue
                result = rule.evaluate(job, self.settings, now)
                self._record_result(summary, result)

        if any(rule.rule_id == "I6" for rule in rules):
            for result in _meal_break_results(scoped_jobs, self.settings, now):
                self._record_result(summary, result)

        if summary.failures and summary.dry_run:
            summary.alerts_would_send = len(summary.failures)
            logger.info("install_audit_dry_run", extra={"failures": summary.fail_count})
        elif summary.failures:
            for result in summary.failures:
                alert_status = self._record_and_alert(result)
                if alert_status == "sent":
                    summary.alerts_sent += 1
                elif alert_status == "deduped":
                    summary.alerts_skipped_dedupe += 1
                elif alert_status == "failed":
                    summary.errors += 1
                    summary.status = "slack_error"

        self.db.log_run_complete(
            run_id,
            summary.status,
            {
                "dry_run": summary.dry_run,
                "raw_jobs_fetched": summary.raw_jobs_fetched,
                "jobs_skipped_out_of_scope": summary.jobs_skipped_out_of_scope,
                "jobs_enriched": summary.jobs_enriched,
                "jobs_scanned": summary.jobs_scanned,
                "appointments_evaluated": summary.appointments_evaluated,
                "rules_evaluated": summary.rules_evaluated,
                "pass": summary.pass_count,
                "fail": summary.fail_count,
                "skip": summary.skip_count,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "data_fields": summary.data_fields,
            },
        )
        logger.info(
            "install_audit_completed",
            extra={
                "dry_run": summary.dry_run,
                "jobs_scanned": summary.jobs_scanned,
                "rules_evaluated": summary.rules_evaluated,
                "failures": summary.fail_count,
                "skips": summary.skip_count,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "data_fields": summary.data_fields,
            },
        )
        return summary

    def _record_result(self, summary: InstallAuditSummary, result: InstallRuleResult) -> None:
        summary.results.append(result)
        summary.rules_evaluated += 1
        if result.status == INSTALL_PASS:
            summary.pass_count += 1
        elif result.status == INSTALL_FAIL:
            summary.fail_count += 1
        elif result.status == INSTALL_SKIP:
            summary.skip_count += 1
        else:
            summary.errors += 1

    def _missing_config(self) -> list[str]:
        missing = []
        required = {
            "SERVICETITAN_CLIENT_ID": self.settings.servicetitan_client_id,
            "SERVICETITAN_CLIENT_SECRET": self.settings.servicetitan_client_secret,
            "SERVICETITAN_TENANT_ID": self.settings.servicetitan_tenant_id,
            "SERVICETITAN_APP_KEY": self.settings.servicetitan_app_key,
            "INSTALL_AUDIT_BUSINESS_UNIT_IDS": self.settings.install_audit_business_unit_ids,
        }
        for key, value in required.items():
            if not value:
                missing.append(key)
        if not self.settings.install_audit_dry_run:
            if not self.settings.slack_bot_token:
                missing.append("SLACK_BOT_TOKEN")
            if not self.settings.install_audit_slack_channel_id:
                missing.append("INSTALL_AUDIT_SLACK_CHANNEL_ID")
        return missing

    def _record_and_alert(self, result: InstallRuleResult) -> str:
        record = self.db.upsert_service_titan_violation(
            violation_key=result.violation_key,
            service_titan_job_id=result.job_id,
            appointment_id=result.appointment_id,
            technician_id=str(result.metadata.get("technician_id") or ""),
            technician_name=result.technician_name,
            dispatcher_id="",
            dispatcher_name="",
            rule_id=result.rule_id,
            ruleset=INSTALL_RULESET,
            severity=result.severity,
            title=result.title,
            description=result.issue,
            recommended_action=result.action,
            metadata={
                "appointment": result.appointment_text,
                "issue": result.issue,
                "action": result.action,
                "rule_metadata": result.metadata,
            },
        )
        if record.get("alert_sent_at"):
            logger.info("install_audit_duplicate_alert_suppressed", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
            return "deduped"
        ts = self.slack.post_message(self.settings.install_audit_slack_channel_id, _alert_text(result))
        if not ts:
            logger.warning("install_audit_slack_failed", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
            return "failed"
        self.db.mark_service_titan_alert_sent(result.violation_key)
        logger.info("install_audit_slack_sent", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
        return "sent"

    def _data_field_summary(self, jobs: list[ServiceTitanJob]) -> dict[str, bool]:
        return {
            "install_business_unit_ids_configured": bool(self.settings.install_audit_business_unit_ids),
            "homeowner_authorization_form_status_readable": any(_form_status(job, AUTHORIZATION_FORM_NAMES) is not None for job in jobs),
            "installation_completion_form_status_readable": any(_form_status(job, COMPLETION_FORM_NAMES) is not None for job in jobs),
            "arrival_timestamp_readable": any("arrived_at" in job.present_fields for job in jobs),
            "timesheet_meal_break_data_readable": any({"clock_in", "clock_out", "lunch_break"}.issubset(job.present_fields) for job in jobs),
            "invoice_payment_deposit_data_readable": any(_payment_data_available(job) for job in jobs),
            "financing_flag_readable": any(_financing_status(job) is not None for job in jobs),
            "per_day_appointment_data_readable": any(bool(_install_appointments(job)) for job in jobs),
        }


def active_install_audit_rules(settings: Settings) -> list[InstallAuditRule]:
    allowlist = {rule_id.strip().upper() for rule_id in settings.install_audit_rule_ids if rule_id.strip()}
    rules = install_audit_rules()
    return [rule for rule in rules if (not allowlist and rule.enabled_by_default) or rule.rule_id in allowlist]


def install_audit_rules() -> list[InstallAuditRule]:
    return [
        InstallAuditRule("I1", "Job Not Marked Complete", "high", True, _rule_i1),
        InstallAuditRule("I2", "Completion Form Not Completed", "high", True, _rule_i2),
        InstallAuditRule("I3", "Authorization Form Not Completed", "high", True, _rule_i3),
        InstallAuditRule("I4", "Arrival Not Marked", "medium", True, _rule_i4),
        InstallAuditRule("I5", "Arrived Late", "medium", True, _rule_i5),
        InstallAuditRule("I6", "Meal Break Not Recorded", "high", True, _rule_i6_placeholder),
        InstallAuditRule("I7", "Deposit Not Collected", "reminder", True, _rule_i7),
        InstallAuditRule("I8", "Payment Milestone Short", "high", True, _rule_i8),
        InstallAuditRule("I9", "Photos Missing", "medium", True, _rule_i9),
        InstallAuditRule("I10", "Materials Not Scanned", "medium", True, _rule_i10),
        InstallAuditRule("I11", "Equipment Not Registered", "medium", True, _rule_i11),
        InstallAuditRule("I12", "Review Not Requested", "low", False, _rule_i12),
    ]


def _rule_i1(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if _job_completed(job):
        return _result(job, "I1", "Job Not Marked Complete", "high", INSTALL_PASS, "Job status is completed.", "No action needed.")
    final_end = _install_final_end(job)
    completion_done = _form_completed(job, COMPLETION_FORM_NAMES)
    full_payment = _full_payment_in(job)
    if completion_done is True:
        return _result(
            job,
            "I1",
            "Job Not Marked Complete",
            "high",
            INSTALL_FAIL,
            f"Installation Completion Form is done, but job status is still {job.status or 'not completed'}",
            "mark job Completed or confirm why it is still open",
        )
    if full_payment is True:
        return _result(
            job,
            "I1",
            "Job Not Marked Complete",
            "high",
            INSTALL_FAIL,
            f"full payment is in, but job status is still {job.status or 'not completed'}",
            "mark job Completed or confirm why it is still open",
        )
    if final_end is None:
        return _result(job, "I1", "Job Not Marked Complete", "high", INSTALL_SKIP, "Final install day/window cannot be determined.", "Confirm final install schedule.")
    if now.astimezone(timezone.utc) <= final_end.astimezone(timezone.utc):
        return _result(job, "I1", "Job Not Marked Complete", "high", INSTALL_SKIP, "Multi-day install is still within scheduled span.", "No action needed.")
    return _result(
        job,
        "I1",
        "Job Not Marked Complete",
        "high",
        INSTALL_FAIL,
        f"final install window passed, but job status is still {job.status or 'not completed'}",
        "mark job Completed or confirm why it is still open",
    )


def _rule_i2(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if not _forms_available(job):
        return _result(job, "I2", "Completion Form Not Completed", "high", INSTALL_SKIP, "Form status is unavailable.", "Confirm ServiceTitan form source.")
    final_done = _final_install_condition(job, now)
    if final_done is None and not _job_completed(job):
        return _result(job, "I2", "Completion Form Not Completed", "high", INSTALL_SKIP, "Final-day condition cannot be determined.", "Confirm final install schedule.")
    if final_done is False and not _job_completed(job):
        return _result(job, "I2", "Completion Form Not Completed", "high", INSTALL_SKIP, "Final install day is not done yet.", "No action needed.")
    if not _matching_forms(job, COMPLETION_FORM_NAMES):
        return _result(job, "I2", "Completion Form Not Completed", "high", INSTALL_FAIL, "Installation Completion Form is missing.", "complete the Installation Completion Form")
    status = _form_status(job, COMPLETION_FORM_NAMES)
    if status is None:
        return _result(job, "I2", "Completion Form Not Completed", "high", INSTALL_SKIP, "Installation Completion Form status is unavailable.", "Confirm ServiceTitan form status source.")
    if status is False:
        return _result(job, "I2", "Completion Form Not Completed", "high", INSTALL_FAIL, "Installation Completion Form is not completed.", "complete the Installation Completion Form")
    return _result(job, "I2", "Completion Form Not Completed", "high", INSTALL_PASS, "Installation Completion Form is completed.", "No action needed.")


def _rule_i3(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if not _forms_available(job):
        return _result(job, "I3", "Authorization Form Not Completed", "high", INSTALL_SKIP, "Form status is unavailable.", "Confirm ServiceTitan form source.")
    started = _install_started(job)
    if started is None:
        return _result(job, "I3", "Authorization Form Not Completed", "high", INSTALL_SKIP, "Started condition cannot be determined.", "Confirm arrival/start data.")
    if not started:
        return _result(job, "I3", "Authorization Form Not Completed", "high", INSTALL_SKIP, "Crew has not arrived or started yet.", "No action needed.")
    if not _matching_forms(job, AUTHORIZATION_FORM_NAMES):
        return _result(job, "I3", "Authorization Form Not Completed", "high", INSTALL_FAIL, "Homeowner Authorization Form is missing.", "complete the Homeowner Authorization Form")
    status = _form_status(job, AUTHORIZATION_FORM_NAMES)
    if status is None:
        return _result(job, "I3", "Authorization Form Not Completed", "high", INSTALL_SKIP, "Homeowner Authorization Form status is unavailable.", "Confirm ServiceTitan form status source.")
    if status is False:
        return _result(job, "I3", "Authorization Form Not Completed", "high", INSTALL_FAIL, "Homeowner Authorization Form is not completed.", "complete the Homeowner Authorization Form")
    return _result(job, "I3", "Authorization Form Not Completed", "high", INSTALL_PASS, "Homeowner Authorization Form is completed.", "No action needed.")


def _rule_i4(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if job.arrived_at:
        return _result(job, "I4", "Arrival Not Marked", "medium", INSTALL_PASS, "Arrival timestamp is recorded.", "No action needed.")
    start = _install_first_start(job)
    if start and now.astimezone(timezone.utc) < start.astimezone(timezone.utc):
        return _result(job, "I4", "Arrival Not Marked", "medium", INSTALL_SKIP, "Appointment start is in the future.", "No action needed.")
    if "arrived_at" not in job.present_fields:
        return _result(job, "I4", "Arrival Not Marked", "medium", INSTALL_SKIP, "Arrival field is unavailable.", "Confirm arrival timestamp source.")
    if start and now.astimezone(timezone.utc) >= start.astimezone(timezone.utc):
        return _result(job, "I4", "Arrival Not Marked", "medium", INSTALL_FAIL, "appointment start has passed, but no arrival/on-site timestamp is recorded", "mark arrival or confirm why crew is not on site")
    if _status_has_words(job.status, ACTIVE_STATUS_WORDS):
        return _result(job, "I4", "Arrival Not Marked", "medium", INSTALL_FAIL, "job is in progress or complete, but no arrival/on-site timestamp is recorded", "mark arrival or confirm why crew is not on site")
    return _result(job, "I4", "Arrival Not Marked", "medium", INSTALL_SKIP, "Scheduled start cannot be determined.", "Confirm appointment schedule.")


def _rule_i5(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if not job.arrived_at:
        return _result(job, "I5", "Arrived Late", "medium", INSTALL_SKIP, "Arrival is not marked; I4 covers missing arrival.", "Mark arrival first.")
    start = _install_first_start(job)
    if start is None:
        return _result(job, "I5", "Arrived Late", "medium", INSTALL_SKIP, "Scheduled start timestamp unavailable.", "Confirm appointment schedule.")
    late_after = start.astimezone(timezone.utc) + timedelta(minutes=settings.install_audit_arrival_grace_min)
    if job.arrived_at.astimezone(timezone.utc) > late_after:
        return _result(
            job,
            "I5",
            "Arrived Late",
            "medium",
            INSTALL_FAIL,
            f"arrival is more than {settings.install_audit_arrival_grace_min} minutes after scheduled start",
            "confirm late arrival reason with the install crew lead",
            metadata={"scheduled_start": start.isoformat(), "arrived_at": job.arrived_at.isoformat()},
        )
    return _result(job, "I5", "Arrived Late", "medium", INSTALL_PASS, "Arrival was within the configured grace period.", "No action needed.")


def _rule_i6_placeholder(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    return _result(job, "I6", "Meal Break Not Recorded", "high", INSTALL_SKIP, "Meal break rule runs per technician per day.", "Review timesheet/break record.")


def _rule_i7(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    start = _install_first_start(job)
    if start is None:
        return _result(job, "I7", "Deposit Not Collected", "reminder", INSTALL_SKIP, "Install start date unavailable.", "Confirm install schedule.")
    lead_end = now.astimezone(timezone.utc) + timedelta(days=settings.install_audit_deposit_reminder_lead_days)
    if start.astimezone(timezone.utc).date() > lead_end.date() or start.astimezone(timezone.utc).date() < now.astimezone(timezone.utc).date():
        return _result(job, "I7", "Deposit Not Collected", "reminder", INSTALL_SKIP, "Install date is outside the deposit reminder window.", "No action needed.")
    financed = _financing_status(job)
    if financed is True:
        return _result(job, "I7", "Deposit Not Collected", "reminder", INSTALL_SKIP, "Financed job.", "No deposit reminder needed.")
    if financed is None:
        return _result(job, "I7", "Deposit Not Collected", "reminder", INSTALL_SKIP, "Financing flag unavailable.", "Confirm financing/deposit source.")
    if _deposit_waived(job):
        return _result(job, "I7", "Deposit Not Collected", "reminder", INSTALL_SKIP, "Deposit waived or customer-arranged.", "No deposit reminder needed.")
    deposit_amount = _deposit_paid_amount(job, settings, start)
    if deposit_amount is None:
        return _result(job, "I7", "Deposit Not Collected", "reminder", INSTALL_SKIP, "Deposit/payment relationship is unclear.", "Confirm deposit payment source.")
    if deposit_amount > 0:
        return _result(job, "I7", "Deposit Not Collected", "reminder", INSTALL_PASS, "Deposit payment is recorded.", "No action needed.")
    return _result(
        job,
        "I7",
        "Deposit Not Collected",
        "reminder",
        INSTALL_FAIL,
        "install starts tomorrow/today, no deposit on file",
        "confirm deposit payment before crew starts",
        metadata={"install_start": start.isoformat()},
    )


def _rule_i8(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    financed = _financing_status(job)
    if financed is True:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Financed job.", "No payment milestone alert needed.")
    if financed is None:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Financing flag unavailable.", "Confirm financing/payment source.")
    appointments = _install_appointments(job)
    if not appointments:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Per-day appointment data unavailable.", "Confirm install appointment data.")
    first_start = _appointment_start(appointments[0])
    first_end = _appointment_end(appointments[0])
    final_end = _appointment_end(appointments[-1])
    if first_start is None or first_end is None or final_end is None:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Per-day appointment end data unavailable.", "Confirm install appointment data.")
    totals = _payment_totals(job)
    if not totals["available"]:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Invoice/payment relationship is unclear.", "Confirm invoice/payment source.")
    total = float(totals["total"] or 0.0)
    paid = float(totals["paid"] or 0.0)
    if total <= 0:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Invoice total is unavailable.", "Confirm invoice/payment source.")
    percent = (paid / total) * 100.0
    final_over = now.astimezone(timezone.utc) > final_end.astimezone(timezone.utc)
    first_day_over = now.astimezone(timezone.utc) > first_end.astimezone(timezone.utc)
    same_install_day = now.astimezone(ZoneInfo(settings.timezone)).date() == first_start.astimezone(ZoneInfo(settings.timezone)).date()
    if len(appointments) == 1 and same_install_day:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Single-day install is still on install day.", "No action needed.")
    if final_over and percent + 0.001 < settings.install_audit_final_day_collect_pct:
        return _result(
            job,
            "I8",
            "Payment Milestone Short",
            "high",
            INSTALL_FAIL,
            "final install day is over, but balance is not fully paid",
            "collect final payment or confirm approved payment exception",
            metadata={"paid_percent": round(percent, 2), "invoice_total": total, "paid_amount": paid},
        )
    if len(appointments) > 1 and first_day_over and percent + 0.001 < settings.install_audit_first_day_collect_pct:
        return _result(
            job,
            "I8",
            "Payment Milestone Short",
            "medium",
            INSTALL_FAIL,
            f"multi-day install is past end of day 1, but less than {settings.install_audit_first_day_collect_pct:g}% is collected",
            "collect first-day milestone payment or confirm approved payment exception",
            metadata={"paid_percent": round(percent, 2), "invoice_total": total, "paid_amount": paid},
        )
    if not final_over and not first_day_over:
        return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_SKIP, "Install payment milestone is not due yet.", "No action needed.")
    return _result(job, "I8", "Payment Milestone Short", "high", INSTALL_PASS, "Payment milestone is met.", "No action needed.")


def _rule_i9(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if not _job_completed(job):
        return _result(job, "I9", "Photos Missing", "medium", INSTALL_SKIP, "Install is not marked complete.", "No action needed.")
    if "photos" not in job.present_fields or job.photo_count is None:
        return _result(job, "I9", "Photos Missing", "medium", INSTALL_SKIP, "Photo/attachment count unavailable.", "Confirm photo source.")
    if job.photo_count < settings.install_audit_completion_photos_min:
        return _result(job, "I9", "Photos Missing", "medium", INSTALL_FAIL, "install is complete, but required completion photos are missing", "attach install completion photos")
    return _result(job, "I9", "Photos Missing", "medium", INSTALL_PASS, "Required install photos are attached.", "No action needed.")


def _rule_i10(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if not _job_completed(job):
        return _result(job, "I10", "Materials Not Scanned", "medium", INSTALL_SKIP, "Install is not marked complete.", "No action needed.")
    if _bare_labor_job(job):
        return _result(job, "I10", "Materials Not Scanned", "medium", INSTALL_SKIP, "Bare-labor/no-material job type.", "No material scan needed.")
    if not job.ply_data_available and "purchase_orders" not in job.present_fields:
        return _result(job, "I10", "Materials Not Scanned", "medium", INSTALL_SKIP, "Material/Ply sync data unavailable.", "Confirm material scan source.")
    if (job.purchase_orders_count or 0) <= 0:
        return _result(job, "I10", "Materials Not Scanned", "medium", INSTALL_FAIL, "install is complete, but no materials were scanned onto the job", "scan install materials or confirm bare-labor exception")
    return _result(job, "I10", "Materials Not Scanned", "medium", INSTALL_PASS, "Materials are recorded on the job.", "No action needed.")


def _rule_i11(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if not _job_completed(job):
        return _result(job, "I11", "Equipment Not Registered", "medium", INSTALL_SKIP, "Install is not marked complete.", "No action needed.")
    if "equipment" not in job.present_fields:
        return _result(job, "I11", "Equipment Not Registered", "medium", INSTALL_SKIP, "Equipment registration data unavailable.", "Confirm equipment source.")
    if not job.equipment_count or job.equipment_complete is False:
        return _result(job, "I11", "Equipment Not Registered", "medium", INSTALL_FAIL, "install is complete, but equipment registration or labels are missing", "register equipment and add equipment labels")
    return _result(job, "I11", "Equipment Not Registered", "medium", INSTALL_PASS, "Equipment registration data is present.", "No action needed.")


def _rule_i12(job: ServiceTitanJob, settings: Settings, now: datetime) -> InstallRuleResult:
    if not _job_completed(job):
        return _result(job, "I12", "Review Not Requested", "low", INSTALL_SKIP, "Install is not marked complete.", "No action needed.")
    requested = _review_requested(job)
    if requested is None:
        return _result(job, "I12", "Review Not Requested", "low", INSTALL_SKIP, "Review-requested field unavailable.", "Confirm review request source.")
    if not requested:
        return _result(job, "I12", "Review Not Requested", "low", INSTALL_FAIL, "install is complete, but review-requested step is empty", "request review or update review-requested flag")
    return _result(job, "I12", "Review Not Requested", "low", INSTALL_PASS, "Review-requested step is recorded.", "No action needed.")


def _meal_break_results(jobs: list[ServiceTitanJob], settings: Settings, now: datetime) -> list[InstallRuleResult]:
    grouped: dict[tuple[str, str], list[ServiceTitanJob]] = {}
    tz = ZoneInfo(settings.timezone)
    for job in jobs:
        day_source = job.clock_in_at or job.arrival_window_start or _install_first_start(job)
        if day_source is None:
            grouped.setdefault((job.technician_id or job.technician_name or "unassigned", "unknown"), []).append(job)
            continue
        day = day_source.astimezone(tz).date().isoformat()
        grouped.setdefault((job.technician_id or job.technician_name or "unassigned", day), []).append(job)

    results: list[InstallRuleResult] = []
    for (_tech_key, day), group in grouped.items():
        sample = group[0]
        starts = [job.clock_in_at for job in group if job.clock_in_at]
        ends = [job.clock_out_at for job in group if job.clock_out_at]
        if not starts or not ends or not any("lunch_break" in job.present_fields for job in group):
            results.append(
                _workday_result(
                    sample,
                    day,
                    INSTALL_SKIP,
                    "Timesheet/break source is unavailable.",
                    "review timesheet/break record",
                    settings,
                    worked_hours=None,
                )
            )
            continue
        start = min(starts)
        end = max(ends)
        worked_minutes = max(0, int((end - start).total_seconds() // 60))
        worked_hours = worked_minutes / 60.0
        break_minutes = sum(job.lunch_break_minutes or 0 for job in group)
        required_breaks = 0
        if worked_hours > settings.install_audit_meal_break_after_hours:
            required_breaks = 1
        if worked_hours > settings.install_audit_second_meal_after_hours:
            required_breaks = 2
        if required_breaks == 0:
            results.append(
                _workday_result(sample, day, INSTALL_SKIP, "Day is under meal break threshold.", "No action needed.", settings, worked_hours=worked_hours)
            )
            continue
        required_minutes = required_breaks * settings.install_audit_meal_break_min_minutes
        if break_minutes >= required_minutes:
            results.append(_workday_result(sample, day, INSTALL_PASS, "Meal break requirement is met.", "No action needed.", settings, worked_hours=worked_hours))
            continue
        agreement_status = _on_duty_meal_agreement_status(group)
        if agreement_status is True:
            results.append(_workday_result(sample, day, INSTALL_SKIP, "On-duty meal agreement applies.", "review timesheet/break record", settings, worked_hours=worked_hours))
            continue
        if agreement_status is None:
            results.append(
                _workday_result(
                    sample,
                    day,
                    INSTALL_SKIP,
                    "On-duty meal agreement cannot be determined.",
                    "review timesheet/break record",
                    settings,
                    worked_hours=worked_hours,
                )
            )
            continue
        if required_breaks == 2 and break_minutes >= settings.install_audit_meal_break_min_minutes:
            issue = f"{worked_hours:g} hrs worked, no second 30-min meal break recorded"
        else:
            issue = f"{worked_hours:g} hrs worked, no 30-min meal break recorded"
        results.append(_workday_result(sample, day, INSTALL_FAIL, issue, "review timesheet/break record", settings, worked_hours=worked_hours))
    return results


def _result(
    job: ServiceTitanJob,
    rule_id: str,
    title: str,
    severity: str,
    status: str,
    issue: str,
    action: str,
    *,
    metadata: dict[str, object] | None = None,
) -> InstallRuleResult:
    return InstallRuleResult(
        rule_id=rule_id,
        title=title,
        severity=severity,
        status=status,
        issue=issue,
        action=action,
        technician_name=_crew_lead(job),
        appointment_text=_appointment_text(job),
        job_id=job.job_id,
        appointment_id=job.appointment_id,
        url=job.url,
        metadata={"technician_id": job.technician_id, **(metadata or {})},
    )


def _workday_result(
    job: ServiceTitanJob,
    day: str,
    status: str,
    issue: str,
    action: str,
    settings: Settings,
    *,
    worked_hours: float | None,
) -> InstallRuleResult:
    return InstallRuleResult(
        rule_id="I6",
        title="Meal Break Not Recorded",
        severity="high",
        status=status,
        issue=issue,
        action=action,
        technician_name=_crew_lead(job),
        appointment_text=day,
        job_id=job.job_id,
        appointment_id=f"{job.technician_id or job.technician_name}:{day}",
        url="",
        metadata={"technician_id": job.technician_id, "work_date": day, "worked_hours": worked_hours},
    )


def _alert_text(result: InstallRuleResult) -> str:
    lines = [f"{result.severity.upper()} - {INSTALL_RULESET}: {result.title}"]
    lines.append(f"Technician: {result.technician_name or 'unassigned'}")
    if result.rule_id == "I6":
        lines.append(f"Date: {result.appointment_text or '<unknown>'}")
    elif result.appointment_text:
        lines.append(f"Appointment: {result.appointment_text}")
    lines.extend(
        [
            f"Issue: {result.issue}",
            f"Action: {result.action}",
        ]
    )
    if result.url:
        lines.append(f"Open in ServiceTitan: {result.url}")
    return "\n".join(lines)


def _job_matches_install_scope(job: ServiceTitanJob, settings: Settings) -> bool:
    configured_ids = {value.strip() for value in settings.install_audit_business_unit_ids if value.strip()}
    return bool(configured_ids and job.business_unit_id in configured_ids)


def _crew_lead(job: ServiceTitanJob) -> str:
    return job.technician_name or job.technician_id or "unassigned"


def _job_completed(job: ServiceTitanJob) -> bool:
    return _status_has_words(job.status, COMPLETE_STATUS_WORDS)


def _status_has_words(status: str, words: tuple[str, ...]) -> bool:
    normalized = _normalize_key(status)
    return any(word in normalized for word in words)


def _install_started(job: ServiceTitanJob) -> bool | None:
    if job.arrived_at or job.clock_in_at:
        return True
    if _status_has_words(job.status, ACTIVE_STATUS_WORDS):
        return True
    start = _install_first_start(job)
    if start is None:
        return None
    return False


def _final_install_condition(job: ServiceTitanJob, now: datetime) -> bool | None:
    final_end = _install_final_end(job)
    if final_end is None:
        return None
    return now.astimezone(timezone.utc) > final_end.astimezone(timezone.utc)


def _install_appointments(job: ServiceTitanJob) -> list[dict[str, object]]:
    raw = job.raw.get("appointments")
    appointments = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    filtered = [item for item in appointments if not _excluded_appointment(item)]
    if not filtered and (job.arrival_window_start or job.arrival_window_end):
        filtered = [{"arrivalWindowStart": job.arrival_window_start, "arrivalWindowEnd": job.arrival_window_end, "id": job.appointment_id}]
    return sorted(filtered, key=lambda item: (_appointment_start(item) or datetime.max.replace(tzinfo=timezone.utc)).astimezone(timezone.utc))


def _excluded_appointment(record: dict[str, object]) -> bool:
    status = _normalize_key(str(_raw_value(record, ("status", "appointmentStatus", "status.name")) or ""))
    return any(word in status for word in EXCLUDED_APPOINTMENT_STATUS_WORDS)


def _install_first_start(job: ServiceTitanJob) -> datetime | None:
    appointments = _install_appointments(job)
    if appointments:
        return _appointment_start(appointments[0])
    return job.arrival_window_start


def _install_final_end(job: ServiceTitanJob) -> datetime | None:
    appointments = _install_appointments(job)
    if appointments:
        return _appointment_end(appointments[-1])
    return job.arrival_window_end or job.completed_on


def _appointment_start(record: dict[str, object]) -> datetime | None:
    return _parse_datetime(_raw_value(record, ("arrivalWindowStart", "scheduledStart", "scheduledStartOn", "start", "startDate")))


def _appointment_end(record: dict[str, object]) -> datetime | None:
    return _parse_datetime(_raw_value(record, ("arrivalWindowEnd", "scheduledEnd", "scheduledEndOn", "end", "endDate"))) or _appointment_start(record)


def _appointment_text(job: ServiceTitanJob) -> str:
    start = _install_first_start(job)
    end = _install_final_end(job)
    if not start:
        return ""
    tz = ZoneInfo("UTC")
    start_local = start.astimezone(tz)
    text = start_local.strftime("%b %-d, %-I:%M %p") if _supports_dash_strftime() else start_local.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    if end:
        end_local = end.astimezone(tz)
        if end_local.date() == start_local.date():
            end_text = end_local.strftime("%-I:%M %p") if _supports_dash_strftime() else end_local.strftime("%I:%M %p").lstrip("0")
        else:
            end_text = end_local.strftime("%b %-d, %-I:%M %p") if _supports_dash_strftime() else end_local.strftime("%b %d, %I:%M %p").replace(" 0", " ")
        text = f"{text}-{end_text}"
    return text


def _supports_dash_strftime() -> bool:
    return True


def _forms_available(job: ServiceTitanJob) -> bool:
    return "forms" in job.present_fields or isinstance(job.raw.get("forms"), list)


def _form_records(job: ServiceTitanJob) -> list[dict[str, object]]:
    raw = job.raw.get("forms")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _form_status(job: ServiceTitanJob, names: tuple[str, ...]) -> bool | None:
    for form in _matching_forms(job, names):
        completed_at = _parse_datetime(_raw_value(form, ("completedAt", "submittedAt", "sentAt")))
        if completed_at:
            return True
        explicit = _bool_value(_raw_value(form, ("completed", "isCompleted", "submitted", "isSubmitted")))
        if explicit is not None:
            return explicit
        status = _normalize_key(str(_raw_value(form, ("status", "result", "state")) or ""))
        if not status:
            return None
        if any(word in status for word in ("complete", "completed", "submitted", "approved", "done")):
            return True
        if any(word in status for word in ("missing", "incomplete", "not complete", "draft", "open", "pending")):
            return False
        return None
    return None


def _form_completed(job: ServiceTitanJob, names: tuple[str, ...]) -> bool | None:
    return _form_status(job, names)


def _matching_forms(job: ServiceTitanJob, names: tuple[str, ...]) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for form in _form_records(job):
        name = str(_raw_value(form, ("name", "formName", "title")) or "")
        if _name_matches(name, names):
            matches.append(form)
    return matches


def _name_matches(value: str, names: tuple[str, ...]) -> bool:
    normalized = _normalize_key(value)
    return any(_normalize_key(name) == normalized for name in names)


def _invoice_records(job: ServiceTitanJob) -> list[dict[str, object]]:
    raw = job.raw.get("invoices")
    invoices = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if invoices:
        return invoices
    if job.invoice_total is not None or job.invoice_balance is not None or job.payment_total is not None:
        return [
            {
                "total": job.invoice_total,
                "balance": job.invoice_balance,
                "paymentTotal": job.payment_total,
                "status": job.invoice_status,
                "lineItems": [{"name": item} for item in job.invoice_line_items],
            }
        ]
    return []


def _payment_data_available(job: ServiceTitanJob) -> bool:
    return "payments" in job.present_fields or bool(_invoice_records(job))


def _payment_totals(job: ServiceTitanJob) -> dict[str, float | bool | None]:
    invoices = _invoice_records(job)
    if not invoices and not _payment_data_available(job):
        return {"available": False, "total": None, "balance": None, "paid": None}
    totals: list[float] = []
    balances: list[float] = []
    paid_values: list[float] = []
    for invoice in invoices:
        total = _pm_money_value(invoice, ("total", "invoiceTotal", "summary.total", "subtotal", "amount"))
        balance = _pm_money_value(invoice, ("balance", "invoiceBalance", "summary.balance", "remainingBalance", "amountDue"))
        explicit_paid = _pm_money_value(invoice, ("paymentTotal", "paymentsTotal", "paidAmount", "amountPaid"))
        if total is not None:
            totals.append(total)
        if balance is not None:
            balances.append(balance)
        if explicit_paid is not None:
            paid_values.append(explicit_paid)
        elif total is not None and balance is not None:
            paid_values.append(max(0.0, total - balance))
    total_value = sum(totals) if totals else job.invoice_total
    balance_value = sum(balances) if balances else job.invoice_balance
    paid_value = sum(paid_values) if paid_values else job.payment_total
    if paid_value is None and total_value is not None and balance_value is not None:
        paid_value = max(0.0, total_value - balance_value)
    return {"available": total_value is not None or paid_value is not None or balance_value is not None, "total": total_value, "balance": balance_value, "paid": paid_value}


def _full_payment_in(job: ServiceTitanJob) -> bool | None:
    totals = _payment_totals(job)
    if not totals["available"]:
        return None
    total = totals["total"]
    balance = totals["balance"]
    paid = totals["paid"]
    if total is not None and balance is not None:
        return float(balance) <= 0.01
    if total is not None and paid is not None:
        return float(paid) + 0.01 >= float(total)
    return None


def _deposit_paid_amount(job: ServiceTitanJob, settings: Settings, install_start: datetime) -> float | None:
    if not _payment_data_available(job):
        return None
    invoices = _invoice_records(job)
    if not invoices:
        return 0.0
    deposit_invoices = [
        invoice
        for invoice in invoices
        if _pm_invoice_has_deposit_line(invoice, settings.pm_audit_deposit_line_item_names)
    ]
    if not deposit_invoices:
        return 0.0
    total = 0.0
    saw_payment_data = False
    for invoice in deposit_invoices:
        paid = _pm_deposit_paid_amount(invoice, settings.pm_audit_deposit_payment_status_values, install_start)
        if paid is None:
            continue
        saw_payment_data = True
        total += max(0.0, paid)
    return total if saw_payment_data else None


def _financing_status(job: ServiceTitanJob) -> bool | None:
    matches = _field_matches(job, ("financing", "financed", "loan", "lender"))
    if not matches:
        return None
    text = _normalize_key(" ".join(value for _name, value in matches if value))
    if not text:
        return None
    words = set(text.split())
    if ("not financed" in text or "declined" in words or "false" in words or "no" in words) and not {"approved", "financed"}.intersection(words):
        return False
    if {"approved", "financed", "loan", "yes", "true"}.intersection(words):
        return True
    if "cash" in words:
        return False
    return None


def _deposit_waived(job: ServiceTitanJob) -> bool:
    matches = _field_matches(job, ("deposit", "down payment", "downpayment", "customer arranged"))
    text = _normalize_key(" ".join(f"{name} {value}" for name, value in matches))
    return any(token in text for token in ("waived", "customer arranged", "customer paid", "no deposit required"))


def _review_requested(job: ServiceTitanJob) -> bool | None:
    matches = _field_matches(job, tuple(INSTALL_REVIEW_FIELD_NAMES))
    if not matches:
        return None
    text = _normalize_key(" ".join(value for _name, value in matches))
    words = set(text.split())
    if {"yes", "true", "sent", "requested", "complete", "completed", "done"}.intersection(words):
        return True
    if "not sent" in text or {"no", "false", "pending"}.intersection(words):
        return False
    return None


def _field_matches(job: ServiceTitanJob, names: tuple[str, ...]) -> list[tuple[str, str]]:
    wanted = [_normalize_key(name) for name in names]
    matches: list[tuple[str, str]] = []
    for name, value in job.operational_data.items():
        normalized_name = _normalize_key(name)
        if any(want in normalized_name for want in wanted):
            matches.append((name, value))
    raw_fields = job.raw.get("customFields")
    if isinstance(raw_fields, list):
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            name = str(_raw_value(item, ("name", "label", "typeName", "fieldName", "customFieldTypeName")) or "")
            value = str(_raw_value(item, ("value", "textValue", "stringValue", "displayValue")) or "")
            normalized_name = _normalize_key(name)
            if any(want in normalized_name for want in wanted):
                matches.append((name, value))
    for raw_name in names:
        value = _raw_value(job.raw, (raw_name, raw_name.replace(" ", ""), raw_name.replace(" ", "_")))
        if value is not None:
            matches.append((raw_name, str(value)))
    return matches


def _bare_labor_job(job: ServiceTitanJob) -> bool:
    text = _normalize_key(" ".join([job.job_type_name, *job.tag_names]))
    return any(token in text for token in ("bare labor", "no material", "no materials", "labor only"))


def _on_duty_meal_agreement_status(jobs: list[ServiceTitanJob]) -> bool | None:
    values = [
        str(value)
        for job in jobs
        for value in [_raw_value(job.raw, ("onDutyMealAgreement", "mealAgreement", "mealBreakAgreement"))]
        if value is not None and str(value).strip()
    ]
    if not values:
        return None
    text = _normalize_key(" ".join(values))
    if any(token in text for token in ("yes", "true", "on duty", "applies")):
        return True
    if any(token in text for token in ("no", "false", "does not apply", "none")):
        return False
    return None


def _raw_value(source: dict[str, object], names: tuple[str, ...]) -> object | None:
    for name in names:
        current: object = source
        for part in name.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None:
            return current
    return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _bool_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = _normalize_key(value)
        if normalized in {"true", "yes", "1", "complete", "completed", "submitted"}:
            return True
        if normalized in {"false", "no", "0", "open", "draft", "incomplete"}:
            return False
    return None


def _normalize_key(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())
