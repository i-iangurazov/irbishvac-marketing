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


def _optional_int_env(name: str) -> int | None:
    raw = _env(name, "").strip()
    if raw == "":
        return None
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


def _json_string_list_env(name: str, default: list[str]) -> list[str]:
    raw = _env(name, "").strip()
    if not raw:
        return list(default)
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{name} must be a JSON array of strings")
    return parsed


def _csv_string_list_env(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env(name, "").strip()
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _weekday_env(name: str, default: str) -> str:
    value = _env(name, default).strip().upper()
    valid = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
    if value not in valid:
        raise ValueError(f"{name} must be one of {', '.join(sorted(valid))}")
    return value


def _hour_env(name: str, default: int) -> int:
    value = _int_env(name, default)
    if value < 0 or value > 23:
        raise ValueError(f"{name} must be an hour from 0 to 23")
    return value


def _json_list_env(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env(name, "").strip()
    if not raw:
        return list(default or [])
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{name} must be a JSON array of strings")
    return parsed


def _json_object_env(name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = _env(name, "").strip()
    if not raw:
        return dict(default or {})
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


DEFAULT_SERVICE_TITAN_BUSINESS_UNIT_LABELS = {
    "1809": "HVAC Install",
    "1810": "HVAC Service",
    "1812": "HVAC Sales / Comfort Advisors",
    "64313020": "Plumbing Install",
    "1809,64313020": "Installs",
    "64326403": "Plumbing Sales",
    "64315277": "Plumbing Service",
}


@dataclass(frozen=True)
class Settings:
    app_env: str
    port: int
    timezone: str
    log_level: str
    sqlite_path: str
    poll_interval_seconds: int
    poll_overlap_seconds: int
    task_reminder_minutes_before: int
    task_date_only_deadline_hour: int
    service_titan_audit_enabled: bool
    service_titan_audit_poll_interval_seconds: int
    service_titan_audit_startup_delay_seconds: int
    service_titan_audit_lookback_minutes: int
    service_titan_audit_overlap_seconds: int
    service_titan_audit_max_pages: int
    service_titan_audit_page_size: int
    service_titan_audit_max_alerts_per_cycle: int
    service_titan_audit_timezone: str
    service_titan_audit_dry_run: bool
    service_titan_audit_backfill_alerts: bool
    service_titan_audit_ignore_checkpoint_once: bool
    service_titan_audit_debug_fields: bool
    service_titan_weekly_summary_enabled: bool
    service_titan_weekly_summary_day: str
    service_titan_weekly_summary_hour: int
    service_titan_weekly_summary_lookback_days: int
    pm_audit_enabled: bool
    pm_audit_schedule_enabled: bool
    pm_audit_run_on_startup: bool
    pm_audit_dry_run: bool
    pm_audit_status_stale_days: int
    pm_audit_task_overdue_days: int
    pm_audit_pm_assignment_grace_hours: int
    pm_audit_task_template_grace_hours: int
    pm_audit_run_hour: int
    pm_audit_run_minute: int
    pm_audit_weekdays_only: bool
    pm_audit_project_page_size: int
    pm_audit_max_projects: int
    pm_audit_max_tasks: int
    pm_audit_enabled_rule_ids: list[str]
    pm_audit_install_business_unit_ids: list[str]
    pm_audit_install_business_unit_names: list[str]
    pm_audit_include_client_name: bool
    pm_audit_sold_by_field_names: list[str]
    pm_audit_permit_field_names: list[str]
    pm_audit_hoa_field_names: list[str]
    pm_audit_hoa_zip_list: list[str]
    pm_audit_asbestos_field_names: list[str]
    pm_audit_asbestos_year_cutoff: int | None
    pm_audit_review_requested_field_names: list[str]
    pm_audit_on_hold_max_days: int
    pm_audit_on_hold_reason_field_names: list[str]
    pm_audit_homeowner_auth_within_hours: int
    pm_audit_homeowner_auth_form_names: list[str]
    pm_audit_completion_report_form_names: list[str]
    pm_audit_equipment_field_names: list[str]
    pm_audit_deposit_fixed_amount: float
    pm_audit_deposit_percent: float
    pm_audit_deposit_before_install_days: int
    pm_audit_deposit_rounding_tolerance: float
    pm_audit_deposit_line_item_names: list[str]
    pm_audit_deposit_payment_status_values: list[str]
    pm_audit_permit_before_install_days: int
    pm_audit_project_left_open_days: int
    pm_audit_rebate_field_names: list[str]
    pm_audit_crew_field_names: list[str]
    pm_audit_change_order_field_names: list[str]
    pm_audit_slack_channel_id: str
    pm_audit_test_send: bool
    install_audit_enabled: bool
    install_audit_dry_run: bool
    install_audit_run_on_startup: bool
    install_audit_schedule_enabled: bool
    install_audit_slack_channel_id: str
    install_audit_job_type_match_keywords: list[str]
    install_audit_business_unit_names: list[str]
    install_audit_business_unit_ids: list[str]
    install_audit_rule_ids: list[str]
    install_audit_max_appointments: int
    install_audit_lookback_days: int
    install_audit_lookahead_days: int
    install_audit_run_hour: int
    install_audit_run_minute: int
    install_audit_weekdays_only: bool
    install_audit_evening_report_enabled: bool
    install_audit_evening_report_hour: int
    install_audit_evening_report_minute: int
    install_audit_evening_report_max_jobs: int
    install_audit_first_day_collect_pct: float
    install_audit_final_day_collect_pct: float
    install_audit_deposit_reminder_lead_days: int
    install_audit_completion_photos_min: int
    install_audit_arrival_grace_min: int
    install_audit_meal_break_after_hours: float
    install_audit_second_meal_after_hours: float
    install_audit_meal_break_min_minutes: int
    notifications_test_send: bool

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
    notion_task_last_reminder_sent_property: str
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
    slack_alert_channel_id: str

    servicetitan_client_id: str
    servicetitan_client_secret: str
    servicetitan_tenant_id: str
    servicetitan_app_key: str
    servicetitan_environment: str
    servicetitan_base_url: str
    servicetitan_auth_url: str
    servicetitan_job_url_template: str
    servicetitan_project_url_template: str

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
    service_titan_arrival_grace_minutes: int
    service_titan_first_call_grace_minutes: int
    service_titan_open_job_grace_minutes: int
    service_titan_min_lunch_break_minutes: int
    service_titan_lunch_required_after_hours: float
    service_titan_min_note_length: int
    service_titan_require_hhr: bool
    service_titan_require_equipment_registration: bool
    service_titan_min_repair_options: int
    service_titan_require_home_comfort_plan_option: bool
    service_titan_po_reconcile_within_hours: int
    service_titan_alert_include_customer_name: bool
    sales_comfort_advisor_audit_enabled: bool
    hvac_service_audit_enabled: bool
    plumbing_service_audit_enabled: bool
    technician_compliance_enabled: bool
    dispatcher_audit_enabled: bool
    dispatcher_audit_slack_channel_id: str
    dispatcher_audit_rule_ids: list[str]

    owner_slack_map: dict[str, str] = field(default_factory=dict)
    owner_email_map: dict[str, str] = field(default_factory=dict)
    task_status_map: dict[str, str] = field(default_factory=dict)
    task_priority_map: dict[str, str] = field(default_factory=dict)
    service_titan_diagnostic_fee_keywords: list[str] = field(default_factory=list)
    service_titan_home_comfort_plan_keywords: list[str] = field(default_factory=list)
    service_titan_hhr_keywords: list[str] = field(default_factory=list)
    service_titan_special_order_required_note_fields: list[str] = field(default_factory=list)
    service_titan_disabled_rule_ids: list[str] = field(default_factory=list)
    service_titan_required_phases: list[str] = field(default_factory=list)
    service_titan_required_operational_fields: list[str] = field(default_factory=list)
    service_titan_rule_scope_config: dict[str, Any] = field(default_factory=dict)
    service_titan_business_unit_labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        app_env = _env("APP_ENV", _env("NODE_ENV", "development"))
        timezone = _env("TIMEZONE", _env("TZ", "America/Los_Angeles"))
        sqlite_path = _env("SQLITE_PATH", "data/marketing_os_agent.sqlite3")
        service_titan_environment = _env("SERVICETITAN_ENVIRONMENT", "production").strip().lower()
        service_titan_base_url = _env(
            "SERVICETITAN_BASE_URL",
            "https://api-integration.servicetitan.io"
            if service_titan_environment == "integration"
            else "https://api.servicetitan.io",
        ).rstrip("/")
        return cls(
            app_env=app_env,
            port=_int_env("PORT", 8080),
            timezone=timezone,
            log_level=_env("LOG_LEVEL", "INFO"),
            sqlite_path=sqlite_path,
            poll_interval_seconds=_int_env("NOTION_POLL_INTERVAL_SECONDS", 120),
            poll_overlap_seconds=_int_env("NOTION_POLL_OVERLAP_SECONDS", 3600),
            task_reminder_minutes_before=_int_env("TASK_REMINDER_MINUTES_BEFORE", 60),
            task_date_only_deadline_hour=_int_env("TASK_DATE_ONLY_DEADLINE_HOUR", 17),
            service_titan_audit_enabled=_bool_env("SERVICE_TITAN_AUDIT_ENABLED", False),
            service_titan_audit_poll_interval_seconds=_int_env("SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS", 300),
            service_titan_audit_startup_delay_seconds=_int_env(
                "SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS",
                _int_env("SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS", 300),
            ),
            service_titan_audit_lookback_minutes=_int_env("SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES", 240),
            service_titan_audit_overlap_seconds=_int_env("SERVICE_TITAN_AUDIT_OVERLAP_SECONDS", 300),
            service_titan_audit_max_pages=_int_env("SERVICE_TITAN_AUDIT_MAX_PAGES", 5),
            service_titan_audit_page_size=_int_env("SERVICE_TITAN_AUDIT_PAGE_SIZE", 100),
            service_titan_audit_max_alerts_per_cycle=max(0, _int_env("SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE", 25)),
            service_titan_audit_timezone=_env("SERVICE_TITAN_AUDIT_TIMEZONE", timezone),
            service_titan_audit_dry_run=_bool_env("SERVICE_TITAN_AUDIT_DRY_RUN", False),
            service_titan_audit_backfill_alerts=_bool_env("SERVICE_TITAN_AUDIT_BACKFILL_ALERTS", False),
            service_titan_audit_ignore_checkpoint_once=_bool_env("SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE", False),
            service_titan_audit_debug_fields=_bool_env("SERVICE_TITAN_AUDIT_DEBUG_FIELDS", False),
            service_titan_weekly_summary_enabled=_bool_env("SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED", False),
            service_titan_weekly_summary_day=_weekday_env("SERVICE_TITAN_WEEKLY_SUMMARY_DAY", "MON"),
            service_titan_weekly_summary_hour=_hour_env("SERVICE_TITAN_WEEKLY_SUMMARY_HOUR", 8),
            service_titan_weekly_summary_lookback_days=max(1, _int_env("SERVICE_TITAN_WEEKLY_SUMMARY_LOOKBACK_DAYS", 7)),
            pm_audit_enabled=_bool_env("PM_AUDIT_ENABLED", False),
            pm_audit_schedule_enabled=_bool_env("PM_AUDIT_SCHEDULE_ENABLED", False),
            pm_audit_run_on_startup=_bool_env("PM_AUDIT_RUN_ON_STARTUP", False),
            pm_audit_dry_run=_bool_env("PM_AUDIT_DRY_RUN", True),
            pm_audit_status_stale_days=max(1, _int_env("PM_AUDIT_STATUS_STALE_DAYS", 14)),
            pm_audit_task_overdue_days=max(1, _int_env("PM_AUDIT_TASK_OVERDUE_DAYS", 3)),
            pm_audit_pm_assignment_grace_hours=max(0, _int_env("PM_AUDIT_PM_ASSIGNMENT_GRACE_HOURS", 24)),
            pm_audit_task_template_grace_hours=max(0, _int_env("PM_AUDIT_TASK_TEMPLATE_GRACE_HOURS", 48)),
            pm_audit_run_hour=_hour_env("PM_AUDIT_RUN_HOUR", 8),
            pm_audit_run_minute=max(0, min(59, _int_env("PM_AUDIT_RUN_MINUTE", 0))),
            pm_audit_weekdays_only=_bool_env("PM_AUDIT_WEEKDAYS_ONLY", True),
            pm_audit_project_page_size=max(1, _int_env("PM_AUDIT_PROJECT_PAGE_SIZE", 50)),
            pm_audit_max_projects=max(1, _int_env("PM_AUDIT_MAX_PROJECTS", 100)),
            pm_audit_max_tasks=max(0, _int_env("PM_AUDIT_MAX_TASKS", 500)),
            pm_audit_enabled_rule_ids=_json_string_list_env("PM_AUDIT_ENABLED_RULE_IDS_JSON", []),
            pm_audit_install_business_unit_ids=_json_string_list_env(
                "PM_AUDIT_INSTALL_BUSINESS_UNIT_IDS_JSON",
                ["1809", "64313020", "64569731"],
            ),
            pm_audit_install_business_unit_names=_json_string_list_env(
                "PM_AUDIT_INSTALL_BUSINESS_UNIT_NAMES_JSON",
                ["HVAC - Install", "Plumbing - Install", "Electrical - Install"],
            ),
            pm_audit_include_client_name=_bool_env("PM_AUDIT_INCLUDE_CLIENT_NAME", False),
            pm_audit_sold_by_field_names=_json_string_list_env(
                "PM_AUDIT_SOLD_BY_FIELD_NAMES",
                ["Sold By", "Sold by", "Comfort Advisor", "Sold By CA"],
            ),
            pm_audit_permit_field_names=_json_string_list_env(
                "PM_AUDIT_PERMIT_FIELD_NAMES",
                ["PERMIT", "Permit", "Permit Number", "Permit #", "Permit Status"],
            ),
            pm_audit_hoa_field_names=_json_string_list_env(
                "PM_AUDIT_HOA_FIELD_NAMES",
                ["HOA Approval", "Under HOA", "HOA Status", "HOA"],
            ),
            pm_audit_hoa_zip_list=_json_string_list_env("PM_AUDIT_HOA_ZIP_LIST", []),
            pm_audit_asbestos_field_names=_json_string_list_env(
                "PM_AUDIT_ASBESTOS_FIELD_NAMES",
                ["Asbestos", "Asbestos Status", "Asbestos Check"],
            ),
            pm_audit_asbestos_year_cutoff=_optional_int_env("PM_AUDIT_ASBESTOS_YEAR_CUTOFF"),
            pm_audit_review_requested_field_names=_json_string_list_env(
                "PM_AUDIT_REVIEW_REQUESTED_FIELD_NAMES",
                ["Review Requested", "Review request", "Review Sent"],
            ),
            pm_audit_on_hold_max_days=max(1, _int_env("PM_AUDIT_ON_HOLD_MAX_DAYS", 30)),
            pm_audit_on_hold_reason_field_names=_json_string_list_env(
                "PM_AUDIT_ON_HOLD_REASON_FIELD_NAMES",
                ["On Hold Reason", "Hold Reason", "Hold Notes"],
            ),
            pm_audit_homeowner_auth_within_hours=max(1, _int_env("PM_AUDIT_HOMEOWNER_AUTH_WITHIN_HOURS", 2)),
            pm_audit_homeowner_auth_form_names=_json_string_list_env(
                "PM_AUDIT_HOMEOWNER_AUTH_FORM_NAMES",
                ["Homeowner Authorization", "Homeowner Authorization Form"],
            ),
            pm_audit_completion_report_form_names=_json_string_list_env(
                "PM_AUDIT_COMPLETION_REPORT_FORM_NAMES",
                ["Installation Completion Report", "Completion Report"],
            ),
            pm_audit_equipment_field_names=_json_string_list_env(
                "PM_AUDIT_EQUIPMENT_FIELD_NAMES",
                ["Equipment Registered", "Equipment Status", "Equipment Registration"],
            ),
            pm_audit_deposit_fixed_amount=max(0.0, _float_env("PM_AUDIT_DEPOSIT_FIXED_AMOUNT", 1000.0)),
            pm_audit_deposit_percent=max(0.0, _float_env("PM_AUDIT_DEPOSIT_PERCENT", 0.10)),
            pm_audit_deposit_before_install_days=max(1, _int_env("PM_AUDIT_DEPOSIT_BEFORE_INSTALL_DAYS", 7)),
            pm_audit_deposit_rounding_tolerance=max(0.0, _float_env("PM_AUDIT_DEPOSIT_ROUNDING_TOLERANCE", 5.0)),
            pm_audit_deposit_line_item_names=_json_string_list_env(
                "PM_AUDIT_DEPOSIT_LINE_ITEM_NAMES",
                ["Deposit", "Project Deposit", "Installation Deposit"],
            ),
            pm_audit_deposit_payment_status_values=_json_string_list_env(
                "PM_AUDIT_DEPOSIT_PAYMENT_STATUS_VALUES",
                ["Paid", "Posted", "Succeeded", "Completed", "Received"],
            ),
            pm_audit_permit_before_install_days=max(1, _int_env("PM_AUDIT_PERMIT_BEFORE_INSTALL_DAYS", 10)),
            pm_audit_project_left_open_days=max(1, _int_env("PM_AUDIT_PROJECT_LEFT_OPEN_DAYS", 7)),
            pm_audit_rebate_field_names=_json_string_list_env(
                "PM_AUDIT_REBATE_FIELD_NAMES",
                ["Rebate", "Rebate Status", "Rebate Approval"],
            ),
            pm_audit_crew_field_names=_json_string_list_env(
                "PM_AUDIT_CREW_FIELD_NAMES",
                ["Crew", "Install Crew", "Team"],
            ),
            pm_audit_change_order_field_names=_json_string_list_env(
                "PM_AUDIT_CHANGE_ORDER_FIELD_NAMES",
                ["Change Order", "Change Order Approval", "Additional Work Approval", "Written Approval"],
            ),
            pm_audit_slack_channel_id=_env("PM_AUDIT_SLACK_CHANNEL_ID"),
            pm_audit_test_send=_bool_env("PM_AUDIT_TEST_SEND", False),
            install_audit_enabled=_bool_env("INSTALL_AUDIT_ENABLED", False),
            install_audit_dry_run=_bool_env("INSTALL_AUDIT_DRY_RUN", True),
            install_audit_run_on_startup=_bool_env("INSTALL_AUDIT_RUN_ON_STARTUP", False),
            install_audit_schedule_enabled=_bool_env("INSTALL_AUDIT_SCHEDULE_ENABLED", False),
            install_audit_slack_channel_id=_env("INSTALL_AUDIT_SLACK_CHANNEL_ID"),
            install_audit_job_type_match_keywords=_json_string_list_env("INSTALL_AUDIT_JOB_TYPE_MATCH_KEYWORDS", ["Installation"]),
            install_audit_business_unit_names=_json_string_list_env(
                "INSTALL_AUDIT_BUSINESS_UNIT_NAMES",
                ["Electrical - Install", "HVAC - Install", "Plumbing - Install"],
            ),
            install_audit_business_unit_ids=_dedupe_strings(
                [
                    *_json_string_list_env("INSTALL_AUDIT_BUSINESS_UNIT_IDS", ["1809", "64313020"]),
                    *_csv_string_list_env("ST_BU_INSTALLERS", []),
                ]
            ),
            install_audit_rule_ids=_json_string_list_env("INSTALL_AUDIT_RULE_IDS_JSON", []),
            install_audit_max_appointments=max(1, _int_env("INSTALL_AUDIT_MAX_APPOINTMENTS", 100)),
            install_audit_lookback_days=max(0, _int_env("INSTALL_AUDIT_LOOKBACK_DAYS", 14)),
            install_audit_lookahead_days=max(0, _int_env("INSTALL_AUDIT_LOOKAHEAD_DAYS", 2)),
            install_audit_run_hour=_hour_env("INSTALL_AUDIT_RUN_HOUR", 8),
            install_audit_run_minute=max(0, min(59, _int_env("INSTALL_AUDIT_RUN_MINUTE", 0))),
            install_audit_weekdays_only=_bool_env("INSTALL_AUDIT_WEEKDAYS_ONLY", True),
            install_audit_evening_report_enabled=_bool_env("INSTALL_AUDIT_EVENING_REPORT_ENABLED", False),
            install_audit_evening_report_hour=_hour_env("INSTALL_AUDIT_EVENING_REPORT_HOUR", 20),
            install_audit_evening_report_minute=max(
                0,
                min(59, _int_env("INSTALL_AUDIT_EVENING_REPORT_MINUTE", 0)),
            ),
            install_audit_evening_report_max_jobs=max(1, _int_env("INSTALL_AUDIT_EVENING_REPORT_MAX_JOBS", 100)),
            install_audit_first_day_collect_pct=max(0.0, _float_env("INSTALL_AUDIT_FIRST_DAY_COLLECT_PCT", 50.0)),
            install_audit_final_day_collect_pct=max(0.0, _float_env("INSTALL_AUDIT_FINAL_DAY_COLLECT_PCT", 100.0)),
            install_audit_deposit_reminder_lead_days=max(0, _int_env("INSTALL_AUDIT_DEPOSIT_REMINDER_LEAD_DAYS", 1)),
            install_audit_completion_photos_min=max(0, _int_env("INSTALL_AUDIT_COMPLETION_PHOTOS_MIN", 1)),
            install_audit_arrival_grace_min=max(0, _int_env("INSTALL_AUDIT_ARRIVAL_GRACE_MIN", 15)),
            install_audit_meal_break_after_hours=max(0.0, _float_env("INSTALL_AUDIT_MEAL_BREAK_AFTER_HOURS", 5.0)),
            install_audit_second_meal_after_hours=max(0.0, _float_env("INSTALL_AUDIT_SECOND_MEAL_AFTER_HOURS", 10.0)),
            install_audit_meal_break_min_minutes=max(0, _int_env("INSTALL_AUDIT_MEAL_BREAK_MIN_MINUTES", 30)),
            dispatcher_audit_slack_channel_id=_env("DISPATCHER_AUDIT_SLACK_CHANNEL_ID"),
            dispatcher_audit_rule_ids=_json_string_list_env("DISPATCHER_AUDIT_RULE_IDS_JSON", []),
            notifications_test_send=_bool_env("NOTIFICATIONS_TEST_SEND", False),
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
            notion_task_last_reminder_sent_property=_env("NOTION_TASK_LAST_REMINDER_SENT_PROPERTY"),
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
            slack_alert_channel_id=_env("SLACK_ALERT_CHANNEL_ID"),
            servicetitan_client_id=_env("SERVICETITAN_CLIENT_ID"),
            servicetitan_client_secret=_env("SERVICETITAN_CLIENT_SECRET"),
            servicetitan_tenant_id=_env("SERVICETITAN_TENANT_ID"),
            servicetitan_app_key=_env("SERVICETITAN_APP_KEY"),
            servicetitan_environment=service_titan_environment,
            servicetitan_base_url=service_titan_base_url,
            servicetitan_auth_url=_env("SERVICETITAN_AUTH_URL", "https://auth.servicetitan.io/connect/token"),
            servicetitan_job_url_template=_env("SERVICETITAN_JOB_URL_TEMPLATE"),
            servicetitan_project_url_template=_env("SERVICE_TITAN_PROJECT_URL_TEMPLATE", "https://go.servicetitan.com/#/project/{project_id}"),
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
            service_titan_arrival_grace_minutes=_int_env("SERVICE_TITAN_ARRIVAL_GRACE_MINUTES", 30),
            service_titan_first_call_grace_minutes=_int_env("SERVICE_TITAN_FIRST_CALL_GRACE_MINUTES", 0),
            service_titan_open_job_grace_minutes=_int_env("SERVICE_TITAN_OPEN_JOB_GRACE_MINUTES", 120),
            service_titan_min_lunch_break_minutes=_int_env("SERVICE_TITAN_MIN_LUNCH_BREAK_MINUTES", 30),
            service_titan_lunch_required_after_hours=_float_env("SERVICE_TITAN_LUNCH_REQUIRED_AFTER_HOURS", 5.0),
            service_titan_min_note_length=_int_env("SERVICE_TITAN_MIN_NOTE_LENGTH", 30),
            service_titan_require_hhr=_bool_env("SERVICE_TITAN_REQUIRE_HHR", True),
            service_titan_require_equipment_registration=_bool_env("SERVICE_TITAN_REQUIRE_EQUIPMENT_REGISTRATION", True),
            service_titan_min_repair_options=_int_env("SERVICE_TITAN_MIN_REPAIR_OPTIONS", 3),
            service_titan_require_home_comfort_plan_option=_bool_env("SERVICE_TITAN_REQUIRE_HOME_COMFORT_PLAN_OPTION", True),
            service_titan_po_reconcile_within_hours=_int_env("SERVICE_TITAN_PO_RECONCILE_WITHIN_HOURS", 24),
            service_titan_alert_include_customer_name=_bool_env("SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME", False),
            sales_comfort_advisor_audit_enabled=_bool_env("SALES_COMFORT_ADVISOR_AUDIT_ENABLED", True),
            hvac_service_audit_enabled=_bool_env("HVAC_SERVICE_AUDIT_ENABLED", False),
            plumbing_service_audit_enabled=_bool_env("PLUMBING_SERVICE_AUDIT_ENABLED", False),
            technician_compliance_enabled=_bool_env("TECHNICIAN_COMPLIANCE_ENABLED", False),
            dispatcher_audit_enabled=_bool_env("DISPATCHER_AUDIT_ENABLED", False),
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
            service_titan_diagnostic_fee_keywords=_json_list_env(
                "SERVICE_TITAN_DIAGNOSTIC_FEE_KEYWORDS_JSON",
                ["diagnostic"],
            ),
            service_titan_home_comfort_plan_keywords=_json_list_env(
                "SERVICE_TITAN_HOME_COMFORT_PLAN_KEYWORDS_JSON",
                ["home comfort plan", "comfort plan", "membership", "maintenance plan"],
            ),
            service_titan_hhr_keywords=_json_list_env(
                "SERVICE_TITAN_HHR_KEYWORDS_JSON",
                ["home health report", "hhr", "report card"],
            ),
            service_titan_special_order_required_note_fields=_json_list_env(
                "SERVICE_TITAN_SPECIAL_ORDER_REQUIRED_NOTE_FIELDS_JSON",
                ["purchase order number", "ordering date", "employee ordered", "eta", "supply house"],
            ),
            service_titan_disabled_rule_ids=_json_list_env("SERVICE_TITAN_DISABLED_RULE_IDS_JSON"),
            service_titan_required_phases=_json_list_env("SERVICE_TITAN_REQUIRED_PHASES_JSON"),
            service_titan_required_operational_fields=_json_list_env("SERVICE_TITAN_REQUIRED_OPERATIONAL_FIELDS_JSON"),
            service_titan_rule_scope_config=_json_object_env("SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON"),
            service_titan_business_unit_labels={
                **DEFAULT_SERVICE_TITAN_BUSINESS_UNIT_LABELS,
                **_json_map_env("SERVICE_TITAN_BUSINESS_UNIT_LABELS_JSON"),
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

    def missing_service_titan_credentials(self, *, require_enabled: bool = True) -> list[str]:
        if require_enabled and not self.service_titan_audit_enabled:
            return []
        required = {
            "SERVICETITAN_CLIENT_ID": self.servicetitan_client_id,
            "SERVICETITAN_CLIENT_SECRET": self.servicetitan_client_secret,
            "SERVICETITAN_TENANT_ID": self.servicetitan_tenant_id,
            "SERVICETITAN_APP_KEY": self.servicetitan_app_key,
        }
        if not self.service_titan_audit_dry_run:
            required["SLACK_BOT_TOKEN"] = self.slack_bot_token
            required["SLACK_ALERT_CHANNEL_ID"] = self.slack_alert_channel_id
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
