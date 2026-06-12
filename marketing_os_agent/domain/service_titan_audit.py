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
from .service_titan_rules import (
    RESULT_ERROR,
    RESULT_FAIL,
    RESULT_INSUFFICIENT,
    RESULT_NOT_APPLICABLE,
    RESULT_PASS,
    RULESET_SALES,
    RuleResult,
    active_service_titan_rules,
)


logger = logging.getLogger(__name__)


@dataclass
class ServiceTitanAuditSummary:
    status: str = "completed"
    dry_run: bool = False
    backfill_alerts: bool = False
    baseline_initialized: bool = False
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
    sales_jobs_scanned: int = 0
    sales_rules_evaluated: int = 0
    sales_pass: int = 0
    sales_fail: int = 0
    sales_insufficient_data: int = 0
    sales_not_applicable: int = 0
    sales_alerts_sent: int = 0
    sales_alerts_would_send: int = 0
    result_counts: dict[str, int] = field(default_factory=dict)
    insufficient_data_by_rule: dict[str, int] = field(default_factory=dict)
    not_applicable_by_rule: dict[str, int] = field(default_factory=dict)
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
            f"- backfill_alerts: {self.backfill_alerts}",
            f"- baseline_initialized: {self.baseline_initialized}",
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
            f"- sales jobs scanned: {self.sales_jobs_scanned}",
            f"- sales rules evaluated: {self.sales_rules_evaluated}",
            f"- sales pass: {self.sales_pass}",
            f"- sales fail: {self.sales_fail}",
            f"- sales insufficient_data: {self.sales_insufficient_data}",
            f"- sales not_applicable: {self.sales_not_applicable}",
            f"- sales alerts that would have been sent: {self.sales_alerts_would_send}",
            f"- sales alerts sent: {self.sales_alerts_sent}",
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
        if self.not_applicable_by_rule:
            lines.append("- not_applicable by rule:")
            for rule_id, count in sorted(self.not_applicable_by_rule.items()):
                lines.append(f"  - {rule_id}: {count}")
            lines.append(f"- rules skipped due to not_applicable: {sum(self.not_applicable_by_rule.values())}")
        if self.result_counts:
            lines.append("- false-positive prevention summary:")
            lines.append(f"  - insufficient_data suppressed from alerts: {self.result_counts.get(RESULT_INSUFFICIENT, 0)}")
            lines.append(f"  - not_applicable suppressed from alerts: {self.result_counts.get(RESULT_NOT_APPLICABLE, 0)}")
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
        summary = ServiceTitanAuditSummary(
            dry_run=self.settings.service_titan_audit_dry_run,
            backfill_alerts=self.settings.service_titan_audit_backfill_alerts,
        )
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
        previous_checkpoint = self.db.get_kv("servicetitan_audit_last_processed")
        if self._should_initialize_baseline(previous_checkpoint):
            baseline_checkpoint = now.astimezone(timezone.utc) + timedelta(seconds=self.settings.service_titan_audit_overlap_seconds)
            self.db.set_kv("servicetitan_audit_last_processed", baseline_checkpoint.isoformat())
            summary.status = "baseline_initialized"
            summary.baseline_initialized = True
            details = {
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "baseline_checkpoint": baseline_checkpoint.isoformat(),
                "alerts_sent": 0,
                "alerts_would_send": 0,
            }
            self.db.log_run_complete(run_id, "baseline_initialized", details)
            logger.info("servicetitan_audit_baseline_initialized", extra=details)
            return summary
        since = self._poll_since(now, previous_checkpoint)
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

        counts = {RESULT_PASS: 0, RESULT_FAIL: 0, RESULT_INSUFFICIENT: 0, RESULT_NOT_APPLICABLE: 0, RESULT_ERROR: 0}
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
        sales_job_ids: set[str] = set()
        for job in jobs:
            try:
                results = self._evaluate_job(job)
                if self.settings.service_titan_audit_debug_fields:
                    self._log_sales_debug(job, results)
                for result in results:
                    summary.rules_evaluated += 1
                    counts[result.status] = counts.get(result.status, 0) + 1
                    summary.result_counts[result.status] = summary.result_counts.get(result.status, 0) + 1
                    if result.ruleset == RULESET_SALES:
                        summary.sales_rules_evaluated += 1
                        if result.status != RESULT_NOT_APPLICABLE:
                            sales_job_ids.add(job.job_id)
                        if result.status == RESULT_PASS:
                            summary.sales_pass += 1
                        elif result.status == RESULT_FAIL:
                            summary.sales_fail += 1
                        elif result.status == RESULT_INSUFFICIENT:
                            summary.sales_insufficient_data += 1
                        elif result.status == RESULT_NOT_APPLICABLE:
                            summary.sales_not_applicable += 1
                    if result.status == RESULT_FAIL:
                        summary.violations_detected += 1
                        destination = result.recommended_alert_recipient or "slack audit channel"
                        summary.alert_destination_counts[destination] = summary.alert_destination_counts.get(destination, 0) + 1
                        alert_status = self._record_and_alert(job, result)
                        if alert_status == "sent":
                            summary.alerts_sent += 1
                            if result.ruleset == RULESET_SALES:
                                summary.sales_alerts_sent += 1
                        elif alert_status == "would_send":
                            summary.alerts_would_send += 1
                            if result.ruleset == RULESET_SALES:
                                summary.sales_alerts_would_send += 1
                        elif alert_status == "deduped":
                            summary.alerts_skipped_dedupe += 1
                    elif result.status == RESULT_PASS:
                        if not summary.dry_run and self.db.resolve_service_titan_violation(result.violation_key):
                            logger.info("servicetitan_violation_resolved", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
                    elif result.status == RESULT_NOT_APPLICABLE:
                        summary.not_applicable_by_rule[result.rule_id] = summary.not_applicable_by_rule.get(result.rule_id, 0) + 1
                        if not summary.dry_run and self.db.resolve_service_titan_violation(result.violation_key):
                            logger.info("servicetitan_violation_resolved_not_applicable", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
                        logger.info(
                            "servicetitan_rule_not_applicable",
                            extra={"job_id": job.job_id, "rule_id": result.rule_id, "explanation": result.explanation},
                        )
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
        summary.sales_jobs_scanned = len(sales_job_ids)

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
                "sales_jobs_seen": summary.sales_jobs_scanned,
                "sales_rules_evaluated": summary.sales_rules_evaluated,
                "sales_pass": summary.sales_pass,
                "sales_fail": summary.sales_fail,
                "sales_insufficient_data": summary.sales_insufficient_data,
                "sales_not_applicable": summary.sales_not_applicable,
                "sales_alerts_sent": summary.sales_alerts_sent,
                "sales_alerts_would_send": summary.sales_alerts_would_send,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "counts": counts,
                "alert_destination_counts": summary.alert_destination_counts,
                "insufficient_data_by_rule": summary.insufficient_data_by_rule,
                "not_applicable_by_rule": summary.not_applicable_by_rule,
                "missing_data_category_counts": summary.missing_data_category_counts,
                "since": since.isoformat(),
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "baseline_initialized": summary.baseline_initialized,
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
                "sales_jobs_seen": summary.sales_jobs_scanned,
                "sales_rules_evaluated": summary.sales_rules_evaluated,
                "sales_pass": summary.sales_pass,
                "sales_fail": summary.sales_fail,
                "sales_insufficient_data": summary.sales_insufficient_data,
                "sales_not_applicable": summary.sales_not_applicable,
                "sales_alerts_sent": summary.sales_alerts_sent,
                "sales_alerts_would_send": summary.sales_alerts_would_send,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "errors": summary.errors,
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "baseline_initialized": summary.baseline_initialized,
                "missing_data_category_counts": summary.missing_data_category_counts,
                "not_applicable_by_rule": summary.not_applicable_by_rule,
                **counts,
            },
        )
        return summary

    def _evaluate_job(self, job: ServiceTitanJob) -> list[RuleResult]:
        return [rule.run(job, self.settings) for rule in active_service_titan_rules(self.settings)]

    def _log_sales_debug(self, job: ServiceTitanJob, results: list[RuleResult]) -> None:
        sales_results = [result for result in results if result.ruleset == RULESET_SALES]
        if not sales_results or all(result.status == RESULT_NOT_APPLICABLE for result in sales_results):
            return
        insufficient = {
            result.rule_id: result.explanation
            for result in sales_results
            if result.status == RESULT_INSUFFICIENT
        }
        logger.info(
            "servicetitan_sales_field_availability",
            extra={
                "job_id": job.job_id,
                "job_number": job.job_number,
                "business_unit": job.business_unit_name or job.business_unit_id,
                "job_type": job.job_type_name or job.job_type_id,
                "status": job.status,
                "has_appointment_window": bool(job.arrival_window_start and job.arrival_window_end),
                "has_arrival_time": bool(job.arrived_at),
                "estimates_count": job.related_counts.get("estimates", 0),
                "opportunities_count": job.related_counts.get("opportunities", 0),
                "options_count": job.estimate_count,
                "attachments_count": job.related_counts.get("attachments", 0),
                "photos_count": job.photo_count,
                "forms_count": job.related_counts.get("forms", 0),
                "sales_rule_statuses": {result.rule_id: result.status for result in sales_results},
                "sales_scope_decisions": {
                    result.rule_id: result.metadata.get("scope_decision", "")
                    for result in sales_results
                },
                "sales_rule_insufficient_reasons": insufficient,
                "missing_data_fields": sorted(job.missing_data),
            },
        )

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
        if "options_count" in result.metadata:
            required = result.metadata.get("required_options_count")
            suffix = f" / required {required}" if required is not None else ""
            lines.append(f"*Options count:* {result.metadata['options_count']}{suffix}")
        if "photos_count" in result.metadata:
            lines.append(f"*Photos count:* {result.metadata['photos_count']}")
        if "arrival_first_half_cutoff" in result.metadata:
            lines.append(f"*First-half cutoff:* {result.metadata['arrival_first_half_cutoff']}")
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

    def _should_initialize_baseline(self, previous_checkpoint: str | None) -> bool:
        return bool(
            not previous_checkpoint
            and not self.settings.service_titan_audit_dry_run
            and not self.settings.service_titan_audit_backfill_alerts
        )

    def _poll_since(self, now: datetime, previous: str | None = None) -> datetime:
        if previous is None:
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
        startup_delay = max(0, self.settings.service_titan_audit_startup_delay_seconds)
        if startup_delay:
            logger.info("servicetitan_audit_startup_delay", extra={"delay_seconds": startup_delay})
            if stop_event.wait(startup_delay):
                logger.info("servicetitan_audit_loop_stopped")
                return
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.audit_once()
            except Exception:
                logger.exception("servicetitan_audit_cycle_failed")
            elapsed = time.monotonic() - started
            wait_seconds = self._wait_seconds_after_cycle(elapsed)
            stop_event.wait(wait_seconds)
        logger.info("servicetitan_audit_loop_stopped")

    def _wait_seconds_after_cycle(self, elapsed_seconds: float) -> float:
        return max(1, self.settings.service_titan_audit_poll_interval_seconds - elapsed_seconds)
