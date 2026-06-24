from __future__ import annotations

import logging
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
    "service call",
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


@dataclass(frozen=True)
class PMRuleResult:
    rule_id: str
    name: str
    status: str
    issue: str
    field: str
    action: str
    due_at: datetime | None = None
    install_date: datetime | None = None
    task_number: str = ""


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
    rules_evaluated: int = 0
    pass_count: int = 0
    fail_count: int = 0
    skip_count: int = 0
    alerts_sent: int = 0
    alerts_would_send: int = 0
    errors: int = 0
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
            f"- projects scanned: {self.projects_scanned}",
            f"- in-scope projects: {self.in_scope_projects}",
            f"- out-of-scope projects skipped: {self.skipped_out_of_scope}",
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
        if self.failures:
            lines.extend(["", self.alert_text()])
        return lines

    def alert_text(self, now: datetime | None = None, timezone_name: str = "UTC") -> str:
        now = now or datetime.now(timezone.utc)
        local_now = now.astimezone(ZoneInfo(timezone_name))
        groups: dict[str, list[str]] = {}
        clean_counts: dict[str, int] = {}
        for audit in self.project_audits:
            pm = audit.project.project_manager_label
            if audit.failures:
                groups.setdefault(pm, [])
                for failure in audit.failures:
                    groups[pm].append(_failure_line(audit.project, failure, timezone_name))
            elif not audit.skipped_out_of_scope:
                clean_counts[pm] = clean_counts.get(pm, 0) + 1

        lines = [f"PM Audit, {local_now.date().isoformat()}"]
        for pm in sorted(groups):
            lines.extend(["", f"{pm}:"])
            lines.extend(groups[pm])

        summary_parts = [f"{pm} {len(items)} issue{'s' if len(items) != 1 else ''}" for pm, items in sorted(groups.items())]
        summary_parts.extend(f"{pm} clean" for pm in sorted(clean_counts) if pm not in groups)
        if summary_parts:
            lines.extend(["", "Summary: " + ", ".join(summary_parts) + "."])
        else:
            lines.extend(["", "Summary: no PM audit failures."])
        return "\n".join(lines)


class PMAuditService:
    def __init__(self, settings: Settings, client: ServiceTitanClient, slack: SlackClient) -> None:
        self.settings = settings
        self.client = client
        self.slack = slack

    def run_once(self, now: datetime | None = None, *, require_enabled: bool = True) -> PMAuditSummary:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        summary = PMAuditSummary(enabled=self.settings.pm_audit_enabled, dry_run=self.settings.pm_audit_dry_run)
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
            projects = self.client.query_pm_projects()
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

        summary.projects_scanned = len(projects)
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

        if summary.fail_count and self.settings.pm_audit_dry_run:
            summary.alerts_would_send = 1
            logger.info("pm_audit_dry_run", extra={"failures": summary.fail_count})
        elif summary.fail_count:
            ts = self.slack.post_message(self.settings.slack_alert_channel_id, summary.alert_text(now, self.settings.timezone))
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
            required["SLACK_ALERT_CHANNEL_ID"] = self.settings.slack_alert_channel_id
        return [key for key, value in required.items() if not value]


def _run_pm_rules(project: ServiceTitanProject, settings: Settings, now: datetime) -> list[PMRuleResult]:
    return [
        _rule_project_type(project),
        _rule_pm_assigned(project, settings, now),
        _rule_sold_by(project),
        _rule_permit_present(project),
        _rule_tasks_applied(project, settings, now),
        _rule_tasks_assigned(project),
        _rule_no_stale_tasks(project, settings, now),
        _rule_completed_closed_out(project),
    ]


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
    if not project.created_on:
        return _result("R3", "PM assigned", PM_SKIP, "Project created timestamp unavailable.", "Project Manager")
    deadline = project.created_on.astimezone(timezone.utc) + timedelta(hours=settings.pm_audit_pm_assignment_grace_hours)
    if now <= deadline:
        return _result("R3", "PM assigned", PM_SKIP, "Project is still inside PM assignment grace period.", "Project Manager")
    return _result("R3", "PM assigned", PM_FAIL, "No PM assigned after the grace period.", "Project Manager", due_at=deadline)


def _rule_sold_by(project: ServiceTitanProject) -> PMRuleResult:
    if not project.custom_fields_available:
        return _result("R6", "Comfort Advisor / Sold By set", PM_SKIP, "Project custom fields unavailable.", "Sold by")
    sold_by = _custom_field(project, "Sold by")
    if not sold_by:
        return _result("R6", "Comfort Advisor / Sold By set", PM_FAIL, "Sold by custom field is empty.", "Sold by")
    return _result("R6", "Comfort Advisor / Sold By set", PM_PASS, "Sold by custom field is present.", "Sold by")


def _rule_permit_present(project: ServiceTitanProject) -> PMRuleResult:
    if not project.custom_fields_available:
        return _result("R7", "Permit field present", PM_SKIP, "Project custom fields unavailable.", "Permit")
    if not _has_custom_field(project, "Permit"):
        return _result("R7", "Permit field present", PM_SKIP, "Permit custom field unavailable.", "Permit")
    if not _custom_field(project, "Permit"):
        return _result("R7", "Permit field present", PM_FAIL, "Permit field is empty.", "Permit")
    return _result("R7", "Permit field present", PM_PASS, "Permit field is present.", "Permit")


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
    for task in project.tasks:
        is_open = task.is_open
        if is_open is None:
            return _result("R15", "No stale open tasks", PM_SKIP, "Task status unavailable.", "Task status", task_number=task.display_name)
        if not is_open:
            continue
        if not task.due_at:
            return _result("R15", "No stale open tasks", PM_SKIP, "Open task due date unavailable.", "Task due date", task_number=task.display_name)
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
            )
    return _result("R15", "No stale open tasks", PM_PASS, "No stale open tasks found.", "Task due date")


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
) -> PMRuleResult:
    return PMRuleResult(
        rule_id=rule_id,
        name=name,
        status=status,
        issue=issue,
        field=field,
        action="Review the PM project in ServiceTitan and correct the missing or stale operational field.",
        due_at=due_at,
        install_date=install_date,
        task_number=task_number,
    )


def _is_explicitly_out_of_scope(project: ServiceTitanProject) -> bool:
    text = " ".join(value for value in (project.project_type_name, project.status, *project.business_unit_ids) if value).lower()
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


def _has_custom_field(project: ServiceTitanProject, field_name: str) -> bool:
    target = _normalize(field_name)
    return any(_normalize(name) == target for name in project.custom_fields)


def _custom_field(project: ServiceTitanProject, field_name: str) -> str:
    target = _normalize(field_name)
    for name, value in project.custom_fields.items():
        if _normalize(name) == target:
            return value.strip()
    return ""


def _failure_line(project: ServiceTitanProject, result: PMRuleResult, timezone_name: str) -> str:
    parts = [
        f"Project #{project.project_number or project.project_id}",
        _short_issue(result),
        result.task_number or result.field,
    ]
    if result.due_at:
        parts.append(f"due {_format_date(result.due_at, timezone_name)}")
    elif result.install_date or project.start_date:
        parts.append(f"install {_format_date(result.install_date or project.start_date, timezone_name)}")
    if project.url:
        parts.append(project.url)
    return " | ".join(parts)


def _short_issue(result: PMRuleResult) -> str:
    mapping = {
        "R1": "Invalid project type",
        "R3": "No PM assigned",
        "R6": "Missing Sold by",
        "R7": "Missing permit field",
        "R11": "No project tasks",
        "R13": "Task missing assignee",
        "R15": "Task overdue",
        "R17": "Completed project has open tasks",
    }
    return mapping.get(result.rule_id, result.issue)


def _format_date(value: datetime, timezone_name: str) -> str:
    local = value.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%b %d").replace(" 0", " ")


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())
