from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from ..clients.servicetitan import ServiceTitanJob
from ..config import Settings


RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_INSUFFICIENT = "insufficient_data"
RESULT_ERROR = "error"

RULESET_TECHNICIAN = "Technician Compliance"
RULESET_DISPATCHER = "Dispatcher / Job Quality Audit"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    ruleset: str
    severity: str
    title: str
    description: str
    status: str
    explanation: str
    recommended_action: str
    required_fields: tuple[str, ...]
    violation_key: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class AuditRule:
    rule_id: str
    ruleset: str
    severity: str
    title: str
    description: str
    required_fields: tuple[str, ...]
    action: str
    evaluate: Callable[[ServiceTitanJob, Settings, "AuditRule"], RuleResult]

    def run(self, job: ServiceTitanJob, settings: Settings) -> RuleResult:
        try:
            return self.evaluate(job, settings, self)
        except Exception as exc:
            return self.result(job, RESULT_ERROR, f"Rule evaluation failed: {exc}", self.action)

    def result(self, job: ServiceTitanJob, status: str, explanation: str, action: str, metadata: dict[str, object] | None = None) -> RuleResult:
        return RuleResult(
            rule_id=self.rule_id,
            ruleset=self.ruleset,
            severity=self.severity,
            title=self.title,
            description=self.description,
            status=status,
            explanation=explanation,
            recommended_action=action,
            required_fields=self.required_fields,
            violation_key=violation_key(job, self.rule_id),
            metadata=metadata or {},
        )


def active_service_titan_rules(settings: Settings) -> list[AuditRule]:
    rules: list[AuditRule] = []
    if settings.technician_compliance_enabled:
        rules.extend(technician_compliance_rules())
    if settings.dispatcher_audit_enabled:
        rules.extend(dispatcher_audit_rules())
    return rules


def technician_compliance_rules() -> list[AuditRule]:
    return [
        AuditRule(
            "tech_clock_in_missing",
            RULESET_TECHNICIAN,
            "high",
            "Technician clock-in missing",
            "Closed jobs must show a technician job clock-in.",
            ("status", "technician", "clock_in"),
            "Confirm the technician clock-in on the job and correct the time entry if needed.",
            _clock_in,
        ),
        AuditRule(
            "tech_clock_out_missing",
            RULESET_TECHNICIAN,
            "high",
            "Technician clock-out missing",
            "Closed jobs must show a technician job clock-out.",
            ("status", "technician", "clock_out"),
            "Confirm the technician clock-out on the job and correct the time entry if needed.",
            _clock_out,
        ),
        AuditRule(
            "tech_lunch_break_missing",
            RULESET_TECHNICIAN,
            "medium",
            "Lunch break missing",
            "Long technician shifts must show a lunch break when required.",
            ("status", "clock_in", "clock_out", "lunch_break"),
            "Review the technician timesheet and add or correct the required lunch break.",
            _lunch_break,
        ),
        AuditRule(
            "tech_invoice_diagnostic_fee_missing",
            RULESET_TECHNICIAN,
            "high",
            "Diagnostic fee missing from invoice",
            "Service invoices must include the required diagnostic fee line item.",
            ("status", "invoice_line_items"),
            "Review the invoice and add or correct the diagnostic fee line item.",
            _diagnostic_fee,
        ),
        AuditRule(
            "tech_required_phases_incomplete",
            RULESET_TECHNICIAN,
            "high",
            "Required job phases incomplete",
            "Closed jobs must complete the configured required operational phases.",
            ("status", "completed_phases"),
            "Review the job workflow and complete or correct the missing required phases.",
            _required_phases,
        ),
        AuditRule(
            "tech_required_operational_data_incomplete",
            RULESET_TECHNICIAN,
            "high",
            "Required operational data incomplete",
            "Closed jobs must include required operational data before closure.",
            ("status", "operational_data"),
            "Review the job and complete the required operational fields.",
            _operational_data,
        ),
    ]


def dispatcher_audit_rules() -> list[AuditRule]:
    return [
        AuditRule(
            "dispatch_arrival_outside_first_30",
            RULESET_DISPATCHER,
            "medium",
            "Technician arrival outside first 30 minutes",
            "Technicians should arrive within the first configured minutes of the arrival window.",
            ("arrival_window", "arrived_at"),
            "Review dispatch timing and coach or reschedule when needed.",
            _arrival_window,
        ),
        AuditRule(
            "dispatch_diagnostic_fee_missing",
            RULESET_DISPATCHER,
            "high",
            "Diagnostic fee not reflected",
            "Closed jobs should show the diagnostic fee as collected or reflected on the invoice.",
            ("status", "invoice_line_items"),
            "Review dispatch/job closeout and correct the diagnostic fee collection record.",
            _diagnostic_fee,
        ),
        AuditRule(
            "dispatch_options_not_presented",
            RULESET_DISPATCHER,
            "high",
            "Required job options not presented",
            "Closed jobs should show required Good / Better / Best or equivalent options.",
            ("status", "options_presented"),
            "Confirm options were presented and update the job record with supporting detail.",
            _options_presented,
        ),
        AuditRule(
            "dispatch_notes_missing",
            RULESET_DISPATCHER,
            "medium",
            "Job notes missing or incomplete",
            "Closed jobs should include useful operational notes.",
            ("status", "notes"),
            "Add clear job notes covering outcome, customer decision, and next step.",
            _notes,
        ),
        AuditRule(
            "dispatch_photos_missing",
            RULESET_DISPATCHER,
            "medium",
            "Required job photos missing",
            "Closed jobs should include required photos before closeout.",
            ("status", "photos"),
            "Upload required job photos or document why photos were not required.",
            _photos,
        ),
        AuditRule(
            "dispatch_supporting_evidence_missing",
            RULESET_DISPATCHER,
            "high",
            "Supporting evidence missing",
            "Closed jobs should include required supporting evidence before closeout.",
            ("status", "supporting_evidence"),
            "Attach supporting evidence such as photos, forms, signatures, or invoice proof.",
            _supporting_evidence,
        ),
    ]


def violation_key(job: ServiceTitanJob, rule_id: str) -> str:
    appointment_id = job.appointment_id or "no-appointment"
    actor_id = job.actor_id
    return f"servicetitan:{job.job_id}:{appointment_id}:{rule_id}:{actor_id}"


def _closed_or_pass(job: ServiceTitanJob, rule: AuditRule) -> RuleResult | None:
    if "status" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, "ServiceTitan payload did not include job status.", rule.action)
    if not job.is_closed:
        return rule.result(job, RESULT_PASS, "Job is not closed; closeout rule does not apply yet.", rule.action)
    return None


def _missing_fields(job: ServiceTitanJob, fields: tuple[str, ...]) -> list[str]:
    return [field for field in fields if field not in job.present_fields]


def _missing_field_explanation(job: ServiceTitanJob, fields: tuple[str, ...]) -> str:
    missing = _missing_fields(job, fields)
    explanation = f"Missing ServiceTitan field(s): {', '.join(missing)}."
    notes = [f"{field}: {job.missing_data[field]}" for field in missing if field in job.missing_data]
    if notes:
        explanation += " Missing data notes: " + " | ".join(notes)
    return explanation


def _field_unavailable(job: ServiceTitanJob, field: str, fallback: str) -> str:
    if field in job.missing_data:
        return f"{fallback} Source note: {job.missing_data[field]}"
    return fallback


def _clock_in(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    missing = _missing_fields(job, rule.required_fields)
    if missing:
        return rule.result(job, RESULT_INSUFFICIENT, _missing_field_explanation(job, rule.required_fields), rule.action)
    if not job.clock_in_at:
        return rule.result(job, RESULT_FAIL, "Closed job has no technician clock-in time.", rule.action)
    return rule.result(job, RESULT_PASS, "Technician clock-in is present.", rule.action)


def _clock_out(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    missing = _missing_fields(job, rule.required_fields)
    if missing:
        return rule.result(job, RESULT_INSUFFICIENT, _missing_field_explanation(job, rule.required_fields), rule.action)
    if not job.clock_out_at:
        return rule.result(job, RESULT_FAIL, "Closed job has no technician clock-out time.", rule.action)
    return rule.result(job, RESULT_PASS, "Technician clock-out is present.", rule.action)


def _lunch_break(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    missing = _missing_fields(job, rule.required_fields)
    if missing:
        return rule.result(job, RESULT_INSUFFICIENT, _missing_field_explanation(job, rule.required_fields), rule.action)
    if not job.clock_in_at or not job.clock_out_at:
        return rule.result(job, RESULT_INSUFFICIENT, "Clock-in/out values are required to determine lunch requirement.", rule.action)
    duration_hours = (job.clock_out_at - job.clock_in_at).total_seconds() / 3600
    if duration_hours < settings.service_titan_lunch_required_after_hours:
        return rule.result(job, RESULT_PASS, "Shift duration does not require a lunch break.", rule.action, {"duration_hours": round(duration_hours, 2)})
    if job.lunch_break_minutes is None:
        return rule.result(job, RESULT_INSUFFICIENT, "Lunch break minutes were not available from ServiceTitan.", rule.action)
    if job.lunch_break_minutes < settings.service_titan_min_lunch_break_minutes:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Lunch break was {job.lunch_break_minutes} minutes; required minimum is {settings.service_titan_min_lunch_break_minutes}.",
            rule.action,
            {"duration_hours": round(duration_hours, 2), "lunch_break_minutes": job.lunch_break_minutes},
        )
    return rule.result(job, RESULT_PASS, "Lunch break requirement is satisfied.", rule.action)


def _diagnostic_fee(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "invoice_line_items" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "invoice_line_items", "Invoice line items were not available from ServiceTitan."), rule.action)
    keywords = [keyword.lower() for keyword in settings.service_titan_diagnostic_fee_keywords if keyword]
    if not keywords:
        return rule.result(job, RESULT_INSUFFICIENT, "Diagnostic fee keywords are not configured.", rule.action)
    haystack = " | ".join(job.invoice_line_items).lower()
    if any(keyword in haystack for keyword in keywords):
        return rule.result(job, RESULT_PASS, "Diagnostic fee line item is present.", rule.action)
    return rule.result(job, RESULT_FAIL, "Invoice line items do not include a configured diagnostic fee keyword.", rule.action, {"line_items": job.invoice_line_items[:10]})


def _required_phases(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if not settings.service_titan_required_phases:
        return rule.result(job, RESULT_INSUFFICIENT, "Required phases are not configured.", rule.action)
    if "completed_phases" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "completed_phases", "Completed phases were not available from ServiceTitan."), rule.action)
    completed = {phase.lower() for phase in job.completed_phases}
    missing = [phase for phase in settings.service_titan_required_phases if phase.lower() not in completed]
    if missing:
        return rule.result(job, RESULT_FAIL, "Required phase(s) missing: " + ", ".join(missing), rule.action, {"missing_phases": missing})
    return rule.result(job, RESULT_PASS, "Required phases are complete.", rule.action)


def _operational_data(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "operational_data" in job.present_fields and job.operational_data_complete is not None:
        if not job.operational_data_complete:
            return rule.result(job, RESULT_FAIL, "ServiceTitan indicates required operational data is incomplete.", rule.action)
        return rule.result(job, RESULT_PASS, "Required operational data is marked complete.", rule.action)
    if not settings.service_titan_required_operational_fields:
        return rule.result(job, RESULT_INSUFFICIENT, "Required operational fields are not configured.", rule.action)
    if "operational_data_fields" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "operational_data_fields", "Operational custom fields were not available from ServiceTitan."), rule.action)
    missing = [field for field in settings.service_titan_required_operational_fields if not job.operational_data.get(field)]
    if missing:
        return rule.result(job, RESULT_FAIL, "Required operational field(s) missing: " + ", ".join(missing), rule.action, {"missing_fields": missing})
    return rule.result(job, RESULT_PASS, "Configured operational fields are complete.", rule.action)


def _arrival_window(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    missing = _missing_fields(job, rule.required_fields)
    if missing:
        return rule.result(job, RESULT_INSUFFICIENT, _missing_field_explanation(job, rule.required_fields), rule.action)
    if not job.arrival_window_start or not job.arrived_at:
        return rule.result(job, RESULT_INSUFFICIENT, "Arrival window start and arrival time are required.", rule.action)
    latest_expected = job.arrival_window_start + timedelta(minutes=settings.service_titan_arrival_grace_minutes)
    if job.arrived_at > latest_expected:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Technician arrived at {job.arrived_at.isoformat()}, after the first {settings.service_titan_arrival_grace_minutes} minutes of the arrival window.",
            rule.action,
            {"arrival_window_start": job.arrival_window_start.isoformat(), "arrived_at": job.arrived_at.isoformat()},
        )
    return rule.result(job, RESULT_PASS, "Technician arrived inside the configured first-window threshold.", rule.action)


def _options_presented(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "options_presented" not in job.present_fields or job.options_presented is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "options_presented", "Options-presented field was not available from ServiceTitan."), rule.action)
    if not job.options_presented:
        return rule.result(job, RESULT_FAIL, "Required customer options were not recorded as presented.", rule.action)
    return rule.result(job, RESULT_PASS, "Required options were recorded as presented.", rule.action)


def _notes(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Job notes were not available from ServiceTitan."), rule.action)
    note_text = (job.notes or "").strip()
    if len(note_text) < settings.service_titan_min_note_length:
        return rule.result(job, RESULT_FAIL, f"Job notes are missing or shorter than {settings.service_titan_min_note_length} characters.", rule.action)
    return rule.result(job, RESULT_PASS, "Job notes are present.", rule.action)


def _photos(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "photos" not in job.present_fields or job.photo_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "photos", "Photo count was not available from ServiceTitan."), rule.action)
    if job.photo_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed job has no uploaded photos.", rule.action)
    return rule.result(job, RESULT_PASS, "Required photos are present.", rule.action)


def _supporting_evidence(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "supporting_evidence" not in job.present_fields or job.supporting_evidence_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "supporting_evidence", "Supporting evidence count was not available from ServiceTitan."), rule.action)
    if job.supporting_evidence_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed job has no supporting evidence attached.", rule.action)
    return rule.result(job, RESULT_PASS, "Supporting evidence is present.", rule.action)
