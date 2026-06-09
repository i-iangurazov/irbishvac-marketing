from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..clients.servicetitan import ServiceTitanApiError, ServiceTitanClient, ServiceTitanJob
from ..clients.slack import SlackClient
from ..config import Settings
from ..models import parse_notion_datetime
from ..persistence import Persistence
from .service_titan_rules import RESULT_ERROR, RESULT_FAIL, RESULT_INSUFFICIENT, RESULT_PASS, RuleResult, active_service_titan_rules


logger = logging.getLogger(__name__)


@dataclass
class ServiceTitanAuditSummary:
    status: str = "completed"
    dry_run: bool = False
    jobs_scanned: int = 0
    appointments_scanned: int = 0
    invoices_scanned: int = 0
    invoice_items_scanned: int = 0
    estimates_scanned: int = 0
    notes_scanned: int = 0
    photos_scanned: int = 0
    forms_scanned: int = 0
    equipment_records_scanned: int = 0
    purchase_orders_scanned: int = 0
    technician_time_records_scanned: int = 0
    rules_evaluated: int = 0
    violations_detected: int = 0
    result_counts: dict[str, int] = field(default_factory=dict)
    insufficient_data_by_rule: dict[str, int] = field(default_factory=dict)
    missing_data_category_counts: dict[str, int] = field(default_factory=dict)
    alert_destination_counts: dict[str, int] = field(default_factory=dict)
    alerts_sent: int = 0
    alerts_would_send: int = 0
    alerts_skipped_dedupe: int = 0
    errors: int = 0
    config_errors: list[str] = field(default_factory=list)

    def to_lines(self) -> list[str]:
        lines = [
            f"ServiceTitan audit: {self.status}",
            f"- dry_run: {self.dry_run}",
            f"- jobs scanned: {self.jobs_scanned}",
            f"- appointments scanned: {self.appointments_scanned}",
            f"- invoices scanned: {self.invoices_scanned}",
            f"- invoice items scanned: {self.invoice_items_scanned}",
            f"- estimates scanned: {self.estimates_scanned}",
            f"- notes scanned: {self.notes_scanned}",
            f"- photos scanned: {self.photos_scanned}",
            f"- forms scanned: {self.forms_scanned}",
            f"- equipment records scanned: {self.equipment_records_scanned}",
            f"- purchase orders scanned: {self.purchase_orders_scanned}",
            f"- technician time records scanned: {self.technician_time_records_scanned}",
            f"- rules evaluated: {self.rules_evaluated}",
            f"- violations detected: {self.violations_detected}",
            f"- alerts sent: {self.alerts_sent}",
            f"- alerts that would have been sent: {self.alerts_would_send}",
            f"- alerts skipped due to dedupe: {self.alerts_skipped_dedupe}",
            f"- errors: {self.errors}",
        ]
        if self.result_counts:
            lines.append("- rule result counts:")
            for status, count in sorted(self.result_counts.items()):
                lines.append(f"  - {status}: {count}")
        if self.insufficient_data_by_rule:
            lines.append("- insufficient_data by rule:")
            for rule_id, count in sorted(self.insufficient_data_by_rule.items()):
                lines.append(f"  - {rule_id}: {count}")
        if self.missing_data_category_counts:
            lines.append("- missing data category counts:")
            for category, count in sorted(self.missing_data_category_counts.items()):
                lines.append(f"  - {category}: {count}")
        if self.alert_destination_counts:
            lines.append("- alert destinations:")
            for destination, count in sorted(self.alert_destination_counts.items()):
                lines.append(f"  - {destination}: {count}")
        if self.config_errors:
            lines.append("- config errors:")
            lines.extend(f"  - {item}" for item in self.config_errors)
        return lines


class ServiceTitanAuditService:
    def __init__(self, settings: Settings, db: Persistence, client: ServiceTitanClient, slack: SlackClient) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.slack = slack

    def audit_once(self, now: datetime | None = None, *, require_enabled: bool = True) -> ServiceTitanAuditSummary:
        summary = ServiceTitanAuditSummary(dry_run=self.settings.service_titan_audit_dry_run)
        if require_enabled and not self.settings.service_titan_audit_enabled:
            logger.info("servicetitan_audit_skipped_disabled")
            summary.status = "disabled"
            return summary
        missing = self.settings.missing_service_titan_credentials(require_enabled=require_enabled)
        if missing:
            logger.warning("servicetitan_audit_skipped_missing_config", extra={"missing": missing})
            summary.status = "config_error"
            summary.config_errors = missing
            return summary

        run_id = self.db.log_run_start("servicetitan_audit")
        now = now or datetime.now(timezone.utc)
        since = self._poll_since(now)
        try:
            jobs = self.client.query_recent_jobs(since)
        except ServiceTitanApiError as exc:
            self.db.log_run_complete(run_id, "skipped", {"status": exc.status, "error_message": exc.message})
            logger.warning("servicetitan_audit_api_error", extra={"status": exc.status, "error_message": exc.message})
            summary.status = "api_error"
            summary.errors = 1
            return summary
        except Exception as exc:
            self.db.log_run_complete(run_id, "skipped", {"error": str(exc)})
            logger.warning("servicetitan_audit_api_failure", exc_info=True, extra={"error": str(exc)})
            summary.status = "api_error"
            summary.errors = 1
            return summary

        counts = {RESULT_PASS: 0, RESULT_FAIL: 0, RESULT_INSUFFICIENT: 0, RESULT_ERROR: 0}
        summary.jobs_scanned = len(jobs)
        summary.appointments_scanned = sum(job.related_counts.get("appointments", 0) for job in jobs)
        summary.invoices_scanned = sum(job.related_counts.get("invoices", 0) for job in jobs)
        summary.invoice_items_scanned = sum(job.related_counts.get("invoice_items", 0) for job in jobs)
        summary.estimates_scanned = sum(job.related_counts.get("estimates", 0) + job.related_counts.get("opportunities", 0) for job in jobs)
        summary.notes_scanned = sum(job.related_counts.get("notes", 0) for job in jobs)
        summary.photos_scanned = sum(job.related_counts.get("photos", 0) for job in jobs)
        summary.forms_scanned = sum(job.related_counts.get("forms", 0) for job in jobs)
        summary.equipment_records_scanned = sum(job.related_counts.get("equipment", 0) for job in jobs)
        summary.purchase_orders_scanned = sum(job.related_counts.get("purchase_orders", 0) for job in jobs)
        summary.technician_time_records_scanned = sum(job.related_counts.get("technician_time_records", 0) for job in jobs)
        for job in jobs:
            for category in job.missing_data:
                summary.missing_data_category_counts[category] = summary.missing_data_category_counts.get(category, 0) + 1
        for job in jobs:
            try:
                for result in self._evaluate_job(job):
                    summary.rules_evaluated += 1
                    counts[result.status] = counts.get(result.status, 0) + 1
                    summary.result_counts[result.status] = summary.result_counts.get(result.status, 0) + 1
                    if result.status == RESULT_FAIL:
                        summary.violations_detected += 1
                        destination = result.recommended_alert_recipient or "slack audit channel"
                        summary.alert_destination_counts[destination] = summary.alert_destination_counts.get(destination, 0) + 1
                        alert_status = self._record_and_alert(job, result)
                        if alert_status == "sent":
                            summary.alerts_sent += 1
                        elif alert_status == "would_send":
                            summary.alerts_would_send += 1
                        elif alert_status == "deduped":
                            summary.alerts_skipped_dedupe += 1
                    elif result.status == RESULT_PASS:
                        if self.db.resolve_service_titan_violation(result.violation_key):
                            logger.info("servicetitan_violation_resolved", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
                    elif result.status == RESULT_INSUFFICIENT:
                        summary.insufficient_data_by_rule[result.rule_id] = summary.insufficient_data_by_rule.get(result.rule_id, 0) + 1
                        logger.info(
                            "servicetitan_rule_insufficient_data",
                            extra={"job_id": job.job_id, "rule_id": result.rule_id, "required_fields": list(result.required_fields)},
                        )
                    elif result.status == RESULT_ERROR:
                        summary.errors += 1
                        logger.warning("servicetitan_rule_error", extra={"job_id": job.job_id, "rule_id": result.rule_id, "explanation": result.explanation})
            except Exception as exc:
                counts[RESULT_ERROR] = counts.get(RESULT_ERROR, 0) + 1
                summary.errors += 1
                logger.warning("servicetitan_job_audit_failed", exc_info=True, extra={"job_id": job.job_id, "error": str(exc)})

        max_modified = max((job.modified_on for job in jobs if job.modified_on), default=now)
        if summary.dry_run:
            logger.info("servicetitan_audit_dry_run_checkpoint_skipped", extra={"max_modified": max_modified.astimezone(timezone.utc).isoformat()})
        else:
            self.db.set_kv("servicetitan_audit_last_processed", max_modified.astimezone(timezone.utc).isoformat())
        self.db.log_run_complete(
            run_id,
            "completed",
            {
                "jobs_seen": len(jobs),
                "appointments_seen": summary.appointments_scanned,
                "invoices_seen": summary.invoices_scanned,
                "invoice_items_seen": summary.invoice_items_scanned,
                "estimates_seen": summary.estimates_scanned,
                "notes_seen": summary.notes_scanned,
                "photos_seen": summary.photos_scanned,
                "forms_seen": summary.forms_scanned,
                "equipment_records_seen": summary.equipment_records_scanned,
                "purchase_orders_seen": summary.purchase_orders_scanned,
                "technician_time_records_seen": summary.technician_time_records_scanned,
                "rules_evaluated": summary.rules_evaluated,
                "violations_detected": summary.violations_detected,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "counts": counts,
                "alert_destination_counts": summary.alert_destination_counts,
                "insufficient_data_by_rule": summary.insufficient_data_by_rule,
                "missing_data_category_counts": summary.missing_data_category_counts,
                "since": since.isoformat(),
                "dry_run": summary.dry_run,
            },
        )
        logger.info(
            "servicetitan_audit_completed",
            extra={
                "jobs_seen": len(jobs),
                "appointments_seen": summary.appointments_scanned,
                "invoices_seen": summary.invoices_scanned,
                "invoice_items_seen": summary.invoice_items_scanned,
                "estimates_seen": summary.estimates_scanned,
                "notes_seen": summary.notes_scanned,
                "photos_seen": summary.photos_scanned,
                "forms_seen": summary.forms_scanned,
                "equipment_records_seen": summary.equipment_records_scanned,
                "purchase_orders_seen": summary.purchase_orders_scanned,
                "technician_time_records_seen": summary.technician_time_records_scanned,
                "rules_evaluated": summary.rules_evaluated,
                "violations_detected": summary.violations_detected,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "errors": summary.errors,
                "dry_run": summary.dry_run,
                "missing_data_category_counts": summary.missing_data_category_counts,
                **counts,
            },
        )
        return summary

    def _evaluate_job(self, job: ServiceTitanJob) -> list[RuleResult]:
        return [rule.run(job, self.settings) for rule in active_service_titan_rules(self.settings)]

    def _record_and_alert(self, job: ServiceTitanJob, result: RuleResult) -> str:
        metadata = {
            "explanation": result.explanation,
            "recommended_action": result.recommended_action,
            "job_number": job.job_number,
            "job_status": job.status,
            "appointment_id": job.appointment_id,
            "technician_name": job.technician_name,
            "dispatcher_name": job.dispatcher_name,
            "alert_recipient": result.recommended_alert_recipient,
            "delivery": result.delivery,
            "handbook_source": result.handbook_source,
            "rule_metadata": result.metadata,
        }
        if self.settings.service_titan_audit_dry_run:
            existing = self.db.get_service_titan_violation(result.violation_key)
            if existing and existing.get("alert_sent_at"):
                logger.info("servicetitan_duplicate_alert_suppressed", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
                return "deduped"
            logger.info(
                "servicetitan_alert_dry_run",
                extra={"violation_key": result.violation_key, "rule_id": result.rule_id, "severity": result.severity, "job_id": job.job_id},
            )
            return "would_send"
        record = self.db.upsert_service_titan_violation(
            violation_key=result.violation_key,
            service_titan_job_id=job.job_id,
            appointment_id=job.appointment_id,
            technician_id=job.technician_id,
            technician_name=job.technician_name,
            dispatcher_id=job.dispatcher_id,
            dispatcher_name=job.dispatcher_name,
            rule_id=result.rule_id,
            ruleset=result.ruleset,
            severity=result.severity,
            title=result.title,
            description=result.description,
            recommended_action=result.recommended_action,
            metadata=metadata,
        )
        if record.get("alert_sent_at"):
            logger.info("servicetitan_duplicate_alert_suppressed", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
            return "deduped"
        logger.info("servicetitan_violation_recorded", extra={"violation_key": result.violation_key, "rule_id": result.rule_id, "severity": result.severity})
        channel = self.settings.slack_alert_channel_id or self.settings.slack_marketing_ops_channel_id
        ts = self.slack.post_message(channel, self._alert_text(job, result))
        if not ts:
            logger.warning("servicetitan_alert_slack_failed", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
            return "failed"
        self.db.mark_service_titan_alert_sent(result.violation_key)
        logger.info("servicetitan_alert_sent", extra={"violation_key": result.violation_key, "rule_id": result.rule_id, "severity": result.severity})
        return "sent"

    def _alert_text(self, job: ServiceTitanJob, result: RuleResult) -> str:
        lines = [
            f"*ServiceTitan Operations Audit* - {result.severity.upper()}",
            f"*Ruleset:* {result.ruleset}",
            f"*Rule:* {result.title}",
            f"*Job:* {job.job_number or job.job_id}",
            f"*Destination:* {result.recommended_alert_recipient}",
            f"*Delivery:* {result.delivery}",
        ]
        if self.settings.service_titan_alert_include_customer_name and job.customer_name:
            lines.append(f"*Customer:* {job.customer_name}")
        if job.technician_name:
            lines.append(f"*Technician:* {job.technician_name}")
        if job.dispatcher_name:
            lines.append(f"*Dispatcher:* {job.dispatcher_name}")
        if job.arrival_window_start:
            window = self._format_dt(job.arrival_window_start)
            if job.arrival_window_end:
                window = f"{window} to {self._format_dt(job.arrival_window_end)}"
            lines.append(f"*Arrival window:* {window}")
        if job.arrived_at:
            lines.append(f"*Arrived:* {self._format_dt(job.arrived_at)}")
        if job.invoice_total is not None:
            lines.append(f"*Invoice total:* {job.invoice_total}")
        lines.extend(
            [
                f"*Issue:* {result.explanation}",
                f"*Recommended action:* {result.recommended_action}",
            ]
        )
        if job.url:
            lines.append(f"*ServiceTitan:* {job.url}")
        return "\n".join(lines)

    def _format_dt(self, value: datetime) -> str:
        return value.astimezone(ZoneInfo(self.settings.service_titan_audit_timezone)).isoformat()

    def _poll_since(self, now: datetime) -> datetime:
        previous = self.db.get_kv("servicetitan_audit_last_processed")
        if not previous:
            return now.astimezone(timezone.utc) - timedelta(minutes=self.settings.service_titan_audit_lookback_minutes)
        try:
            parsed = parse_notion_datetime(previous)
        except ValueError:
            return now.astimezone(timezone.utc) - timedelta(minutes=self.settings.service_titan_audit_lookback_minutes)
        return parsed.astimezone(timezone.utc) - timedelta(seconds=self.settings.service_titan_audit_overlap_seconds)


class ServiceTitanAuditLoop:
    def __init__(self, settings: Settings, audit_once: object) -> None:
        self.settings = settings
        self.audit_once = audit_once

    def run_loop(self, stop_event: threading.Event) -> None:
        if not self.settings.service_titan_audit_enabled:
            logger.info("servicetitan_audit_loop_disabled")
            return
        logger.info("servicetitan_audit_loop_started", extra={"interval_seconds": self.settings.service_titan_audit_poll_interval_seconds})
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.audit_once()
            except Exception:
                logger.exception("servicetitan_audit_cycle_failed")
            elapsed = time.monotonic() - started
            wait_seconds = max(1, self.settings.service_titan_audit_poll_interval_seconds - elapsed)
            stop_event.wait(wait_seconds)
        logger.info("servicetitan_audit_loop_stopped")
