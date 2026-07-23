from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..clients.servicetitan import ServiceTitanApiError, ServiceTitanClient, ServiceTitanJob
from ..clients.slack import SlackClient
from ..config import Settings
from ..persistence import Persistence
from .install_audit import (
    _appointment_end,
    _appointment_start,
    _format_money,
    _install_appointments,
    _payment_totals,
)


logger = logging.getLogger(__name__)

INSTALL_EVENING_REPORT_RUN_TYPE = "install_evening_report"


@dataclass
class InstallEveningReportSummary:
    status: str = "completed"
    enabled: bool = False
    dry_run: bool = True
    report_date: str = ""
    tomorrow_date: str = ""
    raw_jobs_fetched: int = 0
    jobs_skipped_out_of_scope: int = 0
    jobs_enriched: int = 0
    today_install_jobs: int = 0
    tomorrow_install_appointments: int = 0
    payment_records_available: int = 0
    payment_records_unavailable: int = 0
    total_collected: float = 0.0
    total_open_balance: float = 0.0
    slack_sent: int = 0
    slack_would_send: int = 0
    errors: int = 0
    message: str = ""
    config_errors: list[str] = field(default_factory=list)

    def to_lines(self) -> list[str]:
        lines = [
            f"Install evening report: {self.status}",
            f"- enabled: {self.enabled}",
            f"- dry_run: {self.dry_run}",
            f"- report date: {self.report_date}",
            f"- tomorrow date: {self.tomorrow_date}",
            f"- raw jobs fetched: {self.raw_jobs_fetched}",
            f"- out-of-scope jobs skipped: {self.jobs_skipped_out_of_scope}",
            f"- jobs enriched: {self.jobs_enriched}",
            f"- today's install jobs: {self.today_install_jobs}",
            f"- tomorrow's install appointments: {self.tomorrow_install_appointments}",
            f"- payment records available: {self.payment_records_available}",
            f"- payment records unavailable: {self.payment_records_unavailable}",
            f"- collected: {_format_money(self.total_collected)}",
            f"- open balance: {_format_money(self.total_open_balance)}",
            f"- Slack messages sent: {self.slack_sent}",
            f"- Slack messages that would send: {self.slack_would_send}",
            f"- errors: {self.errors}",
        ]
        if self.config_errors:
            lines.append("- config errors:")
            lines.extend(f"  - {error}" for error in self.config_errors)
        if self.message:
            lines.extend(["", self.message])
        return lines


class InstallEveningReportService:
    def __init__(self, settings: Settings, db: Persistence, client: ServiceTitanClient, slack: SlackClient) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.slack = slack

    def run_once(
        self,
        now: datetime | None = None,
        *,
        require_enabled: bool = True,
    ) -> InstallEveningReportSummary:
        tz = ZoneInfo(self.settings.timezone)
        local_now = (now or datetime.now(tz)).astimezone(tz)
        today = local_now.date()
        tomorrow = today + timedelta(days=1)
        summary = InstallEveningReportSummary(
            enabled=self.settings.install_audit_evening_report_enabled,
            dry_run=self.settings.install_audit_dry_run,
            report_date=today.isoformat(),
            tomorrow_date=tomorrow.isoformat(),
        )
        if require_enabled and not self.settings.install_audit_evening_report_enabled:
            summary.status = "disabled"
            logger.info("install_evening_report_skipped_disabled")
            return summary

        missing = self._missing_config()
        if missing:
            summary.status = "config_error"
            summary.config_errors = missing
            logger.warning("install_evening_report_skipped_missing_config", extra={"missing_config": missing})
            return summary

        run_id = self.db.log_run_start(
            INSTALL_EVENING_REPORT_RUN_TYPE,
            {
                "dry_run": summary.dry_run,
                "report_date": summary.report_date,
                "tomorrow_date": summary.tomorrow_date,
                "max_jobs": self.settings.install_audit_evening_report_max_jobs,
            },
        )
        window_start = datetime.combine(today, time.min, tzinfo=tz).astimezone(timezone.utc)
        window_end = datetime.combine(tomorrow + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        try:
            jobs = self.client.query_install_jobs_by_appointment_window(
                business_unit_ids=set(self.settings.install_audit_business_unit_ids),
                business_unit_names=set(self.settings.install_audit_business_unit_names),
                window_start=window_start,
                window_end=window_end,
                max_jobs=self.settings.install_audit_evening_report_max_jobs,
            )
        except ServiceTitanApiError as exc:
            summary.status = "api_error"
            summary.errors = 1
            self.db.log_run_complete(run_id, summary.status, {"status": exc.status, "error_message": exc.message})
            logger.warning("install_evening_report_api_error", extra={"status_code": exc.status, "error_message": exc.message})
            return summary
        except Exception as exc:
            summary.status = "api_error"
            summary.errors = 1
            self.db.log_run_complete(run_id, summary.status, {"error_message": str(exc)})
            logger.warning("install_evening_report_failed", exc_info=True, extra={"error_message": str(exc)})
            return summary

        stats = getattr(self.client, "last_install_evening_report_stats", {}) or {}
        summary.raw_jobs_fetched = int(stats.get("raw_jobs_fetched", len(jobs)))
        summary.jobs_skipped_out_of_scope = int(stats.get("jobs_skipped_out_of_scope", 0))
        summary.jobs_enriched = int(stats.get("jobs_enriched", len(jobs)))

        today_jobs = _jobs_with_appointment_on(jobs, today, tz)
        tomorrow_appointments = _appointments_on(jobs, tomorrow, tz)
        summary.today_install_jobs = len(today_jobs)
        summary.tomorrow_install_appointments = len(tomorrow_appointments)

        collection_lines: list[str] = []
        for job in sorted(today_jobs, key=lambda item: _job_start_on(item, today, tz) or datetime.max.replace(tzinfo=timezone.utc)):
            totals = _payment_totals(job)
            total = _money_or_none(totals.get("total"))
            paid = _money_or_none(totals.get("paid"))
            balance = _money_or_none(totals.get("balance"))
            available = bool(totals.get("available")) and any(value is not None for value in (total, paid, balance))
            if available:
                summary.total_collected += paid or 0.0
                summary.total_open_balance += balance or 0.0
            collection_lines.extend(_collection_lines(job, total, paid, balance, available))

        schedule_lines: list[str] = []
        for job, appointment in tomorrow_appointments:
            schedule_lines.extend(_schedule_lines(job, appointment, tz))

        report_jobs = {job.job_id: job for job in today_jobs}
        report_jobs.update({job.job_id: job for job, _appointment in tomorrow_appointments})
        for job in report_jobs.values():
            totals = _payment_totals(job)
            if bool(totals.get("available")) and any(
                _money_or_none(totals.get(name)) is not None for name in ("total", "paid", "balance")
            ):
                summary.payment_records_available += 1
            else:
                summary.payment_records_unavailable += 1

        summary.message = _build_message(
            local_now,
            tomorrow,
            collection_lines,
            schedule_lines,
            summary,
        )
        if summary.dry_run:
            summary.slack_would_send = 1
            logger.info(
                "install_evening_report_dry_run",
                extra={
                    "today_jobs": summary.today_install_jobs,
                    "tomorrow_appointments": summary.tomorrow_install_appointments,
                    "payment_records_unavailable": summary.payment_records_unavailable,
                },
            )
        else:
            ts = self.slack.post_message(self.settings.install_audit_slack_channel_id, summary.message)
            if ts:
                summary.slack_sent = 1
            else:
                summary.status = "slack_error"
                summary.errors = 1
                logger.warning("install_evening_report_slack_failed")

        self.db.log_run_complete(
            run_id,
            summary.status,
            {
                "dry_run": summary.dry_run,
                "report_date": summary.report_date,
                "tomorrow_date": summary.tomorrow_date,
                "raw_jobs_fetched": summary.raw_jobs_fetched,
                "jobs_skipped_out_of_scope": summary.jobs_skipped_out_of_scope,
                "jobs_enriched": summary.jobs_enriched,
                "today_install_jobs": summary.today_install_jobs,
                "tomorrow_install_appointments": summary.tomorrow_install_appointments,
                "payment_records_available": summary.payment_records_available,
                "payment_records_unavailable": summary.payment_records_unavailable,
                "total_collected": round(summary.total_collected, 2),
                "total_open_balance": round(summary.total_open_balance, 2),
                "slack_sent": summary.slack_sent,
                "slack_would_send": summary.slack_would_send,
            },
        )
        logger.info(
            "install_evening_report_finished",
            extra={
                "status": summary.status,
                "today_jobs": summary.today_install_jobs,
                "tomorrow_appointments": summary.tomorrow_install_appointments,
                "payment_records_available": summary.payment_records_available,
                "payment_records_unavailable": summary.payment_records_unavailable,
                "slack_sent": summary.slack_sent,
            },
        )
        return summary

    def _missing_config(self) -> list[str]:
        required = {
            "SERVICETITAN_CLIENT_ID": self.settings.servicetitan_client_id,
            "SERVICETITAN_CLIENT_SECRET": self.settings.servicetitan_client_secret,
            "SERVICETITAN_TENANT_ID": self.settings.servicetitan_tenant_id,
            "SERVICETITAN_APP_KEY": self.settings.servicetitan_app_key,
        }
        if not self.settings.install_audit_dry_run:
            required["SLACK_BOT_TOKEN"] = self.settings.slack_bot_token
            required["INSTALL_AUDIT_SLACK_CHANNEL_ID"] = self.settings.install_audit_slack_channel_id
        return [name for name, value in required.items() if not value]


def _jobs_with_appointment_on(jobs: list[ServiceTitanJob], target: date, tz: ZoneInfo) -> list[ServiceTitanJob]:
    return [job for job in jobs if any(_appointment_is_on(record, target, tz) for record in _install_appointments(job))]


def _appointments_on(
    jobs: list[ServiceTitanJob],
    target: date,
    tz: ZoneInfo,
) -> list[tuple[ServiceTitanJob, dict[str, object]]]:
    found: list[tuple[ServiceTitanJob, dict[str, object]]] = []
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        for appointment in _install_appointments(job):
            if not _appointment_is_on(appointment, target, tz):
                continue
            appointment_id = str(_raw_value(appointment, ("id", "appointmentId")) or "")
            dedupe_key = (job.job_id, appointment_id or str(_appointment_start(appointment) or ""))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            found.append((job, appointment))
    return sorted(found, key=lambda item: _appointment_start(item[1]) or datetime.max.replace(tzinfo=timezone.utc))


def _appointment_is_on(record: dict[str, object], target: date, tz: ZoneInfo) -> bool:
    start = _appointment_start(record)
    return bool(start and start.astimezone(tz).date() == target)


def _job_start_on(job: ServiceTitanJob, target: date, tz: ZoneInfo) -> datetime | None:
    starts = [
        start
        for record in _install_appointments(job)
        if _appointment_is_on(record, target, tz)
        for start in [_appointment_start(record)]
        if start
    ]
    return min(starts) if starts else None


def _collection_lines(
    job: ServiceTitanJob,
    total: float | None,
    paid: float | None,
    balance: float | None,
    available: bool,
) -> list[str]:
    lines = [f"• Job #{_job_label(job)} — {job.job_type_name or 'Installation'} — {_technician_label(job)}"]
    if available:
        lines.append(
            "  Invoice: "
            f"{_money_text(total)} total / {_money_text(paid)} collected to date / {_money_text(balance)} balance"
        )
        if total is not None and total <= 0.01 and balance is not None and balance <= 0.01:
            status = "zero-dollar invoice"
        else:
            status = "fully collected" if balance is not None and balance <= 0.01 else "balance open"
        lines.append(f"  Status: {status}")
    else:
        lines.append("  Invoice: structured payment data unavailable")
        lines.append("  Status: needs ServiceTitan payment review")
    if _job_url(job):
        lines.append(f"  Link: {_job_url(job)}")
    return lines


def _schedule_lines(job: ServiceTitanJob, appointment: dict[str, object], tz: ZoneInfo) -> list[str]:
    start = _appointment_start(appointment)
    end = _appointment_end(appointment)
    window = _time_window(start, end, tz)
    technician = _appointment_technician(job, appointment)
    totals = _payment_totals(job)
    paid = _money_or_none(totals.get("paid"))
    balance = _money_or_none(totals.get("balance"))
    lines = [f"• {window} — Job #{_job_label(job)} — {job.job_type_name or 'Installation'}"]
    lines.append(f"  Technician: {technician}")
    if bool(totals.get("available")) and (paid is not None or balance is not None):
        lines.append(f"  Payment: {_money_text(paid)} collected to date / {_money_text(balance)} balance")
    else:
        lines.append("  Payment: structured payment data unavailable")
    if _job_url(job):
        lines.append(f"  Link: {_job_url(job)}")
    return lines


def _build_message(
    local_now: datetime,
    tomorrow: date,
    collection_lines: list[str],
    schedule_lines: list[str],
    summary: InstallEveningReportSummary,
) -> str:
    header_date = _date_text(local_now.date())
    lines = [
        f"📋 Install Operations — {header_date}, through {_time_text(local_now)}",
        "",
        "Today's Install Payment Status",
    ]
    lines.extend(collection_lines or ["• No true install appointments found today."])
    lines.extend(["", f"Tomorrow's Schedule — {_date_text(tomorrow)}"])
    lines.extend(schedule_lines or ["• No true install appointments found for tomorrow."])
    lines.extend(
        [
            "",
            (
                f"Summary: {summary.today_install_jobs} installs today · "
                f"{_format_money(summary.total_collected)} collected to date · "
                f"{_format_money(summary.total_open_balance)} open · "
                f"{summary.tomorrow_install_appointments} appointments tomorrow · "
                f"{summary.payment_records_unavailable} payment records unavailable"
            ),
        ]
    )
    return "\n".join(lines)


def _appointment_technician(job: ServiceTitanJob, appointment: dict[str, object]) -> str:
    appointment_id = str(_raw_value(appointment, ("id", "appointmentId")) or "")
    raw_assignments = job.raw.get("appointment_assignments")
    assignments = [item for item in raw_assignments if isinstance(item, dict)] if isinstance(raw_assignments, list) else []
    names: list[str] = []
    for assignment in assignments:
        assigned_appointment_id = str(
            _raw_value(assignment, ("appointmentId", "appointment.id", "appointmentId.value")) or ""
        )
        if appointment_id and assigned_appointment_id and assigned_appointment_id != appointment_id:
            continue
        name = str(
            _raw_value(
                assignment,
                ("technicianName", "employeeName", "name", "technician.name", "employee.name"),
            )
            or ""
        ).strip()
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or _technician_label(job)


def _technician_label(job: ServiceTitanJob) -> str:
    return job.technician_name or job.technician_id or "unassigned"


def _job_label(job: ServiceTitanJob) -> str:
    return job.job_number or job.job_id or "unavailable"


def _job_url(job: ServiceTitanJob) -> str:
    return job.url or (f"https://go.servicetitan.com/#/Job/Index/{job.job_id}" if job.job_id else "")


def _money_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _money_text(value: float | None) -> str:
    return _format_money(value) if value is not None else "unavailable"


def _date_text(value: date) -> str:
    return value.strftime("%b %-d")


def _time_text(value: datetime) -> str:
    return value.strftime("%-I:%M %p")


def _time_window(start: datetime | None, end: datetime | None, tz: ZoneInfo) -> str:
    if not start:
        return "time unavailable"
    start_text = start.astimezone(tz).strftime("%-I:%M %p")
    if not end:
        return start_text
    return f"{start_text}–{end.astimezone(tz).strftime('%-I:%M %p')}"


def _raw_value(source: dict[str, Any], paths: tuple[str, ...]) -> object | None:
    for path in paths:
        current: object = source
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None:
            return current
    return None
