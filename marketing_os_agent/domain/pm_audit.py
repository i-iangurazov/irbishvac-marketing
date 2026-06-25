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


PM_AUDIT_TEST_MESSAGE = """📋 PM Audit — Test

Jane
• Project #PM-TEST-1001 — Missing PM task template
  Field: Tasks
  Action: Apply PM task template
  Link: https://go.servicetitan.com/#/Project/Index/PM-TEST-1001

Gerson
• Project #PM-TEST-1002 — Task has no assignee
  Field: Task #PM-TASK-884
  Action: Assign task owner
  Link: https://go.servicetitan.com/#/Project/Index/PM-TEST-1002

Summary: Jane 1 issue · Gerson 1 issue"""


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
    skipped_open_tasks_without_due: int = 0


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
                    groups[pm].append(_failure_block(audit.project, failure, timezone_name))
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
            projects = self.client.query_pm_projects(
                project_type_ids=PM_INSTALL_PROJECT_TYPE_IDS,
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
            required["PM_AUDIT_SLACK_CHANNEL_ID or SLACK_ALERT_CHANNEL_ID"] = self._alert_channel()
        return [key for key, value in required.items() if not value]

    def _alert_channel(self) -> str:
        return self.settings.pm_audit_slack_channel_id or self.settings.slack_alert_channel_id


def _run_pm_rules(project: ServiceTitanProject, settings: Settings, now: datetime) -> list[PMRuleResult]:
    return [
        _rule_project_type(project),
        _rule_pm_assigned(project, settings, now),
        _rule_sold_by(project, settings),
        _rule_permit_present(project, settings),
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
    return _result("R3", "PM assigned", PM_FAIL, "No PM assigned.", "Project Manager")


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
    skipped_open_tasks_without_due: int = 0,
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
        skipped_open_tasks_without_due=skipped_open_tasks_without_due,
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


def _custom_field_match(project: ServiceTitanProject, field_names: list[str]) -> tuple[str | None, str]:
    normalized_fields = {_normalize(name): (name, value.strip()) for name, value in project.custom_fields.items()}
    for field_name in field_names:
        match = normalized_fields.get(_normalize(field_name))
        if match is not None:
            return match
    return None, ""


def _failure_block(project: ServiceTitanProject, result: PMRuleResult, timezone_name: str) -> str:
    lines = [
        f"• Project #{project.project_number or project.project_id} — {_short_issue(result)}",
        f"  Field: {result.task_number or result.field}",
        f"  Action: {_action_for_rule(result.rule_id)}",
    ]
    if result.due_at:
        lines.append(f"  Due: {_format_date(result.due_at, timezone_name)}")
    elif result.install_date or project.start_date:
        lines.append(f"  Install: {_format_date(result.install_date or project.start_date, timezone_name)}")
    if project.url:
        lines.append(f"  Link: {project.url}")
    return "\n".join(lines)


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
        "R6": "Missing Sold by",
        "R7": "Missing permit field",
        "R11": "No project tasks",
        "R13": "Task missing assignee",
        "R15": "Task overdue",
        "R17": "Completed project has open tasks",
    }
    return mapping.get(result.rule_id, result.issue)


def _action_for_rule(rule_id: str) -> str:
    mapping = {
        "R1": "Set the correct PM install project type.",
        "R3": "Assign the project manager.",
        "R6": "Fill Project Details Sold By.",
        "R7": "Fill Project Details PERMIT information.",
        "R11": "Apply PM task template.",
        "R13": "Assign task owner.",
        "R15": "Update or close overdue task.",
        "R17": "Close completed project tasks.",
    }
    return mapping.get(rule_id, "Review the PM project in ServiceTitan.")


def _format_date(value: datetime, timezone_name: str) -> str:
    local = value.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%b %d").replace(" 0", " ")


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())
