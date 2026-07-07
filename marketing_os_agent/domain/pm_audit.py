from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..clients.servicetitan import ServiceTitanApiError, ServiceTitanClient, ServiceTitanProject, ServiceTitanProjectTask
from ..clients.slack import SlackClient
from ..config import Settings


logger = logging.getLogger(__name__)

PM_PASS = "pass"
PM_FAIL = "fail"
PM_SKIP = "skip"

PM_RULESET = "PM Audit"
PM_INSTALL_PROJECT_TYPE_IDS = {"63812999", "63813000"}
PM_INSTALL_PROJECT_TYPE_NAMES = {"standard install", "construction & remodel", "construction and remodel"}
PM_OUT_OF_SCOPE_KEYWORDS = (
    "service",
    "service call",
    "sales",
    "warranty",
    "recall",
    "home care plan",
    "comfort plan",
    "comfort club",
    "maintenance",
    "free diagnostic",
    "internal",
    "r&d",
)


PM_AUDIT_TEST_MESSAGE = """📋 PM Audit — Test

Jane
• Project #PM-TEST-1001 — Missing PM task template
  Field: Tasks
  Action: Apply PM task template
  Link: https://go.servicetitan.com/#/project/PM-TEST-1001

Gerson
• Project #PM-TEST-1002 — Task has no assignee
  Field: Task #PM-TASK-884
  Action: Assign task owner
  Link: https://go.servicetitan.com/#/project/PM-TEST-1002

Summary: Jane 1 issue · Gerson 1 issue"""


@dataclass(frozen=True)
class PMRuleResult:
    rule_id: str
    name: str
    status: str
    issue: str
    field: str
    action: str
    details: tuple[str, ...] = ()
    due_at: datetime | None = None
    install_date: datetime | None = None
    task_number: str = ""
    skipped_open_tasks_without_due: int = 0


@dataclass(frozen=True)
class _DepositAssessment:
    status: str
    issue: str
    expected_amount: float | None = None
    paid_amount: float | None = None
    job_total: float | None = None


@dataclass
class PMProjectAudit:
    project: ServiceTitanProject
    results: list[PMRuleResult] = field(default_factory=list)
    skipped_out_of_scope: bool = False

    @property
    def failures(self) -> list[PMRuleResult]:
        return [result for result in self.results if result.status == PM_FAIL]


@dataclass
class PMAuditSummary:
    status: str = "completed"
    enabled: bool = False
    dry_run: bool = True
    projects_scanned: int = 0
    in_scope_projects: int = 0
    skipped_out_of_scope: int = 0
    projects_evaluated: int = 0
    tasks_loaded: int = 0
    open_tasks_without_due_skipped: int = 0
    rules_evaluated: int = 0
    pass_count: int = 0
    fail_count: int = 0
    skip_count: int = 0
    alerts_sent: int = 0
    alerts_would_send: int = 0
    errors: int = 0
    include_client_name: bool = False
    config_errors: list[str] = field(default_factory=list)
    project_audits: list[PMProjectAudit] = field(default_factory=list)

    @property
    def failures(self) -> list[tuple[ServiceTitanProject, PMRuleResult]]:
        failures: list[tuple[ServiceTitanProject, PMRuleResult]] = []
        for audit in self.project_audits:
            for result in audit.failures:
                failures.append((audit.project, result))
        return failures

    def to_lines(self) -> list[str]:
        lines = [
            f"PM audit: {self.status}",
            f"- enabled: {self.enabled}",
            f"- dry_run: {self.dry_run}",
            f"- raw projects fetched: {self.projects_scanned}",
            f"- in-scope projects: {self.in_scope_projects}",
            f"- out-of-scope projects skipped: {self.skipped_out_of_scope}",
            f"- projects evaluated: {self.projects_evaluated}",
            f"- tasks loaded: {self.tasks_loaded}",
            f"- open tasks without due date skipped: {self.open_tasks_without_due_skipped}",
            f"- rules evaluated: {self.rules_evaluated}",
            f"- pass: {self.pass_count}",
            f"- fail: {self.fail_count}",
            f"- skip: {self.skip_count}",
            f"- Slack alerts sent: {self.alerts_sent}",
            f"- Slack alerts that would send: {self.alerts_would_send}",
            f"- errors: {self.errors}",
        ]
        if self.config_errors:
            lines.append("- config errors:")
            lines.extend(f"  - {error}" for error in self.config_errors)
        top_fail = self.top_fail_rules(3)
        if top_fail:
            lines.append("- top fail rules:")
            lines.extend(f"  - {rule}: {count}" for rule, count in top_fail)
        top_skip = self.top_skip_reasons(3)
        if top_skip:
            lines.append("- top skip reasons:")
            lines.extend(f"  - {reason}: {count}" for reason, count in top_skip)
        if self.failures:
            lines.extend(["", self.alert_text()])
        return lines

    def top_fail_rules(self, limit: int = 3) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter()
        for _, result in self.failures:
            counts[f"{result.rule_id} {result.name}"] += 1
        return counts.most_common(limit)

    def top_skip_reasons(self, limit: int = 3) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter()
        for audit in self.project_audits:
            for result in audit.results:
                if result.status == PM_SKIP:
                    counts[result.issue] += 1
        return counts.most_common(limit)

    def alert_text(self, now: datetime | None = None, timezone_name: str = "UTC", include_client_name: bool | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        include_client_name = self.include_client_name if include_client_name is None else include_client_name
        local_now = now.astimezone(ZoneInfo(timezone_name))
        groups: dict[str, list[str]] = {}
        clean_counts: dict[str, int] = {}
        for audit in self.project_audits:
            pm = audit.project.project_manager_label
            if audit.failures:
                groups.setdefault(pm, [])
                for failure in audit.failures:
                    groups[pm].append(_failure_block(audit.project, failure, timezone_name, include_client_name=include_client_name))
            elif not audit.skipped_out_of_scope:
                clean_counts[pm] = clean_counts.get(pm, 0) + 1

        lines = [f"📋 PM Audit — {local_now.strftime('%b %d').replace(' 0', ' ')}"]
        for pm in sorted(groups):
            lines.extend(["", pm])
            lines.extend(groups[pm])

        lines.extend(["", f"Summary: {_pm_summary(groups, clean_counts)}"])
        lines.append(f"Totals: {self.projects_evaluated or self.in_scope_projects} projects evaluated · {self.fail_count} fails · {self.skip_count} skips")
        top_fail = self.top_fail_rules(1)
        if top_fail:
            lines.append(f"Top fail: {top_fail[0][0]} ({top_fail[0][1]}).")
        top_skip = self.top_skip_reasons(1)
        if top_skip:
            lines.append(f"Top skip: {top_skip[0][0]} ({top_skip[0][1]}).")
        return "\n".join(lines)


class PMAuditService:
    def __init__(self, settings: Settings, client: ServiceTitanClient, slack: SlackClient) -> None:
        self.settings = settings
        self.client = client
        self.slack = slack

    def run_once(self, now: datetime | None = None, *, require_enabled: bool = True) -> PMAuditSummary:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        summary = PMAuditSummary(
            enabled=self.settings.pm_audit_enabled,
            dry_run=self.settings.pm_audit_dry_run,
            include_client_name=self.settings.pm_audit_include_client_name,
        )
        if require_enabled and not self.settings.pm_audit_enabled:
            summary.status = "disabled"
            logger.info("pm_audit_disabled")
            return summary

        missing = self._missing_config()
        if missing:
            summary.status = "config_error"
            summary.config_errors = missing
            return summary

        try:
            projects = self.client.query_pm_projects(
                project_type_ids=PM_INSTALL_PROJECT_TYPE_IDS,
                business_unit_ids=set(self.settings.pm_audit_install_business_unit_ids),
                business_unit_names=set(self.settings.pm_audit_install_business_unit_names),
                exclude_keywords=PM_OUT_OF_SCOPE_KEYWORDS,
                max_projects=self.settings.pm_audit_max_projects,
                max_tasks=self.settings.pm_audit_max_tasks,
            )
        except ServiceTitanApiError as exc:
            summary.status = "api_error"
            summary.errors = 1
            logger.warning("pm_audit_api_error", extra={"status": exc.status, "error_message": exc.message})
            return summary
        except Exception as exc:
            summary.status = "api_error"
            summary.errors = 1
            logger.warning("pm_audit_failed", extra={"error_message": str(exc)})
            return summary

        stats = getattr(self.client, "last_pm_audit_stats", {})
        summary.projects_scanned = int(stats.get("raw_projects_fetched", len(projects)))
        summary.skipped_out_of_scope = int(stats.get("skipped_out_of_scope", 0))
        summary.projects_evaluated = int(stats.get("projects_evaluated", len(projects)))
        summary.tasks_loaded = int(stats.get("tasks_loaded", 0))
        logger.info(
            "pm_audit_scope_filter",
            extra={
                "raw_projects": summary.projects_scanned,
                "in_scope": int(stats.get("in_scope_projects", len(projects))),
                "skipped_out_of_scope": summary.skipped_out_of_scope,
                "projects_evaluated": summary.projects_evaluated,
                "tasks_loaded": summary.tasks_loaded,
                "invoices_loaded": int(stats.get("invoices_loaded", 0)),
            },
        )
        for project in projects:
            if _is_explicitly_out_of_scope(project):
                summary.skipped_out_of_scope += 1
                summary.project_audits.append(PMProjectAudit(project=project, skipped_out_of_scope=True))
                continue
            audit = PMProjectAudit(project=project)
            audit.results = _run_pm_rules(project, self.settings, now)
            summary.project_audits.append(audit)
            summary.in_scope_projects += 1
            summary.rules_evaluated += len(audit.results)
            for result in audit.results:
                if result.status == PM_PASS:
                    summary.pass_count += 1
                elif result.status == PM_FAIL:
                    summary.fail_count += 1
                elif result.status == PM_SKIP:
                    summary.skip_count += 1
                summary.open_tasks_without_due_skipped += result.skipped_open_tasks_without_due

        if summary.fail_count and self.settings.pm_audit_dry_run:
            summary.alerts_would_send = 1
            logger.info("pm_audit_dry_run", extra={"failures": summary.fail_count})
        elif summary.fail_count:
            ts = self.slack.post_message(self._alert_channel(), summary.alert_text(now, self.settings.timezone))
            if ts:
                summary.alerts_sent = 1
                logger.info("pm_audit_slack_sent", extra={"failures": summary.fail_count})
            else:
                summary.status = "slack_error"
                summary.errors = 1
        return summary

    def _missing_config(self) -> list[str]:
        required = {
            "SERVICETITAN_CLIENT_ID": self.settings.servicetitan_client_id,
            "SERVICETITAN_CLIENT_SECRET": self.settings.servicetitan_client_secret,
            "SERVICETITAN_TENANT_ID": self.settings.servicetitan_tenant_id,
            "SERVICETITAN_APP_KEY": self.settings.servicetitan_app_key,
        }
        if not self.settings.pm_audit_dry_run:
            required["SLACK_BOT_TOKEN"] = self.settings.slack_bot_token
            required["PM_AUDIT_SLACK_CHANNEL_ID"] = self._alert_channel()
        return [key for key, value in required.items() if not value]

    def _alert_channel(self) -> str:
        return self.settings.pm_audit_slack_channel_id


def _run_pm_rules(project: ServiceTitanProject, settings: Settings, now: datetime) -> list[PMRuleResult]:
    enabled_rule_ids = {rule_id.strip().upper() for rule_id in settings.pm_audit_enabled_rule_ids if rule_id.strip()}
    rules = (
        ("R1", lambda: _rule_project_type(project)),
        ("R3", lambda: _rule_pm_assigned(project, settings, now)),
        ("R4", lambda: _rule_status_current(project, settings, now)),
        ("R6", lambda: _rule_sold_by(project, settings)),
        ("R7", lambda: _rule_permit_present(project, settings)),
        ("R8", lambda: _rule_hoa_approval_status(project, settings)),
        ("R9", lambda: _rule_asbestos_check_recorded(project, settings)),
        ("R10", lambda: _rule_review_requested(project, settings)),
        ("R11", lambda: _rule_tasks_applied(project, settings, now)),
        ("R13", lambda: _rule_tasks_assigned(project)),
        ("R15", lambda: _rule_no_stale_tasks(project, settings, now)),
        ("R16", lambda: _rule_on_hold_reason(project, settings, now)),
        ("R17", lambda: _rule_completed_closed_out(project)),
        ("R18", lambda: _rule_payment_order(project)),
        ("R19", lambda: _rule_homeowner_authorization_timing(project, settings)),
        ("R20", lambda: _rule_completion_report_green(project, settings)),
        ("R21", lambda: _rule_equipment_registered(project, settings)),
        ("R22", lambda: _rule_deposit_before_install(project, settings, now)),
        ("R23", lambda: _rule_permit_before_install(project, settings, now)),
        ("R24", lambda: _rule_equipment_confirmed_before_scheduling(project, settings)),
        ("R25", lambda: _rule_rebate_confirmed_before_scheduling(project, settings)),
        ("R26", lambda: _rule_crew_assigned_before_scheduling(project, settings)),
        ("R27", lambda: _rule_project_left_open_too_long(project, settings, now)),
        ("R28", lambda: _rule_change_order_written_approval(project, settings)),
    )
    return [run() for rule_id, run in rules if not enabled_rule_ids or rule_id in enabled_rule_ids]


def _rule_project_type(project: ServiceTitanProject) -> PMRuleResult:
    if not _project_type_field_available(project):
        return _result("R1", "Project type set and valid", PM_SKIP, "Project type field unavailable.", "Project Type")
    if not project.project_type_id and not project.project_type_name:
        return _result("R1", "Project type set and valid", PM_FAIL, "Project type is missing.", "Project Type")
    if _is_install_project_type(project):
        return _result("R1", "Project type set and valid", PM_PASS, "Project type is valid.", "Project Type")
    return _result("R1", "Project type set and valid", PM_FAIL, "Project type is not an approved PM install type.", "Project Type")


def _rule_pm_assigned(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if project.project_manager_ids:
        return _result("R3", "PM assigned", PM_PASS, "PM is assigned.", "Project Manager")
    return _result("R3", "PM assigned", PM_FAIL, "No PM assigned.", "Project Manager")


def _rule_status_current(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if not project.status.strip():
        return _result("R4", "Status set and current", PM_FAIL, "Project status is missing.", "Project status")

    status_updated_at = _status_last_updated_at(project)
    if status_updated_at is None:
        return _result("R4", "Status set and current", PM_PASS, "Project status is present; status timestamp unavailable.", "Project status")
    if project.tasks_available:
        has_open_tasks = any(task.is_open for task in project.tasks)
        stale_after = status_updated_at.astimezone(timezone.utc) + timedelta(days=settings.pm_audit_status_stale_days)
        if has_open_tasks and now > stale_after:
            return _result(
                "R4",
                "Status set and current",
                PM_FAIL,
                "Project status has not been updated within the configured threshold.",
                "Project status",
                due_at=stale_after,
            )
    return _result("R4", "Status set and current", PM_PASS, "Project status is present and current.", "Project status")


def _rule_sold_by(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if not project.custom_fields_available:
        return _result("R6", "Comfort Advisor / Sold By set", PM_SKIP, "Project custom fields unavailable.", "Sold by")
    field_name, sold_by = _custom_field_match(project, settings.pm_audit_sold_by_field_names)
    if field_name is None:
        return _result("R6", "Comfort Advisor / Sold By set", PM_SKIP, "Configured Sold By field unavailable.", "Sold by")
    if not sold_by:
        return _result("R6", "Comfort Advisor / Sold By set", PM_FAIL, "Configured Sold By field is empty.", field_name)
    return _result("R6", "Comfort Advisor / Sold By set", PM_PASS, "Configured Sold By field is present.", field_name)


def _rule_permit_present(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if not project.custom_fields_available:
        return _result("R7", "Permit field present", PM_SKIP, "Project custom fields unavailable.", "Permit")
    field_name, permit = _custom_field_match(project, settings.pm_audit_permit_field_names)
    if field_name is None:
        return _result("R7", "Permit field present", PM_SKIP, "Configured permit field unavailable.", "Permit")
    if not permit:
        return _result("R7", "Permit field present", PM_FAIL, "Configured permit field is empty.", field_name)
    return _result("R7", "Permit field present", PM_PASS, "Configured permit field is present.", field_name)


def _rule_hoa_approval_status(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if not project.custom_fields_available:
        return _result("R8", "HOA approval status set", PM_SKIP, "Project custom fields unavailable.", "HOA")
    matches = _custom_field_matches(project, settings.pm_audit_hoa_field_names)
    if not matches:
        return _result("R8", "HOA approval status set", PM_SKIP, "Configured HOA fields unavailable.", "HOA")
    if any(_negative_signal(value) for name, value in matches if _field_name_suggests_requirement(name)):
        return _result("R8", "HOA approval status set", PM_PASS, "HOA is not required.", "HOA")
    required = any(_positive_signal(value) for name, value in matches if _field_name_suggests_requirement(name))
    approval_matches = [(name, value) for name, value in matches if _field_name_suggests_status(name)]
    if not required and not approval_matches:
        return _result("R8", "HOA approval status set", PM_SKIP, "HOA requirement cannot be determined.", "HOA")
    if any(_nonblank(value) and not _negative_signal(value) for _, value in approval_matches):
        return _result("R8", "HOA approval status set", PM_PASS, "HOA approval/status is present.", approval_matches[0][0])
    if required and approval_matches:
        return _result("R8", "HOA approval status set", PM_FAIL, "HOA is required but approval/status is empty.", approval_matches[0][0])
    return _result("R8", "HOA approval status set", PM_SKIP, "HOA approval/status field unavailable.", "HOA")


def _rule_asbestos_check_recorded(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if settings.pm_audit_asbestos_year_cutoff is None:
        return _result("R9", "Asbestos check recorded", PM_SKIP, "Asbestos year cutoff is not configured.", "Asbestos")
    required = _asbestos_check_required(project, settings.pm_audit_asbestos_year_cutoff)
    if required is None:
        return _result("R9", "Asbestos check recorded", PM_SKIP, "Replacement or system age data unavailable.", "Asbestos")
    if not required:
        return _result("R9", "Asbestos check recorded", PM_PASS, "Asbestos check is not required.", "Asbestos")
    if not project.custom_fields_available:
        return _result("R9", "Asbestos check recorded", PM_SKIP, "Project custom fields unavailable.", "Asbestos")
    field_name, value = _custom_field_match(project, settings.pm_audit_asbestos_field_names)
    if field_name is None:
        return _result("R9", "Asbestos check recorded", PM_SKIP, "Configured asbestos field unavailable.", "Asbestos")
    if not value:
        return _result("R9", "Asbestos check recorded", PM_FAIL, "Asbestos check is required but not recorded.", field_name)
    return _result("R9", "Asbestos check recorded", PM_PASS, "Asbestos check is recorded.", field_name)


def _rule_review_requested(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if not _project_status_field_available(project):
        return _result("R10", "Review-requested flag set", PM_SKIP, "Project status unavailable.", "Review Requested")
    if not project.is_completed:
        return _result("R10", "Review-requested flag set", PM_PASS, "Project is not completed yet.", "Review Requested")
    if not project.custom_fields_available:
        return _result("R10", "Review-requested flag set", PM_SKIP, "Project custom fields unavailable.", "Review Requested")
    field_name, value = _custom_field_match(project, settings.pm_audit_review_requested_field_names)
    if field_name is None:
        return _result("R10", "Review-requested flag set", PM_SKIP, "Configured review-requested field unavailable.", "Review Requested")
    if _positive_signal(value):
        return _result("R10", "Review-requested flag set", PM_PASS, "Review-requested flag is set.", field_name)
    return _result("R10", "Review-requested flag set", PM_FAIL, "Completed project is missing review-requested flag.", field_name)


def _rule_tasks_applied(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if not project.tasks_available:
        return _result("R11", "Tasks applied / task count present", PM_SKIP, "Project task list unavailable.", "Tasks")
    if project.tasks:
        return _result("R11", "Tasks applied / task count present", PM_PASS, "Project tasks are present.", "Tasks")
    if not project.created_on:
        return _result("R11", "Tasks applied / task count present", PM_SKIP, "No PM assignment timestamp or project created timestamp available.", "Tasks")
    deadline = project.created_on.astimezone(timezone.utc) + timedelta(hours=settings.pm_audit_task_template_grace_hours)
    if now <= deadline:
        return _result("R11", "Tasks applied / task count present", PM_SKIP, "Project is still inside task-template grace period.", "Tasks")
    return _result("R11", "Tasks applied / task count present", PM_FAIL, "No project tasks exist after the grace period.", "Tasks", due_at=deadline)


def _rule_tasks_assigned(project: ServiceTitanProject) -> PMRuleResult:
    if not project.tasks_available:
        return _result("R13", "Every task has an assignee", PM_SKIP, "Project task list unavailable.", "Task assignee")
    missing = [task for task in project.tasks if not task.assigned_to_id]
    if missing:
        task = missing[0]
        return _result(
            "R13",
            "Every task has an assignee",
            PM_FAIL,
            f"{len(missing)} task(s) have no assignee.",
            "Task assignee",
            due_at=task.due_at,
            task_number=task.display_name,
        )
    return _result("R13", "Every task has an assignee", PM_PASS, "All project tasks have assignees.", "Task assignee")


def _rule_no_stale_tasks(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if not project.tasks_available:
        return _result("R15", "No stale open tasks", PM_SKIP, "Project task list unavailable.", "Task due date")
    missing_due_count = 0
    missing_status_count = 0
    for task in project.tasks:
        is_open = task.is_open
        if is_open is None:
            missing_status_count += 1
            continue
        if not is_open:
            continue
        if not task.due_at:
            missing_due_count += 1
            continue
        deadline = task.due_at.astimezone(timezone.utc) + timedelta(days=settings.pm_audit_task_overdue_days)
        if now > deadline:
            return _result(
                "R15",
                "No stale open tasks",
                PM_FAIL,
                "Open task is past the configured overdue threshold.",
                "Task due date",
                due_at=task.due_at,
                task_number=task.display_name,
                skipped_open_tasks_without_due=missing_due_count,
            )
    if missing_status_count:
        return _result("R15", "No stale open tasks", PM_SKIP, "Task status unavailable.", "Task status", skipped_open_tasks_without_due=missing_due_count)
    if missing_due_count:
        return _result(
            "R15",
            "No stale open tasks",
            PM_PASS,
            "No stale open tasks with due dates found.",
            "Task due date",
            skipped_open_tasks_without_due=missing_due_count,
        )
    return _result("R15", "No stale open tasks", PM_PASS, "No stale open tasks with due dates found.", "Task due date")


def _rule_on_hold_reason(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if "hold" not in _normalize(project.status):
        return _result("R16", "On-hold has a reason", PM_PASS, "Project is not on hold.", "On Hold Reason")
    on_hold_since = _raw_datetime(
        project,
        (
            "onHoldSince",
            "holdSince",
            "statusLastUpdatedOn",
            "statusLastUpdatedAt",
            "projectStatus.lastUpdatedOn",
            "projectStatus.updatedOn",
        ),
    )
    if on_hold_since is None:
        return _result("R16", "On-hold has a reason", PM_SKIP, "On-hold since date unavailable.", "On Hold Reason")
    deadline = on_hold_since.astimezone(timezone.utc) + timedelta(days=settings.pm_audit_on_hold_max_days)
    if now <= deadline:
        return _result("R16", "On-hold has a reason", PM_PASS, "Project is still inside on-hold review window.", "On Hold Reason", due_at=deadline)
    if not project.custom_fields_available:
        return _result("R16", "On-hold has a reason", PM_SKIP, "Project custom fields unavailable.", "On Hold Reason")
    field_name, value = _custom_field_match(project, settings.pm_audit_on_hold_reason_field_names)
    if field_name is None:
        return _result("R16", "On-hold has a reason", PM_SKIP, "Structured on-hold reason field unavailable.", "On Hold Reason")
    if value:
        return _result("R16", "On-hold has a reason", PM_PASS, "On-hold reason is present.", field_name)
    return _result("R16", "On-hold has a reason", PM_FAIL, "Project is on hold beyond threshold without structured reason.", field_name, due_at=deadline)


def _rule_completed_closed_out(project: ServiceTitanProject) -> PMRuleResult:
    if not _project_status_field_available(project):
        return _result("R17", "Completed projects are closed out", PM_SKIP, "Project status unavailable.", "Project status")
    if not project.tasks_available:
        return _result("R17", "Completed projects are closed out", PM_SKIP, "Project task list unavailable.", "Closeout")
    if not project.is_completed:
        return _result("R17", "Completed projects are closed out", PM_PASS, "Project is not completed yet.", "Closeout")
    open_tasks = [task for task in project.tasks if task.is_open]
    if open_tasks:
        task = open_tasks[0]
        return _result(
            "R17",
            "Completed projects are closed out",
            PM_FAIL,
            f"Completed project still has {len(open_tasks)} open task(s).",
            "Closeout",
            due_at=task.due_at,
            task_number=task.display_name,
        )
    return _result("R17", "Completed projects are closed out", PM_PASS, "Completed project has no open tasks.", "Closeout")


def _rule_payment_order(project: ServiceTitanProject) -> PMRuleResult:
    milestones = _payment_milestones(project)
    if milestones is None:
        return _result("R18", "Payment order", PM_SKIP, "Structured payment milestones unavailable.", "Payment milestones")
    ordered_names = ("deposit", "first", "final")
    milestone_dates: dict[str, datetime] = {}
    for order_name in ordered_names:
        matched = [(name, paid_at) for name, paid_at in milestones if order_name in _normalize(name)]
        if not matched:
            return _result("R18", "Payment order", PM_SKIP, "Required payment milestone unavailable.", "Payment milestones")
        milestone_dates[order_name] = matched[0][1]
    for earlier, later in (("deposit", "first"), ("first", "final")):
        earlier_date = milestone_dates[earlier].astimezone(timezone.utc)
        later_date = milestone_dates[later].astimezone(timezone.utc)
        if later_date.date() == earlier_date.date():
            continue
        if later_date < earlier_date:
            return _result("R18", "Payment order", PM_FAIL, "Later payment milestone was recorded before an earlier milestone.", "Payment milestones")
    return _result("R18", "Payment order", PM_PASS, "Payment milestones are in the expected order.", "Payment milestones")


def _rule_homeowner_authorization_timing(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    arrival_at = _raw_datetime(project, ("crewArrivedAt", "arrivalAt", "arrivedAt", "installCrew.arrivedAt"))
    if arrival_at is None:
        return _result("R19", "Homeowner Authorization timing", PM_SKIP, "Crew arrival timestamp unavailable.", "Homeowner Authorization")
    completed_at = _form_completed_at(project, settings.pm_audit_homeowner_auth_form_names, ("homeownerAuthorizationCompletedAt",))
    if completed_at is None:
        return _result("R19", "Homeowner Authorization timing", PM_SKIP, "Homeowner Authorization form timestamp unavailable.", "Homeowner Authorization")
    deadline = arrival_at.astimezone(timezone.utc) + timedelta(hours=settings.pm_audit_homeowner_auth_within_hours)
    if completed_at.astimezone(timezone.utc) > deadline:
        return _result("R19", "Homeowner Authorization timing", PM_FAIL, "Homeowner Authorization was completed after the configured threshold.", "Homeowner Authorization", due_at=deadline)
    return _result("R19", "Homeowner Authorization timing", PM_PASS, "Homeowner Authorization was completed within the threshold.", "Homeowner Authorization")


def _rule_completion_report_green(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if not project.is_completed:
        return _result("R20", "Installation Completion Report green", PM_PASS, "Install is not completed yet.", "Completion Report")
    status = _form_status(project, settings.pm_audit_completion_report_form_names, ("completionReportStatus",))
    if status is None:
        return _result("R20", "Installation Completion Report green", PM_SKIP, "Installation Completion Report status unavailable.", "Completion Report")
    if _status_is_good(status, ("green", "complete", "completed", "approved", "pass", "passed")):
        return _result("R20", "Installation Completion Report green", PM_PASS, "Installation Completion Report is completed/green.", "Completion Report")
    return _result("R20", "Installation Completion Report green", PM_FAIL, "Installation Completion Report is not completed/green.", "Completion Report")


def _rule_equipment_registered(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if not project.is_completed:
        return _result("R21", "Equipment registered", PM_PASS, "Project is not completed yet.", "Equipment")
    if not project.custom_fields_available:
        return _result("R21", "Equipment registered", PM_SKIP, "Project custom fields unavailable.", "Equipment")
    field_name, value = _custom_field_match(project, settings.pm_audit_equipment_field_names)
    if field_name is None:
        return _result("R21", "Equipment registered", PM_SKIP, "Configured equipment registration field unavailable.", "Equipment")
    if _status_is_good(value, ("registered", "complete", "completed", "yes", "done")):
        return _result("R21", "Equipment registered", PM_PASS, "Equipment registration is complete.", field_name)
    return _result("R21", "Equipment registered", PM_FAIL, "Completed project is missing equipment registration.", field_name)


def _rule_deposit_before_install(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if project.start_date is None:
        return _result("R22", "Deposit before install", PM_SKIP, "Install date unavailable.", "Deposit")
    if not project.invoices_available:
        return _result("R22", "Deposit before install", PM_SKIP, "Linked project invoice data unavailable.", "Deposit", install_date=project.start_date)
    assessment = _deposit_assessment(project, settings)
    details = _deposit_details(assessment)
    if assessment.status == "confirmed":
        return _result(
            "R22",
            "Deposit before install",
            PM_PASS,
            "Structured deposit payment meets the required amount.",
            "Deposit",
            install_date=project.start_date,
            details=details,
        )
    if assessment.status == "missing":
        return _result(
            "R22",
            "Deposit before install",
            PM_FAIL,
            "Deposit missing before install.",
            "Deposit",
            install_date=project.start_date,
            details=details,
        )
    if assessment.status == "partial":
        return _result(
            "R22",
            "Deposit before install",
            PM_FAIL,
            "Deposit below required amount.",
            "Deposit",
            install_date=project.start_date,
            details=details,
        )
    return _result(
        "R22",
        "Deposit before install",
        PM_SKIP,
        assessment.issue,
        "Deposit",
        install_date=project.start_date,
        details=details,
    )


def _rule_permit_before_install(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if project.start_date is None:
        return _result("R23", "Permit before install", PM_SKIP, "Install date unavailable.", "Permit", install_date=project.start_date)
    deadline = project.start_date.astimezone(timezone.utc) - timedelta(days=settings.pm_audit_permit_before_install_days)
    if now < deadline:
        return _result("R23", "Permit before install", PM_PASS, "Install is not inside permit confirmation window.", "Permit", install_date=project.start_date)
    if not project.custom_fields_available:
        return _result("R23", "Permit before install", PM_SKIP, "Project custom fields unavailable.", "Permit", install_date=project.start_date)
    if _permit_owner_is_customer(project):
        return _result("R23", "Permit before install", PM_PASS, "Structured permit owner indicates customer-pulled permit.", "Permit", install_date=project.start_date)
    field_name, permit = _custom_field_match(project, settings.pm_audit_permit_field_names)
    if field_name is None:
        return _result("R23", "Permit before install", PM_SKIP, "Configured permit field unavailable.", "Permit", install_date=project.start_date)
    if permit:
        return _result("R23", "Permit before install", PM_PASS, "Permit information is present before install.", field_name, install_date=project.start_date)
    return _result("R23", "Permit before install", PM_FAIL, "Permit information is missing inside the configured pre-install window.", field_name, install_date=project.start_date)


def _rule_equipment_confirmed_before_scheduling(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if project.start_date is None:
        return _result("R24", "Equipment confirmed before scheduling", PM_PASS, "Install is not scheduled.", "Equipment")
    if not project.custom_fields_available:
        return _result("R24", "Equipment confirmed before scheduling", PM_SKIP, "Project custom fields unavailable.", "Equipment")
    field_name, value = _custom_field_match(project, settings.pm_audit_equipment_field_names)
    if field_name is None:
        return _result("R24", "Equipment confirmed before scheduling", PM_SKIP, "Configured equipment field unavailable.", "Equipment")
    if _status_is_good(value, ("confirmed", "ready", "staged", "available", "registered", "yes")):
        return _result("R24", "Equipment confirmed before scheduling", PM_PASS, "Equipment is confirmed before scheduled install.", field_name, install_date=project.start_date)
    return _result("R24", "Equipment confirmed before scheduling", PM_FAIL, "Scheduled install is missing confirmed equipment status.", field_name, install_date=project.start_date)


def _rule_rebate_confirmed_before_scheduling(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if project.start_date is None:
        return _result("R25", "Rebate confirmed before scheduling", PM_PASS, "Install is not scheduled.", "Rebate")
    if not project.custom_fields_available:
        return _result("R25", "Rebate confirmed before scheduling", PM_SKIP, "Project custom fields unavailable.", "Rebate")
    field_name, value = _custom_field_match(project, settings.pm_audit_rebate_field_names)
    if field_name is None:
        return _result("R25", "Rebate confirmed before scheduling", PM_SKIP, "Configured rebate field unavailable.", "Rebate")
    if not value:
        return _result("R25", "Rebate confirmed before scheduling", PM_SKIP, "Rebate applicability or status unavailable.", field_name)
    if _negative_signal(value):
        return _result("R25", "Rebate confirmed before scheduling", PM_PASS, "Rebate is not applicable.", field_name)
    if _status_is_good(value, ("approved", "confirmed", "complete", "completed", "submitted")):
        return _result("R25", "Rebate confirmed before scheduling", PM_PASS, "Rebate status is confirmed.", field_name)
    return _result("R25", "Rebate confirmed before scheduling", PM_FAIL, "Applicable rebate is not confirmed before scheduled install.", field_name, install_date=project.start_date)


def _rule_crew_assigned_before_scheduling(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if project.start_date is None:
        return _result("R26", "Crew assigned before scheduling", PM_PASS, "Install is not scheduled.", "Crew")
    if not project.custom_fields_available:
        return _result("R26", "Crew assigned before scheduling", PM_SKIP, "Project custom fields unavailable.", "Crew")
    field_name, value = _custom_field_match(project, settings.pm_audit_crew_field_names)
    if field_name is None:
        return _result("R26", "Crew assigned before scheduling", PM_SKIP, "Configured crew assignment field unavailable.", "Crew")
    if value:
        return _result("R26", "Crew assigned before scheduling", PM_PASS, "Crew assignment is present before scheduled install.", field_name, install_date=project.start_date)
    return _result("R26", "Crew assigned before scheduling", PM_FAIL, "Scheduled install is missing crew assignment.", field_name, install_date=project.start_date)


def _rule_project_left_open_too_long(project: ServiceTitanProject, settings: Settings, now: datetime) -> PMRuleResult:
    if project.is_completed:
        return _result("R27", "Project left open too long", PM_PASS, "Project is completed/closed.", "Project status")
    completion_signal = project.actual_completion_date or _raw_datetime(project, ("finalPaymentCompletedAt", "installCompletedAt", "completionDate"))
    if completion_signal is None:
        return _result("R27", "Project left open too long", PM_SKIP, "Final payment or completion signal unavailable.", "Project status")
    deadline = completion_signal.astimezone(timezone.utc) + timedelta(days=settings.pm_audit_project_left_open_days)
    if now > deadline:
        return _result("R27", "Project left open too long", PM_FAIL, "Project appears complete but remains open beyond threshold.", "Project status", due_at=deadline)
    return _result("R27", "Project left open too long", PM_PASS, "Project is still inside closeout grace period.", "Project status", due_at=deadline)


def _rule_change_order_written_approval(project: ServiceTitanProject, settings: Settings) -> PMRuleResult:
    if not project.custom_fields_available:
        return _result("R28", "Change order missing written approval", PM_SKIP, "Project custom fields unavailable.", "Change order")
    matches = _custom_field_matches(project, settings.pm_audit_change_order_field_names)
    if not matches:
        return _result("R28", "Change order missing written approval", PM_SKIP, "Structured change-order approval field unavailable.", "Change order")
    approval_matches = [(name, value) for name, value in matches if any(token in _normalize(name) for token in ("approval", "written", "signed"))]
    if any(_status_is_good(value, ("approved", "signed", "written", "complete", "completed", "yes")) for _, value in approval_matches):
        return _result("R28", "Change order missing written approval", PM_PASS, "Change-order written approval is present.", approval_matches[0][0])
    signal_matches = [(name, value) for name, value in matches if (name, value) not in approval_matches]
    has_change_order_signal = any(_positive_signal(value) for _, value in signal_matches)
    if has_change_order_signal or any(_nonblank(value) and not _negative_signal(value) for _, value in approval_matches):
        field_name = (signal_matches or approval_matches)[0][0]
        return _result("R28", "Change order missing written approval", PM_FAIL, "Change-order signal exists without written approval.", field_name)
    return _result("R28", "Change order missing written approval", PM_PASS, "No structured change-order approval issue detected.", matches[0][0])


def _result(
    rule_id: str,
    name: str,
    status: str,
    issue: str,
    field: str,
    *,
    due_at: datetime | None = None,
    install_date: datetime | None = None,
    task_number: str = "",
    skipped_open_tasks_without_due: int = 0,
    details: tuple[str, ...] = (),
) -> PMRuleResult:
    return PMRuleResult(
        rule_id=rule_id,
        name=name,
        status=status,
        issue=issue,
        field=field,
        action="Review the PM project in ServiceTitan and correct the missing or stale operational field.",
        details=details,
        due_at=due_at,
        install_date=install_date,
        task_number=task_number,
        skipped_open_tasks_without_due=skipped_open_tasks_without_due,
    )


def _is_explicitly_out_of_scope(project: ServiceTitanProject) -> bool:
    text = " ".join(
        value
        for value in (project.project_type_name, project.status, *project.business_unit_ids, *project.business_unit_names)
        if value
    ).lower()
    return any(keyword in text for keyword in PM_OUT_OF_SCOPE_KEYWORDS)


def _is_install_project_type(project: ServiceTitanProject) -> bool:
    if project.project_type_id in PM_INSTALL_PROJECT_TYPE_IDS:
        return True
    normalized = _normalize(project.project_type_name)
    return normalized in PM_INSTALL_PROJECT_TYPE_NAMES


def _project_type_field_available(project: ServiceTitanProject) -> bool:
    return any(key in project.raw for key in ("projectTypeId", "projectType", "type"))


def _project_status_field_available(project: ServiceTitanProject) -> bool:
    return any(key in project.raw for key in ("status", "statusId", "projectStatus"))


def _status_last_updated_at(project: ServiceTitanProject) -> datetime | None:
    raw_value = _nested_raw_value(
        project.raw,
        (
            "statusLastUpdatedOn",
            "statusLastUpdatedAt",
            "statusUpdatedOn",
            "statusUpdatedAt",
            "statusModifiedOn",
            "statusModifiedAt",
            "status.lastUpdatedOn",
            "status.lastUpdatedAt",
            "status.updatedOn",
            "status.updatedAt",
            "status.modifiedOn",
            "status.modifiedAt",
            "projectStatus.lastUpdatedOn",
            "projectStatus.lastUpdatedAt",
            "projectStatus.updatedOn",
            "projectStatus.updatedAt",
            "projectStatus.modifiedOn",
            "projectStatus.modifiedAt",
        ),
    )
    return _parse_datetime(raw_value)


def _nested_raw_value(source: dict[str, object], names: tuple[str, ...]) -> object | None:
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


def _raw_datetime(project: ServiceTitanProject, names: tuple[str, ...]) -> datetime | None:
    return _parse_datetime(_nested_raw_value(project.raw, names))


def _custom_field_match(project: ServiceTitanProject, field_names: list[str]) -> tuple[str | None, str]:
    normalized_fields = {_normalize(name): (name, value.strip()) for name, value in project.custom_fields.items()}
    for field_name in field_names:
        match = normalized_fields.get(_normalize(field_name))
        if match is not None:
            return match
    return None, ""


def _custom_field_matches(project: ServiceTitanProject, field_names: list[str]) -> list[tuple[str, str]]:
    wanted = {_normalize(name) for name in field_names}
    matches: list[tuple[str, str]] = []
    for name, value in project.custom_fields.items():
        if _normalize(name) in wanted:
            matches.append((name, value.strip()))
    return matches


def _field_name_suggests_requirement(name: str) -> bool:
    normalized = _normalize(name)
    return any(value in normalized for value in ("under", "required", "hoa"))


def _field_name_suggests_status(name: str) -> bool:
    normalized = _normalize(name)
    return any(value in normalized for value in ("approval", "status", "permit", "review", "requested"))


def _nonblank(value: str) -> bool:
    return bool(value.strip())


def _positive_signal(value: str) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    return normalized in {"yes", "true", "required", "approved", "complete", "completed", "sent", "done"} or any(
        word in normalized for word in ("approved", "confirmed", "complete", "required", "submitted")
    )


def _negative_signal(value: str) -> bool:
    normalized = _normalize(value)
    return normalized in {"no", "false", "none", "n/a", "na", "not applicable", "not required", "no hoa"} or "not applicable" in normalized


def _status_is_good(value: str, good_words: tuple[str, ...]) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    return any(word in normalized for word in good_words)


def _asbestos_check_required(project: ServiceTitanProject, cutoff_year: int) -> bool | None:
    replacement = _replacement_work_signal(project)
    year = _structure_year(project)
    if replacement is None or year is None:
        return None
    return replacement and year <= cutoff_year


def _replacement_work_signal(project: ServiceTitanProject) -> bool | None:
    values: list[str] = [
        str(_nested_raw_value(project.raw, ("installType", "workType", "replacementType")) or ""),
        project.project_type_name,
    ]
    for name, value in project.custom_fields.items():
        normalized_name = _normalize(name)
        if any(token in normalized_name for token in ("replacement", "install type", "work type", "changeout")):
            values.append(value)
    text = _normalize(" ".join(values))
    if any(token in text for token in ("replacement", "changeout", "replace")):
        return True
    if any(token in text for token in ("new construction", "new build")):
        return False
    return None


def _structure_year(project: ServiceTitanProject) -> int | None:
    raw_value = _nested_raw_value(
        project.raw,
        (
            "yearBuilt",
            "builtYear",
            "homeYear",
            "constructionYear",
            "systemYear",
            "equipmentYear",
        ),
    )
    year = _parse_year(raw_value)
    if year is not None:
        return year
    for name, value in project.custom_fields.items():
        normalized_name = _normalize(name)
        if any(token in normalized_name for token in ("year built", "built year", "home year", "system year", "equipment year")):
            year = _parse_year(value)
            if year is not None:
                return year
    return None


def _parse_year(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) >= 4:
        try:
            return int(digits[:4])
        except ValueError:
            return None
    return None


def _payment_milestones(project: ServiceTitanProject) -> list[tuple[str, datetime]] | None:
    raw = _nested_raw_value(project.raw, ("paymentMilestones", "payment_milestones"))
    if not isinstance(raw, list):
        return None
    milestones: list[tuple[str, datetime]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(_nested_raw_value(item, ("name", "type", "milestone")) or "")
        paid_at = _parse_datetime(_nested_raw_value(item, ("paidAt", "date", "completedAt", "createdOn")))
        if name and paid_at:
            milestones.append((name, paid_at))
    return milestones if milestones else None


def _form_completed_at(project: ServiceTitanProject, names: list[str], raw_names: tuple[str, ...]) -> datetime | None:
    raw_dt = _raw_datetime(project, raw_names)
    if raw_dt is not None:
        return raw_dt
    for form in _project_forms(project):
        if not _name_matches(str(_nested_raw_value(form, ("name", "formName", "title")) or ""), names):
            continue
        completed = _parse_datetime(_nested_raw_value(form, ("completedAt", "submittedAt", "createdOn")))
        if completed is not None:
            return completed
    return None


def _form_status(project: ServiceTitanProject, names: list[str], raw_names: tuple[str, ...]) -> str | None:
    raw_status = _nested_raw_value(project.raw, raw_names)
    if raw_status:
        return str(raw_status)
    for form in _project_forms(project):
        if not _name_matches(str(_nested_raw_value(form, ("name", "formName", "title")) or ""), names):
            continue
        status = _nested_raw_value(form, ("status", "result", "state"))
        if status:
            return str(status)
    return None


def _project_forms(project: ServiceTitanProject) -> list[dict[str, object]]:
    raw = _nested_raw_value(project.raw, ("forms", "formSubmissions", "projectForms"))
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _name_matches(value: str, names: list[str]) -> bool:
    normalized = _normalize(value)
    return any(_normalize(name) == normalized for name in names)


def _deposit_assessment(project: ServiceTitanProject, settings: Settings) -> _DepositAssessment:
    job_total = _deposit_job_total(project)
    if job_total is None or job_total <= 0:
        return _DepositAssessment("unclear", "Project/job total unavailable.")
    expected = min(settings.pm_audit_deposit_fixed_amount, job_total * settings.pm_audit_deposit_percent)
    if expected <= 0:
        return _DepositAssessment("unclear", "Expected deposit cannot be calculated.", job_total=job_total)

    deposit_invoices = [invoice for invoice in project.invoices if _invoice_has_deposit_line(invoice, settings.pm_audit_deposit_line_item_names)]
    if not deposit_invoices:
        return _DepositAssessment(
            "unclear",
            "Deposit invoice/line item cannot be identified.",
            expected_amount=expected,
            job_total=job_total,
        )

    paid_amount = 0.0
    payment_data_available = False
    for invoice in deposit_invoices:
        invoice_paid_amount = _deposit_paid_amount(invoice, settings.pm_audit_deposit_payment_status_values, project.start_date)
        if invoice_paid_amount is None:
            continue
        payment_data_available = True
        paid_amount += max(0.0, invoice_paid_amount)

    if not payment_data_available:
        return _DepositAssessment(
            "unclear",
            "Deposit payment data unavailable or payment status unclear.",
            expected_amount=expected,
            job_total=job_total,
        )

    if paid_amount + settings.pm_audit_deposit_rounding_tolerance >= expected:
        return _DepositAssessment(
            "confirmed",
            "Structured deposit payment meets the required amount.",
            expected_amount=expected,
            paid_amount=paid_amount,
            job_total=job_total,
        )
    if paid_amount > 0:
        return _DepositAssessment(
            "partial",
            "Deposit below required amount.",
            expected_amount=expected,
            paid_amount=paid_amount,
            job_total=job_total,
        )
    return _DepositAssessment(
        "missing",
        "Deposit missing before install.",
        expected_amount=expected,
        paid_amount=paid_amount,
        job_total=job_total,
    )


def _deposit_job_total(project: ServiceTitanProject) -> float | None:
    raw_total = _money_value(
        project.raw,
        (
            "jobTotal",
            "projectTotal",
            "contractTotal",
            "soldAmount",
            "total",
            "summary.total",
        ),
    )
    invoice_totals = [
        _money_value(invoice, ("total", "invoiceTotal", "summary.total", "subtotal", "amount"))
        for invoice in project.invoices
    ]
    totals = [value for value in [raw_total, *invoice_totals] if value is not None and value > 0]
    return max(totals) if totals else None


def _invoice_has_deposit_line(invoice: dict[str, object], line_item_names: list[str]) -> bool:
    aliases = [_normalize(value) for value in line_item_names if value.strip()]
    if not aliases:
        return False
    return any(alias in _normalize(text) for text in _invoice_line_texts(invoice) for alias in aliases)


def _invoice_line_texts(invoice: dict[str, object]) -> list[str]:
    texts: list[str] = []
    nested = _nested_raw_value(invoice, ("lineItems", "items", "invoiceItems"))
    if isinstance(nested, list):
        for item in nested:
            if isinstance(item, dict):
                texts.extend(_invoice_line_texts(item))
            elif item:
                texts.append(str(item))
    direct_parts = [
        _nested_raw_value(invoice, ("name",)),
        _nested_raw_value(invoice, ("description",)),
        _nested_raw_value(invoice, ("itemName",)),
        _nested_raw_value(invoice, ("displayName",)),
        _nested_raw_value(invoice, ("code",)),
        _nested_raw_value(invoice, ("sku",)),
        _nested_raw_value(invoice, ("service.name",)),
    ]
    direct_text = " ".join(str(part) for part in direct_parts if part)
    if direct_text:
        texts.append(direct_text)
    return texts


def _deposit_paid_amount(invoice: dict[str, object], accepted_statuses: list[str], install_date: datetime | None) -> float | None:
    payments = _nested_raw_value(invoice, ("payments", "paymentRecords", "transactions"))
    if isinstance(payments, list):
        total = 0.0
        saw_payment = False
        saw_unclear_status = False
        for payment in payments:
            if not isinstance(payment, dict):
                continue
            amount = _money_value(payment, ("amount", "total", "paymentAmount", "appliedAmount"))
            if amount is None:
                continue
            status = str(_nested_raw_value(payment, ("status", "paymentStatus", "status.name", "state")) or "")
            paid_at = _parse_datetime(_nested_raw_value(payment, ("paidOn", "postedOn", "depositedOn", "paymentDate", "createdOn")))
            if paid_at and install_date and paid_at.astimezone(timezone.utc) > install_date.astimezone(timezone.utc):
                saw_payment = True
                continue
            if _payment_status_accepted(status, accepted_statuses) or paid_at:
                saw_payment = True
                total += amount
            elif status:
                saw_payment = True
            else:
                saw_unclear_status = True
        if saw_unclear_status and total <= 0:
            return None
        if saw_payment:
            return total

    explicit_paid = _money_value(invoice, ("paymentTotal", "paymentsTotal", "paidAmount", "amountPaid"))
    invoice_total = _money_value(invoice, ("total", "invoiceTotal", "summary.total", "subtotal", "amount"))
    invoice_balance = _money_value(invoice, ("balance", "invoiceBalance", "summary.balance", "remainingBalance", "amountDue"))
    paid_from_balance = None
    if invoice_total is not None and invoice_balance is not None:
        paid_from_balance = max(0.0, invoice_total - invoice_balance)

    invoice_status = str(_nested_raw_value(invoice, ("status", "invoiceStatus", "status.name", "state")) or "")
    payment_date = _parse_datetime(_nested_raw_value(invoice, ("paidOn", "depositedOn", "paymentDate", "postedOn")))
    if payment_date and install_date and payment_date.astimezone(timezone.utc) > install_date.astimezone(timezone.utc):
        return 0.0
    has_payment_date = bool(payment_date)
    if explicit_paid is not None:
        if _payment_status_accepted(invoice_status, accepted_statuses) or has_payment_date or paid_from_balance is not None:
            return max(0.0, explicit_paid)
        return None
    if paid_from_balance is not None:
        return paid_from_balance
    if invoice_total is not None and (_payment_status_accepted(invoice_status, accepted_statuses) or has_payment_date):
        return max(0.0, invoice_total)
    if invoice_status:
        return 0.0
    return None


def _payment_status_accepted(status: str, accepted_statuses: list[str]) -> bool:
    normalized = _normalize(status)
    if not normalized:
        return False
    accepted = {_normalize(value) for value in accepted_statuses if value.strip()}
    return normalized in accepted or any(value and value in normalized for value in accepted)


def _deposit_details(assessment: _DepositAssessment) -> tuple[str, ...]:
    details: list[str] = []
    if assessment.job_total is not None:
        details.append(f"Job total: {_format_money(assessment.job_total)}")
    if assessment.expected_amount is not None:
        details.append(f"Expected deposit: {_format_money(assessment.expected_amount)}")
    if assessment.paid_amount is not None:
        details.append(f"Paid deposit: {_format_money(assessment.paid_amount)}")
    return tuple(details)


def _money_value(source: dict[str, object], names: tuple[str, ...]) -> float | None:
    value = _nested_raw_value(source, names)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _permit_owner_is_customer(project: ServiceTitanProject) -> bool:
    for name, value in project.custom_fields.items():
        if "permit" in _normalize(name) and "owner" in _normalize(name) and "customer" in _normalize(value):
            return True
    return False


def _failure_block(project: ServiceTitanProject, result: PMRuleResult, timezone_name: str, *, include_client_name: bool = False) -> str:
    lines = [
        _pm_project_header(project, result, include_client_name=include_client_name),
        f"  Field: {result.task_number or result.field}",
    ]
    if include_client_name and result.rule_id != "R22" and project.client_name:
        lines.insert(1, f"  Client: {project.client_name}")
    lines.extend(f"  {detail}" for detail in result.details)
    lines.append(f"  Action: {_action_for_rule(result.rule_id)}")
    if result.due_at:
        lines.append(f"  Due: {_format_date(result.due_at, timezone_name)}")
    elif result.install_date or project.start_date:
        lines.append(f"  Install: {_format_date(result.install_date or project.start_date, timezone_name)}")
    if project.url:
        lines.append(f"  Link: {project.url}")
    return "\n".join(lines)


def _pm_project_header(project: ServiceTitanProject, result: PMRuleResult, *, include_client_name: bool) -> str:
    header = f"• Project #{project.project_number or project.project_id}"
    if result.rule_id == "R22" and project.client_name:
        header = f"{header} — {project.client_name}"
    return f"{header} — {_short_issue(result)}"


def _pm_summary(groups: dict[str, list[str]], clean_counts: dict[str, int]) -> str:
    parts: list[str] = []
    for pm in sorted(groups):
        issue_count = len(groups[pm])
        parts.append(f"{pm} {issue_count} issue{'s' if issue_count != 1 else ''}")
    for pm in sorted(clean_counts):
        count = clean_counts[pm]
        parts.append(f"{pm} clean" if count == 1 else f"{pm} {count} clean")
    return " · ".join(parts) if parts else "No PM issues"


def _short_issue(result: PMRuleResult) -> str:
    mapping = {
        "R1": "Invalid project type",
        "R3": "No PM assigned",
        "R4": "Status stale or missing",
        "R6": "Missing Sold by",
        "R7": "Missing permit field",
        "R8": "Missing HOA approval",
        "R9": "Missing asbestos check",
        "R10": "Missing review request",
        "R11": "No project tasks",
        "R13": "Task missing assignee",
        "R15": "Task overdue",
        "R16": "On hold without reason",
        "R17": "Completed project has open tasks",
        "R18": "Payment order issue",
        "R19": "Homeowner Authorization late",
        "R20": "Completion Report not green",
        "R21": "Equipment not registered",
        "R22": "Deposit below required amount" if "below" in _normalize(result.issue) else "Deposit missing before install",
        "R23": "Permit missing before install",
        "R24": "Equipment not confirmed",
        "R25": "Rebate not confirmed",
        "R26": "Crew not assigned",
        "R27": "Project left open too long",
        "R28": "Change order missing approval",
    }
    return mapping.get(result.rule_id, result.issue)


def _action_for_rule(rule_id: str) -> str:
    mapping = {
        "R1": "Set the correct PM install project type.",
        "R3": "Assign the project manager.",
        "R4": "Update project status.",
        "R6": "Fill Project Details Sold By.",
        "R7": "Fill Project Details PERMIT information.",
        "R8": "Confirm HOA requirement and approval status.",
        "R9": "Record asbestos check or confirm it is not required.",
        "R10": "Set the review-requested flag.",
        "R11": "Apply PM task template.",
        "R13": "Assign task owner.",
        "R15": "Update or close overdue task.",
        "R16": "Add structured on-hold reason or update status.",
        "R17": "Close completed project tasks.",
        "R18": "Review payment milestone order.",
        "R19": "Confirm Homeowner Authorization timing.",
        "R20": "Complete or correct the Installation Completion Report.",
        "R21": "Register equipment or update registration status.",
        "R22": "Confirm deposit payment before install.",
        "R23": "Confirm permit before install.",
        "R24": "Confirm equipment readiness before scheduling.",
        "R25": "Confirm rebate status before scheduling.",
        "R26": "Assign install crew before scheduling.",
        "R27": "Close or update completed project.",
        "R28": "Attach written approval for change order.",
    }
    return mapping.get(rule_id, "Review the PM project in ServiceTitan.")


def _format_date(value: datetime, timezone_name: str) -> str:
    local = value.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%b %d").replace(" 0", " ")


def _format_money(value: float) -> str:
    return f"${value:,.2f}"


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())
