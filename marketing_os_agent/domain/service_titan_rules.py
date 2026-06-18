from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Callable

from ..clients.servicetitan import ServiceTitanJob
from ..config import Settings
from .service_titan_handbook import HandbookRuleDefinition, handbook_rule_by_id


RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_INSUFFICIENT = "insufficient_data"
RESULT_NOT_APPLICABLE = "not_applicable"
RESULT_ERROR = "error"

RULESET_TECHNICIAN = "Technician Compliance"
RULESET_DISPATCHER = "Dispatcher / Job Quality Audit"
RULESET_SALES = "Sales / Comfort Advisor Audit"
RULESET_HVAC = "HVAC Service Audit"
RULESET_PLUMBING = "Plumbing Service Audit"
RULESET_SERVICE_CALL = "Service Call Handbook Audit"
RULESET_PLY_MATERIALS = "Ply / PO Materials Audit"
RULESET_FOLLOW_UP = "Follow-up / Escalation Audit"


SERVICE_JOB_TYPE_KEYWORDS = (
    "service",
    "diagnostic",
    "diagnosis",
    "repair",
    "maintenance",
    "tune up",
    "tune-up",
    "no heat",
    "no cool",
)
PLUMBING_WORKFLOW_KEYWORDS = ("plumbing", "drain", "sewer", "water heater", "ply", "po", "purchase order", "material")
SALES_WORKFLOW_KEYWORDS = (
    "sales",
    "comfort advisor",
    "advisor",
    "estimate",
    "consultation",
    "replacement",
)
HVAC_SERVICE_WORKFLOW_KEYWORDS = (
    "hvac",
    "heating",
    "cooling",
    "air conditioning",
    "service",
    "diagnostic",
    "diagnosis",
    "repair",
    "maintenance",
    "tune up",
    "tune-up",
)
PLUMBING_SERVICE_BUSINESS_UNIT_IDS = ("64315277",)
PLUMBING_SERVICE_JOB_TYPE_IDS = ("57804592", "64569478", "112338076")
PLUMBING_OPTIONS_EXCLUDED_JOB_TYPES = ("57804592", "Water Heater Maintenance")
CLOSED_STATUS_KEYWORDS = ("complete", "completed", "closed", "done")
ACTIVE_OR_CLOSED_STATUS_KEYWORDS = (
    "scheduled",
    "dispatched",
    "working",
    "in progress",
    "complete",
    "completed",
    "closed",
    "done",
)
EXCLUDED_STATUS_KEYWORDS = ("canceled", "cancelled", "no access", "rescheduled")
EXCLUDED_JOB_TYPE_KEYWORDS = ("admin", "internal", "material only", "warehouse only")
EXCLUDED_TAG_KEYWORDS = ("no access", "safety concern")
BILLING_EXCLUDED_TAG_KEYWORDS = ("warranty", "callback", "call back", "no charge", "no access", "safety concern")


@dataclass(frozen=True)
class RuleScope:
    handbook_source: str = ""
    applies_to_departments: tuple[str, ...] = ()
    applies_to_business_units: tuple[str, ...] = ()
    applies_to_trades: tuple[str, ...] = ()
    applies_to_job_types: tuple[str, ...] = ()
    applies_to_job_statuses: tuple[str, ...] = ()
    applies_to_tags: tuple[str, ...] = ()
    applies_to_campaigns: tuple[str, ...] = ()
    applies_to_roles: tuple[str, ...] = ()
    applies_to_workflows: tuple[str, ...] = ()
    excludes_job_types: tuple[str, ...] = ()
    excludes_statuses: tuple[str, ...] = ()
    excludes_tags: tuple[str, ...] = ()
    excludes_cancellation_reasons: tuple[str, ...] = ()
    required_context_fields: tuple[str, ...] = ()
    required_data_fields: tuple[str, ...] = ()
    alert_routing: str = ""
    default_enabled: bool = True

    def to_metadata(self) -> dict[str, object]:
        return {
            "handbook_source": self.handbook_source,
            "applies_to_departments": list(self.applies_to_departments),
            "applies_to_business_units": list(self.applies_to_business_units),
            "applies_to_trades": list(self.applies_to_trades),
            "applies_to_job_types": list(self.applies_to_job_types),
            "applies_to_job_statuses": list(self.applies_to_job_statuses),
            "applies_to_tags": list(self.applies_to_tags),
            "applies_to_campaigns": list(self.applies_to_campaigns),
            "applies_to_roles": list(self.applies_to_roles),
            "applies_to_workflows": list(self.applies_to_workflows),
            "excludes_job_types": list(self.excludes_job_types),
            "excludes_statuses": list(self.excludes_statuses),
            "excludes_tags": list(self.excludes_tags),
            "excludes_cancellation_reasons": list(self.excludes_cancellation_reasons),
            "required_context_fields": list(self.required_context_fields),
            "required_data_fields": list(self.required_data_fields),
            "alert_routing": self.alert_routing,
            "default_enabled": self.default_enabled,
        }


@dataclass(frozen=True)
class ApplicabilityDecision:
    status: str
    explanation: str
    metadata: dict[str, object]


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
    handbook_source: str
    recommended_alert_recipient: str
    delivery: str


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
    handbook_source: str = ""
    recommended_alert_recipient: str = "slack audit channel"
    delivery: str = "immediate"
    enabled_by_default: bool = True
    scope: RuleScope = RuleScope()

    def run(self, job: ServiceTitanJob, settings: Settings) -> RuleResult:
        try:
            effective_rule = _effective_rule(self, settings)
            decision = _applicability_decision(job, effective_rule.scope)
            if decision.status != "applies":
                return effective_rule.result(job, decision.status, decision.explanation, effective_rule.action, decision.metadata)
            return effective_rule.evaluate(job, settings, effective_rule)
        except Exception as exc:
            return self.result(job, RESULT_ERROR, f"Rule evaluation failed: {exc}", self.action)

    def result(self, job: ServiceTitanJob, status: str, explanation: str, action: str, metadata: dict[str, object] | None = None) -> RuleResult:
        merged_metadata = {"rule_scope": self.scope.to_metadata(), **(metadata or {})}
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
            metadata=merged_metadata,
            handbook_source=self.scope.handbook_source or self.handbook_source,
            recommended_alert_recipient=self.scope.alert_routing or self.recommended_alert_recipient,
            delivery=self.delivery,
        )


def active_service_titan_rules(settings: Settings) -> list[AuditRule]:
    rules: list[AuditRule] = []
    if settings.sales_comfort_advisor_audit_enabled:
        rules.extend(sales_comfort_advisor_rules())
    if settings.hvac_service_audit_enabled:
        rules.extend(hvac_service_rules())
    if settings.plumbing_service_audit_enabled:
        rules.extend(plumbing_service_rules())
    if settings.technician_compliance_enabled:
        rules.extend(technician_compliance_rules())
    if settings.dispatcher_audit_enabled:
        rules.extend(dispatcher_audit_rules())
        rules.extend(handbook_audit_rules())
    disabled = {rule_id.strip() for rule_id in settings.service_titan_disabled_rule_ids if rule_id.strip()}
    return [rule for rule in rules if rule.enabled_by_default and rule.rule_id not in disabled]


def _service_call_scope(
    *,
    handbook_source: str,
    required_data_fields: tuple[str, ...],
    roles: tuple[str, ...],
    alert_routing: str,
    statuses: tuple[str, ...] = CLOSED_STATUS_KEYWORDS,
    required_context_fields: tuple[str, ...] = ("status", "job_type"),
    excludes_tags: tuple[str, ...] = EXCLUDED_TAG_KEYWORDS,
) -> RuleScope:
    return RuleScope(
        handbook_source=handbook_source,
        applies_to_job_types=SERVICE_JOB_TYPE_KEYWORDS,
        applies_to_job_statuses=statuses,
        applies_to_roles=roles,
        excludes_job_types=EXCLUDED_JOB_TYPE_KEYWORDS,
        excludes_statuses=EXCLUDED_STATUS_KEYWORDS,
        excludes_tags=excludes_tags,
        excludes_cancellation_reasons=("no access", "wrong equipment", "safety concern"),
        required_context_fields=required_context_fields,
        required_data_fields=required_data_fields,
        alert_routing=alert_routing,
    )


def _ply_material_scope(definition: HandbookRuleDefinition) -> RuleScope:
    return RuleScope(
        handbook_source=definition.handbook_source,
        applies_to_workflows=PLUMBING_WORKFLOW_KEYWORDS,
        applies_to_roles=("dispatcher", "warehouse", "operations"),
        excludes_statuses=(),
        required_context_fields=("workflow", "materials_or_po"),
        required_data_fields=definition.required_data_fields,
        alert_routing=definition.recommended_alert_recipient,
        default_enabled=definition.enabled_by_default,
    )


def _sales_scope(*, required_data_fields: tuple[str, ...], statuses: tuple[str, ...] = CLOSED_STATUS_KEYWORDS) -> RuleScope:
    return RuleScope(
        handbook_source="Sales / Comfort Advisor audit configuration",
        applies_to_job_statuses=statuses,
        applies_to_workflows=SALES_WORKFLOW_KEYWORDS,
        excludes_job_types=(
            "admin",
            "internal",
            "material only",
            "warehouse only",
            "install",
            "project",
            "plumbing",
            "service",
            "maintenance",
            "diagnostic",
            "repair",
        ),
        excludes_statuses=EXCLUDED_STATUS_KEYWORDS,
        excludes_tags=(
            "admin",
            "internal",
            "install",
            "project management",
            "plumbing",
            "service",
            "canceled",
            "cancelled",
            "no access",
        ),
        required_context_fields=("status",),
        required_data_fields=required_data_fields,
        alert_routing="sales/comfort advisor audit channel",
    )


def _hvac_scope(*, required_data_fields: tuple[str, ...], statuses: tuple[str, ...] = CLOSED_STATUS_KEYWORDS) -> RuleScope:
    return RuleScope(
        handbook_source="HVAC Service audit configuration",
        applies_to_job_statuses=statuses,
        applies_to_workflows=HVAC_SERVICE_WORKFLOW_KEYWORDS,
        excludes_job_types=(
            "admin",
            "internal",
            "material only",
            "warehouse only",
            "install",
            "project",
            "sales",
            "comfort advisor",
            "plumbing",
        ),
        excludes_statuses=EXCLUDED_STATUS_KEYWORDS,
        excludes_tags=(
            "admin",
            "internal",
            "install",
            "project management",
            "sales",
            "comfort advisor",
            "plumbing",
            "canceled",
            "cancelled",
            "no access",
        ),
        required_context_fields=("status",),
        required_data_fields=required_data_fields,
        alert_routing="hvac service audit channel",
    )


def _plumbing_scope(*, required_data_fields: tuple[str, ...], statuses: tuple[str, ...] = CLOSED_STATUS_KEYWORDS) -> RuleScope:
    return RuleScope(
        handbook_source="Plumbing Service audit configuration",
        applies_to_business_units=PLUMBING_SERVICE_BUSINESS_UNIT_IDS,
        applies_to_job_types=PLUMBING_SERVICE_JOB_TYPE_IDS,
        applies_to_job_statuses=statuses,
        excludes_job_types=(
            "30209",
            "111922608",
            "112630828",
            "admin",
            "internal",
            "material only",
            "warehouse only",
            "estimate",
            "sales",
            "install",
            "hvac",
            "comfort advisor",
        ),
        excludes_statuses=EXCLUDED_STATUS_KEYWORDS,
        excludes_tags=(
            "admin",
            "internal",
            "sales",
            "install",
            "hvac",
            "comfort advisor",
            "canceled",
            "cancelled",
            "no access",
        ),
        required_context_fields=("status", "business_unit", "job_type"),
        required_data_fields=required_data_fields,
        alert_routing="plumbing service audit channel",
    )


def _plumbing_options_scope(
    *,
    required_data_fields: tuple[str, ...],
    statuses: tuple[str, ...] = ("Completed", "Closed"),
) -> RuleScope:
    base = _plumbing_scope(required_data_fields=required_data_fields, statuses=statuses)
    return replace(
        base,
        excludes_job_types=base.excludes_job_types + PLUMBING_OPTIONS_EXCLUDED_JOB_TYPES,
    )


def _scope_for_handbook_rule(definition: HandbookRuleDefinition) -> RuleScope:
    if definition.ruleset == RULESET_PLY_MATERIALS:
        return _ply_material_scope(definition)
    if definition.rule_id in {
        "scope_change_missing_escalation_note",
        "cancellation_after_materials_missing_escalation",
        "defective_part_missing_warranty_claim_data",
    }:
        return RuleScope(
            handbook_source=definition.handbook_source,
            applies_to_workflows=("service", "plumbing", "material"),
            applies_to_job_statuses=ACTIVE_OR_CLOSED_STATUS_KEYWORDS,
            applies_to_roles=("dispatcher", "operations", "warehouse"),
            excludes_job_types=("admin", "internal"),
            required_context_fields=("status", "job_type"),
            required_data_fields=definition.required_data_fields,
            alert_routing=definition.recommended_alert_recipient,
            default_enabled=definition.enabled_by_default,
        )
    return _service_call_scope(
        handbook_source=definition.handbook_source,
        required_data_fields=definition.required_data_fields,
        roles=("technician", "dispatcher"),
        alert_routing=definition.recommended_alert_recipient,
        statuses=ACTIVE_OR_CLOSED_STATUS_KEYWORDS if "arrival" in definition.rule_id else CLOSED_STATUS_KEYWORDS,
        excludes_tags=BILLING_EXCLUDED_TAG_KEYWORDS if "diagnostic_fee" in definition.rule_id else EXCLUDED_TAG_KEYWORDS,
    )


def sales_comfort_advisor_rules() -> list[AuditRule]:
    return [
        AuditRule(
            "sales_options_fewer_than_three",
            RULESET_SALES,
            "high",
            "Closed Sales job has fewer than 3 options",
            "Closed Sales / Comfort Advisor jobs must show at least three options or estimates.",
            ("status", "estimates"),
            "Review the Sales job and confirm Good / Better / Best options were presented or documented.",
            _sales_three_options,
            scope=_sales_scope(required_data_fields=("status", "estimates")),
        ),
        AuditRule(
            "sales_photos_missing",
            RULESET_SALES,
            "medium",
            "Closed Sales job is missing required photos",
            "Closed Sales / Comfort Advisor jobs must include supporting photos or attachments.",
            ("status", "photos"),
            "Upload the required Sales photos or document why photos were not required.",
            _sales_photos,
            scope=_sales_scope(required_data_fields=("status", "photos")),
        ),
        AuditRule(
            "sales_arrival_after_first_half",
            RULESET_SALES,
            "medium",
            "Sales advisor arrived after first half of appointment window",
            "Sales / Comfort Advisor appointments should arrive before the first half of the appointment window ends.",
            ("arrival_window", "arrived_at"),
            "Review advisor dispatch timing and coach the arrival process if needed.",
            _sales_arrival_first_half,
            scope=_sales_scope(
                required_data_fields=("arrival_window", "arrived_at"),
                statuses=ACTIVE_OR_CLOSED_STATUS_KEYWORDS,
            ),
        ),
    ]


def hvac_service_rules() -> list[AuditRule]:
    return [
        AuditRule(
            "hvac_options_fewer_than_three",
            RULESET_HVAC,
            "high",
            "Closed HVAC Service job has fewer than 3 options",
            "Closed HVAC Service jobs must show at least three options or estimates.",
            ("status", "estimates"),
            "Review the HVAC Service job and confirm Good / Better / Best options were presented or documented.",
            _hvac_three_options,
            scope=_hvac_scope(required_data_fields=("status", "estimates")),
        ),
        AuditRule(
            "hvac_payment_missing_on_completed_job",
            RULESET_HVAC,
            "high",
            "Closed HVAC Service job is missing payment",
            "Closed HVAC Service jobs with a positive invoice total must show payment, paid status, or zero balance.",
            ("status", "payments"),
            "Review the invoice/payment record and collect or correct payment documentation.",
            _hvac_payment_on_completed_job,
            scope=_hvac_scope(required_data_fields=("status", "payments")),
        ),
        AuditRule(
            "hvac_diagnosis_form_missing",
            RULESET_HVAC,
            "medium",
            "Closed HVAC Service job is missing diagnosis/service form",
            "Closed HVAC Service jobs must include a completed diagnosis or equivalent service form when job-scoped form data is available.",
            ("status", "forms"),
            "Complete the diagnosis/service form or document why it was not required.",
            _hvac_diagnosis_form,
            scope=_hvac_scope(required_data_fields=("status", "forms")),
        ),
        AuditRule(
            "hvac_required_photos_missing",
            RULESET_HVAC,
            "medium",
            "Closed HVAC Service job is missing required photos",
            "Closed HVAC Service jobs must include required photos or attachments when job-scoped photo data is available.",
            ("status", "photos"),
            "Upload the required HVAC Service photos or document why photos were not required.",
            _hvac_photos,
            scope=_hvac_scope(required_data_fields=("status", "photos")),
        ),
        AuditRule(
            "hvac_arrival_outside_window",
            RULESET_HVAC,
            "medium",
            "HVAC Service arrival is outside the configured window threshold",
            "HVAC Service appointments should arrive within the configured first-window threshold.",
            ("arrival_window", "arrived_at"),
            "Review dispatch timing and coach the arrival process if needed.",
            _arrival_window,
            scope=_hvac_scope(
                required_data_fields=("arrival_window", "arrived_at"),
                statuses=ACTIVE_OR_CLOSED_STATUS_KEYWORDS,
            ),
        ),
    ]


def plumbing_service_rules() -> list[AuditRule]:
    return [
        AuditRule(
            "plumbing_options_fewer_than_three",
            RULESET_PLUMBING,
            "high",
            "Closed Plumbing Service job has fewer than 3 options",
            "Closed Plumbing Service jobs must show at least three options or estimates when this requirement is confirmed by the business.",
            ("status", "estimates"),
            "Review the Plumbing Service job and confirm Good / Better / Best options were presented or documented.",
            _plumbing_three_options,
            scope=_plumbing_options_scope(required_data_fields=("status", "estimates")),
        ),
        AuditRule(
            "plumbing_payment_missing_on_completed_job",
            RULESET_PLUMBING,
            "high",
            "Closed Plumbing Service job is missing payment",
            "Closed Plumbing Service jobs with a positive invoice total must show payment, paid status, or zero balance when structured invoice/payment data is available.",
            ("status", "payments"),
            "Review the invoice/payment record and collect or correct payment documentation.",
            _plumbing_payment_on_completed_job,
            scope=_plumbing_scope(required_data_fields=("status", "payments")),
        ),
        AuditRule(
            "plumbing_diagnosis_form_missing",
            RULESET_PLUMBING,
            "medium",
            "Closed Plumbing Service job is missing diagnosis/service form",
            "Closed Plumbing Service jobs must include a completed diagnosis or equivalent service form when job-scoped form data is available.",
            ("status", "forms"),
            "Complete the diagnosis/service form or document why it was not required.",
            _plumbing_diagnosis_form,
            scope=_plumbing_scope(required_data_fields=("status", "forms")),
        ),
        AuditRule(
            "plumbing_required_photos_missing",
            RULESET_PLUMBING,
            "medium",
            "Closed Plumbing Service job is missing required photos",
            "Closed Plumbing Service jobs must include required photos or attachments when job-scoped photo data is available.",
            ("status", "photos"),
            "Upload the required Plumbing Service photos or document why photos were not required.",
            _plumbing_photos,
            scope=_plumbing_scope(required_data_fields=("status", "photos")),
        ),
        AuditRule(
            "plumbing_arrival_outside_window",
            RULESET_PLUMBING,
            "medium",
            "Plumbing Service arrival is outside the configured window threshold",
            "Plumbing Service appointments should arrive within the configured first-window threshold when actual arrival data is available.",
            ("arrival_window", "arrived_at"),
            "Review dispatch timing and coach the arrival process if needed.",
            _arrival_window,
            scope=_plumbing_scope(
                required_data_fields=("arrival_window", "arrived_at"),
                statuses=ACTIVE_OR_CLOSED_STATUS_KEYWORDS,
            ),
        ),
    ]


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
            scope=_service_call_scope(
                handbook_source="Technician compliance configuration",
                required_data_fields=("status", "technician", "clock_in"),
                roles=("technician",),
                alert_routing="service/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Technician compliance configuration",
                required_data_fields=("status", "technician", "clock_out"),
                roles=("technician",),
                alert_routing="service/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Technician compliance configuration",
                required_data_fields=("status", "clock_in", "clock_out", "lunch_break"),
                roles=("technician",),
                alert_routing="service/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Technician compliance configuration",
                required_data_fields=("status", "invoice_line_items", "repair_sold"),
                roles=("technician", "accounting"),
                alert_routing="accounting/operations channel",
                excludes_tags=BILLING_EXCLUDED_TAG_KEYWORDS,
            ),
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
            scope=_service_call_scope(
                handbook_source="Technician compliance configuration",
                required_data_fields=("status", "completed_phases"),
                roles=("technician",),
                alert_routing="service/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Technician compliance configuration",
                required_data_fields=("status", "operational_data"),
                roles=("technician",),
                alert_routing="service/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Dispatcher quality configuration",
                required_data_fields=("arrival_window", "arrived_at"),
                roles=("dispatcher", "technician"),
                alert_routing="dispatcher/operations audit channel",
                statuses=ACTIVE_OR_CLOSED_STATUS_KEYWORDS,
            ),
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
            scope=_service_call_scope(
                handbook_source="Dispatcher quality configuration",
                required_data_fields=("status", "invoice_line_items", "repair_sold"),
                roles=("dispatcher", "accounting"),
                alert_routing="accounting/operations channel",
                excludes_tags=BILLING_EXCLUDED_TAG_KEYWORDS,
            ),
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
            scope=_service_call_scope(
                handbook_source="Dispatcher quality configuration",
                required_data_fields=("status", "options_presented"),
                roles=("dispatcher",),
                alert_routing="dispatcher/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Dispatcher quality configuration",
                required_data_fields=("status", "notes"),
                roles=("dispatcher",),
                alert_routing="dispatcher/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Dispatcher quality configuration",
                required_data_fields=("status", "photos"),
                roles=("technician", "dispatcher"),
                alert_routing="dispatcher/operations audit channel",
            ),
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
            scope=_service_call_scope(
                handbook_source="Dispatcher quality configuration",
                required_data_fields=("status", "supporting_evidence"),
                roles=("technician", "dispatcher"),
                alert_routing="dispatcher/operations audit channel",
            ),
        ),
    ]


def handbook_audit_rules() -> list[AuditRule]:
    evaluators: dict[str, Callable[[ServiceTitanJob, Settings, AuditRule], RuleResult]] = {
        "first_call_on_time_arrival": _first_call_arrival,
        "arrival_outside_window_start_threshold": _arrival_window,
        "missing_job_completion_notes": _missing_completion_notes,
        "job_notes_too_short": _notes,
        "missing_required_photos": _photos,
        "missing_equipment_registration": _equipment_registration,
        "missing_hhr_or_service_form": _hhr_or_service_form,
        "missing_three_repair_options": _three_repair_options,
        "missing_home_comfort_plan_option": _home_comfort_plan_option,
        "missing_same_day_estimate": _same_day_estimate,
        "missing_price_authorization": _price_authorization,
        "missing_diagnostic_fee_when_repair_not_sold": _diagnostic_fee_when_no_repair,
        "diagnostic_fee_not_waived_when_repair_sold": _diagnostic_fee_waiver_when_repair_sold,
        "missing_payment_on_completed_job": _payment_on_completed_job,
        "missing_follow_up_task_when_follow_up_needed": _follow_up_task,
        "special_order_missing_required_notes": _special_order_required_notes,
        "special_order_missing_service_titan_reminder": _special_order_reminder,
        "missing_downpayment_for_special_order": _special_order_downpayment,
        "lead_turnover_missing_required_documentation": _lead_turnover_documentation,
        "po_received_not_reconciled": _po_received_not_reconciled,
        "po_missing_vendor_document": _po_missing_vendor_document,
        "po_missing_attachments": _po_missing_attachments,
        "po_not_synced_to_service_titan": _po_not_synced_to_service_titan,
        "ply_st_material_sync_blocked": _ply_st_material_sync_blocked,
        "scope_change_missing_escalation_note": _scope_change_escalation,
        "cancellation_after_materials_missing_escalation": _cancellation_after_materials_escalation,
        "defective_part_missing_warranty_claim_data": _defective_part_warranty_claim,
    }
    return [_handbook_rule(rule_id, evaluator) for rule_id, evaluator in evaluators.items()]


def _handbook_rule(rule_id: str, evaluator: Callable[[ServiceTitanJob, Settings, AuditRule], RuleResult]) -> AuditRule:
    definition = handbook_rule_by_id(rule_id)
    return AuditRule(
        definition.rule_id,
        definition.ruleset,
        definition.severity,
        definition.title,
        definition.business_reason,
        definition.required_data_fields,
        definition.recommended_action,
        evaluator,
        handbook_source=definition.handbook_source,
        recommended_alert_recipient=definition.recommended_alert_recipient,
        delivery=definition.delivery,
        enabled_by_default=definition.enabled_by_default,
        scope=_scope_for_handbook_rule(definition),
    )


def _effective_rule(rule: AuditRule, settings: Settings) -> AuditRule:
    config = settings.service_titan_rule_scope_config or {}
    rulesets_config = config.get("rulesets", {}) if isinstance(config.get("rulesets", {}), dict) else {}
    ruleset_config = rulesets_config.get(rule.ruleset, {}) if isinstance(rulesets_config.get(rule.ruleset, {}), dict) else {}
    rules_config = config.get("rules", {}) if isinstance(config.get("rules", {}), dict) else {}
    rule_config = rules_config.get(rule.rule_id, {}) if isinstance(rules_config.get(rule.rule_id, {}), dict) else {}

    enabled = rule.scope.default_enabled
    if "enabled" in ruleset_config:
        enabled = _config_bool(ruleset_config["enabled"], enabled)
    if "enabled" in rule_config:
        enabled = _config_bool(rule_config["enabled"], enabled)

    ruleset_applies = ruleset_config.get("applies_to", {}) if isinstance(ruleset_config.get("applies_to", {}), dict) else {}
    ruleset_excludes = ruleset_config.get("excludes", {}) if isinstance(ruleset_config.get("excludes", {}), dict) else {}
    ruleset_alert = ruleset_config.get("alert", {}) if isinstance(ruleset_config.get("alert", {}), dict) else {}
    applies = rule_config.get("applies_to", {}) if isinstance(rule_config.get("applies_to", {}), dict) else {}
    excludes = rule_config.get("excludes", {}) if isinstance(rule_config.get("excludes", {}), dict) else {}
    alert = rule_config.get("alert", {}) if isinstance(rule_config.get("alert", {}), dict) else {}

    scope = replace(
        rule.scope,
        applies_to_departments=_merged_config_tuple(ruleset_applies, applies, ("departments",), rule.scope.applies_to_departments),
        applies_to_business_units=_merged_config_tuple(ruleset_applies, applies, ("business_units", "business_unit_ids"), rule.scope.applies_to_business_units),
        applies_to_trades=_merged_config_tuple(ruleset_applies, applies, ("trades",), rule.scope.applies_to_trades),
        applies_to_job_types=_merged_config_tuple(ruleset_applies, applies, ("job_types", "job_types_contains", "job_type_ids"), rule.scope.applies_to_job_types),
        applies_to_job_statuses=_merged_config_tuple(ruleset_applies, applies, ("statuses", "job_statuses"), rule.scope.applies_to_job_statuses),
        applies_to_tags=_merged_config_tuple(ruleset_applies, applies, ("tags", "tags_contains", "tag_ids"), rule.scope.applies_to_tags),
        applies_to_campaigns=_config_tuple(
            applies,
            ("campaigns", "campaigns_contains", "campaign_ids", "lead_sources", "lead_sources_contains"),
            _config_tuple(
                ruleset_applies,
                ("campaigns", "campaigns_contains", "campaign_ids", "lead_sources", "lead_sources_contains"),
                rule.scope.applies_to_campaigns,
            ),
        ),
        applies_to_roles=_merged_config_tuple(ruleset_applies, applies, ("roles",), rule.scope.applies_to_roles),
        applies_to_workflows=_merged_config_tuple(ruleset_applies, applies, ("workflows", "workflow_contains"), rule.scope.applies_to_workflows),
        excludes_job_types=_merged_config_tuple(ruleset_excludes, excludes, ("job_types", "job_types_contains", "job_type_ids"), rule.scope.excludes_job_types),
        excludes_statuses=_merged_config_tuple(ruleset_excludes, excludes, ("statuses", "job_statuses"), rule.scope.excludes_statuses),
        excludes_tags=_merged_config_tuple(ruleset_excludes, excludes, ("tags", "tags_contains", "tag_ids"), rule.scope.excludes_tags),
        excludes_cancellation_reasons=_merged_config_tuple(
            ruleset_excludes,
            excludes,
            ("cancellation_reasons", "cancellation_reasons_contains"),
            rule.scope.excludes_cancellation_reasons,
        ),
        alert_routing=str(alert.get("channel") or alert.get("destination") or ruleset_alert.get("channel") or ruleset_alert.get("destination") or rule.scope.alert_routing),
        default_enabled=enabled,
    )
    return replace(
        rule,
        scope=scope,
        recommended_alert_recipient=scope.alert_routing or rule.recommended_alert_recipient,
        enabled_by_default=enabled,
    )


def _config_tuple(source: dict[str, Any], keys: tuple[str, ...], default: tuple[str, ...]) -> tuple[str, ...]:
    for key in keys:
        if key not in source:
            continue
        value = source[key]
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, list):
            return tuple(str(item) for item in value if item is not None and str(item).strip())
    return default


def _merged_config_tuple(
    ruleset_source: dict[str, Any],
    rule_source: dict[str, Any],
    keys: tuple[str, ...],
    default: tuple[str, ...],
) -> tuple[str, ...]:
    return _config_tuple(rule_source, keys, _config_tuple(ruleset_source, keys, default))


def _config_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _applicability_decision(job: ServiceTitanJob, scope: RuleScope) -> ApplicabilityDecision:
    if not scope.default_enabled:
        return ApplicabilityDecision(RESULT_NOT_APPLICABLE, "Rule is disabled by scope configuration.", {"scope_decision": "disabled"})

    material_state = _material_context_state(job)
    if "materials_or_po" in scope.required_context_fields:
        if material_state == "absent":
            return ApplicabilityDecision(
                RESULT_NOT_APPLICABLE,
                "No PO, Ply, or material context was available for this job.",
                {"scope_decision": "no_material_or_po_context"},
            )
        if material_state == "unknown":
            return ApplicabilityDecision(
                RESULT_INSUFFICIENT,
                "PO/material scope is unknown because purchase-order or Ply data was unavailable.",
                {"scope_decision": "material_or_po_context_unknown"},
            )

    missing_context = [field for field in scope.required_context_fields if field != "materials_or_po" and not _context_available(job, field)]
    if missing_context:
        return ApplicabilityDecision(
            RESULT_INSUFFICIENT,
            "Required applicability context is missing: " + ", ".join(sorted(missing_context)) + ".",
            {"scope_decision": "missing_context", "missing_context_fields": sorted(missing_context)},
        )

    excluded = _first_matching_scope(job, {
        "status": scope.excludes_statuses,
        "job_type": scope.excludes_job_types,
        "tags": scope.excludes_tags,
        "cancellation_reason": scope.excludes_cancellation_reasons,
    })
    if excluded:
        field, pattern = excluded
        return ApplicabilityDecision(
            RESULT_NOT_APPLICABLE,
            f"Rule scope excludes this job because {field} matched {pattern!r}.",
            {"scope_decision": "excluded", "field": field, "pattern": pattern},
        )

    include_checks = {
        "department": scope.applies_to_departments,
        "business_unit": scope.applies_to_business_units,
        "trade": scope.applies_to_trades,
        "job_type": scope.applies_to_job_types,
        "status": scope.applies_to_job_statuses,
        "tags": scope.applies_to_tags,
        "campaign": scope.applies_to_campaigns,
        "workflow": scope.applies_to_workflows,
    }
    for field, patterns in include_checks.items():
        if not patterns:
            continue
        values = _context_values(job, field)
        if not values:
            return ApplicabilityDecision(
                RESULT_INSUFFICIENT,
                f"Rule applicability requires {field}, but ServiceTitan did not provide it.",
                {"scope_decision": "missing_include_context", "field": field},
            )
        if not _matches_any(values, patterns):
            return ApplicabilityDecision(
                RESULT_NOT_APPLICABLE,
                f"Rule does not apply because {field} did not match configured scope.",
                {"scope_decision": "include_mismatch", "field": field, "patterns": list(patterns)},
            )

    return ApplicabilityDecision("applies", "Rule scope applies to this job.", {"scope_decision": "applies"})


def _first_matching_scope(job: ServiceTitanJob, checks: dict[str, tuple[str, ...]]) -> tuple[str, str] | None:
    for field, patterns in checks.items():
        if not patterns:
            continue
        values = _context_values(job, field)
        pattern = _matched_pattern(values, patterns)
        if pattern:
            return field, pattern
    return None


def _context_available(job: ServiceTitanJob, field: str) -> bool:
    if field in {"status", "technician", "dispatcher", "appointment_id"}:
        return bool(_context_values(job, field))
    if field == "workflow":
        return bool(_context_values(job, field)) and bool({"workflow", "job_type", "business_unit", "department", "trade", "tags"} & job.present_fields)
    if field in {"job_type", "business_unit", "department", "trade", "workflow", "tags", "cancellation_reason"}:
        return field in job.present_fields and bool(_context_values(job, field))
    if field == "campaign":
        return field in job.present_fields and bool(_context_values(job, field))
    return field in job.present_fields


def _context_values(job: ServiceTitanJob, field: str) -> list[str]:
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
    if field == "cancellation_reason":
        return [job.cancellation_reason] if job.cancellation_reason else []
    if field == "technician":
        return [value for value in (job.technician_id, job.technician_name) if value]
    if field == "dispatcher":
        return [value for value in (job.dispatcher_id, job.dispatcher_name) if value]
    if field == "appointment_id":
        return [job.appointment_id] if job.appointment_id else []
    return []


def _material_context_state(job: ServiceTitanJob) -> str:
    if job.ply_data_available:
        return "present"
    if job.purchase_orders_count is not None:
        return "present" if job.purchase_orders_count > 0 else "absent"
    if job.purchase_orders:
        return "present"
    if "purchase_orders" in job.present_fields:
        return "absent"
    if "purchase_orders" in job.missing_data:
        return "unknown"
    text = " ".join(_context_values(job, "workflow") + _context_values(job, "job_type")).lower()
    if any(keyword in text for keyword in ("material", "purchase order", " po ", "ply", "special order")):
        return "present"
    return "unknown"


def _matches_any(values: list[str], patterns: tuple[str, ...]) -> bool:
    return bool(_matched_pattern(values, patterns))


def _matched_pattern(values: list[str], patterns: tuple[str, ...]) -> str:
    normalized_values = [_normalize(value) for value in values if value]
    for pattern in patterns:
        normalized_pattern = _normalize(pattern)
        if not normalized_pattern:
            continue
        if any(normalized_pattern in value or value in normalized_pattern for value in normalized_values):
            return pattern
    return ""


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


def violation_key(job: ServiceTitanJob, rule_id: str) -> str:
    appointment_id = job.appointment_id or "no-appointment"
    actor_id = job.actor_id
    return f"servicetitan:{job.job_id}:{appointment_id}:{rule_id}:{actor_id}"


def _closed_or_pass(job: ServiceTitanJob, rule: AuditRule) -> RuleResult | None:
    if "status" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, "ServiceTitan payload did not include job status.", rule.action)
    if not job.is_closed:
        return rule.result(job, RESULT_NOT_APPLICABLE, "Job is not closed; closeout rule does not apply yet.", rule.action)
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
    if job.repair_sold is None:
        return rule.result(job, RESULT_INSUFFICIENT, "Repair-sold status could not be determined; diagnostic fee rule cannot safely apply.", rule.action)
    if job.repair_sold:
        return rule.result(job, RESULT_NOT_APPLICABLE, "Repair was sold; missing diagnostic fee collection rule does not apply.", rule.action)
    keywords = [keyword.lower() for keyword in settings.service_titan_diagnostic_fee_keywords if keyword]
    if not keywords:
        return rule.result(job, RESULT_INSUFFICIENT, "Diagnostic fee keywords are not configured.", rule.action)
    haystack = " | ".join(job.invoice_line_items).lower()
    if any(keyword in haystack for keyword in keywords):
        return rule.result(job, RESULT_PASS, "Diagnostic fee line item is present.", rule.action)
    if _approved_waiver_note(job):
        return rule.result(job, RESULT_PASS, "Approved diagnostic fee waiver reason is documented.", rule.action)
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


def _sales_three_options(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "estimates" not in job.present_fields or job.estimate_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "estimates", "Estimate/option records were not available from ServiceTitan."), rule.action)
    required_count = max(3, settings.service_titan_min_repair_options)
    metadata = {"options_count": job.estimate_count, "required_options_count": required_count}
    if job.estimate_count < required_count:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Only {job.estimate_count} option/estimate record(s) were found; required minimum is {required_count}.",
            rule.action,
            metadata,
        )
    return rule.result(job, RESULT_PASS, "Required Sales option count is satisfied.", rule.action, metadata)


def _sales_photos(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "photos" not in job.present_fields or job.photo_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "photos", "Photo/attachment records were not available from ServiceTitan."), rule.action)
    metadata = {"photos_count": job.photo_count}
    if job.photo_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed Sales job has no uploaded photos or image attachments.", rule.action, metadata)
    return rule.result(job, RESULT_PASS, "Required Sales photos are present.", rule.action, metadata)


def _sales_arrival_first_half(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    missing = _missing_fields(job, rule.required_fields)
    if missing:
        return rule.result(job, RESULT_INSUFFICIENT, _missing_field_explanation(job, rule.required_fields), rule.action)
    if not job.arrival_window_start or not job.arrival_window_end or not job.arrived_at:
        return rule.result(
            job,
            RESULT_INSUFFICIENT,
            "Arrival window start, arrival window end, and arrival time are required.",
            rule.action,
        )
    window_seconds = (job.arrival_window_end - job.arrival_window_start).total_seconds()
    if window_seconds <= 0:
        return rule.result(job, RESULT_INSUFFICIENT, "Arrival window end must be after arrival window start.", rule.action)
    first_half_cutoff = job.arrival_window_start + timedelta(seconds=window_seconds / 2)
    metadata = {
        "arrival_window_start": job.arrival_window_start.isoformat(),
        "arrival_window_end": job.arrival_window_end.isoformat(),
        "arrival_first_half_cutoff": first_half_cutoff.isoformat(),
        "arrived_at": job.arrived_at.isoformat(),
    }
    if job.arrived_at > first_half_cutoff:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Advisor arrived at {job.arrived_at.isoformat()}, after the first-half cutoff of {first_half_cutoff.isoformat()}.",
            rule.action,
            metadata,
        )
    return rule.result(job, RESULT_PASS, "Advisor arrived before the first half of the appointment window ended.", rule.action, metadata)


def _hvac_three_options(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "estimates" not in job.present_fields or job.estimate_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "estimates", "Estimate/option records were not available from ServiceTitan."), rule.action)
    required_count = max(3, settings.service_titan_min_repair_options)
    metadata = {"options_count": job.estimate_count, "required_options_count": required_count}
    if job.estimate_count < required_count:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Only {job.estimate_count} option/estimate record(s) were found; required minimum is {required_count}.",
            rule.action,
            metadata,
        )
    return rule.result(job, RESULT_PASS, "Required HVAC Service option count is satisfied.", rule.action, metadata)


def _hvac_payment_on_completed_job(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    result = _payment_on_completed_job(job, settings, rule)
    metadata = {
        **result.metadata,
        "payment_total": job.payment_total,
        "payments_count": job.payments_count,
        "invoice_balance": job.invoice_balance,
        "invoice_status": job.invoice_status,
    }
    return replace(result, metadata=metadata)


def _hvac_diagnosis_form(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "forms" not in job.present_fields or job.forms_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "forms", "Job-scoped form submissions were not available from ServiceTitan."), rule.action)
    metadata = {
        "forms_count": job.forms_count,
        "diagnosis_form_present": bool(job.hhr_completed or job.forms_count > 0),
    }
    if job.hhr_completed is False or job.forms_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed HVAC Service job has no completed job-scoped diagnosis/service form.", rule.action, metadata)
    return rule.result(job, RESULT_PASS, "Job-scoped diagnosis/service form evidence is present.", rule.action, metadata)


def _hvac_photos(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "photos" not in job.present_fields or job.photo_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "photos", "Job-scoped photo/attachment records were not available from ServiceTitan."), rule.action)
    metadata = {"photos_count": job.photo_count}
    if job.photo_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed HVAC Service job has no uploaded photos or image attachments.", rule.action, metadata)
    return rule.result(job, RESULT_PASS, "Required HVAC Service photos are present.", rule.action, metadata)


def _plumbing_three_options(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    has_no_charge_signal = job.invoice_total == 0 and job.invoice_balance == 0
    has_charge_signal = (
        (job.invoice_total is not None and job.invoice_total > 0)
        or (job.payment_total is not None and job.payment_total > 0)
        or ("paid" in job.invoice_status.lower())
    )
    billing_metadata = {
        "invoice_total": job.invoice_total,
        "invoice_balance": job.invoice_balance,
        "payment_total": job.payment_total,
        "payments_count": job.payments_count,
        "invoice_status": job.invoice_status,
        "billing_no_charge_signal": has_no_charge_signal,
        "billing_charge_signal": has_charge_signal,
    }
    if has_no_charge_signal:
        return rule.result(
            job,
            RESULT_NOT_APPLICABLE,
            "Structured invoice data shows a zero-dollar no-charge job; Plumbing options rule does not apply.",
            rule.action,
            billing_metadata,
        )
    if not has_charge_signal:
        return rule.result(
            job,
            RESULT_INSUFFICIENT,
            _field_unavailable(
                job,
                "payments",
                "Structured billing context was not available or did not show a positive charge; skipping Plumbing options failure to avoid false positives.",
            ),
            rule.action,
            billing_metadata,
        )
    if "estimates" not in job.present_fields or job.estimate_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "estimates", "Estimate/option records were not available from ServiceTitan."), rule.action)
    required_count = max(3, settings.service_titan_min_repair_options)
    metadata = {"options_count": job.estimate_count, "required_options_count": required_count, **billing_metadata}
    if job.estimate_count < required_count:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Only {job.estimate_count} option/estimate record(s) were found; required minimum is {required_count}.",
            rule.action,
            metadata,
        )
    return rule.result(job, RESULT_PASS, "Required Plumbing Service option count is satisfied.", rule.action, metadata)


def _plumbing_payment_on_completed_job(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    result = _payment_on_completed_job(job, settings, rule)
    metadata = {
        **result.metadata,
        "payment_total": job.payment_total,
        "payments_count": job.payments_count,
        "invoice_balance": job.invoice_balance,
        "invoice_status": job.invoice_status,
    }
    return replace(result, metadata=metadata)


def _plumbing_diagnosis_form(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "forms" not in job.present_fields or job.forms_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "forms", "Job-scoped form submissions were not available from ServiceTitan."), rule.action)
    metadata = {
        "forms_count": job.forms_count,
        "diagnosis_form_present": bool(job.hhr_completed or job.forms_count > 0),
    }
    if job.hhr_completed is False or job.forms_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed Plumbing Service job has no completed job-scoped diagnosis/service form.", rule.action, metadata)
    return rule.result(job, RESULT_PASS, "Job-scoped diagnosis/service form evidence is present.", rule.action, metadata)


def _plumbing_photos(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "photos" not in job.present_fields or job.photo_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "photos", "Job-scoped photos/attachments were not available from ServiceTitan."), rule.action)
    metadata = {"photos_count": job.photo_count}
    if job.photo_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed Plumbing Service job has no job-scoped required photos.", rule.action, metadata)
    return rule.result(job, RESULT_PASS, "Required Plumbing Service photo evidence is present.", rule.action, metadata)


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


def _first_call_arrival(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    return _arrival_with_grace(job, settings.service_titan_first_call_grace_minutes, rule, "first-call")


def _missing_completion_notes(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Job notes were not available from ServiceTitan."), rule.action)
    if not (job.notes or "").strip():
        return rule.result(job, RESULT_FAIL, "Closed job has no completion notes.", rule.action)
    return rule.result(job, RESULT_PASS, "Completion notes are present.", rule.action)


def _equipment_registration(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if not settings.service_titan_require_equipment_registration:
        return rule.result(job, RESULT_NOT_APPLICABLE, "Equipment registration requirement is disabled.", rule.action)
    if "equipment" not in job.present_fields or job.equipment_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "equipment", "Equipment records were not available from ServiceTitan."), rule.action)
    if job.equipment_count <= 0:
        return rule.result(job, RESULT_FAIL, "Closed job has no equipment registration records.", rule.action)
    if job.equipment_complete is False:
        return rule.result(job, RESULT_FAIL, "Equipment records are missing required registration fields.", rule.action)
    return rule.result(job, RESULT_PASS, "Equipment registration is present and complete.", rule.action)


def _hhr_or_service_form(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if not settings.service_titan_require_hhr:
        return rule.result(job, RESULT_NOT_APPLICABLE, "HHR requirement is disabled.", rule.action)
    if "hhr" not in job.present_fields or job.hhr_completed is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "hhr", "HHR/form submissions were not available from ServiceTitan."), rule.action)
    if not job.hhr_completed:
        return rule.result(job, RESULT_FAIL, "Closed job has no HHR or configured equivalent service form.", rule.action)
    return rule.result(job, RESULT_PASS, "HHR or configured equivalent service form is present.", rule.action)


def _three_repair_options(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "estimates" not in job.present_fields or job.estimate_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "estimates", "Estimate/option records were not available from ServiceTitan."), rule.action)
    if job.estimate_count < settings.service_titan_min_repair_options:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Only {job.estimate_count} repair option(s) were found; required minimum is {settings.service_titan_min_repair_options}.",
            rule.action,
            {"estimate_count": job.estimate_count},
        )
    return rule.result(job, RESULT_PASS, "Configured minimum repair option count is satisfied.", rule.action)


def _home_comfort_plan_option(job: ServiceTitanJob, settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if not settings.service_titan_require_home_comfort_plan_option:
        return rule.result(job, RESULT_NOT_APPLICABLE, "Home Comfort Plan option requirement is disabled.", rule.action)
    if "home_comfort_plan_option" not in job.present_fields or job.home_comfort_plan_option_present is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "home_comfort_plan_option", "Home Comfort Plan option data was not available from ServiceTitan."), rule.action)
    if not job.home_comfort_plan_option_present:
        return rule.result(job, RESULT_FAIL, "No configured Home Comfort Plan option indicator was found.", rule.action)
    return rule.result(job, RESULT_PASS, "Home Comfort Plan option is present.", rule.action)


def _same_day_estimate(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "same_day_estimate" not in job.present_fields or job.same_day_estimate_present is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "same_day_estimate", "Same-day estimate data was not available from ServiceTitan."), rule.action)
    if not job.same_day_estimate_present:
        return rule.result(job, RESULT_FAIL, "No same-day estimate was found for the closed job.", rule.action)
    return rule.result(job, RESULT_PASS, "Same-day estimate evidence is present.", rule.action)


def _price_authorization(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "authorization" not in job.present_fields or job.authorization_count is None:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "authorization", "Authorization/signature data was not available from ServiceTitan."), rule.action)
    if job.authorization_count <= 0:
        return rule.result(job, RESULT_FAIL, "No customer price authorization or signature evidence was found.", rule.action)
    return rule.result(job, RESULT_PASS, "Customer authorization evidence is present.", rule.action)


def _diagnostic_fee_when_no_repair(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "invoice_line_items" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "invoice_line_items", "Invoice line items were not available from ServiceTitan."), rule.action)
    if job.repair_sold is None:
        return rule.result(job, RESULT_INSUFFICIENT, "Repair-sold status could not be determined from invoice data.", rule.action)
    if job.repair_sold:
        return rule.result(job, RESULT_NOT_APPLICABLE, "Repair was sold; diagnostic collection rule does not apply.", rule.action)
    if job.diagnostic_fee_present:
        return rule.result(job, RESULT_PASS, "Diagnostic fee is present for a non-repair job.", rule.action)
    if _approved_waiver_note(job):
        return rule.result(job, RESULT_PASS, "Approved diagnostic fee waiver reason is documented.", rule.action)
    return rule.result(job, RESULT_FAIL, "No repair was sold and no diagnostic fee or approved waiver was found.", rule.action)


def _diagnostic_fee_waiver_when_repair_sold(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "invoice_line_items" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "invoice_line_items", "Invoice line items were not available from ServiceTitan."), rule.action)
    if job.repair_sold is None:
        return rule.result(job, RESULT_INSUFFICIENT, "Repair-sold status could not be determined from invoice data.", rule.action)
    if not job.repair_sold:
        return rule.result(job, RESULT_NOT_APPLICABLE, "Repair was not sold; diagnostic waiver rule does not apply.", rule.action)
    if job.diagnostic_fee_charged is None and job.diagnostic_fee_present:
        return rule.result(job, RESULT_INSUFFICIENT, "Diagnostic fee line item was present, but amount/waiver status was unavailable.", rule.action)
    if job.diagnostic_fee_charged and not job.diagnostic_fee_waived:
        return rule.result(job, RESULT_FAIL, "Repair was sold and a positive diagnostic fee charge was visible without waiver evidence.", rule.action)
    return rule.result(job, RESULT_PASS, "Diagnostic fee waiver rule is satisfied for the sold repair.", rule.action)


def _payment_on_completed_job(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    closed = _closed_or_pass(job, rule)
    if closed:
        return closed
    if "payments" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "payments", "Payment fields were not available from ServiceTitan."), rule.action)
    if job.invoice_total is None:
        return rule.result(job, RESULT_INSUFFICIENT, "Invoice total was not available from ServiceTitan.", rule.action)
    if job.invoice_total <= 0:
        return rule.result(job, RESULT_NOT_APPLICABLE, "Invoice total is not positive; payment rule does not apply.", rule.action)
    if (job.payment_total or 0) > 0 or (job.invoice_balance is not None and job.invoice_balance <= 0) or "paid" in job.invoice_status.lower():
        return rule.result(job, RESULT_PASS, "Payment or paid invoice status is present.", rule.action)
    return rule.result(job, RESULT_FAIL, "Completed job has a positive invoice total but no visible payment or paid status.", rule.action)


def _follow_up_task(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Follow-up notes were not available from ServiceTitan."), rule.action)
    if not job.follow_up_needed:
        return rule.result(job, RESULT_NOT_APPLICABLE, "No follow-up need was detected.", rule.action)
    if job.follow_up_task_present:
        return rule.result(job, RESULT_PASS, "Follow-up task/reminder evidence is present.", rule.action)
    return rule.result(job, RESULT_FAIL, "Follow-up was indicated but no follow-up task/reminder evidence was found.", rule.action)


def _special_order_required_notes(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Special-order notes were not available from ServiceTitan."), rule.action)
    if not job.special_order_detected:
        return rule.result(job, RESULT_NOT_APPLICABLE, "No special-order work was detected.", rule.action)
    if job.special_order_missing_fields:
        return rule.result(job, RESULT_FAIL, "Special-order note field(s) missing: " + ", ".join(job.special_order_missing_fields), rule.action, {"missing_fields": job.special_order_missing_fields})
    return rule.result(job, RESULT_PASS, "Special-order notes contain the configured required fields.", rule.action)


def _special_order_reminder(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Special-order notes were not available from ServiceTitan."), rule.action)
    if not job.special_order_detected:
        return rule.result(job, RESULT_NOT_APPLICABLE, "No special-order work was detected.", rule.action)
    return rule.result(
        job,
        RESULT_INSUFFICIENT,
        "ServiceTitan reminder/task endpoint is not integrated yet; reminder evidence cannot be verified without creating false positives.",
        rule.action,
    )


def _special_order_downpayment(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Special-order notes were not available from ServiceTitan."), rule.action)
    if not job.special_order_detected:
        return rule.result(job, RESULT_NOT_APPLICABLE, "No special-order work was detected.", rule.action)
    if "payments" not in job.present_fields and "invoice_line_items" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, "Payment and invoice item data are unavailable for special-order downpayment verification.", rule.action)
    if job.downpayment_recorded:
        return rule.result(job, RESULT_PASS, "Special-order downpayment/payment evidence is present.", rule.action)
    return rule.result(job, RESULT_FAIL, "Special order was detected without downpayment/payment evidence.", rule.action)


def _lead_turnover_documentation(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Lead turnover notes were not available from ServiceTitan."), rule.action)
    if not job.lead_turnover_required:
        return rule.result(job, RESULT_NOT_APPLICABLE, "No lead turnover was detected.", rule.action)
    missing_sources = [field for field in ("hhr", "photos", "estimates", "equipment") if field not in job.present_fields]
    if missing_sources:
        return rule.result(job, RESULT_INSUFFICIENT, "Lead turnover source(s) unavailable: " + ", ".join(missing_sources), rule.action)
    missing: list[str] = []
    if not job.hhr_completed:
        missing.append("HHR")
    if not job.photo_count:
        missing.append("equipment/photos")
    if not job.estimate_count:
        missing.append("options")
    if not job.equipment_count:
        missing.append("equipment registration")
    if not job.lead_turnover_documented:
        missing.append("lead turnover notes")
    if missing:
        return rule.result(job, RESULT_FAIL, "Lead turnover documentation missing: " + ", ".join(missing), rule.action, {"missing": missing})
    return rule.result(job, RESULT_PASS, "Lead turnover documentation is complete.", rule.action)


def _po_received_not_reconciled(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    insufficient = _po_insufficient(job, "po_reconciliation", "PO reconciliation data was not available from ServiceTitan.")
    if insufficient:
        return rule.result(job, RESULT_INSUFFICIENT, insufficient, rule.action)
    if (job.po_received_not_reconciled_count or 0) > 0:
        return rule.result(job, RESULT_FAIL, f"{job.po_received_not_reconciled_count} received PO(s) are not reconciled.", rule.action)
    return rule.result(job, RESULT_PASS, "No received unreconciled PO was found.", rule.action)


def _po_missing_vendor_document(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    insufficient = _po_insufficient(job, "po_vendor_document", "PO vendor document data was not available from ServiceTitan.")
    if insufficient:
        return rule.result(job, RESULT_INSUFFICIENT, insufficient, rule.action)
    if (job.po_missing_vendor_document_count or 0) > 0:
        return rule.result(job, RESULT_FAIL, f"{job.po_missing_vendor_document_count} received PO(s) are missing vendor document numbers.", rule.action)
    return rule.result(job, RESULT_PASS, "Received POs have vendor document numbers.", rule.action)


def _po_missing_attachments(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    insufficient = _po_insufficient(job, "po_attachments", "PO attachment data was not available from ServiceTitan.")
    if insufficient:
        return rule.result(job, RESULT_INSUFFICIENT, insufficient, rule.action)
    if (job.po_missing_attachment_count or 0) > 0:
        return rule.result(job, RESULT_FAIL, f"{job.po_missing_attachment_count} received PO(s) are missing attachments.", rule.action)
    return rule.result(job, RESULT_PASS, "Received POs have attachments.", rule.action)


def _po_not_synced_to_service_titan(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if not job.ply_data_available:
        return rule.result(job, RESULT_INSUFFICIENT, "Ply API/client is not configured in this repository; Ply-to-ServiceTitan PO sync cannot be verified.", rule.action)
    if job.po_not_synced_count and job.po_not_synced_count > 0:
        return rule.result(job, RESULT_FAIL, f"{job.po_not_synced_count} Ply PO(s) are not synced to ServiceTitan.", rule.action)
    return rule.result(job, RESULT_PASS, "Ply POs are synced to ServiceTitan.", rule.action)


def _ply_st_material_sync_blocked(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if not job.ply_data_available:
        return rule.result(job, RESULT_INSUFFICIENT, "Ply API/client is not configured in this repository; material sync status cannot be verified.", rule.action)
    return rule.result(job, RESULT_PASS, "No blocked Ply/ST sync was detected.", rule.action)


def _scope_change_escalation(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    return _escalation_rule(job, rule, job.scope_change_detected, job.scope_change_escalated, "scope change")


def _cancellation_after_materials_escalation(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    return _escalation_rule(job, rule, job.cancellation_after_materials_detected, job.cancellation_escalated, "cancellation after materials")


def _defective_part_warranty_claim(job: ServiceTitanJob, _settings: Settings, rule: AuditRule) -> RuleResult:
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", "Defective part notes were not available from ServiceTitan."), rule.action)
    if not job.defective_part_detected:
        return rule.result(job, RESULT_NOT_APPLICABLE, "No defective-part issue was detected.", rule.action)
    if job.warranty_claim_documented:
        return rule.result(job, RESULT_PASS, "Warranty claim/RMA evidence is documented.", rule.action)
    return rule.result(job, RESULT_FAIL, "Defective part was detected without warranty claim/RMA evidence.", rule.action)


def _arrival_with_grace(job: ServiceTitanJob, grace_minutes: int, rule: AuditRule, label: str) -> RuleResult:
    missing = _missing_fields(job, ("arrival_window", "arrived_at"))
    if missing:
        return rule.result(job, RESULT_INSUFFICIENT, _missing_field_explanation(job, ("arrival_window", "arrived_at")), rule.action)
    if not job.arrival_window_start or not job.arrived_at:
        return rule.result(job, RESULT_INSUFFICIENT, "Arrival window start and arrival time are required.", rule.action)
    latest_expected = job.arrival_window_start + timedelta(minutes=grace_minutes)
    if job.arrived_at > latest_expected:
        return rule.result(
            job,
            RESULT_FAIL,
            f"Technician arrived after the configured {label} threshold of {grace_minutes} minute(s).",
            rule.action,
            {"arrival_window_start": job.arrival_window_start.isoformat(), "arrived_at": job.arrived_at.isoformat()},
        )
    return rule.result(job, RESULT_PASS, f"Technician arrived inside the configured {label} threshold.", rule.action)


def _approved_waiver_note(job: ServiceTitanJob) -> bool:
    text = (job.notes or "").lower()
    return "waiver approved" in text or "approved waiver" in text or "manager approved" in text


def _po_insufficient(job: ServiceTitanJob, field: str, fallback: str) -> str:
    if "purchase_orders" not in job.present_fields or field not in job.present_fields:
        return _field_unavailable(job, field, fallback)
    if job.purchase_orders_count is None:
        return fallback
    return ""


def _escalation_rule(job: ServiceTitanJob, rule: AuditRule, detected: bool | None, escalated: bool | None, label: str) -> RuleResult:
    if "notes" not in job.present_fields:
        return rule.result(job, RESULT_INSUFFICIENT, _field_unavailable(job, "notes", f"{label.title()} notes were not available from ServiceTitan."), rule.action)
    if not detected:
        return rule.result(job, RESULT_NOT_APPLICABLE, f"No {label} was detected.", rule.action)
    if escalated:
        return rule.result(job, RESULT_PASS, f"{label.title()} escalation evidence is documented.", rule.action)
    return rule.result(job, RESULT_FAIL, f"{label.title()} was detected without escalation evidence.", rule.action)
