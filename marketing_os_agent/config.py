from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float_env(name: str, default: float) -> float:
    raw = _env(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _json_map_env(name: str) -> dict[str, str]:
    raw = _env(name, "").strip()
    if not raw:
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        raise ValueError(f"{name} must be a JSON object with string keys and values")
    return parsed


@dataclass(frozen=True)
class Settings:
    app_env: str
    port: int
    timezone: str
    log_level: str
    sqlite_path: str
    poll_interval_seconds: int
    poll_overlap_seconds: int

    anthropic_api_key: str
    claude_model: str

    notion_api_key: str
    notion_api_version: str
    notion_tasks_database_id: str
    notion_tasks_data_source_id: str
    notion_marketing_calendar_database_id: str
    notion_marketing_calendar_data_source_id: str
    notion_workbooks_page_id: str
    notion_workbooks_database_id: str
    notion_workbooks_data_source_id: str
    notion_needs_verification_property: str
    notion_child_tasks_property: str
    notion_dependencies_property: str
    notion_task_name_property: str
    notion_task_owner_property: str
    notion_task_deadline_property: str
    notion_task_original_deadline_property: str
    notion_task_status_property: str
    notion_task_priority_property: str
    notion_task_department_property: str
    notion_task_campaign_property: str
    notion_task_deliverable_property: str
    notion_task_notes_property: str
    notion_task_needs_from_others_property: str
    notion_task_created_property: str
    notion_task_last_edited_property: str
    notion_campaign_name_property: str
    notion_campaign_trade_property: str
    notion_campaign_channel_property: str
    notion_campaign_start_date_property: str
    notion_campaign_end_date_property: str
    notion_campaign_owner_property: str
    notion_campaign_status_property: str
    notion_campaign_planned_spend_property: str
    notion_campaign_expected_leads_property: str
    notion_campaign_expected_cpl_property: str
    notion_campaign_expected_roi_property: str
    notion_campaign_actual_spend_property: str
    notion_campaign_actual_leads_property: str
    notion_campaign_actual_cpl_property: str
    notion_campaign_actual_roi_property: str
    notion_campaign_linked_tasks_property: str
    notion_campaign_linked_workbook_property: str
    notion_campaign_notes_property: str

    slack_bot_token: str
    slack_signing_secret: str
    slack_marketing_ops_channel_id: str
    slack_tim_user_id: str

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    email_from: str
    tim_email: str
    vadim_email: str

    budget_overrun_threshold_percent: float
    campaign_risk_window_percent: float
    campaign_risk_task_completion_percent: float

    owner_slack_map: dict[str, str] = field(default_factory=dict)
    owner_email_map: dict[str, str] = field(default_factory=dict)
    task_status_map: dict[str, str] = field(default_factory=dict)
    task_priority_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        app_env = _env("APP_ENV", _env("NODE_ENV", "development"))
        timezone = _env("TIMEZONE", _env("TZ", "America/Los_Angeles"))
        sqlite_path = _env("SQLITE_PATH", "data/marketing_os_agent.sqlite3")
        return cls(
            app_env=app_env,
            port=_int_env("PORT", 8080),
            timezone=timezone,
            log_level=_env("LOG_LEVEL", "INFO"),
            sqlite_path=sqlite_path,
            poll_interval_seconds=_int_env("NOTION_POLL_INTERVAL_SECONDS", 120),
            poll_overlap_seconds=_int_env("NOTION_POLL_OVERLAP_SECONDS", 3600),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            claude_model=_env("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            notion_api_key=_env("NOTION_API_KEY"),
            notion_api_version=_env("NOTION_API_VERSION", "2026-03-11"),
            notion_tasks_database_id=_env("NOTION_TASKS_DATABASE_ID"),
            notion_tasks_data_source_id=_env("NOTION_TASKS_DATA_SOURCE_ID"),
            notion_marketing_calendar_database_id=_env("NOTION_MARKETING_CALENDAR_DATABASE_ID"),
            notion_marketing_calendar_data_source_id=_env("NOTION_MARKETING_CALENDAR_DATA_SOURCE_ID"),
            notion_workbooks_page_id=_env("NOTION_WORKBOOKS_PAGE_ID"),
            notion_workbooks_database_id=_env("NOTION_WORKBOOKS_DATABASE_ID"),
            notion_workbooks_data_source_id=_env("NOTION_WORKBOOKS_DATA_SOURCE_ID"),
            notion_needs_verification_property=_env("NOTION_NEEDS_VERIFICATION_PROPERTY", "Needs Verification"),
            notion_child_tasks_property=_env("NOTION_CHILD_TASKS_PROPERTY", "Child Tasks"),
            notion_dependencies_property=_env("NOTION_DEPENDENCIES_PROPERTY", "Dependencies"),
            notion_task_name_property=_env("NOTION_TASK_NAME_PROPERTY", "Task name"),
            notion_task_owner_property=_env("NOTION_TASK_OWNER_PROPERTY", "Owner"),
            notion_task_deadline_property=_env("NOTION_TASK_DEADLINE_PROPERTY", "Deadline"),
            notion_task_original_deadline_property=_env("NOTION_TASK_ORIGINAL_DEADLINE_PROPERTY", "Original Deadline"),
            notion_task_status_property=_env("NOTION_TASK_STATUS_PROPERTY", "Status"),
            notion_task_priority_property=_env("NOTION_TASK_PRIORITY_PROPERTY", "Priority"),
            notion_task_department_property=_env("NOTION_TASK_DEPARTMENT_PROPERTY", "Department"),
            notion_task_campaign_property=_env("NOTION_TASK_CAMPAIGN_PROPERTY", "Linked Campaign"),
            notion_task_deliverable_property=_env("NOTION_TASK_DELIVERABLE_PROPERTY", "Deliverable link"),
            notion_task_notes_property=_env("NOTION_TASK_NOTES_PROPERTY", "Notes / Issues"),
            notion_task_needs_from_others_property=_env("NOTION_TASK_NEEDS_FROM_OTHERS_PROPERTY", "Needs From Others"),
            notion_task_created_property=_env("NOTION_TASK_CREATED_PROPERTY", "Created"),
            notion_task_last_edited_property=_env("NOTION_TASK_LAST_EDITED_PROPERTY", "Last Edited"),
            notion_campaign_name_property=_env("NOTION_CAMPAIGN_NAME_PROPERTY", "Campaign name"),
            notion_campaign_trade_property=_env("NOTION_CAMPAIGN_TRADE_PROPERTY", "Trade"),
            notion_campaign_channel_property=_env("NOTION_CAMPAIGN_CHANNEL_PROPERTY", "Channel"),
            notion_campaign_start_date_property=_env("NOTION_CAMPAIGN_START_DATE_PROPERTY", "Start Date"),
            notion_campaign_end_date_property=_env("NOTION_CAMPAIGN_END_DATE_PROPERTY", "End Date"),
            notion_campaign_owner_property=_env("NOTION_CAMPAIGN_OWNER_PROPERTY", "Owner"),
            notion_campaign_status_property=_env("NOTION_CAMPAIGN_STATUS_PROPERTY", "Status"),
            notion_campaign_planned_spend_property=_env("NOTION_CAMPAIGN_PLANNED_SPEND_PROPERTY", "Planned Spend"),
            notion_campaign_expected_leads_property=_env("NOTION_CAMPAIGN_EXPECTED_LEADS_PROPERTY", "Expected Leads"),
            notion_campaign_expected_cpl_property=_env("NOTION_CAMPAIGN_EXPECTED_CPL_PROPERTY", "Expected CPL"),
            notion_campaign_expected_roi_property=_env("NOTION_CAMPAIGN_EXPECTED_ROI_PROPERTY", "Expected ROI"),
            notion_campaign_actual_spend_property=_env("NOTION_CAMPAIGN_ACTUAL_SPEND_PROPERTY", "Actual Spend"),
            notion_campaign_actual_leads_property=_env("NOTION_CAMPAIGN_ACTUAL_LEADS_PROPERTY", "Actual Leads"),
            notion_campaign_actual_cpl_property=_env("NOTION_CAMPAIGN_ACTUAL_CPL_PROPERTY", "Actual CPL"),
            notion_campaign_actual_roi_property=_env("NOTION_CAMPAIGN_ACTUAL_ROI_PROPERTY", "Actual ROI"),
            notion_campaign_linked_tasks_property=_env("NOTION_CAMPAIGN_LINKED_TASKS_PROPERTY", "Linked Tasks"),
            notion_campaign_linked_workbook_property=_env("NOTION_CAMPAIGN_LINKED_WORKBOOK_PROPERTY", "Linked Workbook"),
            notion_campaign_notes_property=_env("NOTION_CAMPAIGN_NOTES_PROPERTY", "Notes"),
            slack_bot_token=_env("SLACK_BOT_TOKEN"),
            slack_signing_secret=_env("SLACK_SIGNING_SECRET"),
            slack_marketing_ops_channel_id=_env("SLACK_MARKETING_OPS_CHANNEL_ID"),
            slack_tim_user_id=_env("SLACK_TIM_USER_ID"),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=_int_env("SMTP_PORT", 587),
            smtp_user=_env("SMTP_USER"),
            smtp_pass=_env("SMTP_PASS"),
            email_from=_env("EMAIL_FROM"),
            tim_email=_env("TIM_EMAIL"),
            vadim_email=_env("VADIM_EMAIL"),
            budget_overrun_threshold_percent=_float_env("BUDGET_OVERRUN_THRESHOLD_PERCENT", 0.0),
            campaign_risk_window_percent=_float_env("CAMPAIGN_RISK_WINDOW_PERCENT", 80.0),
            campaign_risk_task_completion_percent=_float_env("CAMPAIGN_RISK_TASK_COMPLETION_PERCENT", 20.0),
            owner_slack_map=_json_map_env("OWNER_SLACK_MAP_JSON"),
            owner_email_map=_json_map_env("OWNER_EMAIL_MAP_JSON"),
            task_status_map={
                "not started": "Not Started",
                "not_started": "Not Started",
                "in progress": "In Progress",
                "in_progress": "In Progress",
                "done": "Completed",
                "complete": "Completed",
                "completed": "Completed",
                "blocked": "Blocked",
                "needs review": "Needs Review",
                "needs_review": "Needs Review",
                "delayed": "Delayed",
                "cancelled": "Canceled",
                "canceled": "Canceled",
                **_json_map_env("TASK_STATUS_MAP_JSON"),
            },
            task_priority_map={
                "low": "Low",
                "medium": "Medium",
                "high": "High",
                "urgent": "Critical",
                "critical": "Critical",
                **_json_map_env("TASK_PRIORITY_MAP_JSON"),
            },
        )

    @property
    def data_dir(self) -> Path:
        return Path(self.sqlite_path).expanduser().resolve().parent

    def missing_runtime_credentials(self) -> list[str]:
        required = {
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "NOTION_API_KEY": self.notion_api_key,
            "NOTION_TASKS_DATABASE_ID": self.notion_tasks_database_id,
            "NOTION_MARKETING_CALENDAR_DATABASE_ID": self.notion_marketing_calendar_database_id,
            "SLACK_BOT_TOKEN": self.slack_bot_token,
            "SLACK_MARKETING_OPS_CHANNEL_ID": self.slack_marketing_ops_channel_id,
            "SLACK_TIM_USER_ID": self.slack_tim_user_id,
        }
        return [key for key, value in required.items() if not value]

    def missing_email_credentials(self) -> list[str]:
        required = {
            "SMTP_HOST": self.smtp_host,
            "SMTP_USER": self.smtp_user,
            "SMTP_PASS": self.smtp_pass,
            "EMAIL_FROM": self.email_from,
            "TIM_EMAIL": self.tim_email,
            "VADIM_EMAIL": self.vadim_email,
        }
        return [key for key, value in required.items() if not value]


@dataclass
class HealthReport:
    ok: bool
    checks: dict[str, bool]
    messages: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = ["OK" if self.ok else "NOT OK"]
        for name, passed in self.checks.items():
            lines.append(f"- {name}: {'ok' if passed else 'failed'}")
        for message in self.messages:
            lines.append(f"- {message}")
        return "\n".join(lines)
