from __future__ import annotations

import logging
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..clients.servicetitan import ServiceTitanApiError, ServiceTitanClient, ServiceTitanJob
from ..clients.slack import SlackClient
from ..config import DEFAULT_SERVICE_TITAN_BUSINESS_UNIT_LABELS, Settings
from ..models import parse_notion_datetime
from ..persistence import Persistence
from .service_titan_rules import (
    RESULT_ERROR,
    RESULT_FAIL,
    RESULT_INSUFFICIENT,
    RESULT_NOT_APPLICABLE,
    RESULT_PASS,
    RULESET_DISPATCHER,
    RULESET_HVAC,
    RULESET_PLUMBING,
    RULESET_SALES,
    RuleResult,
    active_service_titan_rules,
)


logger = logging.getLogger(__name__)


def service_titan_business_unit_classification(settings: Settings, business_unit_id: str, business_unit_name: str = "") -> dict[str, str]:
    clean_id = (business_unit_id or "").strip()
    labels = {
        **DEFAULT_SERVICE_TITAN_BUSINESS_UNIT_LABELS,
        **settings.service_titan_business_unit_labels,
    }
    label = labels.get(clean_id) if clean_id else ""
    return {
        "label": label or "Unknown Business Unit",
        "id": clean_id or "<missing>",
        "name": (business_unit_name or "").strip() or "<missing>",
    }


@dataclass
class ServiceTitanAuditSummary:
    status: str = "completed"
    dry_run: bool = False
    backfill_alerts: bool = False
    checkpoint_ignored: bool = False
    baseline_initialized: bool = False
    raw_jobs_fetched: int = 0
    jobs_skipped_before_enrichment: int = 0
    jobs_enriched: int = 0
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
    sales_alerts_skipped_limit: int = 0
    hvac_jobs_scanned: int = 0
    hvac_rules_evaluated: int = 0
    hvac_pass: int = 0
    hvac_fail: int = 0
    hvac_insufficient_data: int = 0
    hvac_not_applicable: int = 0
    hvac_alerts_sent: int = 0
    hvac_alerts_would_send: int = 0
    hvac_alerts_skipped_limit: int = 0
    plumbing_jobs_scanned: int = 0
    plumbing_rules_evaluated: int = 0
    plumbing_pass: int = 0
    plumbing_fail: int = 0
    plumbing_insufficient_data: int = 0
    plumbing_not_applicable: int = 0
    plumbing_alerts_sent: int = 0
    plumbing_alerts_would_send: int = 0
    plumbing_alerts_skipped_limit: int = 0
    result_counts: dict[str, int] = field(default_factory=dict)
    insufficient_data_by_rule: dict[str, int] = field(default_factory=dict)
    not_applicable_by_rule: dict[str, int] = field(default_factory=dict)
    missing_data_category_counts: dict[str, int] = field(default_factory=dict)
    alert_destination_counts: dict[str, int] = field(default_factory=dict)
    alert_business_unit_counts: dict[str, int] = field(default_factory=dict)
    alerts_sent: int = 0
    alerts_would_send: int = 0
    alerts_skipped_dedupe: int = 0
    alerts_skipped_limit: int = 0
    alerts_failed: int = 0
    errors: int = 0
    config_errors: list[str] = field(default_factory=list)

    def to_lines(self) -> list[str]:
        lines = [
            f"ServiceTitan audit: {self.status}",
            f"- dry_run: {self.dry_run}",
            f"- backfill_alerts: {self.backfill_alerts}",
            f"- checkpoint_ignored: {self.checkpoint_ignored}",
            f"- baseline_initialized: {self.baseline_initialized}",
            f"ServiceTitan audit scope filter: raw={self.raw_jobs_fetched}, skipped_before_enrichment={self.jobs_skipped_before_enrichment}, enriched={self.jobs_enriched}",
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
            f"- sales alerts skipped due to max alert limit: {self.sales_alerts_skipped_limit}",
            f"- hvac jobs scanned: {self.hvac_jobs_scanned}",
            f"- hvac rules evaluated: {self.hvac_rules_evaluated}",
            f"- hvac pass: {self.hvac_pass}",
            f"- hvac fail: {self.hvac_fail}",
            f"- hvac insufficient_data: {self.hvac_insufficient_data}",
            f"- hvac not_applicable: {self.hvac_not_applicable}",
            f"- hvac alerts that would have been sent: {self.hvac_alerts_would_send}",
            f"- hvac alerts sent: {self.hvac_alerts_sent}",
            f"- hvac alerts skipped due to max alert limit: {self.hvac_alerts_skipped_limit}",
            f"- plumbing jobs scanned: {self.plumbing_jobs_scanned}",
            f"- plumbing rules evaluated: {self.plumbing_rules_evaluated}",
            f"- plumbing pass: {self.plumbing_pass}",
            f"- plumbing fail: {self.plumbing_fail}",
            f"- plumbing insufficient_data: {self.plumbing_insufficient_data}",
            f"- plumbing not_applicable: {self.plumbing_not_applicable}",
            f"- plumbing alerts that would have been sent: {self.plumbing_alerts_would_send}",
            f"- plumbing alerts sent: {self.plumbing_alerts_sent}",
            f"- plumbing alerts skipped due to max alert limit: {self.plumbing_alerts_skipped_limit}",
            f"- alerts sent: {self.alerts_sent}",
            f"- alerts that would have been sent: {self.alerts_would_send}",
            f"- alerts skipped due to dedupe: {self.alerts_skipped_dedupe}",
            f"- alerts skipped due to max alert limit: {self.alerts_skipped_limit}",
            f"- alerts failed: {self.alerts_failed}",
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
        if self.alert_business_unit_counts:
            lines.append("- alert business units:")
            for business_unit, count in sorted(self.alert_business_unit_counts.items()):
                lines.append(f"  - {business_unit}: {count}")
        if self.config_errors:
            lines.append("- config errors:")
            lines.extend(f"  - {item}" for item in self.config_errors)
        return lines


@dataclass
class ServiceTitanWeeklySummary:
    status: str = "completed"
    dry_run: bool = False
    period_start: datetime | None = None
    period_end: datetime | None = None
    lookback_days: int = 7
    total_violations: int = 0
    severity_counts: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    business_unit_counts: dict[str, int] = field(default_factory=dict)
    grouped_counts: list[dict[str, str | int]] = field(default_factory=list)
    slack_sent: bool = False
    slack_skipped_duplicate: bool = False
    slack_skipped_disabled: bool = False
    slack_skipped_dry_run: bool = False
    slack_failed: bool = False
    config_errors: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [
            f"ServiceTitan weekly summary: {self.status}",
            f"- dry_run: {self.dry_run}",
            f"- lookback_days: {self.lookback_days}",
            f"- period_start: {self.period_start.isoformat() if self.period_start else '<unknown>'}",
            f"- period_end: {self.period_end.isoformat() if self.period_end else '<unknown>'}",
            f"- total violations: {self.total_violations}",
            f"- slack sent: {self.slack_sent}",
            f"- slack skipped duplicate: {self.slack_skipped_duplicate}",
            f"- slack skipped disabled: {self.slack_skipped_disabled}",
            f"- slack skipped dry_run: {self.slack_skipped_dry_run}",
            f"- slack failed: {self.slack_failed}",
        ]
        if self.business_unit_counts:
            lines.append("- business unit counts:")
            for label, count in sorted(self.business_unit_counts.items()):
                lines.append(f"  - {label}: {count}")
        if self.severity_counts:
            lines.append("- severity counts:")
            for severity, count in sorted(self.severity_counts.items()):
                lines.append(f"  - {severity}: {count}")
        if self.status_counts:
            lines.append("- status counts:")
            for status, count in sorted(self.status_counts.items()):
                lines.append(f"  - {status}: {count}")
        if self.config_errors:
            lines.append("- config errors:")
            lines.extend(f"  - {item}" for item in self.config_errors)
        lines.extend(["", self.message_text()])
        return "\n".join(lines)

    def message_text(self) -> str:
        start = self.period_start.astimezone(timezone.utc).date().isoformat() if self.period_start else "<unknown>"
        end = self.period_end.astimezone(timezone.utc).date().isoformat() if self.period_end else "<unknown>"
        lines = [
            "ServiceTitan Weekly Audit Summary",
            f"Period: {start} -> {end}",
            "",
        ]
        if not self.grouped_counts:
            lines.append("No ServiceTitan audit violations were recorded for this period.")
        else:
            current_business_unit: tuple[str, str, str] | None = None
            current_ruleset: str | None = None
            for row in self.grouped_counts:
                business_unit = (str(row["business_unit_label"]), str(row["business_unit_id"]), str(row["business_unit_name"]))
                if business_unit != current_business_unit:
                    if current_business_unit is not None:
                        lines.append("")
                    lines.append(str(row["business_unit_label"]))
                    lines.append(f"BU ID: {row['business_unit_id']}")
                    lines.append(f"BU Name: {row['business_unit_name']}")
                    current_business_unit = business_unit
                    current_ruleset = None
                ruleset = str(row["ruleset"])
                if ruleset != current_ruleset:
                    lines.append(f"Ruleset: {ruleset}")
                    current_ruleset = ruleset
                lines.append(f"- {row['rule_id']} [{row['severity']}] {row['status']}: {row['count']}")
        lines.extend(["", "Totals:", f"- Violations: {self.total_violations}"])
        for severity, count in sorted(self.severity_counts.items()):
            lines.append(f"- {severity.title()}: {count}")
        for status, count in sorted(self.status_counts.items()):
            lines.append(f"- {status}: {count}")
        return "\n".join(lines)


class ServiceTitanWeeklySummaryService:
    def __init__(self, settings: Settings, db: Persistence, slack: SlackClient) -> None:
        self.settings = settings
        self.db = db
        self.slack = slack

    def run_once(self, now: datetime | None = None, *, require_enabled: bool = True) -> ServiceTitanWeeklySummary:
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        period_start = now_utc - timedelta(days=self.settings.service_titan_weekly_summary_lookback_days)
        summary = self.build_summary(period_start, now_utc)
        if require_enabled and not self.settings.service_titan_weekly_summary_enabled:
            summary.status = "disabled"
            summary.slack_skipped_disabled = True
            logger.info("servicetitan_weekly_summary_skipped_disabled")
            return summary

        logger.info(
            "servicetitan_weekly_summary_started",
            extra={
                "period_start": period_start.isoformat(),
                "period_end": now_utc.isoformat(),
                "lookback_days": self.settings.service_titan_weekly_summary_lookback_days,
                "dry_run": summary.dry_run,
            },
        )
        if self.settings.service_titan_audit_dry_run:
            summary.slack_skipped_dry_run = True
            logger.info(
                "servicetitan_weekly_summary_dry_run",
                extra={
                    "period_start": period_start.isoformat(),
                    "period_end": now_utc.isoformat(),
                    "total_violations": summary.total_violations,
                    "business_unit_counts": summary.business_unit_counts,
                },
            )
            return summary

        missing = self._missing_slack_config()
        if missing:
            summary.status = "config_error"
            summary.config_errors = missing
            logger.warning("servicetitan_weekly_summary_skipped_missing_config", extra={"missing": missing})
            return summary

        dedupe_key = self._dedupe_key(summary)
        if self.db.has_dedupe(dedupe_key):
            summary.slack_skipped_duplicate = True
            logger.info(
                "servicetitan_weekly_summary_duplicate_suppressed",
                extra={
                    "dedupe_key": dedupe_key,
                    "period_start": period_start.isoformat(),
                    "period_end": now_utc.isoformat(),
                },
            )
            return summary

        ts = self.slack.post_message(self.settings.slack_alert_channel_id, summary.message_text())
        if not ts:
            summary.status = "slack_error"
            summary.slack_failed = True
            logger.warning(
                "servicetitan_weekly_summary_slack_failed",
                extra={"period_start": period_start.isoformat(), "period_end": now_utc.isoformat()},
            )
            return summary
        self.db.mark_dedupe(dedupe_key, "servicetitan_weekly_summary")
        summary.slack_sent = True
        logger.info(
            "servicetitan_weekly_summary_sent",
            extra={
                "period_start": period_start.isoformat(),
                "period_end": now_utc.isoformat(),
                "total_violations": summary.total_violations,
                "business_unit_counts": summary.business_unit_counts,
            },
        )
        return summary

    def build_summary(self, period_start: datetime, period_end: datetime) -> ServiceTitanWeeklySummary:
        rows = self.db.get_service_titan_violations_between(
            period_start.astimezone(timezone.utc).isoformat(),
            period_end.astimezone(timezone.utc).isoformat(),
        )
        grouped: dict[tuple[str, str, str, str, str, str, str], int] = {}
        severity_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        business_unit_counts: dict[str, int] = {}
        for row in rows:
            metadata = _json_dict(row.get("metadata_json"))
            business_unit_id = str(metadata.get("business_unit_id") or "")
            business_unit_name = str(metadata.get("business_unit_name") or "")
            business_unit = service_titan_business_unit_classification(self.settings, business_unit_id, business_unit_name)
            ruleset = str(row.get("ruleset") or "Unknown Ruleset")
            rule_id = str(row.get("rule_id") or "unknown_rule")
            severity = str(row.get("severity") or "unknown").lower()
            status = str(row.get("status") or "unknown").lower()
            key = (
                business_unit["label"],
                business_unit["id"],
                business_unit["name"],
                ruleset,
                rule_id,
                severity,
                status,
            )
            grouped[key] = grouped.get(key, 0) + 1
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            status_counts[status] = status_counts.get(status, 0) + 1
            business_unit_counts[business_unit["label"]] = business_unit_counts.get(business_unit["label"], 0) + 1
        grouped_counts = [
            {
                "business_unit_label": key[0],
                "business_unit_id": key[1],
                "business_unit_name": key[2],
                "ruleset": key[3],
                "rule_id": key[4],
                "severity": key[5],
                "status": key[6],
                "count": count,
            }
            for key, count in sorted(grouped.items())
        ]
        summary = ServiceTitanWeeklySummary(
            dry_run=self.settings.service_titan_audit_dry_run,
            period_start=period_start.astimezone(timezone.utc),
            period_end=period_end.astimezone(timezone.utc),
            lookback_days=self.settings.service_titan_weekly_summary_lookback_days,
            total_violations=len(rows),
            severity_counts=severity_counts,
            status_counts=status_counts,
            business_unit_counts=business_unit_counts,
            grouped_counts=grouped_counts,
        )
        logger.info(
            "servicetitan_weekly_summary_built",
            extra={
                "period_start": summary.period_start.isoformat(),
                "period_end": summary.period_end.isoformat(),
                "total_violations": summary.total_violations,
                "business_unit_counts": summary.business_unit_counts,
                "severity_counts": summary.severity_counts,
                "status_counts": summary.status_counts,
            },
        )
        return summary

    def should_run_at(self, now: datetime) -> bool:
        if not self.settings.service_titan_weekly_summary_enabled:
            return False
        day_index = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
        return (
            now.weekday() == day_index[self.settings.service_titan_weekly_summary_day]
            and now.hour == self.settings.service_titan_weekly_summary_hour
            and now.minute == 0
        )

    def _missing_slack_config(self) -> list[str]:
        missing = []
        if not self.settings.slack_bot_token:
            missing.append("SLACK_BOT_TOKEN")
        if not self.settings.slack_alert_channel_id:
            missing.append("SLACK_ALERT_CHANNEL_ID")
        return missing

    def _dedupe_key(self, summary: ServiceTitanWeeklySummary) -> str:
        start = summary.period_start.date().isoformat() if summary.period_start else "unknown"
        end = summary.period_end.date().isoformat() if summary.period_end else "unknown"
        return f"servicetitan_weekly_summary:{start}:{end}:{summary.lookback_days}"


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
        ignore_checkpoint = self._should_ignore_checkpoint_once(require_enabled=require_enabled)
        summary.checkpoint_ignored = ignore_checkpoint
        if ignore_checkpoint:
            logger.warning(
                "servicetitan_audit_checkpoint_ignored_once",
                extra={
                    "previous_checkpoint_present": bool(previous_checkpoint),
                    "dry_run": summary.dry_run,
                    "backfill_alerts": summary.backfill_alerts,
                    "max_alerts_per_cycle": self.settings.service_titan_audit_max_alerts_per_cycle,
                },
            )
        if not ignore_checkpoint and self._should_initialize_baseline(previous_checkpoint):
            baseline_checkpoint = now.astimezone(timezone.utc) + timedelta(seconds=self.settings.service_titan_audit_overlap_seconds)
            self.db.set_kv("servicetitan_audit_last_processed", baseline_checkpoint.isoformat())
            summary.status = "baseline_initialized"
            summary.baseline_initialized = True
            details = {
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "checkpoint_ignored": summary.checkpoint_ignored,
                "baseline_checkpoint": baseline_checkpoint.isoformat(),
                "alerts_sent": 0,
                "alerts_would_send": 0,
            }
            self.db.log_run_complete(run_id, "baseline_initialized", details)
            logger.info("servicetitan_audit_baseline_initialized", extra=details)
            return summary
        if ignore_checkpoint:
            since = now.astimezone(timezone.utc) - timedelta(minutes=self.settings.service_titan_audit_lookback_minutes)
        else:
            since = self._poll_since(now, previous_checkpoint)
        logger.info(
            "servicetitan_audit_cycle_started",
            extra={
                "since": since.isoformat(),
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "checkpoint_ignored": summary.checkpoint_ignored,
                "max_alerts_per_cycle": self.settings.service_titan_audit_max_alerts_per_cycle,
            },
        )
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

        scope_filter_stats = getattr(self.client, "last_scope_filter_stats", {}) or {}
        summary.raw_jobs_fetched = int(scope_filter_stats.get("raw_jobs_fetched") or len(jobs))
        summary.jobs_skipped_before_enrichment = int(scope_filter_stats.get("jobs_skipped_before_enrichment") or 0)
        summary.jobs_enriched = int(scope_filter_stats.get("jobs_enriched") or len(jobs))
        logger.info(
            "servicetitan_audit_scope_filter",
            extra={
                "raw_jobs_fetched": summary.raw_jobs_fetched,
                "jobs_skipped_before_enrichment": summary.jobs_skipped_before_enrichment,
                "jobs_enriched": summary.jobs_enriched,
            },
        )
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
        hvac_job_ids: set[str] = set()
        plumbing_job_ids: set[str] = set()
        alert_attempts = 0
        for job in jobs:
            try:
                results = self._evaluate_job(job)
                if self.settings.service_titan_audit_debug_fields:
                    self._log_sales_debug(job, results)
                    self._log_hvac_debug(job, results)
                    self._log_plumbing_debug(job, results)
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
                    if result.ruleset == RULESET_HVAC:
                        summary.hvac_rules_evaluated += 1
                        if result.status != RESULT_NOT_APPLICABLE:
                            hvac_job_ids.add(job.job_id)
                        if result.status == RESULT_PASS:
                            summary.hvac_pass += 1
                        elif result.status == RESULT_FAIL:
                            summary.hvac_fail += 1
                        elif result.status == RESULT_INSUFFICIENT:
                            summary.hvac_insufficient_data += 1
                        elif result.status == RESULT_NOT_APPLICABLE:
                            summary.hvac_not_applicable += 1
                    if result.ruleset == RULESET_PLUMBING:
                        summary.plumbing_rules_evaluated += 1
                        if result.status != RESULT_NOT_APPLICABLE:
                            plumbing_job_ids.add(job.job_id)
                        if result.status == RESULT_PASS:
                            summary.plumbing_pass += 1
                        elif result.status == RESULT_FAIL:
                            summary.plumbing_fail += 1
                        elif result.status == RESULT_INSUFFICIENT:
                            summary.plumbing_insufficient_data += 1
                        elif result.status == RESULT_NOT_APPLICABLE:
                            summary.plumbing_not_applicable += 1
                    if result.status == RESULT_FAIL:
                        summary.violations_detected += 1
                        destination = result.recommended_alert_recipient or "slack audit channel"
                        summary.alert_destination_counts[destination] = summary.alert_destination_counts.get(destination, 0) + 1
                        business_unit = self._business_unit_classification(job)
                        summary.alert_business_unit_counts[business_unit["label"]] = summary.alert_business_unit_counts.get(business_unit["label"], 0) + 1
                        alert_status = self._record_and_alert(
                            job,
                            result,
                            alert_limit_reached=self._alert_limit_reached(alert_attempts),
                        )
                        if alert_status == "sent":
                            alert_attempts += 1
                            summary.alerts_sent += 1
                            if result.ruleset == RULESET_SALES:
                                summary.sales_alerts_sent += 1
                            if result.ruleset == RULESET_HVAC:
                                summary.hvac_alerts_sent += 1
                            if result.ruleset == RULESET_PLUMBING:
                                summary.plumbing_alerts_sent += 1
                        elif alert_status == "failed":
                            alert_attempts += 1
                            summary.alerts_failed += 1
                        elif alert_status == "would_send":
                            summary.alerts_would_send += 1
                            if result.ruleset == RULESET_SALES:
                                summary.sales_alerts_would_send += 1
                            if result.ruleset == RULESET_HVAC:
                                summary.hvac_alerts_would_send += 1
                            if result.ruleset == RULESET_PLUMBING:
                                summary.plumbing_alerts_would_send += 1
                        elif alert_status == "deduped":
                            summary.alerts_skipped_dedupe += 1
                        elif alert_status == "limited":
                            summary.alerts_skipped_limit += 1
                            if result.ruleset == RULESET_SALES:
                                summary.sales_alerts_skipped_limit += 1
                            if result.ruleset == RULESET_HVAC:
                                summary.hvac_alerts_skipped_limit += 1
                            if result.ruleset == RULESET_PLUMBING:
                                summary.plumbing_alerts_skipped_limit += 1
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
        summary.hvac_jobs_scanned = len(hvac_job_ids)
        summary.plumbing_jobs_scanned = len(plumbing_job_ids)

        max_modified = max((job.modified_on for job in jobs if job.modified_on), default=now)
        if summary.dry_run:
            logger.info("servicetitan_audit_dry_run_checkpoint_skipped", extra={"max_modified": max_modified.astimezone(timezone.utc).isoformat()})
        elif summary.checkpoint_ignored:
            logger.warning(
                "servicetitan_audit_checkpoint_skipped_manual_backfill",
                extra={
                    "max_modified": max_modified.astimezone(timezone.utc).isoformat(),
                    "alerts_sent": summary.alerts_sent,
                    "alerts_skipped_limit": summary.alerts_skipped_limit,
                },
            )
        elif summary.alerts_failed or summary.alerts_skipped_limit:
            logger.warning(
                "servicetitan_audit_checkpoint_skipped_pending_alerts",
                extra={
                    "max_modified": max_modified.astimezone(timezone.utc).isoformat(),
                    "alerts_failed": summary.alerts_failed,
                    "alerts_skipped_limit": summary.alerts_skipped_limit,
                },
            )
        else:
            self.db.set_kv("servicetitan_audit_last_processed", max_modified.astimezone(timezone.utc).isoformat())
        self.db.log_run_complete(
            run_id,
            "completed",
            {
                "raw_jobs_fetched": summary.raw_jobs_fetched,
                "jobs_skipped_before_enrichment": summary.jobs_skipped_before_enrichment,
                "jobs_enriched": summary.jobs_enriched,
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
                "sales_alerts_skipped_limit": summary.sales_alerts_skipped_limit,
                "hvac_jobs_seen": summary.hvac_jobs_scanned,
                "hvac_rules_evaluated": summary.hvac_rules_evaluated,
                "hvac_pass": summary.hvac_pass,
                "hvac_fail": summary.hvac_fail,
                "hvac_insufficient_data": summary.hvac_insufficient_data,
                "hvac_not_applicable": summary.hvac_not_applicable,
                "hvac_alerts_sent": summary.hvac_alerts_sent,
                "hvac_alerts_would_send": summary.hvac_alerts_would_send,
                "hvac_alerts_skipped_limit": summary.hvac_alerts_skipped_limit,
                "plumbing_jobs_seen": summary.plumbing_jobs_scanned,
                "plumbing_rules_evaluated": summary.plumbing_rules_evaluated,
                "plumbing_pass": summary.plumbing_pass,
                "plumbing_fail": summary.plumbing_fail,
                "plumbing_insufficient_data": summary.plumbing_insufficient_data,
                "plumbing_not_applicable": summary.plumbing_not_applicable,
                "plumbing_alerts_sent": summary.plumbing_alerts_sent,
                "plumbing_alerts_would_send": summary.plumbing_alerts_would_send,
                "plumbing_alerts_skipped_limit": summary.plumbing_alerts_skipped_limit,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "alerts_skipped_limit": summary.alerts_skipped_limit,
                "alerts_failed": summary.alerts_failed,
                "counts": counts,
                "alert_destination_counts": summary.alert_destination_counts,
                "insufficient_data_by_rule": summary.insufficient_data_by_rule,
                "not_applicable_by_rule": summary.not_applicable_by_rule,
                "missing_data_category_counts": summary.missing_data_category_counts,
                "since": since.isoformat(),
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "checkpoint_ignored": summary.checkpoint_ignored,
                "baseline_initialized": summary.baseline_initialized,
            },
        )
        logger.info(
            "servicetitan_audit_completed",
            extra={
                "raw_jobs_fetched": summary.raw_jobs_fetched,
                "jobs_skipped_before_enrichment": summary.jobs_skipped_before_enrichment,
                "jobs_enriched": summary.jobs_enriched,
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
                "sales_alerts_skipped_limit": summary.sales_alerts_skipped_limit,
                "hvac_jobs_seen": summary.hvac_jobs_scanned,
                "hvac_rules_evaluated": summary.hvac_rules_evaluated,
                "hvac_pass": summary.hvac_pass,
                "hvac_fail": summary.hvac_fail,
                "hvac_insufficient_data": summary.hvac_insufficient_data,
                "hvac_not_applicable": summary.hvac_not_applicable,
                "hvac_alerts_sent": summary.hvac_alerts_sent,
                "hvac_alerts_would_send": summary.hvac_alerts_would_send,
                "hvac_alerts_skipped_limit": summary.hvac_alerts_skipped_limit,
                "plumbing_jobs_seen": summary.plumbing_jobs_scanned,
                "plumbing_rules_evaluated": summary.plumbing_rules_evaluated,
                "plumbing_pass": summary.plumbing_pass,
                "plumbing_fail": summary.plumbing_fail,
                "plumbing_insufficient_data": summary.plumbing_insufficient_data,
                "plumbing_not_applicable": summary.plumbing_not_applicable,
                "plumbing_alerts_sent": summary.plumbing_alerts_sent,
                "plumbing_alerts_would_send": summary.plumbing_alerts_would_send,
                "plumbing_alerts_skipped_limit": summary.plumbing_alerts_skipped_limit,
                "alerts_sent": summary.alerts_sent,
                "alerts_would_send": summary.alerts_would_send,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "alerts_skipped_limit": summary.alerts_skipped_limit,
                "alerts_failed": summary.alerts_failed,
                "errors": summary.errors,
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "checkpoint_ignored": summary.checkpoint_ignored,
                "baseline_initialized": summary.baseline_initialized,
                "missing_data_category_counts": summary.missing_data_category_counts,
                "not_applicable_by_rule": summary.not_applicable_by_rule,
                **counts,
            },
        )
        logger.info(
            "servicetitan_audit_cycle_completed",
            extra={
                "raw_jobs_fetched": summary.raw_jobs_fetched,
                "jobs_skipped_before_enrichment": summary.jobs_skipped_before_enrichment,
                "jobs_enriched": summary.jobs_enriched,
                "jobs_scanned": summary.jobs_scanned,
                "sales_jobs_scanned": summary.sales_jobs_scanned,
                "sales_pass": summary.sales_pass,
                "sales_fail": summary.sales_fail,
                "sales_alerts_would_send": summary.sales_alerts_would_send,
                "sales_alerts_sent": summary.sales_alerts_sent,
                "hvac_jobs_scanned": summary.hvac_jobs_scanned,
                "hvac_pass": summary.hvac_pass,
                "hvac_fail": summary.hvac_fail,
                "hvac_alerts_would_send": summary.hvac_alerts_would_send,
                "hvac_alerts_sent": summary.hvac_alerts_sent,
                "plumbing_jobs_scanned": summary.plumbing_jobs_scanned,
                "plumbing_pass": summary.plumbing_pass,
                "plumbing_fail": summary.plumbing_fail,
                "plumbing_alerts_would_send": summary.plumbing_alerts_would_send,
                "plumbing_alerts_sent": summary.plumbing_alerts_sent,
                "alerts_skipped_dedupe": summary.alerts_skipped_dedupe,
                "alerts_skipped_limit": summary.alerts_skipped_limit,
                "alerts_failed": summary.alerts_failed,
                "dry_run": summary.dry_run,
                "backfill_alerts": summary.backfill_alerts,
                "checkpoint_ignored": summary.checkpoint_ignored,
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

    def _log_hvac_debug(self, job: ServiceTitanJob, results: list[RuleResult]) -> None:
        hvac_results = [result for result in results if result.ruleset == RULESET_HVAC]
        if not hvac_results or all(result.status == RESULT_NOT_APPLICABLE for result in hvac_results):
            return
        insufficient = {
            result.rule_id: result.explanation
            for result in hvac_results
            if result.status == RESULT_INSUFFICIENT
        }
        logger.info(
            "servicetitan_hvac_field_availability",
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
                "invoices_count": job.related_counts.get("invoices", 0),
                "payments_count": job.payments_count,
                "invoice_status": job.invoice_status,
                "attachments_count": job.related_counts.get("attachments", 0),
                "photos_count": job.photo_count,
                "forms_count": job.related_counts.get("forms", 0),
                "hvac_rule_statuses": {result.rule_id: result.status for result in hvac_results},
                "hvac_scope_decisions": {
                    result.rule_id: result.metadata.get("scope_decision", "")
                    for result in hvac_results
                },
                "hvac_rule_insufficient_reasons": insufficient,
                "missing_data_fields": sorted(job.missing_data),
            },
        )

    def _log_plumbing_debug(self, job: ServiceTitanJob, results: list[RuleResult]) -> None:
        plumbing_results = [result for result in results if result.ruleset == RULESET_PLUMBING]
        if not plumbing_results or all(result.status == RESULT_NOT_APPLICABLE for result in plumbing_results):
            return
        insufficient = {
            result.rule_id: result.explanation
            for result in plumbing_results
            if result.status == RESULT_INSUFFICIENT
        }
        logger.info(
            "servicetitan_plumbing_field_availability",
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
                "invoices_count": job.related_counts.get("invoices", 0),
                "payments_count": job.payments_count,
                "invoice_status": job.invoice_status,
                "attachments_count": job.related_counts.get("attachments", 0),
                "photos_count": job.photo_count,
                "forms_count": job.related_counts.get("forms", 0),
                "plumbing_rule_statuses": {result.rule_id: result.status for result in plumbing_results},
                "plumbing_scope_decisions": {
                    result.rule_id: result.metadata.get("scope_decision", "")
                    for result in plumbing_results
                },
                "plumbing_rule_insufficient_reasons": insufficient,
                "missing_data_fields": sorted(job.missing_data),
            },
        )

    def _record_and_alert(self, job: ServiceTitanJob, result: RuleResult, *, alert_limit_reached: bool = False) -> str:
        metadata = {
            "explanation": result.explanation,
            "recommended_action": result.recommended_action,
            "job_number": job.job_number,
            "job_status": job.status,
            "appointment_id": job.appointment_id,
            "technician_name": job.technician_name,
            "dispatcher_name": job.dispatcher_name,
            "business_unit_label": self._business_unit_classification(job)["label"],
            "business_unit_id": job.business_unit_id,
            "business_unit_name": job.business_unit_name,
            "job_type_id": job.job_type_id,
            "job_type_name": job.job_type_name,
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
                extra={
                    "violation_key": result.violation_key,
                    "rule_id": result.rule_id,
                    "severity": result.severity,
                    "job_id": job.job_id,
                    "business_unit_label": self._business_unit_classification(job)["label"],
                    "business_unit_id": job.business_unit_id,
                    "business_unit_name": job.business_unit_name,
                    "job_type_id": job.job_type_id,
                    "job_type_name": job.job_type_name,
                },
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
        if alert_limit_reached:
            logger.warning(
                "servicetitan_alert_skipped_max_per_cycle",
                extra={
                    "violation_key": result.violation_key,
                    "rule_id": result.rule_id,
                    "severity": result.severity,
                    "max_alerts_per_cycle": self.settings.service_titan_audit_max_alerts_per_cycle,
                    "backfill_alerts": self.settings.service_titan_audit_backfill_alerts,
                },
            )
            return "limited"
        channel = self._alert_channel_for(result)
        if self.settings.service_titan_audit_backfill_alerts:
            logger.warning(
                "servicetitan_controlled_backfill_alert_attempt",
                extra={
                    "violation_key": result.violation_key,
                    "rule_id": result.rule_id,
                    "severity": result.severity,
                    "max_alerts_per_cycle": self.settings.service_titan_audit_max_alerts_per_cycle,
                    "manual_validation": self._controlled_backfill_manual_validation(),
                },
            )
        ts = self.slack.post_message(channel, self._alert_text(job, result))
        if not ts:
            logger.warning("servicetitan_alert_slack_failed", extra={"violation_key": result.violation_key, "rule_id": result.rule_id})
            return "failed"
        self.db.mark_service_titan_alert_sent(result.violation_key)
        logger.info("servicetitan_alert_sent", extra={"violation_key": result.violation_key, "rule_id": result.rule_id, "severity": result.severity})
        return "sent"

    def _alert_channel_for(self, result: RuleResult) -> str:
        if result.ruleset == RULESET_DISPATCHER and self.settings.dispatcher_audit_slack_channel_id:
            return self.settings.dispatcher_audit_slack_channel_id
        return self.settings.slack_alert_channel_id

    def _alert_limit_reached(self, alert_attempts: int) -> bool:
        if self.settings.service_titan_audit_dry_run:
            return False
        return alert_attempts >= self.settings.service_titan_audit_max_alerts_per_cycle

    def _controlled_backfill_manual_validation(self) -> bool:
        return bool(
            self.settings.service_titan_audit_backfill_alerts
            and self.settings.service_titan_audit_max_alerts_per_cycle == 1
        )

    def _alert_text(self, job: ServiceTitanJob, result: RuleResult) -> str:
        business_unit = self._business_unit_classification(job)
        title = self._friendly_rule_title(result)
        business_unit_label = business_unit["label"] if business_unit["label"] != "Unknown Business Unit" else "Unknown"
        lines = [f"{self._severity_icon(result.severity)} {result.severity.upper()} - {business_unit_label}: {title}"]
        if business_unit_label == "Unknown":
            lines.append("Business Unit: Unknown")
        if job.technician_name:
            lines.append(f"Technician: {job.technician_name}")
        if job.dispatcher_name:
            lines.append(f"Dispatcher: {job.dispatcher_name}")
        if job.job_type_name:
            lines.append(f"Job Type: {job.job_type_name}")
        if job.arrival_window_start:
            window = self._format_dt(job.arrival_window_start)
            if job.arrival_window_end:
                window = self._format_window(job.arrival_window_start, job.arrival_window_end)
            lines.append(f"Appointment: {window}")
        if job.arrived_at:
            lines.append(f"Arrived: {self._format_time(job.arrived_at)}")
        invoice_line = self._invoice_line(job, result)
        if invoice_line:
            lines.append(invoice_line)
        if "options_count" in result.metadata:
            required = result.metadata.get("required_options_count")
            if required is not None:
                lines.append(f"Options: {result.metadata['options_count']} of {required} required")
            else:
                lines.append(f"Options: {result.metadata['options_count']}")
        if "photos_count" in result.metadata:
            lines.append(f"Photos: {result.metadata['photos_count']}")
        if "forms_count" in result.metadata:
            lines.append(f"Forms: {result.metadata['forms_count']}")
        if "diagnosis_form_present" in result.metadata:
            lines.append(f"Diagnosis form present: {result.metadata['diagnosis_form_present']}")
        if "arrival_first_half_cutoff" in result.metadata:
            parsed_cutoff = parse_notion_datetime(str(result.metadata["arrival_first_half_cutoff"]))
            if parsed_cutoff:
                lines.append(f"First-half cutoff: {self._format_dt(parsed_cutoff)}")
        if "appointment_end" in result.metadata:
            parsed_end = parse_notion_datetime(str(result.metadata["appointment_end"]))
            if parsed_end:
                lines.append(f"Appointment ended: {self._format_dt(parsed_end)}")
        if "current_status" in result.metadata:
            lines.append(f"Current status: {result.metadata['current_status']}")
        lines.extend(
            [
                "",
                f"Issue: {result.explanation}",
                f"Action: {result.recommended_action}",
            ]
        )
        if job.url:
            lines.extend(["", "Open in ServiceTitan:", job.url])
        return "\n".join(lines)

    def _business_unit_classification(self, job: ServiceTitanJob) -> dict[str, str]:
        return service_titan_business_unit_classification(self.settings, job.business_unit_id, job.business_unit_name)

    def _job_type_label(self, job: ServiceTitanJob) -> str:
        job_type_id = (job.job_type_id or "").strip()
        job_type_name = (job.job_type_name or "").strip()
        if job_type_name and job_type_id:
            return f"{job_type_name} ({job_type_id})"
        if job_type_name:
            return job_type_name
        if job_type_id:
            return job_type_id
        return "Unknown Job Type"

    def _format_dt(self, value: datetime) -> str:
        localized = value.astimezone(ZoneInfo(self.settings.service_titan_audit_timezone))
        return localized.strftime("%b %d, %I:%M %p").replace(" 0", " ").replace(", 0", ", ")

    def _format_time(self, value: datetime) -> str:
        localized = value.astimezone(ZoneInfo(self.settings.service_titan_audit_timezone))
        return localized.strftime("%I:%M %p").lstrip("0")

    def _format_window(self, start: datetime, end: datetime) -> str:
        start_local = start.astimezone(ZoneInfo(self.settings.service_titan_audit_timezone))
        end_local = end.astimezone(ZoneInfo(self.settings.service_titan_audit_timezone))
        if start_local.date() == end_local.date():
            return f"{self._format_dt(start)}-{self._format_time(end)}"
        return f"{self._format_dt(start)}-{self._format_dt(end)}"

    def _severity_icon(self, severity: str) -> str:
        normalized = severity.strip().lower()
        if normalized == "high":
            return "🚨"
        if normalized == "medium":
            return "⚠️"
        return "ℹ️"

    def _friendly_rule_title(self, result: RuleResult) -> str:
        titles = {
            "sales_options_fewer_than_three": "Fewer Than 3 Options",
            "hvac_options_fewer_than_three": "Fewer Than 3 Options",
            "plumbing_options_fewer_than_three": "Fewer Than 3 Options",
            "hvac_payment_missing_on_completed_job": "Missing Payment",
            "plumbing_payment_missing_on_completed_job": "Missing Payment",
            "job_left_open_after_visit": "Job Still Open After Visit",
        }
        return titles.get(result.rule_id, result.title)

    def _invoice_line(self, job: ServiceTitanJob, result: RuleResult) -> str:
        invoice_total = result.metadata.get("invoice_total", job.invoice_total)
        invoice_balance = result.metadata.get("invoice_balance", job.invoice_balance)
        payment_total = result.metadata.get("payment_total", job.payment_total)
        invoice_status = str(result.metadata.get("invoice_status") or job.invoice_status or "").strip()
        if invoice_total is None and invoice_balance is None and payment_total is None and not invoice_status:
            return ""
        if invoice_total is not None and invoice_balance is not None:
            line = f"Invoice: {self._format_money(invoice_total)} total / {self._format_money(invoice_balance)} balance"
        elif invoice_total is not None:
            line = f"Invoice: {self._format_money(invoice_total)}"
        elif invoice_balance is not None:
            line = f"Invoice balance: {self._format_money(invoice_balance)}"
        else:
            line = ""
        details: list[str] = []
        if payment_total is not None:
            details.append(f"payment {self._format_money(payment_total)}")
        if invoice_status:
            details.append(f"status {invoice_status}")
        if details:
            line = f"{line} ({', '.join(details)})" if line else f"Invoice: {', '.join(details)}"
        return line

    def _format_money(self, value: object) -> str:
        try:
            return f"${float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    def _should_initialize_baseline(self, previous_checkpoint: str | None) -> bool:
        return bool(
            not previous_checkpoint
            and not self.settings.service_titan_audit_dry_run
            and not self.settings.service_titan_audit_backfill_alerts
        )

    def _should_ignore_checkpoint_once(self, *, require_enabled: bool) -> bool:
        if require_enabled:
            return False
        if not self.settings.service_titan_audit_ignore_checkpoint_once:
            return False
        if not self.settings.service_titan_audit_backfill_alerts:
            logger.warning("servicetitan_ignore_checkpoint_once_ignored_without_backfill")
            return False
        if not self.settings.service_titan_audit_dry_run and self.settings.service_titan_audit_max_alerts_per_cycle != 1:
            logger.warning(
                "servicetitan_ignore_checkpoint_once_ignored_without_one_alert_cap",
                extra={"max_alerts_per_cycle": self.settings.service_titan_audit_max_alerts_per_cycle},
            )
            return False
        return True

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


def _json_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
