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
    invoice_id: str = ""
    technician_id: str = ""
    technician_name: str = ""
    dispatcher_id: str = ""
    dispatcher_name: str = ""
    customer_name: str = ""
    arrival_window_start: datetime | None = None
    arrival_window_end: datetime | None = None
    arrived_at: datetime | None = None
    clock_in_at: datetime | None = None
    clock_out_at: datetime | None = None
    lunch_break_minutes: int | None = None
    invoice_line_items: list[str] = field(default_factory=list)
    invoice_total: float | None = None
    completed_phases: list[str] = field(default_factory=list)
    operational_data: dict[str, str] = field(default_factory=dict)
    operational_data_complete: bool | None = None
    options_presented: bool | None = None
    notes: str | None = None
    photo_count: int | None = None
    supporting_evidence_count: int | None = None
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


class ServiceTitanClient:
    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.http = http or HttpClient()
        self._access_token = ""
        self._token_expires_at: datetime | None = None
        self._disabled_related_categories: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(
            self.settings.servicetitan_client_id
            and self.settings.servicetitan_client_secret
            and self.settings.servicetitan_tenant_id
            and self.settings.servicetitan_app_key
        )

    def query_recent_jobs(self, modified_on_or_after: datetime) -> list[ServiceTitanJob]:
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
        return [self._enrich_job(parse_service_titan_job(record, self.settings)) for record in records]

    def _enrich_job(self, job: ServiceTitanJob) -> ServiceTitanJob:
        present_fields = set(job.present_fields)
        related_counts = dict(job.related_counts)
        available_keys = {"job": _top_level_keys(job.raw), **job.available_keys}
        missing_data = dict(job.missing_data)

        appointment_id = job.appointment_id
        invoice_id = job.invoice_id
        technician_id = job.technician_id
        technician_name = job.technician_name
        arrival_window_start = job.arrival_window_start
        arrival_window_end = job.arrival_window_end
        arrived_at = job.arrived_at
        clock_in_at = job.clock_in_at
        clock_out_at = job.clock_out_at
        lunch_break_minutes = job.lunch_break_minutes
        invoice_line_items = list(job.invoice_line_items)
        invoice_total = job.invoice_total
        notes = job.notes
        photo_count = job.photo_count
        supporting_evidence_count = job.supporting_evidence_count
        completed_phases = list(job.completed_phases)
        options_presented = job.options_presented

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

        appointment_ids = [str(_raw_value(record, ("id", "appointmentId")) or "") for record in appointments]
        appointment_ids = [value for value in appointment_ids if value]
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
        else:
            invoice_ids = [value for value in [invoice_id, *[str(_raw_value(record, ("id", "invoiceId")) or "") for record in invoices]] if value]
            if invoices:
                invoice_id = invoice_ids[0] if invoice_ids else invoice_id
                invoice_total = invoice_total if invoice_total is not None else _first_float(invoices, ("total", "subtotal", "balance", "amount"))
                invoice_line_items.extend(_line_item_names_from_records(invoices))
            invoice_item_records: list[dict[str, Any]] = []
            if not invoice_line_items:
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
            related_counts["invoice_items"] = len(invoice_item_records) if invoice_item_records else len(invoice_line_items)
            if invoices or invoice_item_records:
                present_fields.add("invoice_line_items")
            elif not invoices_error:
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
            explicit_options = _explicit_options_presented(estimate_records)
            if explicit_options is not None:
                options_presented = explicit_options
                present_fields.add("options_presented")

        opportunity_records, opportunity_error = self._related_records(
            "opportunities",
            self._tenant_path("sales", "opportunities"),
            {"jobId": job.job_id},
        )
        related_counts["opportunities"] = len(opportunity_records)
        available_keys["opportunities"] = _records_keys(opportunity_records)
        if not opportunity_error and "options_presented" not in present_fields:
            explicit_options = _explicit_options_presented(opportunity_records)
            if explicit_options is not None:
                options_presented = explicit_options
                present_fields.add("options_presented")
        if "options_presented" not in present_fields:
            missing_data.setdefault("options_presented", "sales estimates/opportunities did not expose an explicit options-presented field")

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
            invoice_id=invoice_id,
            technician_id=technician_id,
            technician_name=technician_name,
            arrival_window_start=arrival_window_start,
            arrival_window_end=arrival_window_end,
            arrived_at=arrived_at,
            clock_in_at=clock_in_at,
            clock_out_at=clock_out_at,
            lunch_break_minutes=lunch_break_minutes,
            invoice_line_items=_dedupe_strings(invoice_line_items),
            invoice_total=invoice_total,
            completed_phases=_dedupe_strings(completed_phases),
            options_presented=options_presented,
            notes=notes,
            photo_count=photo_count,
            supporting_evidence_count=supporting_evidence_count,
            present_fields=present_fields,
            related_counts=related_counts,
            available_keys=available_keys,
            missing_data=missing_data,
        )

    def _related_records(self, category: str, path: str, params: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
        if category in self._disabled_related_categories:
            return [], f"{category} endpoint unavailable earlier in this process"
        payload = {"pageSize": str(self.settings.service_titan_audit_page_size), "includeTotal": "true", **{k: v for k, v in params.items() if v}}
        try:
            return self._get_paginated(path, payload), None
        except ServiceTitanApiError as exc:
            if exc.status in {400, 401, 403, 404, 405}:
                self._disabled_related_categories.add(category)
            logger.warning(
                "servicetitan_related_fetch_unavailable",
                extra={"category": category, "path": path, "status": exc.status, "error_message": exc.message},
            )
            return [], f"{path} returned HTTP {exc.status}"
        except Exception as exc:
            logger.warning("servicetitan_related_fetch_failed", extra={"category": category, "path": path, "error": str(exc)})
            return [], f"{path} request failed"

    def _tenant_path(self, api: str, suffix: str) -> str:
        return f"/{api}/v2/tenant/{self.settings.servicetitan_tenant_id}/{suffix.lstrip('/')}"

    def _get_paginated(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while page <= max(1, self.settings.service_titan_audit_max_pages):
            payload = dict(params)
            payload["page"] = str(page)
            data = self._get(path, payload)
            page_records = _records_from_response(data)
            records.extend(page_records)
            if not _has_more(data, page, len(page_records), int(payload["pageSize"])):
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


def parse_service_titan_job(payload: dict[str, Any], settings: Settings) -> ServiceTitanJob:
    present: set[str] = set()
    appointment = _first_dict(payload.get("appointments")) or _first_dict(payload.get("appointment")) or {}
    assignment = _first_dict(payload.get("assignments")) or _first_dict(payload.get("technicianAssignments")) or {}
    invoice = _first_dict(payload.get("invoices")) or _first_dict(payload.get("invoice")) or payload.get("invoice") or {}
    if not isinstance(invoice, dict):
        invoice = {}

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

    notes_value = _value(payload, ("summary", "notes", "jobNotes", "description"), present, "notes")
    photo_count = _count_from_payload(payload, ("photos", "images", "attachments"), present, "photos")
    evidence_count = _count_from_payload(payload, ("supportingEvidence", "documents", "attachments"), present, "supporting_evidence")
    operational_data = _custom_fields(payload)
    if "customFields" in payload:
        present.add("operational_data_fields")
    line_items = _line_item_names(invoice or payload, present)

    completed_phases = _string_list(
        _value(payload, ("completedPhases", "phasesCompleted", "phases"), present, "completed_phases")
    )
    if completed_phases:
        present.add("completed_phases")

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
        invoice_id=str(_value(invoice, ("id", "invoiceId"), present, "invoice") or _value(payload, ("invoiceId",), present, "invoice") or ""),
        technician_id=technician_id,
        technician_name=technician_name,
        dispatcher_id=dispatcher_id,
        dispatcher_name=dispatcher_name,
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
        invoice_total=_float_or_none(_value(invoice, ("total", "invoiceTotal", "amount"), present, "invoice")),
        completed_phases=completed_phases,
        operational_data=operational_data,
        operational_data_complete=_bool_or_none(_value(payload, ("operationalDataComplete", "requiredDataComplete"), present, "operational_data")),
        options_presented=_bool_or_none(_value(payload, ("optionsPresented", "goodBetterBestPresented"), present, "options_presented")),
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


def _records_from_response(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [record for record in data if isinstance(record, dict)]
    records = data.get("data")
    if records is None:
        records = data.get("items", data.get("results", []))
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return []


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
    return sorted(records, key=lambda item: str(_raw_value(item, ("start", "arrivalWindowStart", "createdOn", "id")) or ""))[0]


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
        content_type = str(_raw_value(record, ("contentType", "mimeType", "fileType", "type")) or "").lower()
        name = str(_raw_value(record, ("fileName", "filename", "name", "url")) or "").lower()
        if content_type.startswith("image/") or any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")):
            count += 1
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
