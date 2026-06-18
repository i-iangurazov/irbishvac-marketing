from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..clients.servicetitan import ServiceTitanApiError, ServiceTitanClient, ServiceTitanJob
from ..config import Settings


@dataclass
class ServiceTitanScopeDiscoverySummary:
    status: str = "completed"
    jobs_scanned: int = 0
    related_counts: dict[str, int] = field(default_factory=dict)
    value_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    available_keys: dict[str, list[str]] = field(default_factory=dict)
    errors: int = 0
    config_errors: list[str] = field(default_factory=list)

    def to_lines(self) -> list[str]:
        lines = [
            f"ServiceTitan scope discovery: {self.status}",
            f"- jobs scanned: {self.jobs_scanned}",
            "- sanitized: true",
            "- customer names, addresses, phone numbers, emails, raw notes, and secrets are not printed",
        ]
        if self.related_counts:
            lines.append("- related record counts:")
            for category, count in sorted(self.related_counts.items()):
                lines.append(f"  - {category}: {count}")
        if self.value_counts:
            lines.append("- discovered scope values:")
            for category, counts in sorted(self.value_counts.items()):
                lines.append(f"  - {category}:")
                for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:20]:
                    lines.append(f"    - {value}: {count}")
        if self.available_keys:
            lines.append("- available top-level keys by payload:")
            for category, keys in sorted(self.available_keys.items()):
                lines.append(f"  - {category}: {', '.join(keys) if keys else '<none>'}")
        if self.config_errors:
            lines.append("- config errors:")
            lines.extend(f"  - {item}" for item in self.config_errors)
        if self.errors:
            lines.append(f"- errors: {self.errors}")
        return lines


class ServiceTitanScopeDiscovery:
    def __init__(self, settings: Settings, client: ServiceTitanClient) -> None:
        self.settings = settings
        self.client = client

    def run_once(self, now: datetime | None = None) -> ServiceTitanScopeDiscoverySummary:
        summary = ServiceTitanScopeDiscoverySummary()
        missing = self._missing_service_titan_only()
        if missing:
            summary.status = "config_error"
            summary.config_errors = missing
            return summary

        now = now or datetime.now(timezone.utc)
        since = now.astimezone(timezone.utc) - timedelta(minutes=self.settings.service_titan_audit_lookback_minutes)
        try:
            jobs = self.client.query_recent_jobs(since, apply_ruleset_prefilter=False)
        except ServiceTitanApiError as exc:
            summary.status = "api_error"
            summary.errors = 1
            summary.value_counts["api_error"] = {f"HTTP {exc.status}": 1}
            return summary
        except Exception as exc:
            summary.status = "api_error"
            summary.errors = 1
            summary.value_counts["api_error"] = {type(exc).__name__: 1}
            return summary

        summary.jobs_scanned = len(jobs)
        counters: dict[str, Counter[str]] = {
            "statuses": Counter(),
            "appointment_statuses": Counter(),
            "business_units": Counter(),
            "job_types": Counter(),
            "departments": Counter(),
            "trades": Counter(),
            "workflows": Counter(),
            "tags": Counter(),
            "advisor_or_technician_ids": Counter(),
            "technician_ids": Counter(),
            "dispatcher_ids": Counter(),
            "invoice_statuses": Counter(),
            "cancellation_reasons": Counter(),
            "material_context": Counter(),
        }
        available: dict[str, set[str]] = {}
        related: Counter[str] = Counter()

        for job in jobs:
            self._add(counters["statuses"], job.status)
            self._add(counters["appointment_statuses"], job.appointment_status)
            self._add(counters["business_units"], job.business_unit_name or job.business_unit_id)
            self._add(counters["job_types"], job.job_type_name or job.job_type_id)
            self._add(counters["departments"], job.department)
            self._add(counters["trades"], job.trade)
            self._add(counters["workflows"], job.workflow)
            for tag in [*job.tag_names, *job.tag_ids]:
                self._add(counters["tags"], tag)
            self._add(counters["advisor_or_technician_ids"], job.technician_id or ("<name-present>" if job.technician_name else ""))
            self._add(counters["technician_ids"], job.technician_id or ("<name-present>" if job.technician_name else ""))
            self._add(counters["dispatcher_ids"], job.dispatcher_id or ("<name-present>" if job.dispatcher_name else ""))
            self._add(counters["invoice_statuses"], job.invoice_status)
            self._add(counters["cancellation_reasons"], job.cancellation_reason)
            counters["material_context"][_material_context_label(job)] += 1

            related.update(job.related_counts)
            for category, keys in job.available_keys.items():
                available.setdefault(category, set()).update(keys)

        summary.related_counts = dict(related)
        summary.value_counts = {category: dict(counter) for category, counter in counters.items() if counter}
        summary.available_keys = {category: sorted(keys) for category, keys in available.items()}
        return summary

    def _missing_service_titan_only(self) -> list[str]:
        required = {
            "SERVICETITAN_CLIENT_ID": self.settings.servicetitan_client_id,
            "SERVICETITAN_CLIENT_SECRET": self.settings.servicetitan_client_secret,
            "SERVICETITAN_TENANT_ID": self.settings.servicetitan_tenant_id,
            "SERVICETITAN_APP_KEY": self.settings.servicetitan_app_key,
        }
        return [key for key, value in required.items() if not value]

    def _add(self, counter: Counter[str], value: str) -> None:
        cleaned = value.strip()
        if cleaned:
            counter[cleaned] += 1


def _material_context_label(job: ServiceTitanJob) -> str:
    if job.ply_data_available:
        return "ply_available"
    if job.purchase_orders_count is not None:
        return "po_present" if job.purchase_orders_count > 0 else "no_po"
    if job.purchase_orders:
        return "po_present"
    if "purchase_orders" in job.present_fields:
        return "no_po"
    if "purchase_orders" in job.missing_data:
        return "po_unknown_endpoint_unavailable"
    return "unknown"
