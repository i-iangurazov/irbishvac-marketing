from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from ..config import Settings
from ..models import parse_notion_datetime
from .http import DEFAULT_USER_AGENT, HttpClient


logger = logging.getLogger(__name__)
AUTH_CONTENT_TYPE = "application/x-www-form-urlencoded"


class ServiceTitanApiError(RuntimeError):
    def __init__(self, status: int, data: dict[str, Any]) -> None:
        self.status = status
        self.data = data
        self.message = str(data.get("message") or data.get("title") or data.get("error") or "ServiceTitan API request failed")
        super().__init__(f"ServiceTitan API error {status}: {self.message}")


@dataclass(frozen=True)
class ServiceTitanJob:
    job_id: str
    job_number: str
    status: str
    modified_on: datetime | None
    completed_on: datetime | None
    appointment_id: str = ""
    appointment_status: str = ""
    invoice_id: str = ""
    technician_id: str = ""
    technician_name: str = ""
    dispatcher_id: str = ""
    dispatcher_name: str = ""
    business_unit_id: str = ""
    business_unit_name: str = ""
    job_type_id: str = ""
    job_type_name: str = ""
    department: str = ""
    trade: str = ""
    workflow: str = ""
    tag_ids: list[str] = field(default_factory=list)
    tag_names: list[str] = field(default_factory=list)
    campaign_id: str = ""
    campaign_name: str = ""
    cancellation_reason: str = ""
    customer_name: str = ""
    arrival_window_start: datetime | None = None
    arrival_window_end: datetime | None = None
    arrived_at: datetime | None = None
    clock_in_at: datetime | None = None
    clock_out_at: datetime | None = None
    lunch_break_minutes: int | None = None
    invoice_line_items: list[str] = field(default_factory=list)
    invoice_items: list[dict[str, Any]] = field(default_factory=list)
    invoice_status: str = ""
    invoice_total: float | None = None
    invoice_balance: float | None = None
    payment_total: float | None = None
    payments_count: int | None = None
    diagnostic_fee_present: bool | None = None
    diagnostic_fee_charged: bool | None = None
    diagnostic_fee_waived: bool | None = None
    repair_sold: bool | None = None
    completed_phases: list[str] = field(default_factory=list)
    operational_data: dict[str, str] = field(default_factory=dict)
    operational_data_complete: bool | None = None
    options_presented: bool | None = None
    estimate_count: int | None = None
    same_day_estimate_present: bool | None = None
    home_comfort_plan_option_present: bool | None = None
    notes: str | None = None
    photo_count: int | None = None
    supporting_evidence_count: int | None = None
    forms_count: int | None = None
    hhr_completed: bool | None = None
    equipment_count: int | None = None
    equipment_complete: bool | None = None
    authorization_count: int | None = None
    follow_up_needed: bool | None = None
    follow_up_task_present: bool | None = None
    special_order_detected: bool | None = None
    special_order_missing_fields: list[str] = field(default_factory=list)
    special_order_reminder_present: bool | None = None
    downpayment_recorded: bool | None = None
    lead_turnover_required: bool | None = None
    lead_turnover_documented: bool | None = None
    purchase_orders: list[dict[str, Any]] = field(default_factory=list)
    purchase_orders_count: int | None = None
    po_received_not_reconciled_count: int | None = None
    po_missing_vendor_document_count: int | None = None
    po_missing_attachment_count: int | None = None
    po_not_synced_count: int | None = None
    ply_data_available: bool = False
    scope_change_detected: bool | None = None
    scope_change_escalated: bool | None = None
    cancellation_after_materials_detected: bool | None = None
    cancellation_escalated: bool | None = None
    defective_part_detected: bool | None = None
    warranty_claim_documented: bool | None = None
    url: str = ""
    present_fields: set[str] = field(default_factory=set)
    related_counts: dict[str, int] = field(default_factory=dict)
    available_keys: dict[str, list[str]] = field(default_factory=dict)
    missing_data: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_closed(self) -> bool:
        normalized = self.status.lower().replace("_", " ").replace("-", " ")
        return any(value in normalized for value in ("complete", "completed", "closed", "done"))

    @property
    def actor_id(self) -> str:
        return self.technician_id or self.dispatcher_id or "unknown"


@dataclass(frozen=True)
class ServiceTitanProjectTask:
    task_id: str
    task_number: str
    project_id: str
    job_id: str
    job_number: str
    name: str
    assigned_to_id: str
    assigned_to_name: str
    due_at: datetime | None
    created_on: datetime | None
    modified_on: datetime | None
    closed_on: datetime | None
    status: str
    is_closed: bool | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.task_number or self.task_id or self.name or "Task"

    @property
    def is_open(self) -> bool | None:
        if self.is_closed is not None:
            return not self.is_closed
        normalized = self.status.lower().replace("_", " ").replace("-", " ")
        if not normalized:
            return None
        return not any(value in normalized for value in ("complete", "completed", "closed", "done"))


@dataclass(frozen=True)
class ServiceTitanProject:
    project_id: str
    project_number: str
    project_type_id: str
    project_type_name: str
    status_id: str
    status: str
    sub_status_id: str
    created_on: datetime | None
    modified_on: datetime | None
    start_date: datetime | None
    target_completion_date: datetime | None
    actual_completion_date: datetime | None
    business_unit_ids: list[str] = field(default_factory=list)
    business_unit_names: list[str] = field(default_factory=list)
    client_name: str = ""
    job_ids: list[str] = field(default_factory=list)
    project_manager_ids: list[str] = field(default_factory=list)
    project_manager_names: list[str] = field(default_factory=list)
    custom_fields: dict[str, str] = field(default_factory=dict)
    custom_fields_available: bool = False
    tasks: list[ServiceTitanProjectTask] = field(default_factory=list)
    tasks_available: bool = False
    invoices: list[dict[str, Any]] = field(default_factory=list)
    invoices_available: bool = False
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def project_manager_label(self) -> str:
        if self.project_manager_names:
            return ", ".join(self.project_manager_names)
        if self.project_manager_ids:
            return ", ".join(self.project_manager_ids)
        return "Unassigned"

    @property
    def is_completed(self) -> bool:
        normalized = self.status.lower().replace("_", " ").replace("-", " ")
        return any(value in normalized for value in ("complete", "completed", "closed", "done"))


class ServiceTitanClient:
    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.http = http or HttpClient()
        self._access_token = ""
        self._token_expires_at: datetime | None = None
        self._disabled_related_categories: set[str] = set()
        self._disabled_related_reasons: dict[str, str] = {}
        self.last_scope_filter_stats: dict[str, int] = {
            "raw_jobs_fetched": 0,
            "jobs_skipped_before_enrichment": 0,
            "jobs_enriched": 0,
        }
        self.last_pm_audit_stats: dict[str, int] = {
            "raw_projects_fetched": 0,
            "in_scope_projects": 0,
            "skipped_out_of_scope": 0,
            "projects_evaluated": 0,
            "tasks_loaded": 0,
        }
        self.last_install_audit_stats: dict[str, int] = {
            "raw_jobs_fetched": 0,
            "jobs_skipped_out_of_scope": 0,
            "jobs_enriched": 0,
        }

    @property
    def available(self) -> bool:
        return bool(
            self.settings.servicetitan_client_id
            and self.settings.servicetitan_client_secret
            and self.settings.servicetitan_tenant_id
            and self.settings.servicetitan_app_key
        )

    def query_recent_jobs(self, modified_on_or_after: datetime, *, apply_ruleset_prefilter: bool = True) -> list[ServiceTitanJob]:
        since = modified_on_or_after.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        records = self._get_paginated(
            f"/jpm/v2/tenant/{self.settings.servicetitan_tenant_id}/jobs",
            {
                "modifiedOnOrAfter": since,
                "pageSize": str(self.settings.service_titan_audit_page_size),
                "includeTotal": "true",
                "sort": "+ModifiedOn",
            },
        )
        self.last_scope_filter_stats = {
            "raw_jobs_fetched": len(records),
            "jobs_skipped_before_enrichment": 0,
            "jobs_enriched": len(records),
        }
        if apply_ruleset_prefilter:
            records = self._prefilter_scope_records(records)
        jobs = [self._enrich_job(parse_service_titan_job(record, self.settings)) for record in records]
        self.last_scope_filter_stats["jobs_enriched"] = len(jobs)
        return jobs

    def query_install_audit_jobs(
        self,
        *,
        business_unit_ids: set[str],
        business_unit_names: set[str],
        window_start: datetime,
        window_end: datetime,
        max_appointments: int,
    ) -> list[ServiceTitanJob]:
        since = window_start.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        records = self._get_paginated(
            f"/jpm/v2/tenant/{self.settings.servicetitan_tenant_id}/jobs",
            {
                "modifiedOnOrAfter": since,
                "pageSize": str(self.settings.service_titan_audit_page_size),
                "includeTotal": "true",
                "sort": "+ModifiedOn",
            },
        )
        stats = {
            "raw_jobs_fetched": len(records),
            "jobs_skipped_out_of_scope": 0,
            "jobs_enriched": 0,
        }
        jobs: list[ServiceTitanJob] = []
        business_unit_name_map = self.query_install_business_unit_names()
        job_type_name_map = self.query_install_job_type_names()
        clean_business_unit_ids = {value.strip() for value in business_unit_ids if value.strip()}
        clean_business_unit_names = {value.strip() for value in business_unit_names if value.strip()}
        install_keywords = [value.strip() for value in self.settings.install_audit_job_type_match_keywords if value.strip()]
        for record in records:
            parsed = _hydrate_install_scope_names(
                parse_service_titan_job(record, self.settings),
                business_unit_name_map,
                job_type_name_map,
            )
            if not _install_prefilter_matches(parsed, clean_business_unit_ids, clean_business_unit_names, install_keywords):
                stats["jobs_skipped_out_of_scope"] += 1
                _log_install_scope_drop(parsed, _install_prefilter_failed_gates(parsed, clean_business_unit_ids, clean_business_unit_names, install_keywords))
                continue
            enriched = self._enrich_job(parsed)
            stats["jobs_enriched"] += 1
            failed_gates = install_strict_scope_failed_gates(enriched, clean_business_unit_names, install_keywords)
            if failed_gates:
                stats["jobs_skipped_out_of_scope"] += 1
                _log_install_scope_drop(enriched, failed_gates)
                continue
            if _job_known_outside_install_window(enriched, window_start, window_end):
                stats["jobs_skipped_out_of_scope"] += 1
                _log_install_scope_drop(enriched, ("appointment_window",))
                continue
            jobs.append(enriched)
            if len(jobs) >= max(1, max_appointments):
                break
        self.last_install_audit_stats = stats
        return jobs

    def query_install_business_unit_names(self) -> dict[str, str]:
        return self._install_scope_name_map(
            self._tenant_path("settings", "business-units"),
            ("id", "businessUnitId"),
            ("name", "businessUnitName", "displayName"),
            "business_units",
        )

    def query_install_job_type_names(self) -> dict[str, str]:
        return self._install_scope_name_map(
            self._tenant_path("jpm", "job-types"),
            ("id", "jobTypeId"),
            ("name", "jobTypeName", "displayName"),
            "job_types",
        )

    def _install_scope_name_map(
        self,
        path: str,
        id_fields: tuple[str, ...],
        name_fields: tuple[str, ...],
        category: str,
    ) -> dict[str, str]:
        try:
            records = self._get_paginated(
                path,
                {"pageSize": "200", "includeTotal": "true"},
                related_category=f"install_{category}",
            )
        except Exception as exc:
            logger.warning(
                "install_audit_scope_metadata_unavailable",
                extra={"category": category, "error_type": type(exc).__name__},
            )
            return {}
        result: dict[str, str] = {}
        for record in records:
            identifier = str(_raw_value(record, id_fields) or "").strip()
            name = str(_raw_value(record, name_fields) or "").strip()
            if identifier and name:
                result[identifier] = name
        logger.info(
            "install_audit_scope_metadata_loaded",
            extra={"category": category, "records": len(result)},
        )
        return result

    def query_pm_projects(
        self,
        *,
        project_type_ids: set[str] | None = None,
        business_unit_ids: set[str] | None = None,
        business_unit_names: set[str] | None = None,
        exclude_keywords: tuple[str, ...] = (),
        max_projects: int | None = None,
        max_tasks: int | None = None,
    ) -> list[ServiceTitanProject]:
        project_type_names = self.query_pm_project_types()
        project_status_names = self.query_pm_project_statuses()
        employee_names = self._employee_name_map()
        project_limit = max(1, max_projects if max_projects is not None else self.settings.pm_audit_max_projects)
        task_limit = max(0, max_tasks if max_tasks is not None else self.settings.pm_audit_max_tasks)
        records = self._get_paginated(
            self._tenant_path("jpm", "projects"),
            {
                "pageSize": str(self.settings.pm_audit_project_page_size),
                "includeTotal": "true",
                "sort": "-CreatedOn",
            },
        )
        stats = {
            "raw_projects_fetched": len(records),
            "in_scope_projects": 0,
            "skipped_out_of_scope": 0,
            "projects_evaluated": 0,
            "tasks_loaded": 0,
            "invoices_loaded": 0,
        }
        projects: list[ServiceTitanProject] = []
        fetch_invoices = self._should_fetch_pm_project_invoices()
        for record in records:
            project = parse_service_titan_project(
                record,
                project_type_names,
                project_status_names,
                employee_names,
                self.settings.servicetitan_project_url_template,
            )
            if not _pm_project_matches_scope(project, project_type_ids, business_unit_ids, business_unit_names, exclude_keywords):
                stats["skipped_out_of_scope"] += 1
                continue
            stats["in_scope_projects"] += 1
            if len(projects) >= project_limit:
                continue
            remaining_tasks = task_limit - stats["tasks_loaded"]
            if remaining_tasks <= 0:
                tasks, tasks_available = [], False
            else:
                tasks, tasks_available = self.query_pm_project_tasks(project.project_id, max_tasks=remaining_tasks)
            stats["tasks_loaded"] += len(tasks)
            invoices: list[dict[str, Any]] = []
            invoices_available = False
            if fetch_invoices:
                invoices, invoices_available = self.query_pm_project_invoices(project)
                stats["invoices_loaded"] += len(invoices)
            projects.append(
                replace(
                    project,
                    tasks=tasks,
                    tasks_available=tasks_available,
                    invoices=invoices,
                    invoices_available=invoices_available,
                )
            )
        stats["projects_evaluated"] = len(projects)
        self.last_pm_audit_stats = stats
        return projects

    def query_pm_project_types(self) -> dict[str, str]:
        records = self._get_paginated(
            self._tenant_path("jpm", "project-types"),
            {"pageSize": "200", "includeTotal": "true"},
        )
        return {str(_raw_value(record, ("id", "projectTypeId")) or ""): str(_raw_value(record, ("name", "projectTypeName")) or "") for record in records}

    def query_pm_project_statuses(self) -> dict[str, str]:
        records = self._get_paginated(
            self._tenant_path("jpm", "project-statuses"),
            {"pageSize": "200", "includeTotal": "true"},
        )
        return {str(_raw_value(record, ("id", "statusId")) or ""): str(_raw_value(record, ("name", "statusName")) or "") for record in records}

    def query_pm_project_tasks(self, project_id: str, *, max_tasks: int | None = None) -> tuple[list[ServiceTitanProjectTask], bool]:
        if not project_id:
            return [], False
        try:
            task_page_size = self.settings.service_titan_audit_page_size
            if max_tasks is not None and max_tasks > 0:
                task_page_size = min(task_page_size, max_tasks)
            records = self._get_paginated(
                self._tenant_path("taskmanagement", "tasks"),
                {
                    "projectId": project_id,
                    "pageSize": str(task_page_size),
                    "includeTotal": "true",
                },
                related_category="pm_project_tasks",
            )
        except ServiceTitanApiError as exc:
            logger.warning("servicetitan_pm_tasks_unavailable", extra={"project_id": project_id, "status": exc.status})
            return [], False
        except Exception as exc:
            logger.warning("servicetitan_pm_tasks_failed", extra={"project_id": project_id, "error_message": str(exc)})
            return [], False
        if max_tasks is not None:
            records = records[:max(0, max_tasks)]
        return [parse_service_titan_project_task(record) for record in records], True

    def query_pm_project_invoices(self, project: ServiceTitanProject) -> tuple[list[dict[str, Any]], bool]:
        if not project.job_ids:
            return [], False
        invoices: list[dict[str, Any]] = []
        available = False
        seen: set[str] = set()
        for job_id in project.job_ids:
            if not job_id:
                continue
            try:
                records = self._get_paginated(
                    self._tenant_path("accounting", "invoices"),
                    {
                        "jobId": job_id,
                        "pageSize": "50",
                        "includeTotal": "true",
                    },
                    related_category="pm_project_invoices",
                )
            except ServiceTitanApiError as exc:
                logger.warning("servicetitan_pm_invoices_unavailable", extra={"project_id": project.project_id, "status": exc.status})
                continue
            except Exception as exc:
                logger.warning("servicetitan_pm_invoices_failed", extra={"project_id": project.project_id, "error_message": str(exc)})
                continue
            available = True
            for record in records:
                if not _pm_invoice_matches_project(record, project, job_id):
                    continue
                invoice_id = str(_raw_value(record, ("id", "invoiceId", "number", "invoiceNumber")) or "")
                dedupe_key = invoice_id or f"{job_id}:{len(invoices)}"
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                invoices.append(record)
        return invoices, available

    def _should_fetch_pm_project_invoices(self) -> bool:
        invoice_rule_ids = {"R18", "R22", "R27"}
        enabled = {rule_id.strip().upper() for rule_id in self.settings.pm_audit_enabled_rule_ids if rule_id.strip()}
        return not enabled or bool(enabled & invoice_rule_ids)

    def _employee_name_map(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for suffix in ("technicians", "employees"):
            try:
                records = self._get_paginated(
                    self._tenant_path("settings", suffix),
                    {"pageSize": "200", "includeTotal": "true"},
                )
            except Exception:
                logger.info("servicetitan_pm_employee_map_unavailable", extra={"source": suffix})
                continue
            for record in records:
                identifier = str(_raw_value(record, ("id", "employeeId", "technicianId")) or "")
                name = str(_raw_value(record, ("name", "displayName")) or "")
                if not name:
                    first = str(_raw_value(record, ("firstName",)) or "")
                    last = str(_raw_value(record, ("lastName",)) or "")
                    name = " ".join(part for part in (first, last) if part)
                if identifier and name:
                    names[identifier] = name
        return names

    def _prefilter_scope_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from ..domain.service_titan_rules import (
            RESULT_INSUFFICIENT,
            active_service_titan_rules,
            _applicability_decision,
            _effective_rule,
        )

        rules = active_service_titan_rules(self.settings)
        if not rules:
            self.last_scope_filter_stats = {
                "raw_jobs_fetched": len(records),
                "jobs_skipped_before_enrichment": len(records),
                "jobs_enriched": 0,
            }
            logger.info(
                "servicetitan_scope_prefilter_applied",
                extra={
                    "raw_jobs_fetched": len(records),
                    "jobs_skipped_before_enrichment": len(records),
                    "jobs_enriched": 0,
                    "enabled_rulesets": [],
                },
            )
            return []

        filtered: list[dict[str, Any]] = []
        rulesets = sorted({rule.ruleset for rule in rules})
        for record in records:
            job = parse_service_titan_job(record, self.settings)
            keep = False
            for rule in rules:
                effective_rule = _effective_rule(rule, self.settings)
                decision = _applicability_decision(job, effective_rule.scope)
                if decision.status == "applies" or decision.status == RESULT_INSUFFICIENT:
                    keep = True
                    break
                if _scope_filter_decision_is_uncertain(job, decision.metadata):
                    keep = True
                    break
            if keep:
                filtered.append(record)

        skipped = len(records) - len(filtered)
        self.last_scope_filter_stats = {
            "raw_jobs_fetched": len(records),
            "jobs_skipped_before_enrichment": skipped,
            "jobs_enriched": len(filtered),
        }
        logger.info(
            "servicetitan_scope_prefilter_applied",
            extra={
                "raw_jobs_fetched": len(records),
                "jobs_skipped_before_enrichment": skipped,
                "jobs_enriched": len(filtered),
                "enabled_rulesets": rulesets,
            },
        )
        return filtered

    def _prefilter_sales_only_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._should_prefilter_sales_only():
            return records
        scope = _sales_ruleset_applies_to(self.settings)
        if not scope:
            return records
        parsed = [parse_service_titan_job(record, self.settings) for record in records]
        filtered = [record for record, job in zip(records, parsed) if _sales_scope_matches_job(job, scope)]
        logger.info(
            "servicetitan_sales_prefilter_applied",
            extra={"records_seen": len(records), "records_kept": len(filtered)},
        )
        return filtered

    def _should_prefilter_sales_only(self) -> bool:
        return bool(
            self.settings.sales_comfort_advisor_audit_enabled
            and not self.settings.hvac_service_audit_enabled
            and not self.settings.plumbing_service_audit_enabled
            and not self.settings.technician_compliance_enabled
            and not self.settings.dispatcher_audit_enabled
        )

    def _should_fetch_related_category(self, category: str) -> bool:
        active_categories = self._active_related_categories()
        if active_categories is not None:
            return category in active_categories
        disabled_rules = set(self.settings.service_titan_disabled_rule_ids)
        if self._should_prefilter_sales_only():
            allowed = {"appointments", "appointment_assignments", "estimates", "opportunities"}
            if "sales_photos_missing" not in disabled_rules:
                allowed.update({"attachments", "forms"})
            return category in allowed
        if self._should_fetch_hvac_only():
            allowed = {"appointments", "appointment_assignments", "invoices", "estimates", "opportunities"}
            if "hvac_required_photos_missing" not in disabled_rules:
                allowed.update({"attachments", "forms"})
            if "hvac_diagnosis_form_missing" not in disabled_rules:
                allowed.add("forms")
            return category in allowed
        if self._should_fetch_plumbing_only():
            allowed = {"appointments", "appointment_assignments", "estimates", "opportunities"}
            if (
                "plumbing_options_fewer_than_three" not in disabled_rules
                or "plumbing_payment_missing_on_completed_job" not in disabled_rules
            ):
                allowed.add("invoices")
            if "plumbing_required_photos_missing" not in disabled_rules:
                allowed.update({"attachments", "forms"})
            if "plumbing_diagnosis_form_missing" not in disabled_rules:
                allowed.add("forms")
            return category in allowed
        return True

    def _active_related_categories(self) -> set[str] | None:
        from ..domain.service_titan_rules import active_service_titan_rules

        rule_ids = {rule.rule_id for rule in active_service_titan_rules(self.settings)}
        if not rule_ids:
            return set()

        categories: set[str] = set()
        rule_categories: dict[str, set[str]] = {
            "job_left_open_after_visit": {"appointments", "appointment_assignments"},
            "sales_options_fewer_than_three": {"estimates", "opportunities"},
            "sales_photos_missing": {"attachments", "forms"},
            "sales_arrival_after_first_half": {"appointments", "appointment_assignments"},
            "hvac_options_fewer_than_three": {"estimates", "opportunities"},
            "hvac_payment_missing_on_completed_job": {"invoices"},
            "hvac_diagnosis_form_missing": {"forms"},
            "hvac_required_photos_missing": {"attachments", "forms"},
            "hvac_arrival_outside_window": {"appointments", "appointment_assignments"},
            "plumbing_options_fewer_than_three": {"appointments", "appointment_assignments", "invoices", "estimates", "opportunities"},
            "plumbing_payment_missing_on_completed_job": {"invoices"},
            "plumbing_required_photos_missing": {"attachments", "forms"},
            "plumbing_diagnosis_form_missing": {"forms"},
            "plumbing_arrival_outside_window": {"appointments", "appointment_assignments"},
            "dispatch_arrival_outside_first_30": {"appointments", "appointment_assignments"},
            "dispatch_diagnostic_fee_missing": {"invoices", "invoice_items"},
            "dispatch_options_not_presented": {"estimates", "opportunities"},
            "dispatch_notes_missing": {"notes"},
            "dispatch_photos_missing": {"attachments", "forms"},
            "dispatch_supporting_evidence_missing": {"attachments", "forms", "invoices", "invoice_items"},
            "tech_clock_in_missing": {"technician_time", "non_job_timesheets"},
            "tech_clock_out_missing": {"technician_time", "non_job_timesheets"},
            "tech_lunch_break_missing": {"technician_time", "non_job_timesheets"},
            "tech_invoice_diagnostic_fee_missing": {"invoices", "invoice_items"},
            "tech_required_phases_incomplete": {"job_history"},
            "tech_required_operational_data_incomplete": {"forms", "equipment", "purchase_orders"},
        }
        unknown_rule_ids = rule_ids - set(rule_categories)
        if unknown_rule_ids:
            return None
        for rule_id in rule_ids:
            categories.update(rule_categories[rule_id])
        return categories

    def _should_fetch_hvac_only(self) -> bool:
        return bool(
            self.settings.hvac_service_audit_enabled
            and not self.settings.sales_comfort_advisor_audit_enabled
            and not self.settings.plumbing_service_audit_enabled
            and not self.settings.technician_compliance_enabled
            and not self.settings.dispatcher_audit_enabled
        )

    def _should_fetch_plumbing_only(self) -> bool:
        return bool(
            self.settings.plumbing_service_audit_enabled
            and not self.settings.sales_comfort_advisor_audit_enabled
            and not self.settings.hvac_service_audit_enabled
            and not self.settings.technician_compliance_enabled
            and not self.settings.dispatcher_audit_enabled
        )

    def _enrich_job(self, job: ServiceTitanJob) -> ServiceTitanJob:
        present_fields = set(job.present_fields)
        related_counts = dict(job.related_counts)
        available_keys = {"job": _top_level_keys(job.raw), **job.available_keys}
        missing_data = dict(job.missing_data)

        appointment_id = job.appointment_id
        appointment_status = job.appointment_status
        invoice_id = job.invoice_id
        technician_id = job.technician_id
        technician_name = job.technician_name
        business_unit_id = job.business_unit_id
        business_unit_name = job.business_unit_name
        job_type_id = job.job_type_id
        job_type_name = job.job_type_name
        department = job.department
        trade = job.trade
        workflow = job.workflow
        tag_ids = list(job.tag_ids)
        tag_names = list(job.tag_names)
        campaign_id = job.campaign_id
        campaign_name = job.campaign_name
        cancellation_reason = job.cancellation_reason
        arrival_window_start = job.arrival_window_start
        arrival_window_end = job.arrival_window_end
        arrived_at = job.arrived_at
        clock_in_at = job.clock_in_at
        clock_out_at = job.clock_out_at
        lunch_break_minutes = job.lunch_break_minutes
        invoice_line_items = list(job.invoice_line_items)
        invoice_items = list(job.invoice_items)
        invoice_status = job.invoice_status
        invoice_total = job.invoice_total
        invoice_balance = job.invoice_balance
        payment_total = job.payment_total
        payments_count = job.payments_count
        diagnostic_fee_present = job.diagnostic_fee_present
        diagnostic_fee_charged = job.diagnostic_fee_charged
        diagnostic_fee_waived = job.diagnostic_fee_waived
        repair_sold = job.repair_sold
        notes = job.notes
        photo_count = job.photo_count
        supporting_evidence_count = job.supporting_evidence_count
        forms_count = job.forms_count
        hhr_completed = job.hhr_completed
        equipment_count = job.equipment_count
        equipment_complete = job.equipment_complete
        authorization_count = job.authorization_count
        estimate_count = job.estimate_count
        same_day_estimate_present = job.same_day_estimate_present
        home_comfort_plan_option_present = job.home_comfort_plan_option_present
        purchase_orders = list(job.purchase_orders)
        purchase_orders_count = job.purchase_orders_count
        po_received_not_reconciled_count = job.po_received_not_reconciled_count
        po_missing_vendor_document_count = job.po_missing_vendor_document_count
        po_missing_attachment_count = job.po_missing_attachment_count
        po_not_synced_count = job.po_not_synced_count
        follow_up_needed = job.follow_up_needed
        follow_up_task_present = job.follow_up_task_present
        special_order_detected = job.special_order_detected
        special_order_missing_fields = list(job.special_order_missing_fields)
        special_order_reminder_present = job.special_order_reminder_present
        downpayment_recorded = job.downpayment_recorded
        lead_turnover_required = job.lead_turnover_required
        lead_turnover_documented = job.lead_turnover_documented
        scope_change_detected = job.scope_change_detected
        scope_change_escalated = job.scope_change_escalated
        cancellation_after_materials_detected = job.cancellation_after_materials_detected
        cancellation_escalated = job.cancellation_escalated
        defective_part_detected = job.defective_part_detected
        warranty_claim_documented = job.warranty_claim_documented
        completed_phases = list(job.completed_phases)
        options_presented = job.options_presented
        invoice_item_records: list[dict[str, Any]] = []
        non_job_timesheets: list[dict[str, Any]] = []

        appointments, appointments_error = self._related_records(
            "appointments",
            self._tenant_path("jpm", "appointments"),
            {"jobId": job.job_id},
        )
        related_counts["appointments"] = len(appointments)
        available_keys["appointments"] = _records_keys(appointments)
        if appointments_error:
            _mark_missing(missing_data, ("appointment_id", "arrival_window", "arrived_at"), appointments_error)
        elif appointments:
            appointment = _select_appointment(appointments)
            appointment_id = appointment_id or str(_value(appointment, ("id", "appointmentId"), present_fields, "appointment_id") or "")
            appointment_status = appointment_status or _display_value(
                _value(appointment, ("status", "appointmentStatus", "status.name"), present_fields, "appointment_status")
            )
            arrival_window_start = arrival_window_start or _parse_datetime(
                _value(appointment, ("arrivalWindowStart", "start"), present_fields, "arrival_window")
            )
            arrival_window_end = arrival_window_end or _parse_datetime(
                _value(appointment, ("arrivalWindowEnd", "end"), present_fields, "arrival_window")
            )
            arrived_at = arrived_at or _parse_datetime(
                _value(appointment, ("arrivedOn", "arrivedAt", "arrivalTime", "technicianArrivedOn"), present_fields, "arrived_at")
            )
        else:
            _mark_missing(missing_data, ("appointment_id", "arrival_window", "arrived_at"), "jpm appointments endpoint returned no records for this job")

        appointment_ids = [appointment_id] if appointment_id else []
        if appointment_ids:
            assignments, assignments_error = self._related_records(
                "appointment_assignments",
                self._tenant_path("dispatch", "appointment-assignments"),
                {"appointmentIds": ",".join(appointment_ids)},
            )
            related_counts["appointment_assignments"] = len(assignments)
            available_keys["appointment_assignments"] = _records_keys(assignments)
            if not assignments_error and assignments:
                assignment = assignments[0]
                technician_id = technician_id or str(
                    _raw_value(assignment, ("technicianId", "employeeId", "technician.id", "employee.id")) or ""
                )
                technician_name = technician_name or str(
                    _raw_value(assignment, ("technicianName", "employeeName", "name", "technician.name", "employee.name")) or ""
                )
                arrived_at = arrived_at or _parse_datetime(
                    _raw_value(assignment, ("arrivedOn", "arrivedAt", "arrivalTime", "technicianArrivedOn"))
                )
                if technician_id or technician_name:
                    present_fields.add("technician")
                if arrived_at:
                    present_fields.add("arrived_at")
            elif assignments_error:
                missing_data.setdefault("technician", assignments_error)
                missing_data.setdefault("arrived_at", assignments_error)

        invoices, invoices_error = self._related_records(
            "invoices",
            self._tenant_path("accounting", "invoices"),
            {"jobId": job.job_id},
        )
        related_counts["invoices"] = len(invoices)
        available_keys["invoices"] = _records_keys(invoices)
        if invoices_error:
            missing_data.setdefault("invoice_line_items", invoices_error)
            missing_data.setdefault("invoice_status", invoices_error)
            missing_data.setdefault("payments", invoices_error)
        else:
            invoice_ids = [value for value in [invoice_id, *[str(_raw_value(record, ("id", "invoiceId")) or "") for record in invoices]] if value]
            if invoices:
                invoice_id = invoice_ids[0] if invoice_ids else invoice_id
                invoice_status = invoice_status or _display_value(_raw_value(invoices[0], ("status", "invoiceStatus", "status.name")))
                invoice_total = invoice_total if invoice_total is not None else _first_float(invoices, ("total", "subtotal", "balance", "amount"))
                invoice_balance = invoice_balance if invoice_balance is not None else _first_float(invoices, ("balance", "remainingBalance", "amountDue"))
                payment_summary = _payment_summary(invoices)
                if payment_total is None:
                    payment_total = payment_summary["payment_total"]
                if payments_count is None:
                    payments_count = payment_summary["payments_count"]
                invoice_line_items.extend(_line_item_names_from_records(invoices))
                invoice_items.extend(_invoice_items_from_records(invoices))
            invoice_item_records, invoice_items_error = self._related_records(
                "invoice_items",
                self._tenant_path("accounting", "export/invoice-items"),
                {"invoiceIds": ",".join(invoice_ids)} if invoice_ids else {"jobId": job.job_id},
            )
            available_keys["invoice_items"] = _records_keys(invoice_item_records)
            if invoice_items_error:
                missing_data.setdefault("invoice_line_items", invoice_items_error)
            else:
                invoice_line_items.extend(_invoice_item_names(invoice_item_records))
                invoice_items.extend(_invoice_items_from_records(invoice_item_records))
            related_counts["invoice_items"] = len(invoice_items) if invoice_items else len(invoice_line_items)
            if invoice_line_items or invoice_items or "invoice_line_items" in present_fields:
                present_fields.add("invoice_line_items")
            if invoices or invoice_status:
                present_fields.add("invoice_status")
            if payment_total is not None or payments_count is not None or invoice_balance is not None:
                present_fields.add("payments")
            if not invoice_items_error and not invoices_error and not invoice_line_items and not invoice_items:
                present_fields.add("invoice_line_items")

        timesheets, timesheets_error = self._related_records(
            "technician_time",
            self._tenant_path("payroll", "jobs/timesheets"),
            {"jobIds": job.job_id},
        )
        related_counts["technician_time_records"] = len(timesheets)
        available_keys["technician_time"] = _records_keys(timesheets)
        if timesheets_error:
            _mark_missing(missing_data, ("clock_in", "clock_out", "lunch_break"), timesheets_error)
        else:
            time_data = _time_data_from_records(timesheets)
            clock_in_at = clock_in_at or time_data["clock_in_at"]
            clock_out_at = clock_out_at or time_data["clock_out_at"]
            arrived_at = arrived_at or time_data["arrived_at"]
            if time_data["lunch_break_minutes"] is not None:
                lunch_break_minutes = time_data["lunch_break_minutes"]
            if time_data["technician_id"]:
                technician_id = technician_id or time_data["technician_id"]
            if time_data["technician_name"]:
                technician_name = technician_name or time_data["technician_name"]
            present_fields.update({"clock_in", "clock_out", "lunch_break"})
            if arrived_at:
                present_fields.add("arrived_at")
            if technician_id or technician_name:
                present_fields.add("technician")

        if technician_id and clock_in_at and clock_out_at:
            non_job_timesheets, non_job_error = self._related_records(
                "non_job_timesheets",
                self._tenant_path("payroll", "non-job-timesheets"),
                {
                    "technicianId": technician_id,
                    "startedOn": clock_in_at.astimezone(timezone.utc).isoformat(),
                    "endedOn": clock_out_at.astimezone(timezone.utc).isoformat(),
                },
            )
            related_counts["technician_time_records"] = related_counts.get("technician_time_records", 0) + len(non_job_timesheets)
            available_keys["non_job_timesheets"] = _records_keys(non_job_timesheets)
            if not non_job_error and non_job_timesheets:
                non_job_break = _break_minutes(non_job_timesheets)
                if non_job_break is not None:
                    lunch_break_minutes = (lunch_break_minutes or 0) + non_job_break

        notes_records, notes_error = self._related_records(
            "notes",
            self._tenant_path("jpm", f"jobs/{job.job_id}/notes"),
            {},
        )
        related_counts["notes"] = len(notes_records)
        available_keys["notes"] = _records_keys(notes_records)
        if notes_error:
            missing_data.setdefault("notes", notes_error)
        else:
            note_texts = _note_texts(notes_records)
            notes = notes if notes is not None else "\n".join(note_texts)
            present_fields.add("notes")

        attachments, attachments_error = self._related_records(
            "attachments",
            self._tenant_path("jpm", f"jobs/{job.job_id}/attachments"),
            {},
        )
        related_counts["attachments"] = len(attachments)
        available_keys["attachments"] = _records_keys(attachments)
        if attachments_error:
            missing_data.setdefault("photos", attachments_error)
            missing_data.setdefault("supporting_evidence", attachments_error)
        else:
            photo_count = photo_count if photo_count is not None else _photo_count(attachments)
            supporting_evidence_count = supporting_evidence_count if supporting_evidence_count is not None else len(attachments)
            related_counts["photos"] = photo_count or 0
            present_fields.update({"photos", "supporting_evidence"})

        form_records, forms_error = self._related_records(
            "forms",
            self._tenant_path("forms", "submissions"),
            {"jobId": job.job_id},
        )
        related_counts["forms"] = len(form_records)
        available_keys["forms"] = _records_keys(form_records)
        if forms_error:
            missing_data.setdefault("forms", forms_error)
            missing_data.setdefault("hhr", forms_error)
            missing_data.setdefault("authorization", forms_error)
        else:
            forms_count = len(form_records)
            hhr_completed = _records_contain_keywords(form_records, self.settings.service_titan_hhr_keywords)
            authorization_count = _authorization_count(form_records, attachments)
            form_photo_count = _photo_count(form_records)
            if form_photo_count:
                photo_count = (photo_count or 0) + form_photo_count
                related_counts["photos"] = photo_count
                present_fields.add("photos")
                missing_data.pop("photos", None)
            present_fields.update({"forms", "hhr", "authorization"})

        equipment_records, equipment_error = self._related_records(
            "equipment",
            self._tenant_path("equipments", "installed-equipment"),
            {"jobId": job.job_id},
        )
        related_counts["equipment"] = len(equipment_records)
        available_keys["equipment"] = _records_keys(equipment_records)
        if equipment_error:
            missing_data.setdefault("equipment", equipment_error)
        else:
            equipment_count = len(equipment_records)
            equipment_complete = _equipment_complete(equipment_records)
            present_fields.add("equipment")

        purchase_order_records, purchase_orders_error = self._related_records(
            "purchase_orders",
            self._tenant_path("inventory", "purchase-orders"),
            {"jobId": job.job_id},
        )
        related_counts["purchase_orders"] = len(purchase_order_records)
        available_keys["purchase_orders"] = _records_keys(purchase_order_records)
        if purchase_orders_error:
            missing_data.setdefault("purchase_orders", purchase_orders_error)
            missing_data.setdefault("po_vendor_document", purchase_orders_error)
            missing_data.setdefault("po_attachments", purchase_orders_error)
            missing_data.setdefault("po_reconciliation", purchase_orders_error)
        else:
            purchase_orders = _purchase_order_summaries(purchase_order_records)
            purchase_orders_count = len(purchase_orders)
            po_received_not_reconciled_count = sum(
                1 for record in purchase_orders if record.get("received") and not record.get("reconciled")
            )
            po_missing_vendor_document_count = sum(
                1 for record in purchase_orders if record.get("received") and not record.get("vendor_document_present")
            )
            po_missing_attachment_count = sum(
                1 for record in purchase_orders if record.get("received") and not record.get("attachments_count")
            )
            po_not_synced_count = None
            present_fields.update({"purchase_orders", "po_vendor_document", "po_attachments", "po_reconciliation"})

        job_history, history_error = self._related_records(
            "job_history",
            self._tenant_path("jpm", f"jobs/{job.job_id}/history"),
            {},
        )
        related_counts["job_history"] = len(job_history)
        available_keys["job_history"] = _records_keys(job_history)
        if history_error and "completed_phases" not in present_fields:
            missing_data.setdefault("completed_phases", history_error)
        elif job_history and not completed_phases:
            completed_phases = _completed_phases(job_history)
            if completed_phases:
                present_fields.add("completed_phases")

        estimate_records, estimate_error = self._related_records(
            "estimates",
            self._tenant_path("sales", "estimates"),
            {"jobId": job.job_id},
        )
        related_counts["estimates"] = len(estimate_records)
        available_keys["estimates"] = _records_keys(estimate_records)
        if not estimate_error:
            estimate_count = max(
                estimate_count or 0,
                _option_count_from_records(estimate_records, fallback_count=len(estimate_records)),
            )
            same_day_estimate_present = _same_day_estimate_present(estimate_records, job.completed_on or job.modified_on or job.arrival_window_start)
            home_comfort_plan_option_present = _records_contain_keywords(estimate_records, self.settings.service_titan_home_comfort_plan_keywords)
            present_fields.update({"estimates", "same_day_estimate", "home_comfort_plan_option"})
            explicit_options = _explicit_options_presented(estimate_records)
            if explicit_options is not None:
                options_presented = explicit_options
                present_fields.add("options_presented")
        else:
            if "estimates" not in present_fields:
                missing_data.setdefault("estimates", estimate_error)
            missing_data.setdefault("same_day_estimate", estimate_error)
            missing_data.setdefault("home_comfort_plan_option", estimate_error)

        opportunity_records, opportunity_error = self._related_records(
            "opportunities",
            self._tenant_path("sales", "opportunities"),
            {"jobId": job.job_id},
        )
        related_counts["opportunities"] = len(opportunity_records)
        available_keys["opportunities"] = _records_keys(opportunity_records)
        if not opportunity_error:
            opportunity_option_count = _option_count_from_records(opportunity_records, fallback_count=len(opportunity_records))
            if estimate_count is None:
                estimate_count = opportunity_option_count
            elif not estimate_count:
                estimate_count = opportunity_option_count
            else:
                estimate_count = max(estimate_count, opportunity_option_count)
            if home_comfort_plan_option_present is not True:
                home_comfort_plan_option_present = _records_contain_keywords(
                    opportunity_records,
                    self.settings.service_titan_home_comfort_plan_keywords,
                )
            if "options_presented" not in present_fields:
                explicit_options = _explicit_options_presented(opportunity_records)
                if explicit_options is not None:
                    options_presented = explicit_options
                    present_fields.add("options_presented")
        elif "estimates" not in present_fields:
            missing_data.setdefault("estimates", opportunity_error)
        if estimate_count is not None:
            related_counts["options"] = estimate_count
        if "options_presented" not in present_fields:
            missing_data.setdefault("options_presented", "sales estimates/opportunities did not expose an explicit options-presented field")

        diagnostic_summary = _diagnostic_fee_summary(invoice_items, invoice_line_items, self.settings.service_titan_diagnostic_fee_keywords)
        if diagnostic_fee_present is None:
            diagnostic_fee_present = diagnostic_summary["present"]
        if diagnostic_fee_charged is None:
            diagnostic_fee_charged = diagnostic_summary["charged"]
        if diagnostic_fee_waived is None:
            diagnostic_fee_waived = diagnostic_summary["waived"]
        if repair_sold is None:
            repair_sold = _repair_sold(invoice_items, invoice_line_items, self.settings.service_titan_diagnostic_fee_keywords)
        if downpayment_recorded is None:
            downpayment_recorded = _has_keywords(
                " ".join(invoice_line_items),
                ("deposit", "down payment", "downpayment", "prepayment"),
            ) or bool((payment_total or 0) > 0 and _has_keywords(_records_text(purchase_orders), ("special", "order")))

        audit_text = " ".join(
            part
            for part in (
                notes or "",
                " ".join(invoice_line_items),
                _records_text(form_records),
                _records_text(estimate_records),
                _records_text(opportunity_records),
                _records_text(purchase_order_records),
            )
            if part
        )
        if "notes" in present_fields:
            follow_up_needed = _has_keywords(audit_text, ("follow up", "follow-up", "call back", "return visit", "needs follow"))
            follow_up_task_present = _has_keywords(audit_text, ("follow up task", "follow-up task", "reminder", "recall", "return scheduled"))
            special_order_detected = _has_keywords(audit_text, ("special order", "special-order", "ordered part", "parts ordered", "order part"))
            special_order_missing_fields = _missing_special_order_note_fields(
                audit_text,
                self.settings.service_titan_special_order_required_note_fields,
            ) if special_order_detected else []
            special_order_reminder_present = _has_keywords(audit_text, ("reminder", "expected part arrival", "eta reminder")) if special_order_detected else False
            lead_turnover_required = _has_keywords(audit_text, ("lead turnover", "turn over lead", "sales lead", "lead set"))
            lead_turnover_documented = _has_keywords(audit_text, ("lead turnover note", "lead notes", "turned over", "sales notified")) if lead_turnover_required else False
            scope_change_detected = _has_keywords(audit_text, ("scope change", "changed scope", "additional scope", "change order"))
            scope_change_escalated = _has_keywords(audit_text, ("dastan", "manager notified", "ops notified", "escalated")) if scope_change_detected else False
            cancellation_after_materials_detected = _has_keywords(
                audit_text,
                ("cancelled after materials", "canceled after materials", "cancellation after materials", "cancelled after parts", "canceled after parts"),
            )
            cancellation_escalated = _has_keywords(audit_text, ("manager notified", "ops notified", "warehouse notified", "escalated")) if cancellation_after_materials_detected else False
            defective_part_detected = _has_keywords(audit_text, ("defective part", "bad part", "failed part", "vendor warranty"))
            warranty_claim_documented = _has_keywords(audit_text, ("warranty claim", "vendor claim", "rma", "defective claim")) if defective_part_detected else False

        if self.settings.service_titan_audit_debug_fields:
            logger.info(
                "servicetitan_field_availability",
                extra={
                    "job_id": job.job_id,
                    "available_keys": available_keys,
                    "related_counts": related_counts,
                    "present_fields": sorted(present_fields),
                    "missing_data_fields": sorted(missing_data),
                },
            )

        return replace(
            job,
            appointment_id=appointment_id,
            appointment_status=appointment_status,
            invoice_id=invoice_id,
            technician_id=technician_id,
            technician_name=technician_name,
            business_unit_id=business_unit_id,
            business_unit_name=business_unit_name,
            job_type_id=job_type_id,
            job_type_name=job_type_name,
            department=department,
            trade=trade,
            workflow=workflow,
            tag_ids=_dedupe_strings(tag_ids),
            tag_names=_dedupe_strings(tag_names),
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            cancellation_reason=cancellation_reason,
            arrival_window_start=arrival_window_start,
            arrival_window_end=arrival_window_end,
            arrived_at=arrived_at,
            clock_in_at=clock_in_at,
            clock_out_at=clock_out_at,
            lunch_break_minutes=lunch_break_minutes,
            invoice_line_items=_dedupe_strings(invoice_line_items),
            invoice_items=invoice_items,
            invoice_status=invoice_status,
            invoice_total=invoice_total,
            invoice_balance=invoice_balance,
            payment_total=payment_total,
            payments_count=payments_count,
            diagnostic_fee_present=diagnostic_fee_present,
            diagnostic_fee_charged=diagnostic_fee_charged,
            diagnostic_fee_waived=diagnostic_fee_waived,
            repair_sold=repair_sold,
            completed_phases=_dedupe_strings(completed_phases),
            options_presented=options_presented,
            estimate_count=estimate_count,
            same_day_estimate_present=same_day_estimate_present,
            home_comfort_plan_option_present=home_comfort_plan_option_present,
            notes=notes,
            photo_count=photo_count,
            supporting_evidence_count=supporting_evidence_count,
            forms_count=forms_count,
            hhr_completed=hhr_completed,
            equipment_count=equipment_count,
            equipment_complete=equipment_complete,
            authorization_count=authorization_count,
            follow_up_needed=follow_up_needed,
            follow_up_task_present=follow_up_task_present,
            special_order_detected=special_order_detected,
            special_order_missing_fields=special_order_missing_fields,
            special_order_reminder_present=special_order_reminder_present,
            downpayment_recorded=downpayment_recorded,
            lead_turnover_required=lead_turnover_required,
            lead_turnover_documented=lead_turnover_documented,
            purchase_orders=purchase_orders,
            purchase_orders_count=purchase_orders_count,
            po_received_not_reconciled_count=po_received_not_reconciled_count,
            po_missing_vendor_document_count=po_missing_vendor_document_count,
            po_missing_attachment_count=po_missing_attachment_count,
            po_not_synced_count=po_not_synced_count,
            scope_change_detected=scope_change_detected,
            scope_change_escalated=scope_change_escalated,
            cancellation_after_materials_detected=cancellation_after_materials_detected,
            cancellation_escalated=cancellation_escalated,
            defective_part_detected=defective_part_detected,
            warranty_claim_documented=warranty_claim_documented,
            present_fields=present_fields,
            related_counts=related_counts,
            available_keys=available_keys,
            missing_data=missing_data,
            raw={
                **job.raw,
                "appointments": appointments,
                "invoices": invoices,
                "invoiceItems": invoice_item_records,
                "forms": form_records,
                "equipment": equipment_records,
                "purchaseOrders": purchase_order_records,
                "technicianTimesheets": timesheets,
                "nonJobTimesheets": non_job_timesheets,
            },
        )

    def _related_records(self, category: str, path: str, params: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
        if not self._should_fetch_related_category(category):
            return [], self._related_skip_reason(category)
        if category in self._disabled_related_categories:
            return [], self._disabled_related_reasons.get(category) or f"{category} endpoint unavailable earlier in this process"
        payload = {"pageSize": str(self.settings.service_titan_audit_page_size), "includeTotal": "true", **{k: v for k, v in params.items() if v}}
        try:
            records = self._get_paginated(path, payload, related_category=category)
            if category in self._disabled_related_categories:
                return [], self._disabled_related_reasons.get(category) or f"{category} endpoint unavailable earlier in this process"
            return records, None
        except ServiceTitanApiError as exc:
            if exc.status in {400, 401, 403, 404, 405}:
                self._disabled_related_categories.add(category)
                self._disabled_related_reasons[category] = f"{path} returned HTTP {exc.status}"
            logger.warning(
                "servicetitan_related_fetch_unavailable",
                extra={"category": category, "path": path, "status": exc.status, "error_message": exc.message},
            )
            return [], f"{path} returned HTTP {exc.status}"
        except Exception as exc:
            logger.warning("servicetitan_related_fetch_failed", extra={"category": category, "path": path, "error": str(exc)})
            return [], f"{path} request failed"

    def _related_skip_reason(self, category: str) -> str:
        if self._should_prefilter_sales_only():
            return f"{category} skipped for Sales-only enabled rules"
        if self._should_fetch_hvac_only():
            return f"{category} skipped for HVAC-only enabled rules"
        if self._should_fetch_plumbing_only():
            return f"{category} skipped for Plumbing-only enabled rules"
        return f"{category} skipped by active ruleset configuration"

    def _tenant_path(self, api: str, suffix: str) -> str:
        return f"/{api}/v2/tenant/{self.settings.servicetitan_tenant_id}/{suffix.lstrip('/')}"

    def _get_paginated(self, path: str, params: dict[str, str], *, related_category: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        page_size = int(params.get("pageSize") or self.settings.service_titan_audit_page_size)
        while page <= max(1, self.settings.service_titan_audit_max_pages):
            payload = dict(params)
            payload["page"] = str(page)
            data = self._get(path, payload)
            page_records = _records_from_response(data)
            if (
                related_category
                and page_records
                and len(page_records) >= page_size
                and _has_scoped_filter_params(params)
                and not _records_have_filter_fields(page_records, params)
            ):
                reason = f"{path} returned an unscoped page of {len(page_records)} records; related category disabled for this process"
                self._disabled_related_categories.add(related_category)
                self._disabled_related_reasons[related_category] = reason
                logger.warning(
                    "servicetitan_related_fetch_unscoped",
                    extra={"category": related_category, "path": path, "returned_count": len(page_records), "page_size": page_size},
                )
                return []
            filtered_records = _filter_records_for_params(page_records, params)
            if len(page_records) > page_size:
                logger.warning(
                    "servicetitan_page_size_ignored",
                    extra={"path": path, "returned_count": len(page_records), "page_size": page_size},
                )
                if related_category and len(page_records) > max(page_size * 10, 1000):
                    reason = f"{path} returned an overbroad page of {len(page_records)} records; related category disabled for this process"
                    self._disabled_related_categories.add(related_category)
                    self._disabled_related_reasons[related_category] = reason
                    logger.warning(
                        "servicetitan_related_fetch_overbroad",
                        extra={"category": related_category, "path": path, "returned_count": len(page_records), "page_size": page_size},
                    )
                    return []
                filtered_records = filtered_records[:page_size]
            records.extend(filtered_records)
            if not _has_more(data, page, len(page_records), page_size):
                break
            page += 1
        if page > self.settings.service_titan_audit_max_pages:
            logger.warning("servicetitan_pagination_limited", extra={"path": path, "max_pages": self.settings.service_titan_audit_max_pages})
        return records

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("ServiceTitan credentials are not configured")
        token = self._access_token_or_refresh()
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.settings.servicetitan_base_url}{path}"
        if query:
            url = f"{url}?{query}"
        response = self.http.request_json(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "ST-App-Key": self.settings.servicetitan_app_key,
            },
        )
        if response.status >= 400:
            raise ServiceTitanApiError(response.status, response.data)
        return response.data

    def _access_token_or_refresh(self) -> str:
        now = datetime.now(timezone.utc)
        if self._access_token and self._token_expires_at and now < self._token_expires_at - timedelta(seconds=60):
            return self._access_token
        response = self.http.request_form(
            "POST",
            self.settings.servicetitan_auth_url,
            headers={"Accept": "application/json", "Content-Type": AUTH_CONTENT_TYPE, "User-Agent": DEFAULT_USER_AGENT},
            body={
                "grant_type": "client_credentials",
                "client_id": self.settings.servicetitan_client_id,
                "client_secret": self.settings.servicetitan_client_secret,
            },
        )
        logger.debug(
            "servicetitan_auth_request_completed",
            extra={
                "auth_url": self.settings.servicetitan_auth_url,
                "grant_type": "client_credentials",
                "client_id_present": bool(self.settings.servicetitan_client_id),
                "client_secret_present": bool(self.settings.servicetitan_client_secret),
                "app_key_present": bool(self.settings.servicetitan_app_key),
                "content_type": AUTH_CONTENT_TYPE,
                "status": response.status,
            },
        )
        if response.status >= 400 or not response.data.get("access_token"):
            raise ServiceTitanApiError(response.status, response.data)
        expires_in = int(response.data.get("expires_in") or 900)
        self._access_token = str(response.data["access_token"])
        self._token_expires_at = now + timedelta(seconds=max(60, expires_in))
        return self._access_token


def _scope_filter_decision_is_uncertain(job: ServiceTitanJob, metadata: dict[str, object]) -> bool:
    if metadata.get("scope_decision") != "include_mismatch":
        return False
    field = str(metadata.get("field") or "")
    raw_patterns = metadata.get("patterns")
    patterns = [str(value) for value in raw_patterns] if isinstance(raw_patterns, list) else []
    values = _scope_filter_context_values(job, field)
    if not values:
        return True
    return bool(
        patterns
        and all(_looks_like_numeric_identifier(value) for value in values)
        and any(not _looks_like_numeric_identifier(pattern) for pattern in patterns)
    )


def _scope_filter_context_values(job: ServiceTitanJob, field: str) -> list[str]:
    if field == "status":
        return [job.status] if job.status else []
    if field == "job_type":
        return [value for value in (job.job_type_id, job.job_type_name) if value]
    if field == "business_unit":
        return [value for value in (job.business_unit_id, job.business_unit_name) if value]
    if field == "department":
        return [job.department] if job.department else []
    if field == "trade":
        return [job.trade] if job.trade else []
    if field == "workflow":
        return [
            value
            for value in (
                job.workflow,
                job.job_type_name,
                job.business_unit_name,
                job.department,
                job.trade,
                *job.tag_names,
            )
            if value
        ]
    if field == "tags":
        return [*job.tag_ids, *job.tag_names]
    if field == "campaign":
        return [value for value in (job.campaign_id, job.campaign_name) if value]
    return []


def _looks_like_numeric_identifier(value: str) -> bool:
    return value.strip().isdigit()


def parse_service_titan_job(payload: dict[str, Any], settings: Settings) -> ServiceTitanJob:
    present: set[str] = set()
    appointment = _first_dict(payload.get("appointments")) or _first_dict(payload.get("appointment")) or {}
    assignment = _first_dict(payload.get("assignments")) or _first_dict(payload.get("technicianAssignments")) or {}
    invoice = _first_dict(payload.get("invoices")) or _first_dict(payload.get("invoice")) or payload.get("invoice") or {}
    if not isinstance(invoice, dict):
        invoice = {}
    business_unit = _first_dict(payload.get("businessUnit")) or {}
    job_type = _first_dict(payload.get("jobType")) or _first_dict(payload.get("type")) or {}
    department_record = _first_dict(payload.get("department")) or {}
    campaign_record = _first_dict(payload.get("campaign")) or {}

    job_id = str(_value(payload, ("id", "jobId"), present, "job_id") or "")
    appointment_id = str(
        _value(appointment, ("id", "appointmentId"), present, "appointment_id")
        or _value(payload, ("appointmentId",), present, "appointment_id")
        or ""
    )
    technician_id = str(
        _value(assignment, ("technicianId", "employeeId"), present, "technician")
        or _nested_value(payload, ("technician", "id"), present, "technician")
        or _value(payload, ("technicianId",), present, "technician")
        or ""
    )
    technician_name = str(
        _value(assignment, ("technicianName", "employeeName", "name"), present, "technician")
        or _nested_value(payload, ("technician", "name"), present, "technician")
        or _value(payload, ("technicianName",), present, "technician")
        or ""
    )
    dispatcher_id = str(
        _value(payload, ("dispatcherId", "createdById", "bookedById"), present, "dispatcher")
        or _nested_value(payload, ("dispatcher", "id"), present, "dispatcher")
        or ""
    )
    dispatcher_name = str(
        _value(payload, ("dispatcherName", "createdByName", "bookedByName"), present, "dispatcher")
        or _nested_value(payload, ("dispatcher", "name"), present, "dispatcher")
        or ""
    )
    business_unit_id = str(
        _value(payload, ("businessUnitId",), present, "business_unit")
        or _value(business_unit, ("id",), present, "business_unit")
        or ""
    )
    business_unit_name = str(
        _value(payload, ("businessUnitName",), present, "business_unit")
        or _value(business_unit, ("name", "displayName"), present, "business_unit")
        or ""
    )
    job_type_id = str(
        _value(payload, ("jobTypeId", "typeId"), present, "job_type")
        or _value(job_type, ("id",), present, "job_type")
        or ""
    )
    job_type_name = str(
        _value(payload, ("jobTypeName", "typeName"), present, "job_type")
        or _value(job_type, ("name", "displayName", "title"), present, "job_type")
        or ""
    )
    department = str(
        _value(payload, ("departmentName",), present, "department")
        or _value(department_record, ("name", "displayName"), present, "department")
        or ""
    )
    trade = _display_value(
        _value(payload, ("tradeName",), present, "trade")
        or _nested_value(payload, ("trade", "name"), present, "trade")
        or _value(payload, ("trade",), present, "trade")
    )
    workflow = _display_value(
        _value(payload, ("workflowName", "jobClass"), present, "workflow")
        or _nested_value(payload, ("workflow", "name"), present, "workflow")
        or _value(payload, ("workflow",), present, "workflow")
    )
    campaign_id = str(
        _value(payload, ("campaignId",), present, "campaign")
        or _value(campaign_record, ("id",), present, "campaign")
        or ""
    )
    campaign_name = str(
        _value(payload, ("campaignName",), present, "campaign")
        or _value(campaign_record, ("name",), present, "campaign")
        or ""
    )
    cancellation_reason = _display_value(
        _value(payload, ("cancellationReason", "cancelReason", "canceledReason"), present, "cancellation_reason")
        or _nested_value(payload, ("cancellationReason", "name"), present, "cancellation_reason")
    )
    tag_ids, tag_names = _tag_values(payload, present)

    notes_value = _value(payload, ("summary", "notes", "jobNotes", "description"), present, "notes")
    photo_count = _count_from_payload(payload, ("photos", "images", "attachments"), present, "photos")
    evidence_count = _count_from_payload(payload, ("supportingEvidence", "documents", "attachments"), present, "supporting_evidence")
    operational_data = _custom_fields(payload)
    if "customFields" in payload:
        present.add("operational_data_fields")
    line_items = _line_item_names(invoice or payload, present)
    parsed_invoice_items = _invoice_items_from_records([invoice or payload])
    invoice_status = _display_value(_raw_value(invoice, ("status", "invoiceStatus", "status.name")))
    if invoice_status:
        present.add("invoice_status")
    invoice_balance = _float_or_none(_raw_value(invoice, ("balance", "remainingBalance", "amountDue")))
    payment_summary = _payment_summary([invoice])
    if payment_summary["payment_total"] is not None or payment_summary["payments_count"] is not None or invoice_balance is not None:
        present.add("payments")

    completed_phases = _string_list(
        _value(payload, ("completedPhases", "phasesCompleted", "phases"), present, "completed_phases")
    )
    if completed_phases:
        present.add("completed_phases")
    estimate_ids = _id_list(_value(payload, ("estimateIds",), present, "estimates"))
    estimate_count = len(estimate_ids) if "estimates" in present else None

    url = ""
    if settings.servicetitan_job_url_template and job_id:
        try:
            url = settings.servicetitan_job_url_template.format(
                job_id=job_id,
                job_number=_value(payload, ("jobNumber", "number"), present, "job_number") or "",
                tenant_id=settings.servicetitan_tenant_id,
            )
        except KeyError:
            logger.warning("servicetitan_job_url_template_invalid", extra={"job_id": job_id})
    status_value = _value(payload, ("status", "jobStatus"), present, "status")

    return ServiceTitanJob(
        job_id=job_id,
        job_number=str(_value(payload, ("jobNumber", "number"), present, "job_number") or job_id),
        status=_display_value(status_value),
        modified_on=_parse_datetime(_value(payload, ("modifiedOn", "lastModifiedOn", "updatedOn"), present, "modified_on")),
        completed_on=_parse_datetime(_value(payload, ("completedOn", "closedOn", "completedDate"), present, "completed_on")),
        appointment_id=appointment_id,
        appointment_status=_display_value(_value(appointment, ("status", "appointmentStatus", "status.name"), present, "appointment_status")),
        invoice_id=str(_value(invoice, ("id", "invoiceId"), present, "invoice") or _value(payload, ("invoiceId",), present, "invoice") or ""),
        technician_id=technician_id,
        technician_name=technician_name,
        dispatcher_id=dispatcher_id,
        dispatcher_name=dispatcher_name,
        business_unit_id=business_unit_id,
        business_unit_name=business_unit_name,
        job_type_id=job_type_id,
        job_type_name=job_type_name,
        department=department,
        trade=trade,
        workflow=workflow,
        tag_ids=tag_ids,
        tag_names=tag_names,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        cancellation_reason=cancellation_reason,
        customer_name=str(_nested_value(payload, ("customer", "name"), present, "customer") or _value(payload, ("customerName",), present, "customer") or ""),
        arrival_window_start=_parse_datetime(
            _value(appointment, ("arrivalWindowStart", "start"), present, "arrival_window")
            or _value(payload, ("arrivalWindowStart",), present, "arrival_window")
        ),
        arrival_window_end=_parse_datetime(
            _value(appointment, ("arrivalWindowEnd", "end"), present, "arrival_window")
            or _value(payload, ("arrivalWindowEnd",), present, "arrival_window")
        ),
        arrived_at=_parse_datetime(_value(payload, ("arrivedOn", "arrivalTime", "technicianArrivedOn"), present, "arrived_at")),
        clock_in_at=_parse_datetime(_value(payload, ("clockInOn", "clockInTime", "jobClockInOn"), present, "clock_in")),
        clock_out_at=_parse_datetime(_value(payload, ("clockOutOn", "clockOutTime", "jobClockOutOn"), present, "clock_out")),
        lunch_break_minutes=_int_or_none(_value(payload, ("lunchBreakMinutes", "breakMinutes"), present, "lunch_break")),
        invoice_line_items=line_items,
        invoice_items=parsed_invoice_items,
        invoice_status=invoice_status,
        invoice_total=_float_or_none(_value(invoice, ("total", "invoiceTotal", "amount"), present, "invoice")),
        invoice_balance=invoice_balance,
        payment_total=payment_summary["payment_total"],
        payments_count=payment_summary["payments_count"],
        completed_phases=completed_phases,
        operational_data=operational_data,
        operational_data_complete=_bool_or_none(_value(payload, ("operationalDataComplete", "requiredDataComplete"), present, "operational_data")),
        options_presented=_bool_or_none(_value(payload, ("optionsPresented", "goodBetterBestPresented"), present, "options_presented")),
        estimate_count=estimate_count,
        notes=str(notes_value) if notes_value is not None else None,
        photo_count=photo_count,
        supporting_evidence_count=evidence_count,
        url=url,
        present_fields=present,
        related_counts={
            "appointments": 1 if appointment else 0,
            "invoices": 1 if invoice else 0,
            "invoice_items": len(line_items),
            "notes": 1 if notes_value else 0,
            "attachments": photo_count or 0,
            "photos": photo_count or 0,
            "technician_time_records": 0,
        },
        available_keys={"job": _top_level_keys(payload)},
        missing_data={},
        raw=payload,
    )


def parse_service_titan_project(
    payload: dict[str, Any],
    project_type_names: dict[str, str],
    project_status_names: dict[str, str],
    employee_names: dict[str, str],
    project_url_template: str = "https://go.servicetitan.com/#/project/{project_id}",
) -> ServiceTitanProject:
    project_id = str(_raw_value(payload, ("projectId", "id")) or "")
    project_type_id = str(_raw_value(payload, ("projectTypeId", "projectType.id", "type.id")) or "")
    status_id = str(_raw_value(payload, ("statusId", "projectStatus.id", "status.id")) or "")
    status = _display_value(_raw_value(payload, ("status", "projectStatus.name", "status.name"))) or project_status_names.get(status_id, "")
    project_manager_ids = _id_list(_raw_value(payload, ("projectManagerIds", "projectManagerId", "projectManagers")))
    business_units = _raw_value(payload, ("businessUnits", "businessUnit", "businessUnitNames", "businessUnitName"))
    custom_fields, custom_fields_available = _project_custom_fields(payload)
    return ServiceTitanProject(
        project_id=project_id,
        project_number=str(_raw_value(payload, ("number", "projectNumber")) or project_id),
        project_type_id=project_type_id,
        project_type_name=_display_value(_raw_value(payload, ("projectType.name", "type.name", "projectType"))) or project_type_names.get(project_type_id, ""),
        status_id=status_id,
        status=status,
        sub_status_id=str(_raw_value(payload, ("subStatusId", "subStatus.id")) or ""),
        created_on=_parse_datetime(_raw_value(payload, ("createdOn", "createdDate"))),
        modified_on=_parse_datetime(_raw_value(payload, ("modifiedOn", "updatedOn"))),
        start_date=_parse_datetime(_raw_value(payload, ("startDate", "installDate", "scheduledStart", "installationDate"))),
        target_completion_date=_parse_datetime(_raw_value(payload, ("targetCompletionDate", "targetCompletedOn"))),
        actual_completion_date=_parse_datetime(_raw_value(payload, ("actualCompletionDate", "completedOn", "closedOn"))),
        business_unit_ids=_id_list(_raw_value(payload, ("businessUnitIds", "businessUnits", "businessUnit"))),
        business_unit_names=_name_list(business_units),
        client_name=str(_raw_value(payload, ("customer.name", "customer.displayName", "client.name", "clientName", "customerName")) or "").strip(),
        job_ids=_id_list(_raw_value(payload, ("jobIds", "jobs"))),
        project_manager_ids=project_manager_ids,
        project_manager_names=[employee_names[identifier] for identifier in project_manager_ids if identifier in employee_names],
        custom_fields=custom_fields,
        custom_fields_available=custom_fields_available,
        url=_build_project_url(project_url_template, project_id),
        raw=payload,
    )


def _build_project_url(project_url_template: str, project_id: str) -> str:
    if not project_id:
        return ""
    template = project_url_template or "https://go.servicetitan.com/#/project/{project_id}"
    try:
        return template.format(project_id=project_id)
    except KeyError:
        logger.warning("servicetitan_project_url_template_invalid", extra={"project_id": project_id})
        return ""


def parse_service_titan_project_task(payload: dict[str, Any]) -> ServiceTitanProjectTask:
    assigned_to_id = str(_raw_value(payload, ("assignedToId", "assigneeId", "assignedTo.id")) or "")
    return ServiceTitanProjectTask(
        task_id=str(_raw_value(payload, ("id", "taskId")) or ""),
        task_number=str(_raw_value(payload, ("taskNumber", "number")) or ""),
        project_id=str(_raw_value(payload, ("projectId", "project.id")) or ""),
        job_id=str(_raw_value(payload, ("jobId", "job.id")) or ""),
        job_number=str(_raw_value(payload, ("jobNumber", "job.number")) or ""),
        name=str(_raw_value(payload, ("name", "title")) or ""),
        assigned_to_id=assigned_to_id,
        assigned_to_name=str(_raw_value(payload, ("assignedTo.name", "assignee.name")) or ""),
        due_at=_parse_datetime(_raw_value(payload, ("completeBy", "dueDate", "dueOn"))),
        created_on=_parse_datetime(_raw_value(payload, ("createdOn", "createdDate"))),
        modified_on=_parse_datetime(_raw_value(payload, ("modifiedOn", "updatedOn"))),
        closed_on=_parse_datetime(_raw_value(payload, ("closedOn", "completedOn"))),
        status=_display_value(_raw_value(payload, ("status", "status.name"))),
        is_closed=_bool_or_none(_raw_value(payload, ("isClosed", "closed"))),
        raw=payload,
    )


def _pm_project_matches_scope(
    project: ServiceTitanProject,
    project_type_ids: set[str] | None,
    business_unit_ids: set[str] | None,
    business_unit_names: set[str] | None,
    exclude_keywords: tuple[str, ...],
) -> bool:
    if project_type_ids is not None and project.project_type_id not in project_type_ids:
        return False
    if business_unit_ids or business_unit_names:
        business_unit_matched = bool(business_unit_ids and set(project.business_unit_ids) & business_unit_ids)
        normalized_allowed = {_normalize_key(value) for value in (business_unit_names or set())}
        normalized_project = {_normalize_key(value) for value in project.business_unit_names}
        business_unit_matched = business_unit_matched or bool(normalized_allowed and normalized_project & normalized_allowed)
        if not business_unit_matched:
            return False
    if exclude_keywords:
        text = " ".join(
            value
            for value in (
                project.project_type_name,
                project.status,
                *project.business_unit_ids,
                *project.business_unit_names,
            )
            if value
        ).lower()
        if any(keyword in text for keyword in exclude_keywords):
            return False
    return True


def _install_prefilter_matches(
    job: ServiceTitanJob,
    business_unit_ids: set[str],
    business_unit_names: set[str],
    keywords: list[str],
) -> bool:
    return not _install_prefilter_failed_gates(job, business_unit_ids, business_unit_names, keywords)


def _install_prefilter_failed_gates(
    job: ServiceTitanJob,
    business_unit_ids: set[str],
    business_unit_names: set[str],
    keywords: list[str],
) -> tuple[str, ...]:
    allowed_names = {_normalize_key(value) for value in business_unit_names if value}
    normalized_keywords = {_normalize_key(value) for value in keywords if value}
    business_unit_name = _normalize_key(job.business_unit_name) if job.business_unit_name else ""
    business_unit_candidate = business_unit_name in allowed_names or bool(job.business_unit_id and job.business_unit_id in business_unit_ids)
    job_type_name = _normalize_key(job.job_type_name) if job.job_type_name else ""
    job_type_candidate = bool(job_type_name and any(keyword in job_type_name for keyword in normalized_keywords))
    failed: list[str] = []
    if not business_unit_candidate:
        failed.append("business_unit")
    if not job_type_candidate or _install_job_type_excluded(job_type_name):
        failed.append("job_type")
    return tuple(failed)


def install_strict_scope_failed_gates(
    job: ServiceTitanJob,
    business_unit_names: set[str],
    keywords: list[str],
) -> tuple[str, ...]:
    allowed_names = {_normalize_key(value) for value in business_unit_names if value}
    normalized_keywords = {_normalize_key(value) for value in keywords if value}
    business_unit_name = _normalize_key(job.business_unit_name) if job.business_unit_name else ""
    job_type_name = _normalize_key(job.job_type_name) if job.job_type_name else ""
    failed: list[str] = []
    if not business_unit_name or business_unit_name not in allowed_names:
        failed.append("business_unit")
    if (
        not job_type_name
        or not any(keyword in job_type_name for keyword in normalized_keywords)
        or _install_job_type_excluded(job_type_name)
    ):
        failed.append("job_type")
    return tuple(failed)


def _install_job_type_excluded(normalized_job_type: str) -> bool:
    return any(
        word in normalized_job_type
        for word in (
            "service call",
            "maintenance",
            "warranty",
            "recall",
            "sales",
            "estimate",
            "standby",
            "internal",
            "placeholder",
            "city inspection",
        )
    )


def _hydrate_install_scope_names(
    job: ServiceTitanJob,
    business_unit_name_map: dict[str, str],
    job_type_name_map: dict[str, str],
) -> ServiceTitanJob:
    business_unit_name = job.business_unit_name or business_unit_name_map.get(job.business_unit_id, "")
    job_type_name = job.job_type_name or job_type_name_map.get(job.job_type_id, "")
    if business_unit_name == job.business_unit_name and job_type_name == job.job_type_name:
        return job
    present_fields = set(job.present_fields)
    if business_unit_name:
        present_fields.add("business_unit")
    if job_type_name:
        present_fields.add("job_type")
    return replace(
        job,
        business_unit_name=business_unit_name,
        job_type_name=job_type_name,
        present_fields=present_fields,
    )


def _log_install_scope_drop(job: ServiceTitanJob, failed_gates: tuple[str, ...]) -> None:
    logger.info(
        "install_audit_scope_drop",
        extra={
            "job_id": job.job_id,
            "business_unit_id": job.business_unit_id,
            "business_unit_name": job.business_unit_name,
            "job_type_id": job.job_type_id,
            "job_type_name": job.job_type_name,
            "failed_gates": list(failed_gates),
        },
    )


def _job_known_outside_install_window(job: ServiceTitanJob, window_start: datetime, window_end: datetime) -> bool:
    records = job.raw.get("appointments")
    appointments = [item for item in records if isinstance(item, dict)] if isinstance(records, list) else []
    starts: list[datetime] = []
    ends: list[datetime] = []
    for appointment in appointments:
        status = _normalize_key(str(_raw_value(appointment, ("status", "appointmentStatus", "status.name")) or ""))
        if any(word in status for word in ("cancel", "rescheduled", "no access")):
            continue
        start = _parse_datetime(
            _raw_value(appointment, ("arrivalWindowStart", "scheduledStart", "scheduledStartOn", "start", "startDate"))
        )
        end = _parse_datetime(
            _raw_value(appointment, ("arrivalWindowEnd", "scheduledEnd", "scheduledEndOn", "end", "endDate"))
        )
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    if not starts and job.arrival_window_start:
        starts.append(job.arrival_window_start)
    if not ends and job.arrival_window_end:
        ends.append(job.arrival_window_end)
    if not starts and not ends:
        return False
    earliest_start = min(starts or ends).astimezone(timezone.utc)
    latest_end = max(ends or starts).astimezone(timezone.utc)
    return latest_end < window_start.astimezone(timezone.utc) or earliest_start > window_end.astimezone(timezone.utc)


def _pm_invoice_matches_project(record: dict[str, Any], project: ServiceTitanProject, job_id: str) -> bool:
    invoice_project_id = str(_raw_value(record, ("projectId", "project.id")) or "")
    invoice_job_id = str(_raw_value(record, ("jobId", "job.id")) or "")
    if invoice_project_id and project.project_id and invoice_project_id != project.project_id:
        return False
    if invoice_job_id and invoice_job_id != job_id:
        return False
    return bool(invoice_project_id or invoice_job_id)


def _records_from_response(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [record for record in data if isinstance(record, dict)]
    records = data.get("data")
    if records is None:
        records = data.get("items", data.get("results", []))
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return []


def _filter_records_for_params(records: list[dict[str, Any]], params: dict[str, str]) -> list[dict[str, Any]]:
    filters = _filter_specs_for_params(params)
    filters = [(expected, paths) for expected, paths in filters if expected]
    if not filters:
        return records

    matched: list[dict[str, Any]] = []
    saw_filter_field = False
    for record in records:
        include = True
        for expected, paths in filters:
            values = _identifier_values(record, paths)
            if values:
                saw_filter_field = True
                if values.isdisjoint(expected):
                    include = False
                    break
        if include:
            matched.append(record)
    return matched if saw_filter_field else records


def _filter_specs_for_params(params: dict[str, str]) -> list[tuple[set[str], tuple[str, ...]]]:
    filters: list[tuple[set[str], tuple[str, ...]]] = []
    if params.get("projectId"):
        filters.append(({str(params["projectId"])}, ("projectId", "project.id")))
    if params.get("jobId"):
        filters.append(({str(params["jobId"])}, ("jobId", "job.id", "job.jobId")))
    if params.get("jobIds"):
        filters.append((_csv_values(params["jobIds"]), ("jobId", "job.id", "job.jobId")))
    if params.get("invoiceIds"):
        filters.append((_csv_values(params["invoiceIds"]), ("invoiceId", "invoice.id", "invoice.invoiceId")))
    if params.get("appointmentIds"):
        filters.append((_csv_values(params["appointmentIds"]), ("appointmentId", "appointment.id")))
    if params.get("technicianId"):
        filters.append(({str(params["technicianId"])}, ("technicianId", "technician.id", "employeeId", "employee.id")))
    return filters


def _has_scoped_filter_params(params: dict[str, str]) -> bool:
    return any(expected for expected, _paths in _filter_specs_for_params(params))


def _records_have_filter_fields(records: list[dict[str, Any]], params: dict[str, str]) -> bool:
    filters = [(expected, paths) for expected, paths in _filter_specs_for_params(params) if expected]
    if not filters:
        return True
    for record in records:
        for _expected, paths in filters:
            if _identifier_values(record, paths):
                return True
    return False


def _csv_values(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _identifier_values(record: dict[str, Any], paths: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for path in paths:
        value = _value_at_path(record, path)
        if value is not None and value != "":
            values.add(str(value))
    return values


def _value_at_path(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _has_more(data: dict[str, Any], page: int, count: int, page_size: int) -> bool:
    if bool(data.get("hasMore") or data.get("has_more")):
        return True
    total = data.get("totalCount") or data.get("total")
    if isinstance(total, int):
        return page * page_size < total
    return count >= page_size


def _value(source: dict[str, Any], names: tuple[str, ...], present: set[str], field_name: str) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            present.add(field_name)
            return source.get(name)
    return None


def _nested_value(source: dict[str, Any], path: tuple[str, ...], present: set[str], field_name: str) -> Any:
    current: Any = source
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    present.add(field_name)
    return current


def _first_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_notion_datetime(str(value))
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _count_from_payload(payload: dict[str, Any], names: tuple[str, ...], present: set[str], field_name: str) -> int | None:
    value = _value(payload, names, present, field_name)
    if value is None:
        return None
    if isinstance(value, list):
        return len(value)
    return _int_or_none(value)


def _line_item_names(source: dict[str, Any], present: set[str]) -> list[str]:
    items = _value(source, ("lineItems", "items", "invoiceItems"), present, "invoice_line_items")
    if items is None:
        return []
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("description") or item.get("skuName") or item.get("displayName")
            if name:
                names.append(str(name))
        elif item:
            names.append(str(item))
    return names


def _custom_fields(payload: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    raw_fields = payload.get("customFields")
    if not isinstance(raw_fields, list):
        return fields
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("typeName") or item.get("key")
        value = item.get("value")
        if name and value is not None:
            fields[str(name)] = str(value)
    return fields


def _project_custom_fields(payload: dict[str, Any]) -> tuple[dict[str, str], bool]:
    fields: dict[str, str] = {}
    available = False
    for key in (
        "projectDetails",
        "projectDetail",
        "projectDetailsSections",
        "projectDetailSections",
        "details",
        "detailSections",
        "sections",
    ):
        if key in payload:
            fields.update(_project_detail_fields(payload.get(key)))
            available = True

    if "customFields" not in payload:
        return fields, available
    raw_fields = payload.get("customFields")
    if not isinstance(raw_fields, list):
        return fields, True
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("label") or item.get("typeName") or item.get("fieldName") or item.get("customFieldTypeName")
        if not name:
            type_id = item.get("typeId") or item.get("customFieldTypeId")
            name = f"type:{type_id}" if type_id else ""
        if not name:
            continue
        value = item.get("value")
        if value is None:
            value = item.get("textValue") or item.get("stringValue") or item.get("displayValue") or ""
        fields[str(name)] = str(value).strip()
    return fields, True


def _project_detail_fields(raw: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    permit_section_seen = False

    def visit(node: Any, section_name: str = "") -> None:
        nonlocal permit_section_seen
        if isinstance(node, list):
            for item in node:
                visit(item, section_name)
            return
        if not isinstance(node, dict):
            return

        name = _detail_field_name(node)
        normalized_name = _normalize_key(name)
        child_keys = ("fields", "items", "customFields", "details", "values", "questions", "sections", "children")
        has_children = any(isinstance(node.get(key), (list, dict)) for key in child_keys)
        value = _detail_field_value(node)
        has_value = value is not None

        child_section = section_name
        if has_children and name:
            if normalized_name == "permit":
                permit_section_seen = True
                child_section = "PERMIT"
            elif normalized_name not in {"project details", "project detail", "details"}:
                child_section = section_name or str(name)

        if name and has_value:
            clean_value = str(value or "").strip()
            fields[str(name)] = clean_value
            if section_name:
                fields[f"{section_name} {name}"] = clean_value
            if section_name == "PERMIT":
                if clean_value:
                    existing = fields.get("PERMIT", "")
                    fields["PERMIT"] = f"{existing}; {clean_value}".strip("; ") if existing else clean_value
                else:
                    fields.setdefault("PERMIT", "")

        for key in child_keys:
            if key in node:
                visit(node.get(key), child_section)

    visit(raw)
    if permit_section_seen:
        fields.setdefault("PERMIT", "")
    return fields


def _detail_field_name(node: dict[str, Any]) -> str:
    raw_name = (
        node.get("name")
        or node.get("label")
        or node.get("title")
        or node.get("fieldName")
        or node.get("customFieldTypeName")
        or node.get("sectionName")
        or node.get("typeName")
        or node.get("key")
    )
    return str(raw_name or "").strip()


def _detail_field_value(node: dict[str, Any]) -> Any:
    for key in ("value", "textValue", "stringValue", "displayValue", "selectedValue", "number", "status"):
        if key in node:
            value = node.get(key)
            if isinstance(value, dict):
                return _display_value(value)
            return value
    return None


def _normalize_key(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


def _string_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("phase")
                if name:
                    result.append(str(name))
            else:
                result.append(str(item))
        return result
    return [str(value)]


def _id_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        raw = _raw_value(item, ("id", "estimateId", "value")) if isinstance(item, dict) else item
        if raw is not None and str(raw).strip():
            result.append(str(raw))
    return _dedupe_strings(result)


def _name_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        raw = _raw_value(item, ("name", "displayName", "businessUnitName", "value")) if isinstance(item, dict) else item
        if raw is not None and str(raw).strip():
            result.append(str(raw))
    return _dedupe_strings(result)


def _tag_values(payload: dict[str, Any], present: set[str]) -> tuple[list[str], list[str]]:
    raw = _value(payload, ("tagTypeIds", "tagIds", "tags", "tagTypes"), present, "tags")
    if raw is None:
        return [], []
    values = raw if isinstance(raw, list) else [raw]
    ids: list[str] = []
    names: list[str] = []
    for item in values:
        if isinstance(item, dict):
            raw_id = item.get("id") or item.get("tagTypeId") or item.get("value")
            raw_name = item.get("name") or item.get("displayName") or item.get("label")
            if raw_id is not None:
                ids.append(str(raw_id))
            if raw_name is not None:
                names.append(str(raw_name))
        elif item is not None:
            ids.append(str(item))
    return _dedupe_strings(ids), _dedupe_strings(names)


def _display_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("title") or value.get("value") or "")
    return str(value or "")


def _raw_value(source: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        current: Any = source
        for part in name.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None:
            return current
    return None


def _top_level_keys(record: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in record.keys())


def _records_keys(records: list[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for record in records:
        keys.update(str(key) for key in record.keys())
    return sorted(keys)


def _select_appointment(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}

    sequence_fields = ("sequence", "sequenceNumber", "appointmentNumber", "visitNumber", "order", "sortOrder")

    def sort_key(item: dict[str, Any]) -> tuple[int, float, str, str]:
        sequence = _first_float([item], sequence_fields)
        if sequence is not None:
            return (0, sequence, "", str(_raw_value(item, ("id", "appointmentId")) or ""))
        start = _parse_datetime(
            _raw_value(
                item,
                (
                    "arrivalWindowStart",
                    "scheduledStart",
                    "scheduledStartOn",
                    "start",
                    "startDate",
                    "createdOn",
                ),
            )
        )
        start_key = start.astimezone(timezone.utc).isoformat() if start else ""
        return (1, 0.0, start_key, str(_raw_value(item, ("id", "appointmentId")) or ""))

    return sorted(records, key=sort_key)[0]


def _mark_missing(missing_data: dict[str, str], fields: tuple[str, ...], reason: str) -> None:
    for field in fields:
        missing_data.setdefault(field, reason)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        result.append(normalized)
    return result


def _line_item_names_from_records(records: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for record in records:
        names.extend(_line_item_names(record, set()))
    return names


def _invoice_item_names(records: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for record in records:
        name = (
            _raw_value(record, ("name", "description", "skuName", "itemName", "displayName", "code", "sku"))
            or _raw_value(record, ("service.name", "material.name", "equipment.name"))
        )
        description = _raw_value(record, ("description", "itemDescription"))
        code = _raw_value(record, ("code", "sku", "itemCode"))
        combined = " ".join(str(part) for part in (code, name, description) if part)
        if combined:
            names.append(combined)
    return names


def _invoice_items_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for record in records:
        nested = _raw_value(record, ("lineItems", "items", "invoiceItems"))
        if isinstance(nested, list):
            items.extend(_invoice_items_from_records([item for item in nested if isinstance(item, dict)]))
            continue
        name = (
            _raw_value(record, ("name", "description", "skuName", "itemName", "displayName", "code", "sku"))
            or _raw_value(record, ("service.name", "material.name", "equipment.name"))
        )
        if not name:
            continue
        amount = _float_or_none(_raw_value(record, ("amount", "total", "totalAmount", "price", "unitPrice", "cost", "subtotal")))
        quantity = _float_or_none(_raw_value(record, ("quantity", "qty")))
        items.append(
            {
                "name": str(name),
                "amount": amount,
                "quantity": quantity,
            }
        )
    return items


def _payment_summary(records: list[dict[str, Any]]) -> dict[str, int | float | None]:
    payments_count: int | None = None
    payment_total: float | None = None
    explicit_total = _first_float(records, ("paymentTotal", "paymentsTotal", "paidAmount", "amountPaid"))
    if explicit_total is not None:
        payment_total = explicit_total
    for record in records:
        payments = _raw_value(record, ("payments", "paymentRecords", "transactions"))
        if not isinstance(payments, list):
            continue
        valid_payments = [payment for payment in payments if isinstance(payment, dict)]
        payments_count = (payments_count or 0) + len(valid_payments)
        amounts = [_float_or_none(_raw_value(payment, ("amount", "total", "paymentAmount"))) for payment in valid_payments]
        numeric_amounts = [amount for amount in amounts if amount is not None]
        if numeric_amounts:
            payment_total = (payment_total or 0.0) + sum(numeric_amounts)
    return {"payments_count": payments_count, "payment_total": payment_total}


def _first_float(records: list[dict[str, Any]], names: tuple[str, ...]) -> float | None:
    for record in records:
        value = _float_or_none(_raw_value(record, names))
        if value is not None:
            return value
    return None


def _time_data_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    starts: list[datetime] = []
    ends: list[datetime] = []
    arrivals: list[datetime] = []
    technician_id = ""
    technician_name = ""
    for record in records:
        start = _parse_datetime(_raw_value(record, ("clockInOn", "clockIn", "startedOn", "start", "startTime", "from")))
        end = _parse_datetime(_raw_value(record, ("clockOutOn", "clockOut", "endedOn", "end", "endTime", "to")))
        arrival = _parse_datetime(_raw_value(record, ("arrivedOn", "arrivedAt", "arrivalTime", "technicianArrivedOn")))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
        if arrival:
            arrivals.append(arrival)
        technician_id = technician_id or str(_raw_value(record, ("technicianId", "employeeId", "technician.id", "employee.id")) or "")
        technician_name = technician_name or str(_raw_value(record, ("technicianName", "employeeName", "technician.name", "employee.name", "name")) or "")
    return {
        "clock_in_at": min(starts) if starts else None,
        "clock_out_at": max(ends) if ends else None,
        "arrived_at": min(arrivals) if arrivals else None,
        "lunch_break_minutes": _break_minutes(records),
        "technician_id": technician_id,
        "technician_name": technician_name,
    }


def _break_minutes(records: list[dict[str, Any]]) -> int | None:
    total = 0
    saw_break = False
    for record in records:
        label = " ".join(
            str(part)
            for part in (
                _raw_value(record, ("code", "name", "type", "activity", "activityCode.name", "timesheetCode.name")),
                _raw_value(record, ("description", "memo")),
            )
            if part
        ).lower()
        minutes = _int_or_none(_raw_value(record, ("minutes", "durationMinutes", "paidMinutes")))
        if minutes is None:
            start = _parse_datetime(_raw_value(record, ("startedOn", "start", "startTime", "from")))
            end = _parse_datetime(_raw_value(record, ("endedOn", "end", "endTime", "to")))
            if start and end:
                minutes = max(0, int((end - start).total_seconds() // 60))
        if ("lunch" in label or "break" in label) and minutes is not None:
            total += minutes
            saw_break = True
    if saw_break:
        return total
    return None


def _note_texts(records: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for record in records:
        text = _raw_value(record, ("text", "note", "memo", "content", "body", "summary"))
        if text:
            texts.append(str(text))
    return texts


def _photo_count(records: list[dict[str, Any]]) -> int:
    count = 0
    for record in records:
        count += _photo_count_from_value(record)
    return count


def _photo_count_from_value(value: Any) -> int:
    count = 0
    if isinstance(value, dict):
        content_type = str(_raw_value(value, ("contentType", "mimeType", "fileType", "type")) or "").lower()
        name = str(_raw_value(value, ("fileName", "filename", "name", "url")) or "").lower()
        if content_type.startswith("image/") or any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")):
            count += 1
        for key in ("attachments", "files", "images", "photos", "media"):
            child = value.get(key)
            if isinstance(child, list):
                count += sum(_photo_count_from_value(item) for item in child)
            elif isinstance(child, dict):
                count += _photo_count_from_value(child)
    elif isinstance(value, list):
        count += sum(_photo_count_from_value(item) for item in value)
    return count


def _completed_phases(records: list[dict[str, Any]]) -> list[str]:
    phases: list[str] = []
    for record in records:
        value = _raw_value(record, ("phase", "phaseName", "status", "status.name", "event", "type", "name"))
        if value:
            phases.append(_display_value(value))
    return _dedupe_strings(phases)


def _explicit_options_presented(records: list[dict[str, Any]]) -> bool | None:
    explicit_values: list[bool] = []
    for record in records:
        for key in ("optionsPresented", "goodBetterBestPresented", "presented", "wasPresented", "isPresented"):
            value = _bool_or_none(_raw_value(record, (key,)))
            if value is not None:
                explicit_values.append(value)
    if explicit_values:
        return any(explicit_values)
    return None


def _option_count_from_records(records: list[dict[str, Any]], *, fallback_count: int) -> int:
    explicit_counts: list[int] = []
    for record in records:
        for key in ("options", "estimateOptions", "presentedOptions", "proposals", "choices"):
            value = _raw_value(record, (key,))
            if isinstance(value, list):
                explicit_counts.append(len([item for item in value if item is not None]))
            else:
                count = _int_or_none(value)
                if count is not None:
                    explicit_counts.append(count)
        count_value = _int_or_none(_raw_value(record, ("optionsCount", "optionCount", "presentedOptionsCount")))
        if count_value is not None:
            explicit_counts.append(count_value)
    if explicit_counts:
        return sum(explicit_counts)
    return fallback_count


def _records_text(records: list[dict[str, Any]]) -> str:
    parts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if any(sensitive in str(key).lower() for sensitive in ("phone", "email", "address", "token", "secret")):
                    continue
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, (str, int, float, bool)) and value is not None:
            parts.append(str(value))

    for record in records:
        visit(record)
    return " ".join(parts)


def _sales_ruleset_applies_to(settings: Settings) -> dict[str, Any]:
    config = settings.service_titan_rule_scope_config or {}
    rulesets = config.get("rulesets")
    if not isinstance(rulesets, dict):
        return {}
    sales = rulesets.get("Sales / Comfort Advisor Audit")
    if not isinstance(sales, dict):
        return {}
    applies = sales.get("applies_to")
    if not isinstance(applies, dict):
        return {}
    for key in (
        "business_units",
        "business_unit_ids",
        "job_types",
        "job_types_contains",
        "job_type_ids",
        "statuses",
        "job_statuses",
        "tags",
        "tags_contains",
        "tag_ids",
        "campaigns",
        "lead_sources",
        "workflows",
        "workflow_contains",
    ):
        value = applies.get(key)
        if value is not None and _scope_patterns(value):
            return applies
    return {}


def _sales_scope_matches_job(job: ServiceTitanJob, applies: dict[str, Any]) -> bool:
    checks = (
        (("business_units", "business_unit_ids"), [job.business_unit_id, job.business_unit_name]),
        (("job_types", "job_types_contains", "job_type_ids"), [job.job_type_id, job.job_type_name]),
        (("statuses", "job_statuses"), [job.status]),
        (("tags", "tags_contains", "tag_ids"), [*job.tag_ids, *job.tag_names]),
        (("campaigns", "lead_sources"), [job.campaign_id, job.campaign_name]),
        (("workflows", "workflow_contains"), [job.workflow, job.job_type_name, job.business_unit_name, job.department, job.trade, *job.tag_names]),
    )
    for keys, values in checks:
        patterns = _patterns_for_keys(applies, keys)
        if patterns and not _values_match_patterns(values, patterns):
            return False
    return True


def _patterns_for_keys(config: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, ...]:
    patterns: list[str] = []
    for key in keys:
        if key in config and config.get(key) is None:
            continue
        patterns.extend(_scope_patterns(config.get(key)))
    return tuple(patterns)


def _scope_patterns(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (int, float)):
        return (str(value),)
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _values_match_patterns(values: list[str], patterns: tuple[str, ...]) -> bool:
    normalized_values = [_normalize_scope_value(value) for value in values if value]
    for pattern in patterns:
        normalized_pattern = _normalize_scope_value(pattern)
        if not normalized_pattern:
            continue
        if any(normalized_pattern in value or value in normalized_pattern for value in normalized_values):
            return True
    return False


def _normalize_scope_value(value: str) -> str:
    return " ".join(str(value).lower().replace("_", " ").replace("-", " ").split())


def _has_keywords(text: str, keywords: tuple[str, ...] | list[str]) -> bool:
    normalized = text.lower()
    return any(keyword.lower() in normalized for keyword in keywords if keyword)


def _records_contain_keywords(records: list[dict[str, Any]], keywords: list[str]) -> bool:
    if not records:
        return False
    return _has_keywords(_records_text(records), keywords)


def _authorization_count(forms: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> int:
    count = 0
    for record in [*forms, *attachments]:
        if _has_keywords(_records_text([record]), ("authorization", "authorized", "signature", "signed", "approval", "approved")):
            count += 1
    return count


def _equipment_complete(records: list[dict[str, Any]]) -> bool:
    if not records:
        return False
    for record in records:
        manufacturer = _raw_value(record, ("manufacturer", "manufacturerName", "make"))
        model = _raw_value(record, ("model", "modelNumber"))
        serial = _raw_value(record, ("serialNumber", "serial"))
        location = _raw_value(record, ("location", "locationName", "installedLocation"))
        installed = _raw_value(record, ("installedOn", "installedDate", "installDate"))
        if not (manufacturer and model and serial and location and installed):
            return False
    return True


def _purchase_order_summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for record in records:
        status = _display_value(_raw_value(record, ("status", "poStatus", "status.name"))).lower()
        attachments = _raw_value(record, ("attachments", "documents", "files"))
        attachments_count = len(attachments) if isinstance(attachments, list) else _int_or_none(_raw_value(record, ("attachmentsCount", "documentsCount"))) or 0
        vendor_document = _raw_value(record, ("vendorDocumentNumber", "vendorInvoiceNumber", "documentNumber", "invoiceNumber", "packingSlipNumber"))
        reconciled_value = _bool_or_none(_raw_value(record, ("reconciled", "isReconciled")))
        summaries.append(
            {
                "id": str(_raw_value(record, ("id", "purchaseOrderId", "number", "poNumber")) or ""),
                "status": status,
                "received": "received" in status or bool(_raw_value(record, ("receivedOn", "receivedDate"))),
                "reconciled": bool(reconciled_value) or bool(_raw_value(record, ("reconciledOn", "reconciledDate"))),
                "vendor_document_present": bool(vendor_document),
                "attachments_count": attachments_count,
            }
        )
    return summaries


def _same_day_estimate_present(records: list[dict[str, Any]], anchor: datetime | None) -> bool | None:
    if not records:
        return False
    if anchor is None:
        return None
    saw_date = False
    anchor_date = anchor.astimezone(timezone.utc).date()
    for record in records:
        created = _parse_datetime(_raw_value(record, ("createdOn", "createdAt", "date", "estimateDate", "soldOn")))
        if not created:
            continue
        saw_date = True
        if created.astimezone(timezone.utc).date() == anchor_date:
            return True
    return False if saw_date else None


def _diagnostic_fee_summary(invoice_items: list[dict[str, Any]], line_items: list[str], keywords: list[str]) -> dict[str, bool | None]:
    present = False
    charged: bool | None = None
    waived = False
    for item in invoice_items:
        name = str(item.get("name") or "")
        if not _has_keywords(name, keywords):
            continue
        present = True
        amount = item.get("amount")
        if isinstance(amount, (int, float)):
            if amount > 0:
                charged = True
            elif amount == 0:
                waived = True
        if _has_keywords(name, ("waived", "waiver", "no charge", "included")):
            waived = True
    if not present:
        present = _has_keywords(" ".join(line_items), keywords)
    return {"present": present, "charged": charged, "waived": waived}


def _repair_sold(invoice_items: list[dict[str, Any]], line_items: list[str], diagnostic_keywords: list[str]) -> bool | None:
    names = [str(item.get("name") or "") for item in invoice_items] or line_items
    if not names:
        return None
    excluded = [*diagnostic_keywords, "dispatch", "trip charge", "tune up", "maintenance", "membership"]
    repair_items = [name for name in names if not _has_keywords(name, excluded)]
    return bool(repair_items)


def _missing_special_order_note_fields(text: str, required_fields: list[str]) -> list[str]:
    synonyms = {
        "purchase order number": ("purchase order", "po #", "po number", "po#"),
        "ordering date": ("ordering date", "ordered on", "date ordered"),
        "employee ordered": ("employee ordered", "ordered by", "who ordered"),
        "eta": ("eta", "arrival date", "expected arrival"),
        "supply house": ("supply house", "supplier", "vendor"),
        "supply house employee": ("supply house employee", "supplier rep", "vendor rep"),
    }
    missing: list[str] = []
    for field_name in required_fields:
        candidates = synonyms.get(field_name.lower(), (field_name,))
        if not _has_keywords(text, candidates):
            missing.append(field_name)
    return missing
