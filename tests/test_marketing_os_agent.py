from __future__ import annotations

import tempfile
import unittest
import logging
import os
import re
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from logging import LogRecord
from unittest.mock import patch

from marketing_os_agent.clients.email_client import EmailClient
from marketing_os_agent.clients.claude import ClaudeClient
from marketing_os_agent.clients.http import DEFAULT_USER_AGENT, HttpResponse
from marketing_os_agent.clients.notion import NotionApiError, NotionClient
from marketing_os_agent.clients.servicetitan import (
    ServiceTitanApiError,
    ServiceTitanClient,
    ServiceTitanJob,
    ServiceTitanProject,
    ServiceTitanProjectTask,
    _install_prefilter_matches,
    install_strict_scope_failed_gates,
    parse_service_titan_project,
)
from marketing_os_agent.clients.slack import SlackClient
from marketing_os_agent.config import Settings, load_dotenv
from marketing_os_agent.app import AgentApp
from marketing_os_agent.__main__ import _settings_error_diagnostics_text
from marketing_os_agent.domain.campaign_health import CampaignHealthService
from marketing_os_agent.domain.install_audit import INSTALL_FAIL, INSTALL_PASS, INSTALL_SKIP, InstallAuditService, active_install_audit_rules
from marketing_os_agent.domain.owner_mapping import OwnerResolver
from marketing_os_agent.domain.pm_audit import PM_FAIL, PM_PASS, PM_SKIP, PMAuditService
from marketing_os_agent.domain.reports import ReportService, month_bounds, select_campaigns_starting_between, week_bounds
from marketing_os_agent.domain.service_titan_audit import (
    ServiceTitanAuditLoop,
    ServiceTitanAuditService,
    ServiceTitanAuditSummary,
    ServiceTitanWeeklySummaryService,
)
from marketing_os_agent.domain.service_titan_handbook import handbook_rule_matrix
from marketing_os_agent.domain.service_titan_discovery import ServiceTitanScopeDiscovery
from marketing_os_agent.domain.service_titan_rules import (
    RESULT_FAIL,
    RESULT_INSUFFICIENT,
    RESULT_NOT_APPLICABLE,
    RESULT_PASS,
    active_service_titan_rules,
    handbook_audit_rules,
)
from marketing_os_agent.domain.task_processor import TaskProcessor
from marketing_os_agent.models import Campaign, Owner, Task
from marketing_os_agent.persistence import Persistence


logging.disable(logging.CRITICAL)


def settings(sqlite_path: str, **overrides: object) -> Settings:
    base = dict(
        app_env="test",
        port=8080,
        timezone="UTC",
        log_level="CRITICAL",
        sqlite_path=sqlite_path,
        poll_interval_seconds=120,
        poll_overlap_seconds=3600,
        task_reminder_minutes_before=60,
        task_date_only_deadline_hour=17,
        service_titan_audit_enabled=False,
        service_titan_audit_poll_interval_seconds=300,
        service_titan_audit_startup_delay_seconds=300,
        service_titan_audit_lookback_minutes=240,
        service_titan_audit_overlap_seconds=300,
        service_titan_audit_max_pages=5,
        service_titan_audit_page_size=100,
        service_titan_audit_max_alerts_per_cycle=25,
        service_titan_audit_timezone="UTC",
        service_titan_audit_dry_run=False,
        service_titan_audit_backfill_alerts=True,
        service_titan_audit_ignore_checkpoint_once=False,
        service_titan_audit_debug_fields=False,
        service_titan_weekly_summary_enabled=False,
        service_titan_weekly_summary_day="MON",
        service_titan_weekly_summary_hour=8,
        service_titan_weekly_summary_lookback_days=7,
        pm_audit_enabled=False,
        pm_audit_schedule_enabled=False,
        pm_audit_run_on_startup=False,
        pm_audit_dry_run=True,
        pm_audit_status_stale_days=14,
        pm_audit_task_overdue_days=3,
        pm_audit_pm_assignment_grace_hours=24,
        pm_audit_task_template_grace_hours=48,
        pm_audit_project_page_size=50,
        pm_audit_max_projects=100,
        pm_audit_max_tasks=500,
        pm_audit_run_hour=8,
        pm_audit_run_minute=0,
        pm_audit_weekdays_only=True,
        pm_audit_enabled_rule_ids=[],
        pm_audit_install_business_unit_ids=["1809", "64313020", "64569731"],
        pm_audit_install_business_unit_names=["HVAC - Install", "Plumbing - Install", "Electrical - Install"],
        pm_audit_include_client_name=False,
        pm_audit_sold_by_field_names=["Sold By", "Sold by", "Comfort Advisor", "Sold By CA"],
        pm_audit_permit_field_names=["PERMIT", "Permit", "Permit Number", "Permit #", "Permit Status"],
        pm_audit_hoa_field_names=["HOA Approval", "Under HOA", "HOA Status", "HOA"],
        pm_audit_hoa_zip_list=[],
        pm_audit_asbestos_field_names=["Asbestos", "Asbestos Status", "Asbestos Check"],
        pm_audit_asbestos_year_cutoff=None,
        pm_audit_review_requested_field_names=["Review Requested", "Review request", "Review Sent"],
        pm_audit_on_hold_max_days=30,
        pm_audit_on_hold_reason_field_names=["On Hold Reason", "Hold Reason", "Hold Notes"],
        pm_audit_homeowner_auth_within_hours=2,
        pm_audit_homeowner_auth_form_names=["Homeowner Authorization", "Homeowner Authorization Form"],
        pm_audit_completion_report_form_names=["Installation Completion Report", "Completion Report"],
        pm_audit_equipment_field_names=["Equipment Registered", "Equipment Status", "Equipment Registration"],
        pm_audit_deposit_fixed_amount=1000.0,
        pm_audit_deposit_percent=0.10,
        pm_audit_deposit_before_install_days=7,
        pm_audit_deposit_rounding_tolerance=5.0,
        pm_audit_deposit_line_item_names=["Deposit", "Project Deposit", "Installation Deposit"],
        pm_audit_deposit_payment_status_values=["Paid", "Posted", "Succeeded", "Completed", "Received"],
        pm_audit_permit_before_install_days=10,
        pm_audit_project_left_open_days=7,
        pm_audit_rebate_field_names=["Rebate", "Rebate Status", "Rebate Approval"],
        pm_audit_crew_field_names=["Crew", "Install Crew", "Team"],
        pm_audit_change_order_field_names=["Change Order", "Change Order Approval", "Additional Work Approval", "Written Approval"],
        pm_audit_slack_channel_id="",
        pm_audit_test_send=False,
        install_audit_enabled=False,
        install_audit_dry_run=True,
        install_audit_run_on_startup=False,
        install_audit_schedule_enabled=False,
        install_audit_slack_channel_id="",
        install_audit_job_type_match_keywords=["Installation"],
        install_audit_business_unit_names=["Electrical - Install", "HVAC - Install", "Plumbing - Install"],
        install_audit_business_unit_ids=["1809", "64313020"],
        install_audit_rule_ids=[],
        install_audit_max_appointments=100,
        install_audit_lookback_days=14,
        install_audit_lookahead_days=2,
        install_audit_run_hour=8,
        install_audit_run_minute=0,
        install_audit_weekdays_only=True,
        install_audit_first_day_collect_pct=50.0,
        install_audit_final_day_collect_pct=100.0,
        install_audit_deposit_reminder_lead_days=1,
        install_audit_completion_photos_min=1,
        install_audit_arrival_grace_min=15,
        install_audit_meal_break_after_hours=5.0,
        install_audit_second_meal_after_hours=10.0,
        install_audit_meal_break_min_minutes=30,
        notifications_test_send=False,
        anthropic_api_key="",
        claude_model="claude-test",
        notion_api_key="notion",
        notion_api_version="2026-03-11",
        notion_tasks_database_id="tasks",
        notion_tasks_data_source_id="tasks-source",
        notion_marketing_calendar_database_id="campaigns",
        notion_marketing_calendar_data_source_id="campaigns-source",
        notion_workbooks_page_id="workbooks-page",
        notion_workbooks_database_id="workbooks-db",
        notion_workbooks_data_source_id="workbooks-source",
        notion_needs_verification_property="Needs Verification",
        notion_task_last_reminder_sent_property="",
        notion_child_tasks_property="Child Tasks",
        notion_dependencies_property="Dependencies",
        notion_task_name_property="Task name",
        notion_task_owner_property="Owner",
        notion_task_deadline_property="Deadline",
        notion_task_original_deadline_property="Original Deadline",
        notion_task_status_property="Status",
        notion_task_priority_property="Priority",
        notion_task_department_property="Department",
        notion_task_campaign_property="Linked Campaign",
        notion_task_deliverable_property="Deliverable link",
        notion_task_notes_property="Notes / Issues",
        notion_task_needs_from_others_property="Needs From Others",
        notion_task_created_property="Created",
        notion_task_last_edited_property="Last Edited",
        notion_campaign_name_property="Campaign name",
        notion_campaign_trade_property="Trade",
        notion_campaign_channel_property="Channel",
        notion_campaign_start_date_property="Start Date",
        notion_campaign_end_date_property="End Date",
        notion_campaign_owner_property="Owner",
        notion_campaign_status_property="Status",
        notion_campaign_planned_spend_property="Planned Spend",
        notion_campaign_expected_leads_property="Expected Leads",
        notion_campaign_expected_cpl_property="Expected CPL",
        notion_campaign_expected_roi_property="Expected ROI",
        notion_campaign_actual_spend_property="Actual Spend",
        notion_campaign_actual_leads_property="Actual Leads",
        notion_campaign_actual_cpl_property="Actual CPL",
        notion_campaign_actual_roi_property="Actual ROI",
        notion_campaign_linked_tasks_property="Linked Tasks",
        notion_campaign_linked_workbook_property="Linked Workbook",
        notion_campaign_notes_property="Notes",
        slack_bot_token="slack",
        slack_signing_secret="secret",
        slack_marketing_ops_channel_id="COPS",
        slack_tim_user_id="UTIM",
        slack_alert_channel_id="CST",
        servicetitan_client_id="st-client",
        servicetitan_client_secret="st-secret",
        servicetitan_tenant_id="12345",
        servicetitan_app_key="ak1.test",
        servicetitan_environment="production",
        servicetitan_base_url="https://api.servicetitan.io",
        servicetitan_auth_url="https://auth.servicetitan.io/connect/token",
        servicetitan_job_url_template="https://go.servicetitan.com/#/Job/Index/{job_id}",
        servicetitan_project_url_template="https://go.servicetitan.com/#/project/{project_id}",
        smtp_host="",
        smtp_port=587,
        smtp_user="",
        smtp_pass="",
        email_from="",
        tim_email="tim@example.com",
        vadim_email="vadim@example.com",
        budget_overrun_threshold_percent=0.0,
        campaign_risk_window_percent=80.0,
        campaign_risk_task_completion_percent=20.0,
        service_titan_arrival_grace_minutes=30,
        service_titan_first_call_grace_minutes=0,
        service_titan_open_job_grace_minutes=120,
        service_titan_min_lunch_break_minutes=30,
        service_titan_lunch_required_after_hours=5.0,
        service_titan_min_note_length=30,
        service_titan_require_hhr=True,
        service_titan_require_equipment_registration=True,
        service_titan_min_repair_options=3,
        service_titan_require_home_comfort_plan_option=True,
        service_titan_po_reconcile_within_hours=24,
        service_titan_alert_include_customer_name=False,
        sales_comfort_advisor_audit_enabled=True,
        hvac_service_audit_enabled=False,
        plumbing_service_audit_enabled=False,
        technician_compliance_enabled=True,
        dispatcher_audit_enabled=True,
        dispatcher_audit_slack_channel_id="",
        dispatcher_audit_rule_ids=[],
        owner_slack_map={"Emil": "UEMIL", "Vadim": "UVADIM"},
        owner_email_map={},
        task_status_map={
            "not started": "Not Started",
            "in progress": "In Progress",
            "done": "Completed",
            "blocked": "Blocked",
            "delayed": "Delayed",
            "canceled": "Canceled",
        },
        task_priority_map={"urgent": "Critical", "critical": "Critical"},
        service_titan_diagnostic_fee_keywords=["diagnostic"],
        service_titan_home_comfort_plan_keywords=["home comfort plan", "comfort plan", "membership", "maintenance plan"],
        service_titan_hhr_keywords=["home health report", "hhr", "report card"],
        service_titan_special_order_required_note_fields=["purchase order number", "ordering date", "employee ordered", "eta", "supply house"],
        service_titan_disabled_rule_ids=[],
        service_titan_required_phases=["diagnosis", "estimate"],
        service_titan_required_operational_fields=["System Type"],
        service_titan_rule_scope_config={},
        service_titan_business_unit_labels={
            "1812": "HVAC Sales / Comfort Advisors",
            "64326403": "Plumbing Sales",
            "64315277": "Plumbing Service",
        },
    )
    base.update(overrides)
    return Settings(**base)


def owner(name: str = "Emil") -> Owner:
    return Owner(notion_user_id=f"notion-{name.lower()}", name=name, email=f"{name.lower()}@example.com")


def task(
    task_id: str,
    status: str,
    *,
    name: str = "Task",
    owner_name: str | None = "Emil",
    deadline: date | None = date(2026, 5, 15),
    original_deadline: date | None = None,
    deadline_at: datetime | None = None,
    last_reminder_sent_at: datetime | None = None,
    deliverable_link: str = "",
    notes: str = "",
    needs: str = "",
    edited: datetime | None = None,
    campaigns: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        name=name,
        owner=owner(owner_name) if owner_name else None,
        deadline=deadline,
        original_deadline=deadline if original_deadline is None else original_deadline,
        status=status,
        priority="High",
        department="Marketing",
        linked_campaign_ids=campaigns or [],
        deliverable_link=deliverable_link,
        notes_issues=notes,
        needs_from_others=needs,
        created_time=datetime(2026, 5, 1, tzinfo=timezone.utc),
        last_edited_time=edited or datetime(2026, 5, 14, 12, tzinfo=timezone.utc),
        url=f"https://notion.so/{task_id}",
        deadline_at=deadline_at,
        last_reminder_sent_at=last_reminder_sent_at,
    )


def campaign(
    campaign_id: str,
    *,
    start: date = date(2026, 5, 1),
    end: date = date(2026, 5, 31),
    planned: float | None = 1000,
    actual: float | None = 0,
    linked_tasks: list[str] | None = None,
) -> Campaign:
    return Campaign(
        id=campaign_id,
        name="May Campaign",
        trade=["HVAC"],
        channel=["Google Ads"],
        start_date=start,
        end_date=end,
        owner=owner("Emil"),
        status="In Flight",
        planned_spend=planned,
        expected_leads=10,
        expected_roi=100,
        actual_spend=actual,
        actual_leads=0,
        actual_roi=0,
        linked_task_ids=linked_tasks or [],
    )


def st_job(
    job_id: str = "1001",
    *,
    status: str = "Completed",
    appointment_id: str = "2001",
    technician_id: str = "tech-1",
    technician_name: str = "Tech One",
    dispatcher_id: str = "disp-1",
    dispatcher_name: str = "Dispatcher One",
    business_unit_id: str = "bu-service",
    business_unit_name: str = "HVAC Service",
    job_type_id: str = "jt-diagnostic",
    job_type_name: str = "Diagnostic Service",
    department: str = "Service",
    trade: str = "HVAC",
    workflow: str = "Service Call",
    tag_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    campaign_id: str = "",
    campaign_name: str = "",
    cancellation_reason: str = "",
    customer_name: str = "",
    modified_on: datetime | None = None,
    clock_in_at: datetime | None = datetime(2026, 5, 15, 9, tzinfo=timezone.utc),
    clock_out_at: datetime | None = datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
    lunch_break_minutes: int | None = 30,
    line_items: list[str] | None = None,
    invoice_items: list[dict[str, object]] | None = None,
    invoice_status: str = "Paid",
    invoice_total: float | None = 300.0,
    invoice_balance: float | None = 0.0,
    payment_total: float | None = 300.0,
    payments_count: int | None = 1,
    diagnostic_fee_present: bool | None = True,
    diagnostic_fee_charged: bool | None = True,
    diagnostic_fee_waived: bool | None = False,
    repair_sold: bool | None = False,
    completed_phases: list[str] | None = None,
    operational_data: dict[str, str] | None = None,
    operational_data_complete: bool | None = True,
    options_presented: bool | None = True,
    estimate_count: int | None = 3,
    same_day_estimate_present: bool | None = True,
    home_comfort_plan_option_present: bool | None = True,
    notes: str | None = "Completed diagnostic and presented options.",
    photo_count: int | None = 2,
    supporting_evidence_count: int | None = 1,
    forms_count: int | None = 1,
    hhr_completed: bool | None = True,
    equipment_count: int | None = 1,
    equipment_complete: bool | None = True,
    authorization_count: int | None = 1,
    follow_up_needed: bool | None = False,
    follow_up_task_present: bool | None = False,
    special_order_detected: bool | None = False,
    special_order_missing_fields: list[str] | None = None,
    special_order_reminder_present: bool | None = False,
    downpayment_recorded: bool | None = False,
    lead_turnover_required: bool | None = False,
    lead_turnover_documented: bool | None = False,
    purchase_orders: list[dict[str, object]] | None = None,
    purchase_orders_count: int | None = 1,
    po_received_not_reconciled_count: int | None = 0,
    po_missing_vendor_document_count: int | None = 0,
    po_missing_attachment_count: int | None = 0,
    po_not_synced_count: int | None = None,
    ply_data_available: bool = False,
    scope_change_detected: bool | None = False,
    scope_change_escalated: bool | None = False,
    cancellation_after_materials_detected: bool | None = False,
    cancellation_escalated: bool | None = False,
    defective_part_detected: bool | None = False,
    warranty_claim_documented: bool | None = False,
    arrival_window_start: datetime | None = datetime(2026, 5, 15, 9, tzinfo=timezone.utc),
    arrival_window_end: datetime | None = datetime(2026, 5, 15, 11, tzinfo=timezone.utc),
    arrived_at: datetime | None = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc),
    present_fields: set[str] | None = None,
) -> ServiceTitanJob:
    fields = present_fields or {
        "status",
        "technician",
        "dispatcher",
        "clock_in",
        "clock_out",
        "lunch_break",
        "invoice_line_items",
        "completed_phases",
        "operational_data",
        "operational_data_fields",
        "options_presented",
        "estimates",
        "same_day_estimate",
        "home_comfort_plan_option",
        "notes",
        "photos",
        "supporting_evidence",
        "forms",
        "hhr",
        "equipment",
        "authorization",
        "payments",
        "purchase_orders",
        "po_vendor_document",
        "po_attachments",
        "po_reconciliation",
        "arrival_window",
        "arrived_at",
        "business_unit",
        "job_type",
        "department",
        "trade",
        "workflow",
        "tags",
        "campaign",
    }
    return ServiceTitanJob(
        job_id=job_id,
        job_number=f"J-{job_id}",
        status=status,
        modified_on=modified_on or datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        completed_on=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        appointment_id=appointment_id,
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
        tag_ids=tag_ids or [],
        tag_names=tag_names or [],
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        cancellation_reason=cancellation_reason,
        customer_name=customer_name,
        arrival_window_start=arrival_window_start,
        arrival_window_end=arrival_window_end,
        arrived_at=arrived_at,
        clock_in_at=clock_in_at,
        clock_out_at=clock_out_at,
        lunch_break_minutes=lunch_break_minutes,
        invoice_line_items=line_items if line_items is not None else ["Diagnostic Fee", "Capacitor"],
        invoice_items=invoice_items if invoice_items is not None else [{"name": "Diagnostic Fee", "amount": 89.0}],
        invoice_status=invoice_status,
        invoice_total=invoice_total,
        invoice_balance=invoice_balance,
        payment_total=payment_total,
        payments_count=payments_count,
        diagnostic_fee_present=diagnostic_fee_present,
        diagnostic_fee_charged=diagnostic_fee_charged,
        diagnostic_fee_waived=diagnostic_fee_waived,
        repair_sold=repair_sold,
        completed_phases=completed_phases if completed_phases is not None else ["diagnosis", "estimate"],
        operational_data=operational_data if operational_data is not None else {"System Type": "Split"},
        operational_data_complete=operational_data_complete,
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
        special_order_missing_fields=special_order_missing_fields or [],
        special_order_reminder_present=special_order_reminder_present,
        downpayment_recorded=downpayment_recorded,
        lead_turnover_required=lead_turnover_required,
        lead_turnover_documented=lead_turnover_documented,
        purchase_orders=purchase_orders or [{"id": "po-1", "status": "received", "received": True, "reconciled": True}],
        purchase_orders_count=purchase_orders_count,
        po_received_not_reconciled_count=po_received_not_reconciled_count,
        po_missing_vendor_document_count=po_missing_vendor_document_count,
        po_missing_attachment_count=po_missing_attachment_count,
        po_not_synced_count=po_not_synced_count,
        ply_data_available=ply_data_available,
        scope_change_detected=scope_change_detected,
        scope_change_escalated=scope_change_escalated,
        cancellation_after_materials_detected=cancellation_after_materials_detected,
        cancellation_escalated=cancellation_escalated,
        defective_part_detected=defective_part_detected,
        warranty_claim_documented=warranty_claim_documented,
        url=f"https://go.servicetitan.com/#/Job/Index/{job_id}",
        present_fields=fields,
        raw={"id": job_id},
    )


def install_job(
    job_id: str = "install-1001",
    *,
    status: str = "In Progress",
    business_unit_id: str = "1809",
    business_unit_name: str = "HVAC - Install",
    job_type_id: str = "1930",
    job_type_name: str = "HVAC Installation",
    technician_name: str = "Install Lead",
    start: datetime | None = datetime(2026, 6, 24, 8, tzinfo=timezone.utc),
    end: datetime | None = datetime(2026, 6, 24, 16, tzinfo=timezone.utc),
    appointments: list[dict[str, object]] | None = None,
    forms: list[dict[str, object]] | None = None,
    forms_available: bool = True,
    arrived_at: datetime | None = datetime(2026, 6, 24, 8, tzinfo=timezone.utc),
    arrived_field_available: bool = True,
    invoices: list[dict[str, object]] | None = None,
    invoice_total: float | None = 10000.0,
    invoice_balance: float | None = 10000.0,
    payment_total: float | None = 0.0,
    financing: str | None = "No",
    deposit_waived: str | None = None,
    photo_count: int | None = 1,
    photos_available: bool = True,
    equipment_count: int | None = 1,
    equipment_complete: bool | None = True,
    equipment_available: bool = True,
    purchase_orders_count: int | None = 1,
    purchase_orders_available: bool = True,
    ply_data_available: bool = True,
    clock_in_at: datetime | None = datetime(2026, 6, 24, 8, tzinfo=timezone.utc),
    clock_out_at: datetime | None = datetime(2026, 6, 24, 16, tzinfo=timezone.utc),
    lunch_break_minutes: int | None = 30,
    time_available: bool = True,
    review_requested: str | None = "Sent",
    job_progress: object | None = None,
    raw_extra: dict[str, object] | None = None,
) -> ServiceTitanJob:
    if appointments is None and start is not None:
        appointments = [{"id": f"appt-{job_id}", "arrivalWindowStart": start.isoformat(), "arrivalWindowEnd": end.isoformat() if end else ""}]
    if forms is None and forms_available:
        forms = [
            {"name": "Homeowner Authorization Form", "status": "Completed"},
            {"name": "Installation Completion Form", "status": "Completed"},
        ]
    custom_fields: list[dict[str, object]] = []
    if financing is not None:
        custom_fields.append({"name": "Financing", "value": financing})
    if deposit_waived is not None:
        custom_fields.append({"name": "Deposit Waived", "value": deposit_waived})
    if review_requested is not None:
        custom_fields.append({"name": "Review Requested", "value": review_requested})
    raw = {
        "id": job_id,
        "appointments": appointments or [],
        "invoices": invoices if invoices is not None else [],
        "customFields": custom_fields,
        **(raw_extra or {}),
    }
    if job_progress is not None:
        raw["progress"] = job_progress
    if forms_available:
        raw["forms"] = forms or []
    present_fields = {"status", "business_unit", "job_type", "arrival_window", "technician"}
    if arrived_field_available:
        present_fields.add("arrived_at")
    if forms_available:
        present_fields.add("forms")
    if photos_available:
        present_fields.add("photos")
    if equipment_available:
        present_fields.add("equipment")
    if purchase_orders_available:
        present_fields.add("purchase_orders")
    if invoice_total is not None or invoice_balance is not None or payment_total is not None or invoices is not None:
        present_fields.update({"payments", "invoice_line_items", "invoice_status"})
    if time_available:
        present_fields.update({"clock_in", "clock_out", "lunch_break"})
    return replace(
        st_job(
            job_id,
            status=status,
            technician_name=technician_name,
            business_unit_id=business_unit_id,
            business_unit_name=business_unit_name,
            job_type_id=job_type_id,
            job_type_name=job_type_name,
            department="Install",
            trade="HVAC",
            workflow="Install",
            arrival_window_start=start,
            arrival_window_end=end,
            arrived_at=arrived_at,
            invoice_total=invoice_total,
            invoice_balance=invoice_balance,
            payment_total=payment_total,
            payments_count=1 if payment_total else 0,
            photo_count=photo_count,
            equipment_count=equipment_count,
            equipment_complete=equipment_complete,
            purchase_orders_count=purchase_orders_count,
            ply_data_available=ply_data_available,
            clock_in_at=clock_in_at,
            clock_out_at=clock_out_at,
            lunch_break_minutes=lunch_break_minutes,
            present_fields=present_fields,
        ),
        raw=raw,
        operational_data={str(item["name"]): str(item["value"]) for item in custom_fields},
    )


def sales_job(job_id: str = "sales-1001", **overrides: object) -> ServiceTitanJob:
    base = dict(
        business_unit_id="bu-sales",
        business_unit_name="Sales",
        job_type_id="jt-comfort-advisor",
        job_type_name="Comfort Advisor",
        department="Sales",
        trade="Sales",
        workflow="Sales Consultation",
        technician_id="advisor-1",
        technician_name="Advisor One",
        dispatcher_id="",
        dispatcher_name="",
        status="Completed",
    )
    base.update(overrides)
    return st_job(job_id, **base)


def hvac_job(job_id: str = "hvac-1001", **overrides: object) -> ServiceTitanJob:
    base = dict(
        business_unit_id="bu-hvac",
        business_unit_name="HVAC Service",
        job_type_id="jt-hvac-diagnostic",
        job_type_name="HVAC Diagnostic Service",
        department="Service",
        trade="HVAC",
        workflow="HVAC Service",
        technician_id="tech-hvac-1",
        technician_name="HVAC Tech One",
        dispatcher_id="",
        dispatcher_name="",
        status="Completed",
    )
    base.update(overrides)
    return st_job(job_id, **base)


def plumbing_job(job_id: str = "plumbing-1001", **overrides: object) -> ServiceTitanJob:
    base = dict(
        business_unit_id="64315277",
        business_unit_name="Plumbing - Service",
        job_type_id="112338076",
        job_type_name="Plumbing Diagnostic",
        department="Service",
        trade="Plumbing",
        workflow="",
        technician_id="tech-plumbing-1",
        technician_name="Plumbing Tech One",
        dispatcher_id="",
        dispatcher_name="",
        status="Completed",
    )
    base.update(overrides)
    return st_job(job_id, **base)


def pm_task(task_id: str = "task-1", **overrides: object) -> ServiceTitanProjectTask:
    base = dict(
        task_id=task_id,
        task_number=f"T-{task_id}",
        project_id="pm-1001",
        job_id="",
        job_number="",
        name="PM Pre-Scheduling",
        assigned_to_id="pm-1",
        assigned_to_name="Jane",
        due_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        created_on=datetime(2026, 6, 18, tzinfo=timezone.utc),
        modified_on=datetime(2026, 6, 18, tzinfo=timezone.utc),
        closed_on=None,
        status="To Do",
        is_closed=False,
        raw={"id": task_id},
    )
    base.update(overrides)
    return ServiceTitanProjectTask(**base)


def pm_project(project_id: str = "pm-1001", **overrides: object) -> ServiceTitanProject:
    base = dict(
        project_id=project_id,
        project_number=project_id,
        project_type_id="63812999",
        project_type_name="Standard Install",
        status_id="22936527",
        status="Scheduled",
        sub_status_id="",
        created_on=datetime(2026, 6, 18, tzinfo=timezone.utc),
        modified_on=datetime(2026, 6, 20, tzinfo=timezone.utc),
        start_date=datetime(2026, 6, 28, tzinfo=timezone.utc),
        target_completion_date=None,
        actual_completion_date=None,
        business_unit_ids=["1809"],
        job_ids=["job-1"],
        project_manager_ids=["pm-1"],
        project_manager_names=["Jane"],
        custom_fields={"Sold by": "Advisor One", "Permit": "PERMIT-1"},
        custom_fields_available=True,
        tasks=[pm_task(project_id=project_id)],
        tasks_available=True,
        url=f"https://go.servicetitan.com/#/project/{project_id}",
        raw={"id": project_id, "projectTypeId": "63812999", "status": "Scheduled"},
    )
    base.update(overrides)
    return ServiceTitanProject(**base)


def sales_job_payload(
    job_id: str = "sales-1001",
    *,
    estimate_ids: list[str] | None = None,
    business_unit_id: str = "bu-sales",
    business_unit_name: str = "Sales",
    job_type_id: str = "jt-comfort-advisor",
    job_type_name: str = "Comfort Advisor",
    department_name: str = "Sales",
    trade: str = "Sales",
    include_workflow: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": job_id,
        "jobNumber": f"J-{job_id}",
        "status": "Completed",
        "modifiedOn": "2026-05-15T16:00:00Z",
        "completedOn": "2026-05-15T16:00:00Z",
        "businessUnitId": business_unit_id,
        "jobTypeId": job_type_id,
    }
    if department_name:
        payload["departmentName"] = department_name
    if trade:
        payload["trade"] = trade
    if business_unit_name:
        payload["businessUnit"] = {"id": business_unit_id, "name": business_unit_name}
    if job_type_name:
        payload["jobType"] = {"id": job_type_id, "name": job_type_name}
    if include_workflow:
        payload["workflow"] = "Sales Consultation"
    if estimate_ids is not None:
        payload["estimateIds"] = estimate_ids
    return payload


class FakeNotion:
    def __init__(self) -> None:
        self.available = True
        self.comments: list[tuple[str, str, bool]] = []
        self.flags: list[tuple[str, bool]] = []
        self.tasks: list[Task] = []
        self.campaigns: list[Campaign] = []
        self.original_deadlines: list[str] = []
        self.last_reminders: list[tuple[str, datetime]] = []

    def add_task_comment(self, task: Task, text: str, mention_owner: bool = False) -> None:
        self.comments.append((task.id, text, mention_owner))

    def set_needs_verification(self, task_id: str, value: bool) -> None:
        self.flags.append((task_id, value))

    def set_original_deadline_if_missing(self, task: Task) -> None:
        self.original_deadlines.append(task.id)

    def set_last_reminder_sent_at(self, task_id: str, sent_at: datetime) -> None:
        self.last_reminders.append((task_id, sent_at))

    def query_all_campaigns(self) -> list[Campaign]:
        return self.campaigns

    def query_all_tasks(self) -> list[Task]:
        return self.tasks

    def query_tasks_modified_since(self, _since: str | None) -> list[Task]:
        return self.tasks


class SequencedNotion(FakeNotion):
    def __init__(self, all_tasks: list[Task], modified_tasks: list[Task]) -> None:
        super().__init__()
        self.all_tasks = all_tasks
        self.modified_tasks = modified_tasks

    def query_all_tasks(self) -> list[Task]:
        return self.all_tasks

    def query_tasks_modified_since(self, _since: str | None) -> list[Task]:
        return self.modified_tasks


class BrokenNotion(FakeNotion):
    def query_tasks_modified_since(self, _since: str | None) -> list[Task]:
        raise NotionApiError(404, {"code": "object_not_found", "message": "Could not find database"})


class FakeHttp:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.requests: list[dict[str, object]] = []

    def request_json(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: dict[str, object] | None = None) -> HttpResponse:
        self.calls.append(url)
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "body": body or {}, "kind": "json"})
        return self.responses.pop(0)

    def request_form(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: dict[str, str] | None = None) -> HttpResponse:
        self.calls.append(url)
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "body": body or {}, "kind": "form"})
        return self.responses.pop(0)


class FakeSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str | None]] = []
        self.dms: list[tuple[str, str]] = []
        self.email_lookup: dict[str, str] = {}
        self.email_lookup_calls: list[str] = []
        self.counter = 0
        self.auth_ok = True

    def auth_test(self) -> dict[str, object] | None:
        if not self.auth_ok:
            return None
        return {"ok": True, "team": "Test Team", "user": "test-bot"}

    def post_message(self, channel: str, text: str, blocks: object | None = None, thread_ts: str | None = None) -> str:
        self.counter += 1
        self.messages.append((channel, text, thread_ts))
        return f"{self.counter}.000"

    def reply(self, channel: str, thread_ts: str, text: str) -> str:
        return self.post_message(channel, text, thread_ts=thread_ts)

    def dm_user(self, user_id: str, text: str) -> str:
        self.dms.append((user_id, text))
        return f"dm-{len(self.dms)}"

    def lookup_user_by_email(self, email: str) -> str | None:
        self.email_lookup_calls.append(email)
        return self.email_lookup.get(email)


class FailingSlack(FakeSlack):
    def post_message(self, channel: str, text: str, blocks: object | None = None, thread_ts: str | None = None) -> str | None:
        self.messages.append((channel, text, thread_ts))
        return None


class FailingDMSlack(FakeSlack):
    def dm_user(self, user_id: str, text: str) -> str | None:
        self.dms.append((user_id, text))
        return None


class FakeServiceTitan:
    def __init__(self, jobs: list[ServiceTitanJob] | None = None, fail: bool = False) -> None:
        self.jobs = jobs or []
        self.fail = fail
        self.since_seen: datetime | None = None

    def query_recent_jobs(self, modified_on_or_after: datetime) -> list[ServiceTitanJob]:
        self.since_seen = modified_on_or_after
        if self.fail:
            raise ServiceTitanApiError(503, {"message": "unavailable"})
        return self.jobs


class FakePMServiceTitan:
    def __init__(self, projects: list[ServiceTitanProject] | None = None, fail: bool = False) -> None:
        self.projects = projects or []
        self.fail = fail
        self.query_kwargs: dict[str, object] = {}
        self.last_pm_audit_stats: dict[str, int] = {
            "raw_projects_fetched": len(self.projects),
            "in_scope_projects": len(self.projects),
            "skipped_out_of_scope": 0,
            "projects_evaluated": len(self.projects),
            "tasks_loaded": sum(len(project.tasks) for project in self.projects),
        }

    def query_pm_projects(self, **kwargs: object) -> list[ServiceTitanProject]:
        self.query_kwargs = kwargs
        if self.fail:
            raise ServiceTitanApiError(503, {"message": "unavailable"})
        return self.projects


class FakeInstallServiceTitan:
    def __init__(self, jobs: list[ServiceTitanJob] | None = None, fail: bool = False) -> None:
        self.jobs = jobs or []
        self.fail = fail
        self.query_kwargs: dict[str, object] = {}
        self.last_install_audit_stats: dict[str, int] = {
            "raw_jobs_fetched": len(self.jobs),
            "jobs_skipped_out_of_scope": 0,
            "jobs_enriched": len(self.jobs),
        }

    def query_install_audit_jobs(self, **kwargs: object) -> list[ServiceTitanJob]:
        self.query_kwargs = kwargs
        if self.fail:
            raise ServiceTitanApiError(503, {"message": "unavailable"})
        jobs = list(self.jobs)
        self.last_install_audit_stats = {
            "raw_jobs_fetched": len(self.jobs),
            "jobs_skipped_out_of_scope": len(self.jobs) - len(jobs),
            "jobs_enriched": len(jobs),
        }
        max_appointments = int(kwargs.get("max_appointments") or len(jobs) or 1)
        return jobs[:max_appointments]


class FilteringInstallServiceTitan(ServiceTitanClient):
    def __init__(self, audit_settings: Settings, records: list[dict[str, object]]) -> None:
        super().__init__(audit_settings)
        self.records = records
        self.enriched_job_ids: list[str] = []

    def _get_paginated(self, path: str, params: dict[str, str], *, related_category: str | None = None) -> list[dict[str, object]]:
        return self.records

    def query_install_business_unit_names(self) -> dict[str, str]:
        return {
            "1809": "HVAC - Install",
            "service-bu": "HVAC - Service",
        }

    def query_install_job_type_names(self) -> dict[str, str]:
        return {
            "install-type": "HVAC Installation",
            "inspection-type": "City Inspection",
            "follow-up-type": "Installation Follow-Up",
        }

    def _enrich_job(self, job: ServiceTitanJob) -> ServiceTitanJob:
        self.enriched_job_ids.append(job.job_id)
        return job


class FilteringPMServiceTitan(ServiceTitanClient):
    def __init__(self, audit_settings: Settings, records: list[dict[str, object]], tasks_by_project: dict[str, list[ServiceTitanProjectTask]]) -> None:
        super().__init__(audit_settings)
        self.records = records
        self.tasks_by_project = tasks_by_project
        self.task_calls: list[str] = []

    def query_pm_project_types(self) -> dict[str, str]:
        return {
            "63812999": "Standard Install",
            "63813000": "Construction & Remodel",
            "recall": "Recall",
        }

    def query_pm_project_statuses(self) -> dict[str, str]:
        return {"scheduled": "Scheduled"}

    def _employee_name_map(self) -> dict[str, str]:
        return {}

    def _get_paginated(self, path: str, params: dict[str, str], *, related_category: str | None = None) -> list[dict[str, object]]:
        return self.records

    def query_pm_project_tasks(self, project_id: str, *, max_tasks: int | None = None) -> tuple[list[ServiceTitanProjectTask], bool]:
        self.task_calls.append(project_id)
        tasks = self.tasks_by_project.get(project_id, [])
        if max_tasks is not None:
            tasks = tasks[:max(0, max_tasks)]
        return tasks, True


class ScopeFilteringServiceTitan(ServiceTitanClient):
    def __init__(self, settings: Settings, records: list[dict[str, object]]) -> None:
        super().__init__(settings)
        self.records = records
        self.enriched_job_ids: list[str] = []

    def _get_paginated(self, path: str, params: dict[str, str], *, related_category: str | None = None) -> list[dict[str, object]]:
        return self.records

    def _enrich_job(self, job: ServiceTitanJob) -> ServiceTitanJob:
        self.enriched_job_ids.append(job.job_id)
        return job


def st_enrichment_http(
    *,
    job_payload: dict[str, object] | None = None,
    appointments: list[dict[str, object]] | None = None,
    assignments: list[dict[str, object]] | None = None,
    invoices: list[dict[str, object]] | None = None,
    invoice_items: list[dict[str, object]] | None = None,
    timesheets: list[dict[str, object]] | None = None,
    non_job_timesheets: list[dict[str, object]] | None = None,
    notes: list[dict[str, object]] | None = None,
    attachments: list[dict[str, object]] | None = None,
    forms: list[dict[str, object]] | None = None,
    equipment: list[dict[str, object]] | None = None,
    purchase_orders: list[dict[str, object]] | None = None,
    history: list[dict[str, object]] | None = None,
    estimates: list[dict[str, object]] | None = None,
    opportunities: list[dict[str, object]] | None = None,
    attachments_response: HttpResponse | None = None,
    forms_response: HttpResponse | None = None,
    estimates_response: HttpResponse | None = None,
    opportunities_response: HttpResponse | None = None,
) -> FakeHttp:
    responses = [
        HttpResponse(200, {"access_token": "token", "expires_in": 900}, {}),
        HttpResponse(
            200,
            {
                "data": [
                    job_payload
                    or {
                        "id": 123,
                        "jobNumber": "J123",
                        "status": "Completed",
                        "modifiedOn": "2026-05-15T16:00:00Z",
                        "businessUnit": {"id": "bu-service", "name": "HVAC Service"},
                        "jobType": {"id": "jt-diagnostic", "name": "Diagnostic Service"},
                        "departmentName": "Service",
                        "trade": "HVAC",
                        "workflow": "Service Call",
                    }
                ],
                "hasMore": False,
            },
            {},
        ),
        HttpResponse(200, {"data": appointments or [], "hasMore": False}, {}),
    ]
    if appointments:
        responses.append(HttpResponse(200, {"data": assignments or [], "hasMore": False}, {}))
    responses.extend(
        [
            HttpResponse(200, {"data": invoices or [], "hasMore": False}, {}),
            HttpResponse(200, {"data": invoice_items or [], "hasMore": False}, {}),
            HttpResponse(200, {"data": timesheets or [], "hasMore": False}, {}),
        ]
    )
    if timesheets:
        responses.append(HttpResponse(200, {"data": non_job_timesheets or [], "hasMore": False}, {}))
    responses.extend(
        [
            HttpResponse(200, {"data": notes or [], "hasMore": False}, {}),
            attachments_response or HttpResponse(200, {"data": attachments or [], "hasMore": False}, {}),
            forms_response or HttpResponse(200, {"data": forms or [], "hasMore": False}, {}),
            HttpResponse(200, {"data": equipment or [], "hasMore": False}, {}),
            HttpResponse(200, {"data": purchase_orders or [], "hasMore": False}, {}),
            HttpResponse(200, {"data": history or [], "hasMore": False}, {}),
            estimates_response or HttpResponse(200, {"data": estimates or [], "hasMore": False}, {}),
            opportunities_response or HttpResponse(200, {"data": opportunities or [], "hasMore": False}, {}),
        ]
    )
    return FakeHttp(responses)


class FakeClaude:
    def draft_monday_owner_message(self, owner_name: str, task_lines: list[str]) -> str:
        return f"Monday for {owner_name}\n" + "\n".join(task_lines)

    def draft_friday_roundup(self, structured_sections: dict[str, list[str]]) -> str:
        return "\n".join(f"{key}: {len(value)}" for key, value in structured_sections.items())

    def draft_verification_comment(self, status: str, issues: list[str]) -> str:
        return f"Marked {status.lower()} — " + " ".join(issues)


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[str], str | None]] = []

    def send_email(self, subject: str, body: str, recipients: list[str], html_body: str | None = None) -> bool:
        self.sent.append((subject, body, recipients, html_body))
        return True


class Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings(str(Path(self.tmp.name) / "agent.sqlite3"))
        self.db = Persistence(self.settings.sqlite_path)
        self.db.initialize()
        self.owner_resolver = OwnerResolver(self.settings, self.db)
        self.owner_resolver.seed_from_config()
        self.notion = FakeNotion()
        self.slack = FakeSlack()
        self.claude = FakeClaude()
        self.campaign_health = CampaignHealthService(self.settings, self.db, self.slack)
        self.processor = TaskProcessor(
            self.settings,
            self.db,
            self.notion,
            self.slack,
            self.claude,
            self.owner_resolver,
            self.campaign_health,
        )
        self.reports = ReportService(self.settings, self.db, self.slack, self.claude, FakeEmail(), self.owner_resolver)

    def close(self) -> None:
        self.tmp.cleanup()

    def save_previous(self, item: Task, status: str = "In Progress", deadline: str | None = "2026-05-15") -> None:
        self.db.upsert_task_state(
            task_id=item.id,
            name=item.name,
            owner_name=item.owner_name,
            owner_notion_user_id=item.owner_notion_user_id,
            owner_email=item.owner_email,
            status=status,
            deadline=deadline,
            original_deadline=deadline,
            last_edited_time="2026-05-13T12:00:00+00:00",
        )


def _st_rule(audit_settings: Settings, rule_id: str):
    for rule in active_service_titan_rules(audit_settings):
        if rule.rule_id == rule_id:
            return rule
    for rule in handbook_audit_rules():
        if rule.rule_id == rule_id:
            return rule
    raise AssertionError(f"Rule not found: {rule_id}")


def _insert_service_titan_violation(
    db: Persistence,
    *,
    violation_key: str,
    rule_id: str = "sales_options_fewer_than_three",
    ruleset: str = "Sales / Comfort Advisor Audit",
    severity: str = "high",
    metadata: dict[str, object] | None = None,
    resolve: bool = False,
) -> None:
    db.upsert_service_titan_violation(
        violation_key=violation_key,
        service_titan_job_id=f"job-{violation_key}",
        appointment_id="appt-1",
        technician_id="advisor-1",
        technician_name="Private Advisor",
        dispatcher_id="",
        dispatcher_name="",
        rule_id=rule_id,
        ruleset=ruleset,
        severity=severity,
        title="Synthetic audit violation",
        description="Synthetic persisted violation for summary testing.",
        recommended_action="Review the job.",
        metadata=metadata or {},
    )
    if resolve:
        db.resolve_service_titan_violation(violation_key)


class MarketingOsAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def _run_pm_audit(self, projects: list[ServiceTitanProject], **overrides: object):
        audit_settings = settings(
            self.h.settings.sqlite_path,
            pm_audit_enabled=True,
            pm_audit_dry_run=True,
            **overrides,
        )
        return PMAuditService(audit_settings, FakePMServiceTitan(projects), self.h.slack).run_once(
            datetime(2026, 6, 24, 12, tzinfo=timezone.utc)
        )

    def _pm_result(self, project: ServiceTitanProject, rule_id: str, **overrides: object):
        summary = self._run_pm_audit([project], **overrides)
        for audit in summary.project_audits:
            for result in audit.results:
                if result.rule_id == rule_id:
                    return result
        raise AssertionError(f"PM rule not found: {rule_id}")

    def _pm_app(self, projects: list[ServiceTitanProject], **overrides: object) -> AgentApp:
        audit_settings = settings(self.h.settings.sqlite_path, **overrides)
        app = AgentApp(audit_settings)
        app.db = self.h.db
        app.slack = self.h.slack
        app.pm_audit = PMAuditService(audit_settings, FakePMServiceTitan(projects), self.h.slack)
        return app

    def _run_install_audit(self, jobs: list[ServiceTitanJob], **overrides: object):
        audit_settings = settings(
            self.h.settings.sqlite_path,
            install_audit_enabled=True,
            install_audit_dry_run=True,
            **overrides,
        )
        return InstallAuditService(audit_settings, self.h.db, FakeInstallServiceTitan(jobs), self.h.slack).run_once(
            datetime(2026, 6, 24, 17, tzinfo=timezone.utc),
            require_enabled=False,
        )

    def _install_result(self, job: ServiceTitanJob, rule_id: str, **overrides: object):
        summary = self._run_install_audit([job], install_audit_rule_ids=[rule_id], **overrides)
        for result in summary.results:
            if result.rule_id == rule_id:
                return result
        raise AssertionError(f"Install rule not found: {rule_id}")

    def _install_app(self, jobs: list[ServiceTitanJob], **overrides: object) -> AgentApp:
        audit_settings = settings(self.h.settings.sqlite_path, **overrides)
        app = AgentApp(audit_settings)
        app.db = self.h.db
        app.slack = self.h.slack
        app.install_audit = InstallAuditService(audit_settings, self.h.db, FakeInstallServiceTitan(jobs), self.h.slack)
        return app

    def test_pm_audit_defaults_disabled(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path)
        self.assertFalse(audit_settings.pm_audit_enabled)
        self.assertFalse(audit_settings.pm_audit_schedule_enabled)
        self.assertFalse(audit_settings.pm_audit_run_on_startup)
        self.assertTrue(audit_settings.pm_audit_dry_run)
        self.assertEqual(audit_settings.pm_audit_run_hour, 8)
        self.assertEqual(audit_settings.pm_audit_run_minute, 0)
        self.assertTrue(audit_settings.pm_audit_weekdays_only)
        self.assertEqual(audit_settings.pm_audit_project_page_size, 50)
        self.assertEqual(audit_settings.pm_audit_max_projects, 100)
        self.assertEqual(audit_settings.pm_audit_max_tasks, 500)
        self.assertEqual(audit_settings.pm_audit_enabled_rule_ids, [])
        self.assertEqual(audit_settings.pm_audit_install_business_unit_ids, ["1809", "64313020", "64569731"])
        self.assertIn("Electrical - Install", audit_settings.pm_audit_install_business_unit_names)
        self.assertFalse(audit_settings.pm_audit_include_client_name)
        self.assertIn("Comfort Advisor", audit_settings.pm_audit_sold_by_field_names)
        self.assertIn("PERMIT", audit_settings.pm_audit_permit_field_names)
        self.assertIn("Permit Number", audit_settings.pm_audit_permit_field_names)
        self.assertIn("HOA Approval", audit_settings.pm_audit_hoa_field_names)
        self.assertIn("Asbestos Check", audit_settings.pm_audit_asbestos_field_names)
        self.assertIsNone(audit_settings.pm_audit_asbestos_year_cutoff)
        self.assertEqual(audit_settings.pm_audit_on_hold_max_days, 30)
        self.assertEqual(audit_settings.pm_audit_homeowner_auth_within_hours, 2)
        self.assertIn("Installation Completion Report", audit_settings.pm_audit_completion_report_form_names)
        self.assertEqual(audit_settings.pm_audit_deposit_fixed_amount, 1000.0)
        self.assertEqual(audit_settings.pm_audit_deposit_percent, 0.10)
        self.assertEqual(audit_settings.pm_audit_deposit_before_install_days, 7)
        self.assertEqual(audit_settings.pm_audit_deposit_rounding_tolerance, 5.0)
        self.assertIn("Project Deposit", audit_settings.pm_audit_deposit_line_item_names)
        self.assertIn("Received", audit_settings.pm_audit_deposit_payment_status_values)
        self.assertEqual(audit_settings.pm_audit_permit_before_install_days, 10)
        self.assertEqual(audit_settings.pm_audit_project_left_open_days, 7)
        self.assertFalse(audit_settings.pm_audit_test_send)
        self.assertEqual(audit_settings.servicetitan_project_url_template, "https://go.servicetitan.com/#/project/{project_id}")

    def test_install_audit_defaults_disabled(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path)
        self.assertFalse(audit_settings.install_audit_enabled)
        self.assertTrue(audit_settings.install_audit_dry_run)
        self.assertFalse(audit_settings.install_audit_run_on_startup)
        self.assertFalse(audit_settings.install_audit_schedule_enabled)
        self.assertEqual(audit_settings.install_audit_slack_channel_id, "")
        self.assertEqual(audit_settings.install_audit_job_type_match_keywords, ["Installation"])
        self.assertEqual(
            audit_settings.install_audit_business_unit_names,
            ["Electrical - Install", "HVAC - Install", "Plumbing - Install"],
        )
        self.assertEqual(audit_settings.install_audit_business_unit_ids, ["1809", "64313020"])
        self.assertEqual(audit_settings.install_audit_rule_ids, [])
        self.assertEqual(audit_settings.install_audit_max_appointments, 100)
        self.assertEqual(audit_settings.install_audit_lookback_days, 14)
        self.assertEqual(audit_settings.install_audit_lookahead_days, 2)
        self.assertEqual(audit_settings.install_audit_run_hour, 8)
        self.assertEqual(audit_settings.install_audit_run_minute, 0)
        self.assertTrue(audit_settings.install_audit_weekdays_only)
        self.assertEqual(audit_settings.install_audit_first_day_collect_pct, 50.0)
        self.assertEqual(audit_settings.install_audit_final_day_collect_pct, 100.0)
        self.assertEqual(audit_settings.install_audit_deposit_reminder_lead_days, 1)
        self.assertEqual(audit_settings.install_audit_completion_photos_min, 1)
        self.assertEqual(audit_settings.install_audit_arrival_grace_min, 15)
        self.assertEqual(audit_settings.install_audit_meal_break_after_hours, 5.0)
        self.assertEqual(audit_settings.install_audit_second_meal_after_hours, 10.0)
        self.assertEqual(audit_settings.install_audit_meal_break_min_minutes, 30)

    def test_install_audit_manual_command_path_runs_when_auto_disabled(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            install_audit_enabled=False,
            install_audit_dry_run=True,
            install_audit_business_unit_ids=["install-bu"],
            install_audit_rule_ids=["I1"],
        )
        app = AgentApp(audit_settings)
        app.db = self.h.db
        app.install_audit = InstallAuditService(audit_settings, self.h.db, FakeInstallServiceTitan([install_job(status="Completed")]), self.h.slack)
        summary = app.run_install_audit_once(datetime(2026, 6, 24, 17, tzinfo=timezone.utc))
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.rules_evaluated, 1)
        self.assertEqual(summary.pass_count, 1)

    def test_install_audit_scheduler_startup_and_daily_dedupe(self) -> None:
        app = self._install_app(
            [install_job("install-auto", status="In Progress", end=datetime(2026, 6, 24, 12, tzinfo=timezone.utc))],
            install_audit_enabled=True,
            install_audit_schedule_enabled=True,
            install_audit_dry_run=True,
            install_audit_business_unit_ids=["install-bu"],
            install_audit_rule_ids=["I1"],
            install_audit_run_hour=8,
            install_audit_run_minute=15,
            install_audit_weekdays_only=True,
        )
        self.assertTrue(app.should_run_install_audit_at(datetime(2026, 6, 24, 8, 15, tzinfo=timezone.utc)))
        self.assertFalse(app.should_run_install_audit_at(datetime(2026, 6, 24, 8, 16, tzinfo=timezone.utc)))
        first = app.run_install_audit_automatic("startup", datetime(2026, 6, 24, 8, 15, tzinfo=timezone.utc))
        second = app.run_install_audit_automatic("scheduled", datetime(2026, 6, 24, 9, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.h.db.get_kv("install_audit_auto_last_run_date"), "2026-06-24")
        self.assertEqual(self.h.slack.messages, [])

    def test_install_audit_allowlist_and_first_enabled_rules(self) -> None:
        summary = self._run_install_audit(
            [install_job("install-allowlist", status="Completed")],
            install_audit_rule_ids=["I1", "I2", "I3", "I7", "I8"],
        )
        self.assertEqual({result.rule_id for result in summary.results}, {"I1", "I2", "I3", "I7", "I8"})
        self.assertIn("  - I1: pass 1, fail 0, skip 0", "\n".join(summary.to_lines()))
        default_rules = {rule.rule_id for rule in active_install_audit_rules(settings(self.h.settings.sqlite_path))}
        self.assertIn("I11", default_rules)
        self.assertIn("I12", default_rules)
        self.assertEqual(default_rules, {"I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9", "I11", "I12"})
        retired = active_install_audit_rules(
            settings(self.h.settings.sqlite_path, install_audit_rule_ids=["I10"])
        )
        self.assertEqual(retired, [])

    def test_install_audit_v4_scope_requires_exact_install_bu_and_installation_job_type(self) -> None:
        valid = [
            install_job("electrical-install", business_unit_id="electrical", business_unit_name=" Electrical  -  Install ", job_type_name="Panel Installation"),
            install_job("hvac-install", business_unit_name="hvac - install", job_type_name="Furnace Installation"),
            install_job("plumbing-install", business_unit_id="64313020", business_unit_name="Plumbing - Install", job_type_name="WH Installation"),
        ]
        keyword_only = install_job(
            "keyword-only",
            business_unit_id="service-bu",
            business_unit_name="HVAC - Service",
            job_type_name="Installation Follow-Up",
        )
        business_unit_only = install_job(
            "business-unit-only",
            business_unit_name="HVAC - Install",
            job_type_name="City Inspection",
        )
        summary = self._run_install_audit(
            [*valid, keyword_only, business_unit_only],
            install_audit_rule_ids=["I1"],
        )
        self.assertEqual(summary.jobs_scanned, 3)
        self.assertEqual({result.job_id for result in summary.results}, {job.job_id for job in valid})
        self.assertEqual(summary.jobs_skipped_out_of_scope, 2)

    def test_install_audit_v4_prefilter_uses_ids_only_to_reach_strict_name_gate(self) -> None:
        allowed_ids = {"1809", "64313020"}
        allowed_names = {"Electrical - Install", "HVAC - Install", "Plumbing - Install"}
        keywords = ["Installation"]
        raw_candidate = install_job(
            "raw-candidate",
            business_unit_id="1809",
            business_unit_name="",
            job_type_name="HVAC Installation",
        )
        self.assertTrue(_install_prefilter_matches(raw_candidate, allowed_ids, allowed_names, keywords))
        self.assertEqual(
            install_strict_scope_failed_gates(raw_candidate, allowed_names, keywords),
            ("business_unit",),
        )
        city_inspection = install_job(
            "city-inspection",
            business_unit_id="1809",
            business_unit_name="HVAC - Install",
            job_type_name="City Inspection",
        )
        self.assertFalse(_install_prefilter_matches(city_inspection, allowed_ids, allowed_names, keywords))
        service_follow_up = install_job(
            "service-follow-up",
            business_unit_id="service-bu",
            business_unit_name="HVAC - Service",
            job_type_name="Installation Follow-Up",
        )
        self.assertFalse(_install_prefilter_matches(service_follow_up, allowed_ids, allowed_names, keywords))

    def test_install_audit_v4_hydrates_scope_names_before_expensive_enrichment(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path)
        records = [
            {"id": "real-install", "businessUnitId": "1809", "jobTypeId": "install-type", "status": "Scheduled"},
            {"id": "city-inspection", "businessUnitId": "1809", "jobTypeId": "inspection-type", "status": "Scheduled"},
            {"id": "service-follow-up", "businessUnitId": "service-bu", "jobTypeId": "follow-up-type", "status": "Scheduled"},
        ]
        client = FilteringInstallServiceTitan(audit_settings, records)
        jobs = client.query_install_audit_jobs(
            business_unit_ids={"1809", "64313020"},
            business_unit_names={"Electrical - Install", "HVAC - Install", "Plumbing - Install"},
            window_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 7, 1, tzinfo=timezone.utc),
            max_appointments=100,
        )
        self.assertEqual([job.job_id for job in jobs], ["real-install"])
        self.assertEqual(jobs[0].business_unit_name, "HVAC - Install")
        self.assertEqual(jobs[0].job_type_name, "HVAC Installation")
        self.assertEqual(client.enriched_job_ids, ["real-install"])
        self.assertEqual(client.last_install_audit_stats["jobs_enriched"], 1)

    def test_install_audit_scope_excludes_non_install_job_names(self) -> None:
        jobs = [
            install_job("service-call", business_unit_name="HVAC Service Call", job_type_name="Service Call"),
            install_job("maintenance", business_unit_name="HVAC Maintenance", job_type_name="Maintenance"),
            install_job("warranty", business_unit_name="HVAC Warranty", job_type_name="Warranty"),
            install_job("recall", business_unit_name="HVAC Recall", job_type_name="Recall"),
            install_job("sales", business_unit_name="HVAC Sales", job_type_name="Estimate"),
            install_job("internal", business_unit_name="Internal", job_type_name="Internal Placeholder"),
        ]
        summary = self._run_install_audit(jobs, install_audit_rule_ids=["I1"])
        self.assertEqual(summary.jobs_scanned, 0)
        self.assertEqual(summary.results, [])

    def test_install_audit_scope_does_not_match_installation_form_name(self) -> None:
        job = install_job(
            "form-name-only",
            business_unit_id="other-bu",
            business_unit_name="HVAC Service",
            job_type_name="Diagnostic",
            forms=[{"name": "Installation Completion Form", "status": "Completed"}],
        )
        summary = self._run_install_audit([job], install_audit_rule_ids=["I1"], install_audit_business_unit_ids=[])
        self.assertEqual(summary.jobs_scanned, 0)
        self.assertEqual(summary.results, [])

    def test_install_audit_v4_scope_fails_closed_when_bu_or_job_type_name_is_missing(self) -> None:
        jobs = [
            install_job("missing-bu-name", business_unit_id="1809", business_unit_name="", job_type_name="HVAC Installation"),
            install_job("missing-job-type", business_unit_id="1809", business_unit_name="HVAC - Install", job_type_name=""),
        ]
        summary = self._run_install_audit(jobs, install_audit_rule_ids=["I1"])
        self.assertEqual(summary.jobs_scanned, 0)
        self.assertEqual(summary.results, [])
        self.assertEqual(summary.jobs_skipped_out_of_scope, 2)

    def test_install_audit_st_bu_installers_env_adds_business_unit_ids(self) -> None:
        with patch.dict(
            os.environ,
            {"INSTALL_AUDIT_BUSINESS_UNIT_IDS": '["1809"]', "ST_BU_INSTALLERS": "64313020"},
            clear=False,
        ):
            loaded = Settings.from_env()
        self.assertEqual(loaded.install_audit_business_unit_ids, ["1809", "64313020"])

    def test_install_i1_pass_fail_skip(self) -> None:
        self.assertEqual(self._install_result(install_job(status="Completed"), "I1").status, INSTALL_PASS)
        fail = self._install_result(install_job(status="In Progress", end=datetime(2026, 6, 24, 12, tzinfo=timezone.utc)), "I1")
        self.assertEqual(fail.status, INSTALL_FAIL)
        self.assertIn("job status is still", fail.issue)
        screenshot_case = self._install_result(
            install_job(
                "130652551",
                status="In Progress",
                business_unit_name="HVAC - Install",
                job_type_name="HVAC Installation",
                job_progress=100,
            ),
            "I1",
        )
        self.assertEqual(screenshot_case.status, INSTALL_FAIL)
        self.assertIn("Job progress is 100%", screenshot_case.issue)
        future = self._install_result(
            install_job(
                status="In Progress",
                end=datetime(2026, 6, 25, 16, tzinfo=timezone.utc),
                forms=[{"name": "Installation Completion Form", "status": "Pending"}],
            ),
            "I1",
        )
        self.assertEqual(future.status, INSTALL_SKIP)

    def test_install_i2_and_i3_form_rules_pass_fail_skip(self) -> None:
        complete = install_job(status="Completed")
        self.assertEqual(self._install_result(complete, "I2").status, INSTALL_PASS)
        self.assertEqual(self._install_result(complete, "I3").status, INSTALL_PASS)

        missing_completion = install_job(status="Completed", forms=[{"name": "Homeowner Authorization Form", "status": "Completed"}])
        self.assertEqual(self._install_result(missing_completion, "I2").status, INSTALL_FAIL)
        missing_auth = install_job(status="In Progress", forms=[{"name": "Installation Completion Form", "status": "Completed"}])
        self.assertEqual(self._install_result(missing_auth, "I3").status, INSTALL_FAIL)

        forms_unavailable = install_job(status="Completed", forms_available=False, forms=[])
        forms_result = self._install_result(forms_unavailable, "I2")
        self.assertEqual(forms_result.status, INSTALL_SKIP)
        self.assertEqual(forms_result.issue, "form_status_unavailable")
        raw_unscoped_forms = replace(
            install_job(status="Completed", forms_available=True, forms=[]),
            present_fields={"status", "business_unit", "job_type", "arrival_window", "technician"},
        )
        unscoped_result = self._install_result(raw_unscoped_forms, "I2")
        self.assertEqual(unscoped_result.status, INSTALL_SKIP)
        self.assertEqual(unscoped_result.issue, "form_status_unavailable")
        not_started = install_job(status="Scheduled", arrived_at=None, clock_in_at=None, forms=[{"name": "Homeowner Authorization Form", "status": "Completed"}])
        self.assertEqual(self._install_result(not_started, "I3").status, INSTALL_SKIP)

    def test_install_i4_and_i5_arrival_rules_pass_fail_skip(self) -> None:
        self.assertEqual(self._install_result(install_job(arrived_at=datetime(2026, 6, 24, 8, 5, tzinfo=timezone.utc)), "I4").status, INSTALL_PASS)
        self.assertEqual(self._install_result(install_job(arrived_at=datetime(2026, 6, 24, 8, 5, tzinfo=timezone.utc)), "I5").status, INSTALL_PASS)
        missing_arrival = install_job(arrived_at=None, arrived_field_available=True, start=datetime(2026, 6, 24, 8, tzinfo=timezone.utc))
        self.assertEqual(self._install_result(missing_arrival, "I4").status, INSTALL_FAIL)
        late = install_job(arrived_at=datetime(2026, 6, 24, 8, 20, tzinfo=timezone.utc), start=datetime(2026, 6, 24, 8, tzinfo=timezone.utc))
        self.assertEqual(self._install_result(late, "I5").status, INSTALL_FAIL)
        future = install_job(arrived_at=None, arrived_field_available=True, start=datetime(2026, 6, 25, 8, tzinfo=timezone.utc), end=datetime(2026, 6, 25, 16, tzinfo=timezone.utc))
        self.assertEqual(self._install_result(future, "I4").status, INSTALL_SKIP)
        self.assertEqual(self._install_result(missing_arrival, "I5").status, INSTALL_SKIP)

    def test_install_i6_meal_break_pass_fail_skip(self) -> None:
        self.assertEqual(self._install_result(install_job(lunch_break_minutes=30), "I6").status, INSTALL_PASS)
        no_break = install_job(lunch_break_minutes=0, raw_extra={"onDutyMealAgreement": "No"})
        fail = self._install_result(no_break, "I6")
        self.assertEqual(fail.status, INSTALL_FAIL)
        self.assertIn("no 30-min meal break", fail.issue)
        unknown_agreement = install_job(lunch_break_minutes=0)
        self.assertEqual(self._install_result(unknown_agreement, "I6").status, INSTALL_SKIP)
        unavailable = install_job(time_available=False, clock_in_at=None, clock_out_at=None, lunch_break_minutes=None)
        unavailable_result = self._install_result(unavailable, "I6")
        self.assertEqual(unavailable_result.status, INSTALL_SKIP)
        self.assertEqual(unavailable_result.issue, "timesheet_breaks_unavailable")
        short_day = install_job(clock_in_at=datetime(2026, 6, 24, 8, tzinfo=timezone.utc), clock_out_at=datetime(2026, 6, 24, 12, tzinfo=timezone.utc), lunch_break_minutes=0)
        self.assertEqual(self._install_result(short_day, "I6").status, INSTALL_SKIP)

    def test_install_i7_deposit_reminder_pass_fail_skip(self) -> None:
        start = datetime(2026, 6, 25, 8, tzinfo=timezone.utc)
        invoice_paid = {"id": "inv-dep", "total": 1000, "balance": 0, "status": "Paid", "lineItems": [{"name": "Project Deposit"}]}
        paid = install_job(status="Scheduled", start=start, end=datetime(2026, 6, 25, 16, tzinfo=timezone.utc), invoices=[invoice_paid], financing="No")
        self.assertEqual(
            self._install_result(paid, "I7", install_audit_deposit_reminder_lead_days=1).status,
            INSTALL_PASS,
        )
        missing = install_job(status="Scheduled", start=start, end=datetime(2026, 6, 25, 16, tzinfo=timezone.utc), invoices=[], financing="No")
        self.assertEqual(
            self._install_result(missing, "I7", install_audit_deposit_reminder_lead_days=1).status,
            INSTALL_FAIL,
        )
        financed = install_job(status="Scheduled", start=start, end=datetime(2026, 6, 25, 16, tzinfo=timezone.utc), invoices=[], financing="Approved")
        self.assertEqual(self._install_result(financed, "I7", install_audit_deposit_reminder_lead_days=1).status, INSTALL_SKIP)
        unclear = install_job(status="Scheduled", start=start, end=datetime(2026, 6, 25, 16, tzinfo=timezone.utc), financing=None)
        self.assertEqual(self._install_result(unclear, "I7", install_audit_deposit_reminder_lead_days=1).status, INSTALL_SKIP)

    def test_install_i8_payment_milestone_pass_fail_skip(self) -> None:
        final_fail = install_job(
            status="Completed",
            start=datetime(2026, 6, 23, 8, tzinfo=timezone.utc),
            end=datetime(2026, 6, 23, 16, tzinfo=timezone.utc),
            invoice_total=10000,
            invoice_balance=1000,
            payment_total=9000,
            financing="No",
        )
        self.assertEqual(self._install_result(final_fail, "I8").status, INSTALL_FAIL)

        multi_day = [
            {"id": "day1", "arrivalWindowStart": "2026-06-23T08:00:00+00:00", "arrivalWindowEnd": "2026-06-23T16:00:00+00:00"},
            {"id": "day2", "arrivalWindowStart": "2026-06-25T08:00:00+00:00", "arrivalWindowEnd": "2026-06-25T16:00:00+00:00"},
        ]
        day1_short = install_job(status="In Progress", appointments=multi_day, invoice_total=10000, invoice_balance=6000, payment_total=4000, financing="No")
        result = self._install_result(day1_short, "I8")
        self.assertEqual(result.status, INSTALL_FAIL)
        self.assertEqual(result.severity, "medium")
        day1_paid = install_job(status="In Progress", appointments=multi_day, invoice_total=10000, invoice_balance=4000, payment_total=6000, financing="No")
        self.assertEqual(self._install_result(day1_paid, "I8").status, INSTALL_PASS)
        same_day = install_job(status="In Progress", invoice_total=10000, invoice_balance=10000, payment_total=0, financing="No")
        self.assertEqual(self._install_result(same_day, "I8").status, INSTALL_SKIP)
        financed = install_job(status="Completed", invoice_total=10000, invoice_balance=10000, payment_total=0, financing="Approved")
        self.assertEqual(self._install_result(financed, "I8").status, INSTALL_SKIP)

    def test_install_i9_i11_i12_skip_when_data_unavailable(self) -> None:
        job = install_job(
            status="Completed",
            photos_available=False,
            photo_count=None,
            purchase_orders_available=False,
            ply_data_available=False,
            purchase_orders_count=None,
            equipment_available=False,
            equipment_count=None,
            equipment_complete=None,
            review_requested=None,
        )
        i9 = self._install_result(job, "I9")
        i11 = self._install_result(job, "I11")
        i12 = self._install_result(job, "I12")
        self.assertEqual((i9.status, i9.issue), (INSTALL_SKIP, "photo_count_unavailable"))
        self.assertEqual((i11.status, i11.issue), (INSTALL_SKIP, "equipment_registration_unavailable"))
        self.assertEqual((i12.status, i12.issue), (INSTALL_SKIP, "review_requested_field_unavailable"))

    def test_install_audit_live_slack_uses_install_channel_and_omits_customer_pii(self) -> None:
        pii_job = install_job(
            "install-pii",
            status="In Progress",
            end=datetime(2026, 6, 24, 12, tzinfo=timezone.utc),
            raw_extra={
                "customer": {"name": "Private Customer", "email": "private@example.com", "phone": "555-1212"},
                "summary": "Raw customer summary with 123 Main St",
            },
        )
        pii_job = replace(pii_job, url="")
        audit_settings = settings(
            self.h.settings.sqlite_path,
            install_audit_enabled=True,
            install_audit_dry_run=False,
            install_audit_slack_channel_id="C-INSTALL",
            pm_audit_slack_channel_id="C-PM",
            slack_alert_channel_id="C-LIVE",
            slack_bot_token="xoxb-test",
            install_audit_business_unit_ids=["install-bu"],
            install_audit_rule_ids=["I1"],
        )
        summary = InstallAuditService(audit_settings, self.h.db, FakeInstallServiceTitan([pii_job]), self.h.slack).run_once(
            datetime(2026, 6, 24, 17, tzinfo=timezone.utc),
            require_enabled=True,
        )
        self.assertEqual(summary.alerts_sent, 1)
        persisted = self.h.db.get_service_titan_violation(summary.failures[0].violation_key)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["rule_id"], "install_job_not_marked_complete")
        self.assertEqual(persisted["ruleset"], "Installer Audit")
        self.assertEqual(self.h.slack.messages[0][0], "C-INSTALL")
        text = self.h.slack.messages[0][1]
        self.assertIn("HIGH - Installs: Job Not Marked Complete", text)
        self.assertIn("Technician:", text)
        self.assertIn("Appointment:", text)
        self.assertIn("Arrived:", text)
        self.assertIn("Invoice:", text)
        self.assertIn("Issue:", text)
        self.assertIn("Action:", text)
        self.assertIn("Open in ServiceTitan:", text)
        self.assertIn("https://go.servicetitan.com/#/Job/Index/install-pii", text)
        for forbidden in ("Private Customer", "private@example.com", "555-1212", "123 Main St", "Raw customer summary"):
            self.assertNotIn(forbidden, text)

    def test_install_audit_does_not_change_sales_hvac_plumbing_or_pm_r22(self) -> None:
        before = [rule.rule_id for rule in active_service_titan_rules(settings(self.h.settings.sqlite_path))]
        after = [
            rule.rule_id
            for rule in active_service_titan_rules(
                settings(
                    self.h.settings.sqlite_path,
                    install_audit_enabled=True,
                    install_audit_business_unit_ids=["install-bu"],
                    install_audit_rule_ids=["I1", "I2", "I3", "I7", "I8"],
                )
            )
        ]
        self.assertEqual(after, before)
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)
        deposit_invoice = {"id": "inv-1", "total": 10000, "balance": 9000, "status": "Paid", "lineItems": [{"name": "Project Deposit"}]}
        self.assertEqual(
            self._pm_result(pm_project("pm-r22-intact", start_date=install_date, invoices=[deposit_invoice], invoices_available=True), "R22").status,
            PM_PASS,
        )

    def test_pm_audit_passes_bounded_config_to_client(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            pm_audit_enabled=True,
            pm_audit_dry_run=True,
            pm_audit_max_projects=12,
            pm_audit_max_tasks=34,
        )
        client = FakePMServiceTitan([pm_project()])
        PMAuditService(audit_settings, client, self.h.slack).run_once(datetime(2026, 6, 24, 12, tzinfo=timezone.utc))
        self.assertEqual(client.query_kwargs["max_projects"], 12)
        self.assertEqual(client.query_kwargs["max_tasks"], 34)
        self.assertEqual(client.query_kwargs["project_type_ids"], {"63812999", "63813000"})
        self.assertEqual(client.query_kwargs["business_unit_ids"], {"1809", "64313020", "64569731"})

    def test_pm_audit_dry_run_does_not_send_slack(self) -> None:
        project = pm_project("pm-dry-run", custom_fields={"Sold by": "Advisor One", "Permit": ""})
        summary = self._run_pm_audit([project])
        self.assertEqual(summary.status, "completed")
        self.assertGreater(summary.fail_count, 0)
        self.assertEqual(summary.alerts_would_send, 1)
        self.assertEqual(summary.alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_pm_audit_disabled_command_path_stays_silent(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, pm_audit_enabled=False)
        service = PMAuditService(audit_settings, FakePMServiceTitan([pm_project()]), self.h.slack)
        summary = service.run_once(datetime(2026, 6, 24, 12, tzinfo=timezone.utc))
        self.assertEqual(summary.status, "disabled")
        self.assertEqual(summary.projects_scanned, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_pm_audit_scheduler_should_run_when_enabled(self) -> None:
        app = self._pm_app(
            [pm_project()],
            pm_audit_enabled=True,
            pm_audit_schedule_enabled=True,
            pm_audit_run_hour=8,
            pm_audit_run_minute=15,
            pm_audit_weekdays_only=True,
        )
        self.assertTrue(app.should_run_pm_audit_at(datetime(2026, 6, 30, 8, 15, tzinfo=timezone.utc)))
        self.assertFalse(app.should_run_pm_audit_at(datetime(2026, 6, 30, 8, 16, tzinfo=timezone.utc)))
        self.assertFalse(app.should_run_pm_audit_at(datetime(2026, 6, 28, 8, 15, tzinfo=timezone.utc)))

    def test_pm_audit_scheduler_does_not_run_when_disabled(self) -> None:
        app = self._pm_app(
            [pm_project()],
            pm_audit_enabled=True,
            pm_audit_schedule_enabled=False,
            pm_audit_run_hour=8,
            pm_audit_run_minute=0,
        )
        self.assertFalse(app.should_run_pm_audit_at(datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc)))
        disabled = self._pm_app([pm_project()], pm_audit_enabled=False, pm_audit_schedule_enabled=True)
        self.assertIsNone(disabled.run_pm_audit_automatic("scheduled", datetime(2026, 6, 30, 8, tzinfo=timezone.utc)))
        self.assertEqual(self.h.slack.messages, [])

    def test_pm_audit_startup_run_runs_when_enabled(self) -> None:
        app = self._pm_app(
            [pm_project("pm-startup", custom_fields={"Sold By": "", "Permit": "P-1"})],
            pm_audit_enabled=True,
            pm_audit_run_on_startup=True,
            pm_audit_dry_run=True,
        )
        summary = app.run_pm_audit_automatic("startup", datetime(2026, 6, 30, 8, tzinfo=timezone.utc))
        self.assertIsNotNone(summary)
        self.assertGreater(summary.fail_count, 0)
        self.assertEqual(summary.alerts_would_send, 1)
        self.assertEqual(self.h.slack.messages, [])

    def test_pm_audit_automatic_dry_run_sends_no_slack(self) -> None:
        app = self._pm_app(
            [pm_project("pm-auto-dry", custom_fields={"Sold By": "", "Permit": "P-1"})],
            pm_audit_enabled=True,
            pm_audit_schedule_enabled=True,
            pm_audit_dry_run=True,
        )
        summary = app.run_pm_audit_automatic("scheduled", datetime(2026, 6, 30, 8, tzinfo=timezone.utc))
        self.assertIsNotNone(summary)
        self.assertEqual(summary.alerts_would_send, 1)
        self.assertEqual(summary.alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_pm_audit_automatic_sends_to_pm_channel_not_alert_channel(self) -> None:
        app = self._pm_app(
            [pm_project("pm-auto-live", custom_fields={"Sold By": "", "Permit": "P-1"})],
            pm_audit_enabled=True,
            pm_audit_schedule_enabled=True,
            pm_audit_dry_run=False,
            pm_audit_slack_channel_id="C-PM",
            slack_alert_channel_id="C-LIVE",
            slack_bot_token="xoxb-test",
        )
        summary = app.run_pm_audit_automatic("scheduled", datetime(2026, 6, 30, 8, tzinfo=timezone.utc))
        self.assertIsNotNone(summary)
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(self.h.slack.messages[0][0], "C-PM")
        self.assertNotEqual(self.h.slack.messages[0][0], "C-LIVE")

    def test_pm_audit_live_requires_pm_channel_and_does_not_fallback(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            pm_audit_enabled=True,
            pm_audit_dry_run=False,
            pm_audit_slack_channel_id="",
            slack_alert_channel_id="C-LIVE",
            slack_bot_token="xoxb-test",
        )
        summary = PMAuditService(
            audit_settings,
            FakePMServiceTitan([pm_project("pm-no-pm-channel", custom_fields={"Sold By": "", "Permit": "P-1"})]),
            self.h.slack,
        ).run_once(datetime(2026, 6, 30, 8, tzinfo=timezone.utc))
        self.assertEqual(summary.status, "config_error")
        self.assertIn("PM_AUDIT_SLACK_CHANNEL_ID", summary.config_errors)
        self.assertEqual(self.h.slack.messages, [])

    def test_pm_audit_scheduled_dedupes_same_date(self) -> None:
        app = self._pm_app(
            [pm_project("pm-dedupe", custom_fields={"Sold By": "", "Permit": "P-1"})],
            pm_audit_enabled=True,
            pm_audit_schedule_enabled=True,
            pm_audit_dry_run=False,
            pm_audit_slack_channel_id="C-PM",
            slack_bot_token="xoxb-test",
        )
        first = app.run_pm_audit_automatic("scheduled", datetime(2026, 6, 30, 8, tzinfo=timezone.utc))
        second = app.run_pm_audit_automatic("scheduled", datetime(2026, 6, 30, 9, tzinfo=timezone.utc))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.h.slack.messages), 1)
        self.assertEqual(self.h.db.get_kv("pm_audit_auto_last_run_date"), "2026-06-30")

    def test_manual_pm_audit_once_ignores_automatic_daily_dedupe(self) -> None:
        self.h.db.set_kv("pm_audit_auto_last_run_date", "2026-06-30")
        audit_settings = settings(
            self.h.settings.sqlite_path,
            pm_audit_enabled=True,
            pm_audit_dry_run=False,
            pm_audit_slack_channel_id="C-PM",
            slack_bot_token="xoxb-test",
        )
        summary = PMAuditService(
            audit_settings,
            FakePMServiceTitan([pm_project("pm-manual", custom_fields={"Sold By": "", "Permit": "P-1"})]),
            self.h.slack,
        ).run_once(datetime(2026, 6, 30, 10, tzinfo=timezone.utc))
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(self.h.slack.messages[0][0], "C-PM")

    def test_pm_audit_filters_project_types_before_task_enrichment(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, pm_audit_project_page_size=50, pm_audit_max_projects=10, pm_audit_max_tasks=10)
        records = [
            {"id": "pm-service", "projectTypeId": "recall", "status": "Scheduled"},
            {"id": "pm-install", "projectTypeId": "63812999", "status": "Scheduled", "customFields": [{"name": "Sold by", "value": "Advisor"}, {"name": "Permit", "value": "P-1"}]},
        ]
        client = FilteringPMServiceTitan(audit_settings, records, {"pm-install": [pm_task(project_id="pm-install")]})
        projects = client.query_pm_projects(project_type_ids={"63812999", "63813000"}, max_projects=10, max_tasks=10)
        self.assertEqual([project.project_id for project in projects], ["pm-install"])
        self.assertEqual(client.task_calls, ["pm-install"])
        self.assertEqual(client.last_pm_audit_stats["raw_projects_fetched"], 2)
        self.assertEqual(client.last_pm_audit_stats["skipped_out_of_scope"], 1)

    def test_pm_audit_filters_install_business_units_before_task_enrichment(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, pm_audit_project_page_size=50, pm_audit_max_projects=10, pm_audit_max_tasks=10)
        records = [
            {
                "id": "pm-hvac-service",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "businessUnitIds": ["1810"],
            },
            {
                "id": "pm-hvac-install",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "businessUnitIds": ["1809"],
            },
            {
                "id": "pm-electrical-install",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "businessUnits": [{"id": "64569731", "name": "Electrical - Install"}],
            },
        ]
        client = FilteringPMServiceTitan(
            audit_settings,
            records,
            {
                "pm-hvac-install": [pm_task(project_id="pm-hvac-install")],
                "pm-electrical-install": [pm_task(project_id="pm-electrical-install")],
            },
        )
        projects = client.query_pm_projects(
            project_type_ids={"63812999"},
            business_unit_ids={"1809", "64313020", "64569731"},
            business_unit_names={"HVAC - Install", "Plumbing - Install", "Electrical - Install"},
            max_projects=10,
            max_tasks=10,
        )
        self.assertEqual([project.project_id for project in projects], ["pm-hvac-install", "pm-electrical-install"])
        self.assertEqual(client.task_calls, ["pm-hvac-install", "pm-electrical-install"])
        self.assertEqual(client.last_pm_audit_stats["skipped_out_of_scope"], 1)

    def test_pm_audit_respects_max_project_and_task_limits(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, pm_audit_project_page_size=50)
        records = [
            {"id": "pm-1", "projectTypeId": "63812999", "status": "Scheduled"},
            {"id": "pm-2", "projectTypeId": "63812999", "status": "Scheduled"},
        ]
        client = FilteringPMServiceTitan(
            audit_settings,
            records,
            {"pm-1": [pm_task("1", project_id="pm-1"), pm_task("2", project_id="pm-1")], "pm-2": [pm_task("3", project_id="pm-2")]},
        )
        projects = client.query_pm_projects(project_type_ids={"63812999"}, max_projects=1, max_tasks=1)
        self.assertEqual(len(projects), 1)
        self.assertEqual(len(projects[0].tasks), 1)
        self.assertEqual(client.task_calls, ["pm-1"])
        self.assertEqual(client.last_pm_audit_stats["tasks_loaded"], 1)

    def test_pm_audit_empty_rule_allowlist_preserves_all_rule_behavior(self) -> None:
        summary = self._run_pm_audit([pm_project()], pm_audit_enabled_rule_ids=[])
        rule_ids = {result.rule_id for result in summary.project_audits[0].results}
        self.assertEqual(
            rule_ids,
            {
                "R1",
                "R3",
                "R4",
                "R6",
                "R7",
                "R8",
                "R9",
                "R10",
                "R11",
                "R13",
                "R15",
                "R16",
                "R17",
                "R18",
                "R19",
                "R20",
                "R21",
                "R22",
                "R23",
                "R24",
                "R25",
                "R26",
                "R27",
                "R28",
            },
        )

    def test_pm_audit_rule_allowlist_evaluates_only_selected_rules(self) -> None:
        summary = self._run_pm_audit([pm_project()], pm_audit_enabled_rule_ids=["R1", "R13", "R17"])
        rule_ids = [result.rule_id for result in summary.project_audits[0].results]
        self.assertEqual(rule_ids, ["R1", "R13", "R17"])

    def test_pm_audit_first_live_allowlist_excludes_noisy_rules(self) -> None:
        summary = self._run_pm_audit([pm_project()], pm_audit_enabled_rule_ids=["R1", "R13", "R17"])
        rule_ids = {result.rule_id for result in summary.project_audits[0].results}
        self.assertEqual(rule_ids, {"R1", "R13", "R17"})
        self.assertTrue({"R3", "R4", "R6", "R7", "R8", "R9", "R10", "R11", "R15", "R16", "R18", "R19", "R20", "R21", "R22", "R23", "R24", "R25", "R26", "R27", "R28"}.isdisjoint(rule_ids))

    def test_pm_r1_project_type_valid_invalid_missing_and_unavailable(self) -> None:
        self.assertEqual(self._pm_result(pm_project(project_type_id="63812999", project_type_name="Standard Install"), "R1").status, PM_PASS)
        self.assertEqual(
            self._pm_result(
                pm_project(project_type_id="999", project_type_name="Custom Install", raw={"id": "pm-invalid", "projectTypeId": "999", "status": "Scheduled"}),
                "R1",
            ).status,
            PM_FAIL,
        )
        self.assertEqual(
            self._pm_result(pm_project(project_type_id="", project_type_name="", raw={"id": "pm-missing", "projectTypeId": "", "status": "Scheduled"}), "R1").status,
            PM_FAIL,
        )
        self.assertEqual(
            self._pm_result(pm_project(project_type_id="", project_type_name="", raw={"id": "pm-unavailable", "status": "Scheduled"}), "R1").status,
            PM_SKIP,
        )

    def test_pm_r3_fails_missing_pm_after_grace(self) -> None:
        result = self._pm_result(
            pm_project(project_manager_ids=[], project_manager_names=[], created_on=datetime(2026, 6, 20, tzinfo=timezone.utc)),
            "R3",
        )
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r3_fails_when_no_pm_even_if_created_timestamp_missing(self) -> None:
        result = self._pm_result(pm_project(project_manager_ids=[], project_manager_names=[], created_on=None), "R3")
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r3_passes_when_pm_assigned(self) -> None:
        result = self._pm_result(pm_project(project_manager_ids=["pm-1"], project_manager_names=["Jane"], created_on=None), "R3")
        self.assertEqual(result.status, PM_PASS)

    def test_pm_r4_fails_missing_status(self) -> None:
        result = self._pm_result(pm_project(status="", raw={"id": "pm-no-status", "projectTypeId": "63812999", "status": ""}), "R4")
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r4_passes_when_status_exists(self) -> None:
        result = self._pm_result(pm_project(status="Scheduled", raw={"id": "pm-status", "projectTypeId": "63812999", "status": "Scheduled"}), "R4")
        self.assertEqual(result.status, PM_PASS)

    def test_pm_r4_fails_stale_status_only_with_status_timestamp_and_open_tasks(self) -> None:
        project = pm_project(
            status="In Progress",
            raw={
                "id": "pm-stale-status",
                "projectTypeId": "63812999",
                "status": "In Progress",
                "statusLastUpdatedOn": "2026-06-01T00:00:00+00:00",
            },
            tasks=[pm_task(status="To Do", is_closed=False)],
        )
        result = self._pm_result(project, "R4", pm_audit_status_stale_days=14)
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r4_does_not_fail_stale_check_without_status_timestamp(self) -> None:
        project = pm_project(
            status="In Progress",
            raw={"id": "pm-no-status-time", "projectTypeId": "63812999", "status": "In Progress"},
            tasks=[pm_task(status="To Do", is_closed=False)],
        )
        result = self._pm_result(project, "R4", pm_audit_status_stale_days=14)
        self.assertEqual(result.status, PM_PASS)

    def test_pm_r6_skips_when_no_configured_sold_by_field_exists(self) -> None:
        result = self._pm_result(pm_project(custom_fields={"Unrelated": ""}, custom_fields_available=True), "R6")
        self.assertEqual(result.status, PM_SKIP)

    def test_pm_r6_fails_when_configured_sold_by_field_exists_but_empty(self) -> None:
        result = self._pm_result(pm_project(custom_fields={"Sold By": "", "Permit": "PERMIT-1"}), "R6")
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r6_passes_when_configured_sold_by_field_has_value(self) -> None:
        result = self._pm_result(pm_project(custom_fields={"Comfort Advisor": "Advisor One", "Permit": "PERMIT-1"}), "R6")
        self.assertEqual(result.status, PM_PASS)

    def test_pm_r6_passes_project_details_sold_by_values(self) -> None:
        for value in ("Advisor One", "HVAC Service", "Plumbing Service"):
            record = {
                "id": f"pm-{value}",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "projectDetails": [{"name": "Sold By", "value": value}],
            }
            parsed = parse_service_titan_project(
                record,
                {"63812999": "Standard Install"},
                {},
                {},
            )
            result = self._pm_result(replace(parsed, tasks=[pm_task(project_id=parsed.project_id)], tasks_available=True), "R6")
            self.assertEqual(result.status, PM_PASS)

    def test_pm_r7_skips_when_no_configured_permit_field_exists(self) -> None:
        result = self._pm_result(pm_project(custom_fields={"Sold by": "Advisor One"}, custom_fields_available=True), "R7")
        self.assertEqual(result.status, PM_SKIP)

    def test_pm_r7_fails_when_configured_permit_field_exists_but_empty(self) -> None:
        result = self._pm_result(pm_project(custom_fields={"Sold by": "Advisor One", "Permit": ""}), "R7")
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r7_passes_when_configured_permit_field_has_value(self) -> None:
        result = self._pm_result(pm_project(custom_fields={"Sold by": "Advisor One", "Permit Number": "P-1"}), "R7")
        self.assertEqual(result.status, PM_PASS)

    def test_pm_r7_reads_project_details_permit_section(self) -> None:
        blank = parse_service_titan_project(
            {
                "id": "pm-permit-blank",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "projectDetails": [{"name": "PERMIT", "fields": [{"name": "Permit Number", "value": ""}]}],
            },
            {"63812999": "Standard Install"},
            {},
            {},
        )
        blank_result = self._pm_result(replace(blank, tasks=[pm_task(project_id=blank.project_id)], tasks_available=True), "R7")
        self.assertEqual(blank_result.status, PM_FAIL)

        present = parse_service_titan_project(
            {
                "id": "pm-permit-present",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "projectDetails": [{"name": "PERMIT", "fields": [{"name": "Permit Number", "value": "P-1"}]}],
            },
            {"63812999": "Standard Install"},
            {},
            {},
        )
        present_result = self._pm_result(replace(present, tasks=[pm_task(project_id=present.project_id)], tasks_available=True), "R7")
        self.assertEqual(present_result.status, PM_PASS)

        missing = parse_service_titan_project(
            {"id": "pm-permit-missing", "projectTypeId": "63812999", "status": "Scheduled", "projectDetails": [{"name": "Sold By", "value": "Advisor"}]},
            {"63812999": "Standard Install"},
            {},
            {},
        )
        missing_result = self._pm_result(replace(missing, tasks=[pm_task(project_id=missing.project_id)], tasks_available=True), "R7")
        self.assertEqual(missing_result.status, PM_SKIP)

    def test_pm_r11_fails_no_tasks_after_grace(self) -> None:
        result = self._pm_result(pm_project(tasks=[], created_on=datetime(2026, 6, 20, tzinfo=timezone.utc)), "R11")
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r11_passes_when_task_count_present(self) -> None:
        result = self._pm_result(pm_project(tasks=[pm_task()]), "R11")
        self.assertEqual(result.status, PM_PASS)

    def test_pm_r13_fails_task_without_assignee(self) -> None:
        result = self._pm_result(pm_project(tasks=[pm_task(assigned_to_id="", assigned_to_name="")]), "R13")
        self.assertEqual(result.status, PM_FAIL)
        self.assertIn("T-task-1", result.task_number)

    def test_pm_r15_fails_overdue_open_task(self) -> None:
        overdue = pm_task(due_at=datetime(2026, 6, 18, tzinfo=timezone.utc), status="To Do", is_closed=False)
        result = self._pm_result(pm_project(tasks=[overdue]), "R15", pm_audit_task_overdue_days=3)
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r15_ignores_open_task_with_missing_due_date(self) -> None:
        missing_due = self._pm_result(pm_project(tasks=[pm_task(due_at=None, status="To Do", is_closed=False)]), "R15")
        self.assertEqual(missing_due.status, PM_PASS)

    def test_pm_r15_skips_missing_status(self) -> None:
        missing_status = self._pm_result(pm_project(tasks=[pm_task(status="", is_closed=None)]), "R15")
        self.assertEqual(missing_status.status, PM_SKIP)

    def test_pm_r15_skips_missing_due_task_but_still_flags_other_overdue_task(self) -> None:
        missing_due = pm_task("missing", due_at=None, status="To Do", is_closed=False)
        overdue = pm_task("overdue", due_at=datetime(2026, 6, 18, tzinfo=timezone.utc), status="To Do", is_closed=False)
        summary = self._run_pm_audit([pm_project(tasks=[missing_due, overdue])], pm_audit_task_overdue_days=3)
        result = next(result for result in summary.project_audits[0].results if result.rule_id == "R15")
        self.assertEqual(result.status, PM_FAIL)
        self.assertEqual(summary.open_tasks_without_due_skipped, 1)

    def test_pm_r17_fails_completed_project_with_open_tasks(self) -> None:
        result = self._pm_result(
            pm_project(status_id="22936529", status="Completed", raw={"id": "pm-complete", "projectTypeId": "63812999", "status": "Completed"}),
            "R17",
        )
        self.assertEqual(result.status, PM_FAIL)

    def test_pm_r8_hoa_approval_pass_fail_skip(self) -> None:
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Under HOA": "No"}), "R8").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Under HOA": "Yes", "HOA Approval": ""}), "R8").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Sold By": "Advisor"}, custom_fields_available=True), "R8").status, PM_SKIP)

    def test_pm_r9_asbestos_check_pass_fail_skip(self) -> None:
        replacement_raw = {"id": "pm-asbestos", "projectTypeId": "63812999", "status": "Scheduled", "yearBuilt": 1975, "installType": "Replacement"}
        fail = self._pm_result(
            pm_project(raw=replacement_raw, custom_fields={"Asbestos Check": ""}),
            "R9",
            pm_audit_asbestos_year_cutoff=1980,
        )
        self.assertEqual(fail.status, PM_FAIL)
        passed = self._pm_result(
            pm_project(raw=replacement_raw, custom_fields={"Asbestos Check": "Recorded"}),
            "R9",
            pm_audit_asbestos_year_cutoff=1980,
        )
        self.assertEqual(passed.status, PM_PASS)
        skipped = self._pm_result(pm_project(raw=replacement_raw, custom_fields={"Asbestos Check": ""}), "R9")
        self.assertEqual(skipped.status, PM_SKIP)

    def test_pm_r10_review_requested_pass_fail_skip(self) -> None:
        completed_raw = {"id": "pm-review", "projectTypeId": "63812999", "status": "Completed"}
        self.assertEqual(self._pm_result(pm_project(status="Scheduled", custom_fields={"Review Requested": ""}), "R10").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=completed_raw, custom_fields={"Review Requested": ""}), "R10").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=completed_raw, custom_fields={"Review Requested": "Yes"}), "R10").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=completed_raw, custom_fields={"Sold By": "Advisor"}), "R10").status, PM_SKIP)

    def test_pm_r16_on_hold_reason_pass_fail_skip(self) -> None:
        hold_raw = {"id": "pm-hold", "projectTypeId": "63812999", "status": "On Hold", "onHoldSince": "2026-05-01T00:00:00+00:00"}
        self.assertEqual(self._pm_result(pm_project(status="Scheduled"), "R16").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="On Hold", raw=hold_raw, custom_fields={"On Hold Reason": ""}), "R16", pm_audit_on_hold_max_days=30).status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(status="On Hold", raw=hold_raw, custom_fields={"On Hold Reason": "Waiting on parts"}), "R16", pm_audit_on_hold_max_days=30).status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="On Hold", raw={"id": "pm-hold", "projectTypeId": "63812999", "status": "On Hold"}), "R16").status, PM_SKIP)

    def test_pm_r18_payment_order_pass_fail_skip(self) -> None:
        valid = pm_project(
            raw={
                "id": "pm-pay-valid",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "paymentMilestones": [
                    {"name": "Deposit", "paidAt": "2026-06-01T00:00:00+00:00"},
                    {"name": "First installment", "paidAt": "2026-06-02T00:00:00+00:00"},
                    {"name": "Final balance", "paidAt": "2026-06-03T00:00:00+00:00"},
                ],
            }
        )
        self.assertEqual(self._pm_result(valid, "R18").status, PM_PASS)
        invalid = replace(
            valid,
            raw={
                "id": "pm-pay-invalid",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "paymentMilestones": [
                    {"name": "Deposit", "paidAt": "2026-06-02T00:00:00+00:00"},
                    {"name": "First installment", "paidAt": "2026-06-01T00:00:00+00:00"},
                    {"name": "Final balance", "paidAt": "2026-06-03T00:00:00+00:00"},
                ],
            },
        )
        self.assertEqual(self._pm_result(invalid, "R18").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(raw={"id": "pm-no-pay", "projectTypeId": "63812999", "status": "Scheduled"}), "R18").status, PM_SKIP)

    def test_pm_r19_homeowner_authorization_timing_pass_fail_skip(self) -> None:
        base_raw = {"id": "pm-auth", "projectTypeId": "63812999", "status": "Scheduled", "crewArrivedAt": "2026-06-24T08:00:00+00:00"}
        self.assertEqual(self._pm_result(pm_project(raw={**base_raw, "homeownerAuthorizationCompletedAt": "2026-06-24T09:00:00+00:00"}), "R19").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(raw={**base_raw, "homeownerAuthorizationCompletedAt": "2026-06-24T11:30:00+00:00"}), "R19").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(raw={"id": "pm-auth", "projectTypeId": "63812999", "status": "Scheduled"}), "R19").status, PM_SKIP)

    def test_pm_r20_completion_report_pass_fail_skip(self) -> None:
        completed = {"id": "pm-report", "projectTypeId": "63812999", "status": "Completed"}
        self.assertEqual(self._pm_result(pm_project(status="Scheduled"), "R20").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw={**completed, "completionReportStatus": "Green"}), "R20").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw={**completed, "completionReportStatus": "Red"}), "R20").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=completed), "R20").status, PM_SKIP)

    def test_pm_r21_equipment_registered_pass_fail_skip(self) -> None:
        raw = {"id": "pm-equipment", "projectTypeId": "63812999", "status": "Completed"}
        self.assertEqual(self._pm_result(pm_project(status="Scheduled", custom_fields={"Equipment Registered": ""}), "R21").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=raw, custom_fields={"Equipment Registered": "Done"}), "R21").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=raw, custom_fields={"Equipment Registered": ""}), "R21").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=raw, custom_fields={}), "R21").status, PM_SKIP)

    def test_pm_r22_deposit_before_install_pass_fail_skip(self) -> None:
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)

        def invoice(total: object = 10000, balance: object = 9000, *, line_name: str = "Project Deposit", status: str = "Paid") -> dict[str, object]:
            return {
                "id": "inv-1",
                "projectId": "pm-deposit",
                "jobId": "job-1",
                "total": total,
                "balance": balance,
                "status": status,
                "paidOn": "2026-06-20T00:00:00+00:00" if status == "Paid" else "",
                "lineItems": [{"name": line_name, "amount": 1000}],
            }

        self.assertEqual(
            self._pm_result(pm_project("pm-deposit", start_date=install_date, invoices=[invoice()], invoices_available=True), "R22").status,
            PM_PASS,
        )
        self.assertEqual(
            self._pm_result(pm_project("pm-deposit", start_date=install_date, invoices=[invoice(balance=10000, status="Open")], invoices_available=True), "R22").status,
            PM_FAIL,
        )
        self.assertEqual(self._pm_result(pm_project("pm-deposit", start_date=install_date, invoices=[], invoices_available=False), "R22").status, PM_SKIP)

    def test_pm_r22_expected_deposit_uses_lesser_of_fixed_or_percent(self) -> None:
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)
        smaller_job = {
            "id": "inv-small",
            "total": 5000,
            "balance": 4500,
            "status": "Paid",
            "lineItems": [{"name": "Deposit"}],
        }
        large_job = {
            "id": "inv-large",
            "total": 50000,
            "balance": 49000,
            "status": "Paid",
            "lineItems": [{"name": "Deposit"}],
        }
        small_result = self._pm_result(pm_project("pm-small-deposit", start_date=install_date, invoices=[smaller_job], invoices_available=True), "R22")
        large_result = self._pm_result(pm_project("pm-large-deposit", start_date=install_date, invoices=[large_job], invoices_available=True), "R22")
        self.assertEqual(small_result.status, PM_PASS)
        self.assertIn("Expected deposit: $500.00", small_result.details)
        self.assertEqual(large_result.status, PM_PASS)
        self.assertIn("Expected deposit: $1,000.00", large_result.details)

    def test_pm_r22_fails_partial_deposit_below_tolerance(self) -> None:
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)
        partial_invoice = {
            "id": "inv-partial",
            "total": 10000,
            "balance": 9500,
            "status": "Partially Paid",
            "lineItems": [{"name": "Installation Deposit"}],
        }
        result = self._pm_result(pm_project("pm-partial-deposit", start_date=install_date, invoices=[partial_invoice], invoices_available=True), "R22")
        self.assertEqual(result.status, PM_FAIL)
        self.assertIn("below required", result.issue)
        self.assertIn("Paid deposit: $500.00", result.details)

    def test_pm_r22_respects_rounding_tolerance(self) -> None:
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)
        within_tolerance = {
            "id": "inv-tolerance",
            "total": 10000,
            "balance": 9004,
            "status": "Partially Paid",
            "lineItems": [{"name": "Deposit"}],
        }
        result = self._pm_result(pm_project("pm-tolerance-deposit", start_date=install_date, invoices=[within_tolerance], invoices_available=True), "R22")
        self.assertEqual(result.status, PM_PASS)
        self.assertIn("Paid deposit: $996.00", result.details)

    def test_pm_r22_skips_when_required_structured_data_is_missing(self) -> None:
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)
        no_job_total = {"id": "inv-no-total", "balance": 0, "status": "Paid", "lineItems": [{"name": "Deposit"}]}
        no_deposit_line = {"id": "inv-no-line", "total": 10000, "balance": 9000, "status": "Paid", "lineItems": [{"name": "Install labor"}]}
        unclear_payment = {"id": "inv-no-pay-data", "total": 10000, "status": "", "lineItems": [{"name": "Deposit"}]}
        self.assertEqual(self._pm_result(pm_project("pm-no-total", start_date=install_date, invoices=[no_job_total], invoices_available=True), "R22").status, PM_SKIP)
        self.assertEqual(self._pm_result(pm_project("pm-no-line", start_date=install_date, invoices=[no_deposit_line], invoices_available=True), "R22").status, PM_SKIP)
        self.assertEqual(self._pm_result(pm_project("pm-no-pay-data", start_date=install_date, invoices=[unclear_payment], invoices_available=True), "R22").status, PM_SKIP)

    def test_pm_r22_runs_alone_with_allowlist_and_formats_safe_slack(self) -> None:
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)
        invoice_unpaid = {
            "id": "inv-unpaid",
            "total": 10000,
            "balance": 10000,
            "status": "Open",
            "lineItems": [{"name": "Project Deposit"}],
        }
        project = pm_project(
            "31970134",
            project_number="31970134",
            client_name="Client Example",
            start_date=install_date,
            invoices=[invoice_unpaid],
            invoices_available=True,
            raw={
                "id": "31970134",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "address": "private address",
                "phone": "555-123-4567",
                "email": "private@example.com",
                "notes": "private raw notes",
            },
        )
        summary = self._run_pm_audit([project], pm_audit_enabled_rule_ids=["R22"])
        rule_ids = [result.rule_id for result in summary.project_audits[0].results]
        text = summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC")
        self.assertEqual(rule_ids, ["R22"])
        self.assertEqual(summary.fail_count, 1)
        self.assertIn("Project #31970134 — Client Example — Deposit missing before install", text)
        self.assertIn("Expected deposit: $1,000.00", text)
        self.assertIn("Paid deposit: $0.00", text)
        self.assertIn("Link: https://go.servicetitan.com/#/project/31970134", text)
        for forbidden in ("private address", "555-123-4567", "private@example.com", "private raw notes"):
            self.assertNotIn(forbidden, text)

    def test_pm_r23_permit_before_install_pass_fail_skip(self) -> None:
        install_date = datetime(2026, 6, 28, tzinfo=timezone.utc)
        self.assertEqual(self._pm_result(pm_project(start_date=install_date, custom_fields={"Permit": "P-1"}), "R23").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(start_date=install_date, custom_fields={"Permit": ""}), "R23").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(start_date=install_date, custom_fields={"Permit Owner": "Customer", "Permit": ""}), "R23").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(start_date=install_date, custom_fields={}), "R23").status, PM_SKIP)

    def test_pm_r24_equipment_confirmed_pass_fail_skip(self) -> None:
        self.assertEqual(self._pm_result(pm_project(start_date=None, custom_fields={"Equipment Status": ""}), "R24").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Equipment Status": "Ready"}), "R24").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Equipment Status": ""}), "R24").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(custom_fields={}), "R24").status, PM_SKIP)

    def test_pm_r25_rebate_confirmed_pass_fail_skip(self) -> None:
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Rebate Status": "Approved"}), "R25").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Rebate Status": "Pending"}), "R25").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Rebate Status": "N/A"}), "R25").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={}), "R25").status, PM_SKIP)

    def test_pm_r26_crew_assigned_pass_fail_skip(self) -> None:
        self.assertEqual(self._pm_result(pm_project(start_date=None, custom_fields={"Crew": ""}), "R26").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Crew": "Install Team 1"}), "R26").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Crew": ""}), "R26").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(custom_fields={}), "R26").status, PM_SKIP)

    def test_pm_r27_project_left_open_too_long_pass_fail_skip(self) -> None:
        completed_raw = {"id": "pm-left-open", "projectTypeId": "63812999", "status": "Completed"}
        self.assertEqual(self._pm_result(pm_project(status="Completed", raw=completed_raw), "R27").status, PM_PASS)
        self.assertEqual(
            self._pm_result(
                pm_project(
                    status="In Progress",
                    raw={"id": "pm-left-open", "projectTypeId": "63812999", "status": "In Progress"},
                    actual_completion_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
                ),
                "R27",
                pm_audit_project_left_open_days=7,
            ).status,
            PM_FAIL,
        )
        self.assertEqual(self._pm_result(pm_project(status="In Progress", actual_completion_date=None), "R27").status, PM_SKIP)

    def test_pm_r28_change_order_written_approval_pass_fail_skip(self) -> None:
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Change Order Approval": "Approved"}), "R28").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Change Order Approval": "Pending"}), "R28").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Change Order": "Yes"}), "R28").status, PM_FAIL)
        self.assertEqual(self._pm_result(pm_project(custom_fields={"Change Order Approval": "No"}), "R28").status, PM_PASS)
        self.assertEqual(self._pm_result(pm_project(custom_fields={}), "R28").status, PM_SKIP)

    def test_new_pm_rules_respect_allowlist_and_stay_out_of_first_live_set(self) -> None:
        summary = self._run_pm_audit(
            [pm_project(custom_fields={"Under HOA": "Yes", "HOA Approval": ""})],
            pm_audit_enabled_rule_ids=["R8"],
        )
        rule_ids = [result.rule_id for result in summary.project_audits[0].results]
        self.assertEqual(rule_ids, ["R8"])
        self.assertEqual(summary.fail_count, 1)
        first_live = self._run_pm_audit([pm_project()], pm_audit_enabled_rule_ids=["R1", "R13", "R17"])
        first_live_ids = {result.rule_id for result in first_live.project_audits[0].results}
        self.assertTrue({"R8", "R9", "R10", "R16", "R18", "R19", "R20", "R21", "R22", "R23", "R24", "R25", "R26", "R27", "R28"}.isdisjoint(first_live_ids))

    def test_pm_missing_or_unsafe_data_returns_skip(self) -> None:
        project = pm_project(custom_fields={}, custom_fields_available=False, tasks=[], tasks_available=False)
        summary = self._run_pm_audit([project])
        skip_by_rule = {result.rule_id: result.status for result in summary.project_audits[0].results}
        self.assertEqual(skip_by_rule["R6"], PM_SKIP)
        self.assertEqual(skip_by_rule["R7"], PM_SKIP)
        self.assertEqual(skip_by_rule["R11"], PM_SKIP)
        self.assertEqual(skip_by_rule["R13"], PM_SKIP)
        self.assertEqual(skip_by_rule["R15"], PM_SKIP)
        self.assertEqual(skip_by_rule["R17"], PM_SKIP)
        self.assertEqual(skip_by_rule["R8"], PM_SKIP)
        self.assertEqual(skip_by_rule["R9"], PM_SKIP)
        self.assertEqual(skip_by_rule["R10"], PM_PASS)
        self.assertEqual(skip_by_rule["R16"], PM_PASS)
        self.assertEqual(skip_by_rule["R18"], PM_SKIP)
        self.assertEqual(skip_by_rule["R19"], PM_SKIP)
        self.assertEqual(skip_by_rule["R20"], PM_PASS)
        self.assertEqual(skip_by_rule["R21"], PM_PASS)
        self.assertEqual(skip_by_rule["R22"], PM_SKIP)
        self.assertEqual(skip_by_rule["R23"], PM_SKIP)
        self.assertEqual(skip_by_rule["R24"], PM_SKIP)
        self.assertEqual(skip_by_rule["R25"], PM_SKIP)
        self.assertEqual(skip_by_rule["R26"], PM_SKIP)
        self.assertEqual(skip_by_rule["R27"], PM_SKIP)
        self.assertEqual(skip_by_rule["R28"], PM_SKIP)

    def test_pm_out_of_scope_project_type_is_not_evaluated(self) -> None:
        project = pm_project(project_type_id="recall", project_type_name="Recall", raw={"id": "pm-recall", "projectTypeId": "recall", "status": "Scheduled"})
        summary = self._run_pm_audit([project])
        self.assertEqual(summary.in_scope_projects, 0)
        self.assertEqual(summary.skipped_out_of_scope, 1)
        self.assertEqual(summary.rules_evaluated, 0)

    def test_pm_explicit_service_sales_warranty_recall_labels_are_skipped(self) -> None:
        projects = [
            pm_project("pm-service", business_unit_names=["HVAC Service"]),
            pm_project("pm-sales", business_unit_names=["HVAC Sales"]),
            pm_project("pm-warranty", project_type_name="Warranty"),
            pm_project("pm-recall", project_type_name="Recall"),
        ]
        summary = self._run_pm_audit(projects)
        self.assertEqual(summary.in_scope_projects, 0)
        self.assertEqual(summary.skipped_out_of_scope, 4)
        self.assertEqual(summary.rules_evaluated, 0)

    def test_pm_parser_preserves_business_unit_names_when_available(self) -> None:
        parsed = parse_service_titan_project(
            {
                "id": "pm-install",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "businessUnits": [{"id": "1809", "name": "HVAC - Install"}],
            },
            {"63812999": "Standard Install"},
            {},
            {},
        )
        self.assertEqual(parsed.business_unit_ids, ["1809"])
        self.assertEqual(parsed.business_unit_names, ["HVAC - Install"])

    def test_pm_parser_preserves_client_name_without_showing_by_default(self) -> None:
        parsed = parse_service_titan_project(
            {
                "id": "pm-client",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "customer": {"name": "Sensitive Customer"},
            },
            {"63812999": "Standard Install"},
            {},
            {},
        )
        self.assertEqual(parsed.client_name, "Sensitive Customer")
        project = replace(parsed, custom_fields={"Sold By": "", "Permit": "P-1"}, custom_fields_available=True, tasks=[pm_task(project_id="pm-client")], tasks_available=True)
        summary = self._run_pm_audit([project], pm_audit_enabled_rule_ids=["R6"])
        self.assertNotIn("Sensitive Customer", summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC"))

    def test_pm_alert_can_include_client_name_when_opted_in(self) -> None:
        project = pm_project(
            "pm-client-opt-in",
            project_number="130000111",
            client_name="Sensitive Customer",
            custom_fields={"Sold By": "", "Permit": "P-1"},
        )
        summary = self._run_pm_audit([project], pm_audit_enabled_rule_ids=["R6"], pm_audit_include_client_name=True)
        text = summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC")
        self.assertIn("Client: Sensitive Customer", text)

    def test_pm_summary_groups_failures_by_pm_and_omits_pii(self) -> None:
        jane_project = pm_project(
            "pm-jane",
            project_number="127623147",
            project_manager_names=["Jane"],
            custom_fields={"Sold by": "Advisor One", "Permit": ""},
            raw={
                "id": "pm-jane",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "customerName": "Sensitive Customer",
                "summary": "private raw notes",
            },
        )
        gerson_project = pm_project(
            "pm-gerson",
            project_number="127623148",
            project_manager_ids=["pm-2"],
            project_manager_names=["Gerson"],
            tasks=[pm_task("884", project_id="pm-gerson", due_at=datetime(2026, 6, 18, tzinfo=timezone.utc), status="To Do", is_closed=False)],
        )
        summary = self._run_pm_audit([jane_project, gerson_project], pm_audit_task_overdue_days=3)
        text = summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC")
        self.assertIn("📋 PM Audit", text)
        self.assertIn("Jane", text)
        self.assertIn("Gerson", text)
        self.assertIn("• Project #127623147 — Missing permit field", text)
        self.assertIn("Field: Permit", text)
        self.assertIn("• Project #127623148 — Task overdue", text)
        self.assertIn("Field: T-884", text)
        self.assertIn("Link: https://go.servicetitan.com/#/project/pm-gerson", text)
        self.assertIn("Summary:", text)
        for pii in ("Sensitive Customer", "private raw notes", "address", "phone", "email"):
            self.assertNotIn(pii, text)

    def test_pm_slack_output_only_includes_allowlisted_rule_failures(self) -> None:
        project = pm_project(
            "pm-allowlisted-alert",
            project_number="130000001",
            project_manager_ids=[],
            project_manager_names=[],
            status="Completed",
            raw={
                "id": "pm-allowlisted-alert",
                "projectTypeId": "63812999",
                "status": "Completed",
                "customerName": "Sensitive Customer",
            },
            tasks=[pm_task(status="To Do", is_closed=False)],
        )
        summary = self._run_pm_audit([project], pm_audit_enabled_rule_ids=["R17"])
        text = summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC")
        self.assertIn("Completed project has open tasks", text)
        self.assertNotIn("No PM assigned", text)
        self.assertNotIn("Sensitive Customer", text)

    def test_pm_project_parser_uses_project_url_template_not_job_link(self) -> None:
        parsed = parse_service_titan_project(
            {
                "id": "131747228",
                "projectId": "31970134",
                "number": "131747228",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "jobIds": ["131747228"],
            },
            {"63812999": "Standard Install"},
            {},
            {},
            "https://go.servicetitan.com/#/project/{project_id}",
        )
        self.assertEqual(parsed.project_number, "131747228")
        self.assertEqual(parsed.url, "https://go.servicetitan.com/#/project/31970134")
        self.assertNotIn("#/Job/Index/131747228", parsed.url)

    def test_pm_slack_formatter_uses_project_link_not_job_link(self) -> None:
        project = pm_project(
            "31970134",
            project_number="131747228",
            job_ids=["131747228"],
            custom_fields={"Sold By": "", "Permit": "P-1"},
            url="https://go.servicetitan.com/#/project/31970134",
        )
        summary = self._run_pm_audit([project], pm_audit_enabled_rule_ids=["R6"])
        text = summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC")
        self.assertIn("Project #131747228", text)
        self.assertIn("Link: https://go.servicetitan.com/#/project/31970134", text)
        self.assertNotIn("#/Job/Index/131747228", text)

    def test_pm_missing_project_id_omits_link_instead_of_using_job_link(self) -> None:
        parsed = parse_service_titan_project(
            {
                "number": "131747228",
                "projectTypeId": "63812999",
                "status": "Scheduled",
                "jobIds": ["131747228"],
            },
            {"63812999": "Standard Install"},
            {},
            {},
            "https://go.servicetitan.com/#/project/{project_id}",
        )
        self.assertEqual(parsed.project_number, "131747228")
        self.assertEqual(parsed.url, "")
        summary = self._run_pm_audit(
            [replace(parsed, custom_fields={"Sold By": "", "Permit": "P-1"}, custom_fields_available=True, tasks=[pm_task(project_id="")], tasks_available=True)],
            pm_audit_enabled_rule_ids=["R6"],
        )
        text = summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC")
        self.assertIn("Project #131747228", text)
        self.assertNotIn("Link:", text)
        self.assertNotIn("#/Job/Index/131747228", text)

    def test_pm_dry_run_summary_includes_top_fail_and_skip_reasons(self) -> None:
        current_task = pm_task(due_at=datetime(2026, 6, 30, tzinfo=timezone.utc), status="To Do", is_closed=False)
        fail_project = pm_project("pm-fail", custom_fields={"Sold by": "", "Permit": "P-1"}, tasks=[current_task])
        skip_project = pm_project("pm-skip", custom_fields={}, custom_fields_available=False, tasks=[current_task])
        summary = self._run_pm_audit([fail_project, skip_project])
        text = "\n".join(summary.to_lines())
        self.assertIn("- top fail rules:", text)
        self.assertIn("R6 Comfort Advisor / Sold By set", text)
        self.assertIn("- top skip reasons:", text)
        self.assertIn("Project custom fields unavailable.", text)
        self.assertIn("Top fail: R6 Comfort Advisor / Sold By set", summary.alert_text(datetime(2026, 6, 24, tzinfo=timezone.utc), "UTC"))

    def test_pm_live_alert_uses_pm_channel_when_provided(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            pm_audit_enabled=True,
            pm_audit_dry_run=False,
            pm_audit_slack_channel_id="C-PM",
            slack_alert_channel_id="C-LIVE",
            slack_bot_token="xoxb-test",
        )
        project = pm_project("pm-live", custom_fields={"Sold By": "", "Permit": "P-1"})
        summary = PMAuditService(audit_settings, FakePMServiceTitan([project]), self.h.slack).run_once(
            datetime(2026, 6, 24, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(self.h.slack.messages[0][0], "C-PM")

    def test_pm_test_slack_uses_test_channel_only(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            pm_audit_slack_channel_id="C-PM-TEST",
            pm_audit_test_send=True,
            slack_alert_channel_id="C-LIVE",
            slack_bot_token="xoxb-test",
        )
        app = AgentApp(audit_settings)
        app.slack = self.h.slack
        ok, text = app.pm_audit_slack_test_text()
        self.assertTrue(ok)
        self.assertEqual(self.h.slack.messages[0][0], "C-PM-TEST")
        self.assertNotIn("C-LIVE", text)
        self.assertIn("uses live ServiceTitan audit channel fallback: false", text)

    def test_completed_without_deliverable_gets_flagged(self) -> None:
        item = task("t1", "Completed", deliverable_link="", notes="")
        self.h.save_previous(item)
        self.h.processor.process_status_change(item, "In Progress", "2026-05-15")
        self.assertEqual(len(self.h.notion.comments), 1)
        self.assertEqual(self.h.notion.flags, [("t1", True)])
        self.assertEqual(self.h.slack.dms, [])

    def test_completed_with_deliverable_on_time_does_not_notify_tim(self) -> None:
        item = task("t2", "Completed", deliverable_link="https://drive.google.com/file")
        self.h.save_previous(item)
        self.h.processor.process_status_change(item, "In Progress", "2026-05-15")
        self.assertEqual(self.h.notion.comments, [])
        self.assertEqual(self.h.slack.dms, [])

    def test_delayed_without_reason_gets_flagged(self) -> None:
        item = task("t3", "Delayed", notes="", deadline=date(2026, 5, 15))
        self.h.save_previous(item, deadline="2026-05-15")
        self.h.processor.process_status_change(item, "In Progress", "2026-05-15")
        self.assertEqual(len(self.h.notion.comments), 1)
        self.assertIn("please add the reason", self.h.notion.comments[0][1])

    def test_delayed_twice_triggers_tim_dm(self) -> None:
        first = task("t4", "Delayed", notes="Waiting", edited=datetime(2026, 5, 14, 12, tzinfo=timezone.utc))
        second = task("t4", "Delayed", notes="Still waiting", edited=datetime(2026, 5, 15, 12, tzinfo=timezone.utc))
        self.h.processor.process_status_change(first, "In Progress", "2026-05-13")
        self.h.processor.process_status_change(second, "In Progress", "2026-05-14")
        self.assertEqual(len(self.h.slack.dms), 1)
        self.assertIn("delayed twice", self.h.slack.dms[0][1])

    def test_blocked_without_needs_from_others_gets_flagged(self) -> None:
        item = task("t5", "Blocked", needs="")
        self.h.processor.process_status_change(item, "In Progress", "2026-05-15")
        self.assertEqual(len(self.h.notion.comments), 1)
        self.assertTrue(self.h.notion.flags)

    def test_blocked_with_named_person_posts_to_marketing_ops(self) -> None:
        item = task("t6", "Blocked", needs="Need Vadim to approve copy")
        self.h.processor.process_status_change(item, "In Progress", "2026-05-15")
        combined = "\n".join(message[1] for message in self.h.slack.messages)
        self.assertIn("<@UVADIM>", combined)

    def test_campaign_progress_risk_triggers_tim_dm(self) -> None:
        tasks = [task("a", "Not Started"), task("b", "In Progress"), task("c", "Blocked"), task("d", "Delayed"), task("e", "Needs Review")]
        camp = campaign("c1", start=date(2026, 5, 1), end=date(2026, 5, 31), linked_tasks=[t.id for t in tasks])
        alerts = self.h.campaign_health.scan([camp], tasks, date(2026, 5, 28))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(len(self.h.slack.dms), 1)

    def test_budget_overrun_triggers_tim_dm(self) -> None:
        camp = campaign("c2", planned=1000, actual=1001)
        alerts = self.h.campaign_health.scan([camp], [], date(2026, 5, 14))
        self.assertEqual(len(alerts), 1)
        self.assertIn("Budget overrun", self.h.slack.dms[0][1])

    def test_friday_roundup_groups_tasks_correctly(self) -> None:
        week_start, week_end = week_bounds(date(2026, 5, 15))
        sections = self.h.reports.build_friday_sections(
            [
                task("r1", "Completed", deadline=date(2026, 5, 15)),
                task("r2", "Delayed", deadline=date(2026, 5, 20), notes="Vendor delay"),
                task("r3", "Blocked", deadline=date(2026, 5, 16), needs="Emil"),
                task("r4", "Canceled", deadline=date(2026, 5, 15)),
                task("r5", "Not Started", deadline=date(2026, 5, 18)),
                task("r6", "In Progress", deadline=date(2026, 5, 15)),
                task("r7", "Needs Review", deadline=date(2026, 5, 8)),
            ],
            week_start,
            week_end,
            week_end.replace(day=week_end.day + 1),
            week_end.replace(day=week_end.day + 7),
        )
        self.assertEqual(len(sections["Completed"]), 1)
        self.assertEqual(len(sections["Delayed, with new deadline and reason"]), 1)
        self.assertEqual(len(sections["Blocked"]), 1)
        self.assertEqual(len(sections["Not completed, needs rollover"]), 3)
        self.assertEqual(len(sections["Canceled"]), 1)
        self.assertEqual(len(sections["Coming next week"]), 1)

    def test_monday_push_groups_tasks_by_owner(self) -> None:
        grouped = self.h.reports.monday_push(
            [
                task("m1", "Not Started", owner_name="Emil", deadline=date(2026, 5, 11)),
                task("m2", "In Progress", owner_name="Vadim", deadline=date(2026, 5, 12)),
                task("m3", "Completed", owner_name="Emil", deadline=date(2026, 5, 13)),
                task("m4", "In Progress", owner_name="Emil", deadline=date(2026, 5, 8)),
                task("m5", "Not Started", owner_name="Emil", deadline=date(2026, 5, 14), original_deadline=date(2026, 5, 8)),
            ],
            datetime(2026, 5, 11, 8, tzinfo=timezone.utc),
        )
        self.assertEqual(set(grouped), {"Emil", "Vadim"})
        self.assertEqual(len(grouped["Emil"]), 3)
        self.assertEqual(len(self.h.slack.dms), 2)
        channel_text = "\n".join(message[1] for message in self.h.slack.messages)
        self.assertIn("Not completed last week", channel_text)
        self.assertIn("Moved to this week", channel_text)
        self.assertIn("original due 2026-05-08", channel_text)

    def test_monthly_kickoff_selects_campaigns_starting_in_month(self) -> None:
        start, end = month_bounds(date(2026, 6, 1))
        selected = select_campaigns_starting_between(
            [
                campaign("jun", start=date(2026, 6, 5)),
                campaign("may", start=date(2026, 5, 31)),
                campaign("jul", start=date(2026, 7, 1)),
            ],
            start,
            end,
        )
        self.assertEqual([item.id for item in selected], ["jun"])

    def test_duplicate_status_updates_are_not_posted_twice(self) -> None:
        item = task("dup", "Completed", deliverable_link="https://drive.google.com/file")
        self.h.processor.process_status_change(item, "In Progress", "2026-05-15")
        self.h.processor.process_status_change(item, "In Progress", "2026-05-15")
        status_posts = [message for message in self.h.slack.messages if "Task:" in message[1]]
        self.assertEqual(len(status_posts), 1)

    def test_deadline_reminder_sends_dm_within_one_hour_once(self) -> None:
        item = task(
            "reminder-1",
            "In Progress",
            name="Finish landing page",
            deadline=date(2026, 5, 15),
            deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        )
        self.h.slack.email_lookup["emil@example.com"] = "UEMAIL"
        now = datetime(2026, 5, 15, 14, 5, tzinfo=timezone.utc)
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 1)
        self.assertEqual(len(self.h.slack.dms), 1)
        self.assertEqual(self.h.slack.email_lookup_calls, ["emil@example.com"])
        self.assertEqual(self.h.slack.dms[0][0], "UEMAIL")
        self.assertIn("Finish landing page", self.h.slack.dms[0][1])
        self.assertEqual(len(self.h.notion.last_reminders), 1)

        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 0)
        self.assertEqual(len(self.h.slack.dms), 1)

    def test_deadline_reminder_waits_until_one_hour_window(self) -> None:
        item = task(
            "reminder-early",
            "In Progress",
            deadline=date(2026, 5, 15),
            deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 15, 13, 59, tzinfo=timezone.utc)
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 0)
        self.assertEqual(self.h.slack.dms, [])

    def test_deadline_reminder_skips_completed_missing_owner_and_missing_deadline(self) -> None:
        now = datetime(2026, 5, 15, 14, 5, tzinfo=timezone.utc)
        tasks = [
            task("reminder-done", "Completed", deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc)),
            task("reminder-no-owner", "In Progress", owner_name=None, deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc)),
            task("reminder-no-deadline", "In Progress", deadline=None),
        ]
        self.assertEqual(self.h.processor.process_deadline_reminders(tasks, now=now), 0)
        self.assertEqual(self.h.slack.dms, [])

    def test_deadline_reminder_logs_unmapped_owner_without_crashing(self) -> None:
        item = task(
            "reminder-unmapped",
            "In Progress",
            owner_name="NoMap",
            deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 15, 14, 5, tzinfo=timezone.utc)
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 0)
        self.assertEqual(self.h.slack.dms, [])

    def test_deadline_reminder_can_resolve_slack_user_by_email(self) -> None:
        item = task(
            "reminder-email",
            "In Progress",
            owner_name="Unmapped",
            deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        )
        self.h.slack.email_lookup["unmapped@example.com"] = "UEMAIL"
        now = datetime(2026, 5, 15, 14, 5, tzinfo=timezone.utc)
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 1)
        self.assertEqual(self.h.slack.dms[0][0], "UEMAIL")

    def test_deadline_reminder_falls_back_to_owner_mapping_after_email_lookup_fails(self) -> None:
        item = task(
            "reminder-fallback",
            "In Progress",
            owner_name="Emil",
            deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 15, 14, 5, tzinfo=timezone.utc)
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 1)
        self.assertEqual(self.h.slack.email_lookup_calls, ["emil@example.com"])
        self.assertEqual(self.h.slack.dms[0][0], "UEMIL")

    def test_deadline_reminder_slack_failure_does_not_record_duplicate_state(self) -> None:
        item = task(
            "reminder-fail",
            "In Progress",
            deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 15, 14, 5, tzinfo=timezone.utc)
        self.h.slack = FailingDMSlack()
        self.h.processor.slack = self.h.slack
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 0)

        self.h.slack = FakeSlack()
        self.h.processor.slack = self.h.slack
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 1)
        self.assertEqual(len(self.h.slack.dms), 1)

    def test_date_only_deadline_uses_configured_local_hour(self) -> None:
        item = task("reminder-date-only", "In Progress", deadline=date(2026, 5, 15), deadline_at=None)
        now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 1)

    def test_notion_reminder_property_suppresses_duplicate_after_state_loss(self) -> None:
        item = task(
            "reminder-notion-state",
            "In Progress",
            deadline_at=datetime(2026, 5, 15, 15, tzinfo=timezone.utc),
            last_reminder_sent_at=datetime(2026, 5, 15, 14, 1, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 15, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(self.h.processor.process_deadline_reminders([item], now=now), 0)
        self.assertEqual(self.h.slack.dms, [])

    def test_service_titan_rule_passes_when_required_condition_satisfied(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "tech_clock_in_missing")
        result = rule.run(st_job(), audit_settings)
        self.assertEqual(result.status, RESULT_PASS)

    def test_service_titan_rule_fails_when_violation_present(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "tech_clock_out_missing")
        result = rule.run(st_job(clock_out_at=None), audit_settings)
        self.assertEqual(result.status, RESULT_FAIL)
        self.assertIn("clock-out", result.explanation)

    def test_service_titan_rule_returns_insufficient_data_when_fields_missing(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "tech_clock_in_missing")
        result = rule.run(st_job(clock_in_at=None, present_fields={"status", "technician"}), audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)

    def test_service_titan_duplicate_violations_do_not_alert_twice(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = FakeServiceTitan([st_job("dup-st", clock_out_at=None)])
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
        first = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        second = audit.audit_once(datetime(2026, 5, 15, 16, 5, tzinfo=timezone.utc))
        self.assertEqual(first.alerts_sent, 1)
        self.assertEqual(second.alerts_sent, 0)
        self.assertEqual(second.alerts_skipped_dedupe, 1)
        alerts = [message for message in self.h.slack.messages if "Technician clock-out missing" in message[1]]
        self.assertEqual(len(alerts), 1)

    def test_service_titan_fail_alert_sends_on_non_friday_one_time_run(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = FakeServiceTitan([st_job("tuesday-st", clock_out_at=None)])
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
        summary = audit.audit_once(datetime(2026, 6, 9, 16, tzinfo=timezone.utc))
        self.assertEqual(datetime(2026, 6, 9).weekday(), 1)
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(len(self.h.slack.messages), 1)

    def test_service_titan_new_violation_sends_immediately_in_poll_cycle(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        self.h.db.set_kv("servicetitan_audit_last_processed", "2026-05-15T15:55:00+00:00")
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([st_job("new-cycle", clock_out_at=None)]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(len(self.h.slack.messages), 1)

    def test_service_titan_no_violations_sends_no_slack_message(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([st_job("clean-cycle")]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.violations_detected, 0)
        self.assertEqual(summary.alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_service_titan_continuous_loop_runs_on_interval_not_friday_schedule(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_poll_interval_seconds=999,
            service_titan_audit_startup_delay_seconds=0,
        )
        stop_event = threading.Event()
        calls: list[str] = []

        def audit_once() -> None:
            calls.append("called")
            stop_event.set()

        ServiceTitanAuditLoop(audit_settings, audit_once).run_loop(stop_event)
        self.assertEqual(calls, ["called"])

    def test_service_titan_poll_interval_config_controls_cycle_wait(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_poll_interval_seconds=300,
        )
        loop = ServiceTitanAuditLoop(audit_settings, lambda: None)
        self.assertEqual(loop._wait_seconds_after_cycle(12), 288)
        self.assertEqual(loop._wait_seconds_after_cycle(400), 1)

    def test_service_titan_continuous_loop_delays_first_startup_cycle(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_startup_delay_seconds=60,
        )
        stop_event = threading.Event()
        calls: list[str] = []
        thread = threading.Thread(target=ServiceTitanAuditLoop(audit_settings, lambda: calls.append("called")).run_loop, args=(stop_event,))
        thread.start()
        time.sleep(0.02)
        stop_event.set()
        thread.join(timeout=1)
        self.assertEqual(calls, [])

    def test_service_titan_continuous_loop_disabled_when_feature_flag_false(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=False)
        stop_event = threading.Event()
        calls: list[str] = []
        ServiceTitanAuditLoop(audit_settings, lambda: calls.append("called")).run_loop(stop_event)
        self.assertEqual(calls, [])

    def test_service_titan_polling_continues_if_one_job_fails_evaluation(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = FakeServiceTitan([st_job("bad"), st_job("good", clock_out_at=None)])
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
        original = audit._evaluate_job

        def evaluate(job: ServiceTitanJob):
            if job.job_id == "bad":
                raise RuntimeError("bad payload")
            return original(job)

        audit._evaluate_job = evaluate  # type: ignore[method-assign]
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(summary.errors, 1)
        self.assertEqual(len(self.h.slack.messages), 1)

    def test_service_titan_api_failure_does_not_crash_agent(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan(fail=True), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.status, "api_error")
        self.assertEqual(summary.errors, 1)
        self.assertEqual(self.h.slack.messages, [])

    def test_service_titan_first_startup_baselines_without_historical_alert_flood(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_backfill_alerts=False,
        )
        client = FakeServiceTitan([st_job("historical", clock_out_at=None)])
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.status, "baseline_initialized")
        self.assertTrue(summary.baseline_initialized)
        self.assertEqual(summary.alerts_sent, 0)
        self.assertEqual(summary.violations_detected, 0)
        self.assertEqual(self.h.slack.messages, [])
        self.assertIsNone(client.since_seen)
        self.assertIsNotNone(self.h.db.get_kv("servicetitan_audit_last_processed"))
        self.assertIsNone(self.h.db.get_service_titan_violation("servicetitan:historical:2001:tech_clock_out_missing:tech-1"))

    def test_service_titan_backfill_alerts_send_only_when_enabled(self) -> None:
        no_backfill = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_backfill_alerts=False,
        )
        disabled_audit = ServiceTitanAuditService(no_backfill, self.h.db, FakeServiceTitan([st_job("old-disabled", clock_out_at=None)]), self.h.slack)
        disabled = disabled_audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(disabled.status, "baseline_initialized")
        self.assertEqual(disabled.alerts_sent, 0)

        fresh_db_path = str(Path(self.h.tmp.name) / "backfill-enabled.sqlite3")
        backfill_db = Persistence(fresh_db_path)
        backfill_db.initialize()
        backfill = settings(
            fresh_db_path,
            service_titan_audit_enabled=True,
            service_titan_audit_backfill_alerts=True,
        )
        enabled_slack = FakeSlack()
        enabled_audit = ServiceTitanAuditService(backfill, backfill_db, FakeServiceTitan([st_job("old-enabled", clock_out_at=None)]), enabled_slack)
        enabled = enabled_audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(enabled.status, "completed")
        self.assertEqual(enabled.alerts_sent, 1)
        self.assertEqual(len(enabled_slack.messages), 1)

    def test_service_titan_max_alerts_per_cycle_caps_live_sends_and_keeps_checkpoint_open(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_max_alerts_per_cycle=1,
            sales_comfort_advisor_audit_enabled=False,
            dispatcher_audit_enabled=False,
            technician_compliance_enabled=True,
        )
        jobs = [
            st_job("capped-1", clock_out_at=None),
            st_job("capped-2", clock_out_at=None),
        ]
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan(jobs), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(summary.alerts_skipped_limit, 1)
        self.assertEqual(len(self.h.slack.messages), 1)
        first = self.h.db.get_service_titan_violation("servicetitan:capped-1:2001:tech_clock_out_missing:tech-1")
        second = self.h.db.get_service_titan_violation("servicetitan:capped-2:2001:tech_clock_out_missing:tech-1")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(first["alert_sent_at"])
        self.assertIsNone(second["alert_sent_at"])
        self.assertIsNone(self.h.db.get_kv("servicetitan_audit_last_processed"))

    def test_service_titan_max_alerts_per_cycle_does_not_cap_dry_run_would_send_count(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=True,
            service_titan_audit_max_alerts_per_cycle=1,
            sales_comfort_advisor_audit_enabled=False,
            dispatcher_audit_enabled=False,
            technician_compliance_enabled=True,
        )
        jobs = [
            st_job("dry-cap-1", clock_out_at=None),
            st_job("dry-cap-2", clock_out_at=None),
        ]
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan(jobs), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.alerts_sent, 0)
        self.assertEqual(summary.alerts_would_send, 2)
        self.assertEqual(summary.alerts_skipped_limit, 0)
        self.assertEqual(self.h.slack.messages, [])
        self.assertIsNone(self.h.db.get_service_titan_violation("servicetitan:dry-cap-1:2001:tech_clock_out_missing:tech-1"))

    def test_controlled_one_alert_backfill_suppresses_customer_name_even_if_enabled(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_backfill_alerts=True,
            service_titan_audit_max_alerts_per_cycle=1,
            service_titan_alert_include_customer_name=True,
            sales_comfort_advisor_audit_enabled=False,
            dispatcher_audit_enabled=False,
            technician_compliance_enabled=True,
        )
        job = st_job("manual-validation", clock_out_at=None, customer_name="Private Customer")
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(len(self.h.slack.messages), 1)
        self.assertNotIn("Private Customer", self.h.slack.messages[0][1])

    def test_controlled_one_alert_backfill_can_ignore_existing_checkpoint_once(self) -> None:
        self.h.db.set_kv("servicetitan_audit_last_processed", "2026-06-16T07:55:00+00:00")
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=False,
            service_titan_audit_backfill_alerts=True,
            service_titan_audit_ignore_checkpoint_once=True,
            service_titan_audit_max_alerts_per_cycle=1,
            sales_comfort_advisor_audit_enabled=False,
            dispatcher_audit_enabled=False,
            technician_compliance_enabled=True,
        )
        jobs = [
            st_job("checkpoint-ignore-1", clock_out_at=None),
            st_job("checkpoint-ignore-2", clock_out_at=None),
        ]
        client = FakeServiceTitan(jobs)
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
        summary = audit.audit_once(datetime(2026, 6, 16, 8, tzinfo=timezone.utc), require_enabled=False)
        self.assertTrue(summary.checkpoint_ignored)
        self.assertEqual(client.since_seen, datetime(2026, 6, 16, 4, tzinfo=timezone.utc))
        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(summary.alerts_skipped_limit, 1)
        self.assertEqual(self.h.db.get_kv("servicetitan_audit_last_processed"), "2026-06-16T07:55:00+00:00")

    def test_sales_only_client_prefilter_uses_configured_scope_before_enrichment(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={
                "rulesets": {
                    "Sales / Comfort Advisor Audit": {
                        "applies_to": {
                            "business_unit_ids": ["1812"],
                            "job_type_ids": ["1816"],
                            "workflows": None,
                            "statuses": ["Completed", "Closed"],
                        }
                    }
                }
            },
        )
        client = ServiceTitanClient(audit_settings)
        records = [
            {"id": 1, "status": "Completed", "businessUnitId": 1812, "jobTypeId": 1816},
            {"id": 2, "status": "Rescheduled", "businessUnitId": 1812, "jobTypeId": 1816},
            {"id": 3, "status": "Completed", "businessUnitId": 9999, "jobTypeId": 1816},
        ]
        filtered = client._prefilter_sales_only_records(records)
        self.assertEqual([record["id"] for record in filtered], [1])

    def test_sales_only_client_prefilter_is_disabled_without_scope_config(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={},
        )
        client = ServiceTitanClient(audit_settings)
        records = [
            {"id": 1, "status": "Completed", "businessUnitId": 1812, "jobTypeId": 1816},
            {"id": 2, "status": "Rescheduled", "businessUnitId": 1812, "jobTypeId": 1816},
        ]
        self.assertEqual(client._prefilter_sales_only_records(records), records)

    def test_scope_filter_skips_out_of_scope_jobs_before_enrichment(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        client = ScopeFilteringServiceTitan(
            audit_settings,
            [
                {"id": "plumbing-match", "status": "Completed", "businessUnitId": "64315277", "jobTypeId": "112338076"},
                {"id": "plumbing-sales", "status": "Completed", "businessUnitId": "64326403", "jobTypeId": "123562931"},
                {"id": "hvac-service", "status": "Completed", "businessUnitId": "1810", "jobTypeId": "1933"},
            ],
        )
        jobs = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))
        self.assertEqual([job.job_id for job in jobs], ["plumbing-match"])
        self.assertEqual(client.enriched_job_ids, ["plumbing-match"])
        self.assertEqual(client.last_scope_filter_stats["raw_jobs_fetched"], 3)
        self.assertEqual(client.last_scope_filter_stats["jobs_skipped_before_enrichment"], 2)
        self.assertEqual(client.last_scope_filter_stats["jobs_enriched"], 1)

    def test_scope_filter_keeps_sales_jobs_for_sales_ruleset(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=True,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={
                "rulesets": {
                    "Sales / Comfort Advisor Audit": {
                        "applies_to": {
                            "business_unit_ids": ["1812"],
                            "job_type_ids": ["1816"],
                            "workflows": None,
                            "statuses": ["Completed", "Closed"],
                        }
                    }
                }
            },
        )
        client = ScopeFilteringServiceTitan(
            audit_settings,
            [
                {"id": "sales-match", "status": "Completed", "businessUnitId": "1812", "jobTypeId": "1816"},
                {"id": "sales-wrong-status", "status": "Rescheduled", "businessUnitId": "1812", "jobTypeId": "1816"},
                {"id": "plumbing-service", "status": "Completed", "businessUnitId": "64315277", "jobTypeId": "112338076"},
            ],
        )
        jobs = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))
        self.assertEqual([job.job_id for job in jobs], ["sales-match"])
        self.assertEqual(client.enriched_job_ids, ["sales-match"])

    def test_scope_filter_keeps_hvac_jobs_when_hvac_enabled(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            plumbing_service_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        client = ScopeFilteringServiceTitan(
            audit_settings,
            [
                {
                    "id": "hvac-match",
                    "status": "Completed",
                    "businessUnit": {"id": "1810", "name": "HVAC - Service"},
                    "jobType": {"id": "1933", "name": "HVAC Diagnostic"},
                },
                {
                    "id": "plumbing-service",
                    "status": "Completed",
                    "businessUnit": {"id": "64315277", "name": "Plumbing - Service"},
                    "jobType": {"id": "112338076", "name": "Plumbing Diagnostic"},
                },
            ],
        )
        jobs = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))
        self.assertEqual([job.job_id for job in jobs], ["hvac-match"])
        self.assertEqual(client.enriched_job_ids, ["hvac-match"])

    def test_scope_filter_disabled_rulesets_do_not_trigger_enrichment(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        client = ScopeFilteringServiceTitan(
            audit_settings,
            [{"id": "raw-job", "status": "Completed", "businessUnitId": "64315277", "jobTypeId": "112338076"}],
        )
        jobs = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))
        self.assertEqual(jobs, [])
        self.assertEqual(client.enriched_job_ids, [])
        self.assertEqual(client.last_scope_filter_stats["jobs_skipped_before_enrichment"], 1)

    def test_service_titan_audit_summary_includes_scope_filter_counts(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        client = FakeServiceTitan([])
        client.last_scope_filter_stats = {"raw_jobs_fetched": 5, "jobs_skipped_before_enrichment": 4, "jobs_enriched": 1}
        summary = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack).audit_once(
            datetime(2026, 5, 15, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(summary.raw_jobs_fetched, 5)
        self.assertEqual(summary.jobs_skipped_before_enrichment, 4)
        self.assertEqual(summary.jobs_enriched, 1)
        self.assertIn(
            "ServiceTitan audit scope filter: raw=5, skipped_before_enrichment=4, enriched=1",
            summary.to_lines(),
        )

    def test_scope_discovery_does_not_apply_sales_only_prefilter(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={
                "rulesets": {
                    "Sales / Comfort Advisor Audit": {
                        "applies_to": {
                            "business_unit_ids": ["1812"],
                            "job_type_ids": ["1816"],
                            "workflows": None,
                            "statuses": ["Completed"],
                        }
                    }
                }
            },
        )
        http = st_enrichment_http(
            job_payload={
                "id": "hvac-discovery",
                "jobNumber": "J-hvac-discovery",
                "status": "Completed",
                "modifiedOn": "2026-05-15T16:00:00Z",
                "businessUnitId": "9999",
                "businessUnit": {"id": "9999", "name": "HVAC Service"},
                "jobTypeId": "2000",
                "jobType": {"id": "2000", "name": "HVAC Diagnostic"},
                "trade": "HVAC",
                "workflow": "HVAC Service",
            }
        )
        discovery = ServiceTitanScopeDiscovery(audit_settings, ServiceTitanClient(audit_settings, http))
        summary = discovery.run_once(datetime(2026, 5, 15, 17, tzinfo=timezone.utc))
        self.assertEqual(summary.jobs_scanned, 1)
        self.assertIn("HVAC Service", summary.value_counts["business_units"])

    def test_sales_only_client_skips_unneeded_related_categories_when_photos_disabled(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=["sales_photos_missing"],
        )
        client = ServiceTitanClient(audit_settings)
        self.assertTrue(client._should_fetch_related_category("appointments"))
        self.assertTrue(client._should_fetch_related_category("appointment_assignments"))
        self.assertTrue(client._should_fetch_related_category("estimates"))
        self.assertTrue(client._should_fetch_related_category("opportunities"))
        self.assertFalse(client._should_fetch_related_category("invoices"))
        self.assertFalse(client._should_fetch_related_category("technician_time"))
        self.assertFalse(client._should_fetch_related_category("attachments"))
        self.assertFalse(client._should_fetch_related_category("forms"))

    def test_sales_only_client_fetches_photo_sources_when_photos_rule_enabled(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=[],
        )
        client = ServiceTitanClient(audit_settings)
        self.assertTrue(client._should_fetch_related_category("attachments"))
        self.assertTrue(client._should_fetch_related_category("forms"))

    def test_hvac_only_client_skips_unneeded_related_categories_when_photo_and_form_rules_disabled(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=["hvac_required_photos_missing", "hvac_diagnosis_form_missing"],
        )
        client = ServiceTitanClient(audit_settings)
        self.assertTrue(client._should_fetch_related_category("appointments"))
        self.assertTrue(client._should_fetch_related_category("appointment_assignments"))
        self.assertTrue(client._should_fetch_related_category("invoices"))
        self.assertTrue(client._should_fetch_related_category("estimates"))
        self.assertTrue(client._should_fetch_related_category("opportunities"))
        self.assertFalse(client._should_fetch_related_category("invoice_items"))
        self.assertFalse(client._should_fetch_related_category("technician_time"))
        self.assertFalse(client._should_fetch_related_category("attachments"))
        self.assertFalse(client._should_fetch_related_category("forms"))
        self.assertFalse(client._should_fetch_related_category("notes"))
        self.assertEqual(client._related_skip_reason("forms"), "forms skipped for HVAC-only enabled rules")

    def test_service_titan_pass_and_insufficient_results_do_not_alert(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        pass_audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([st_job("pass-job")]), self.h.slack)
        pass_summary = pass_audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(pass_summary.alerts_sent, 0)
        self.assertEqual(pass_summary.alerts_would_send, 0)
        self.assertEqual(self.h.slack.messages, [])

        insufficient_job = st_job("insufficient-job", present_fields={"status"})
        insufficient_audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([insufficient_job]), self.h.slack)
        insufficient_summary = insufficient_audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertGreater(sum(insufficient_summary.insufficient_data_by_rule.values()), 0)
        self.assertEqual(insufficient_summary.alerts_sent, 0)
        self.assertEqual(insufficient_summary.alerts_would_send, 0)
        self.assertEqual(self.h.slack.messages, [])

        not_applicable_job = st_job("not-applicable-job", status="Canceled", clock_out_at=None, photo_count=0)
        not_applicable_audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([not_applicable_job]), self.h.slack)
        not_applicable_summary = not_applicable_audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertGreater(sum(not_applicable_summary.not_applicable_by_rule.values()), 0)
        self.assertEqual(not_applicable_summary.alerts_sent, 0)
        self.assertEqual(not_applicable_summary.alerts_would_send, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_service_titan_auth_403_returns_api_error_without_logging_crash(self) -> None:
        logging.disable(logging.NOTSET)
        audit_logger = logging.getLogger("marketing_os_agent.domain.service_titan_audit")
        old_propagate = audit_logger.propagate
        handler = logging.NullHandler()
        audit_logger.addHandler(handler)
        audit_logger.propagate = False
        try:
            audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
            http = FakeHttp([HttpResponse(403, {"error": "error code: 1010"}, {})])
            client = ServiceTitanClient(audit_settings, http)
            audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
            summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
            self.assertEqual(summary.status, "api_error")
            self.assertEqual(summary.errors, 1)
            self.assertEqual(self.h.slack.messages, [])
            self.assertEqual(http.calls, [audit_settings.servicetitan_auth_url])

            request = http.requests[0]
            self.assertEqual(request["kind"], "form")
            self.assertEqual(request["method"], "POST")
            self.assertEqual(request["url"], audit_settings.servicetitan_auth_url)
            headers = request["headers"]
            body = request["body"]
            self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
            self.assertEqual(headers["User-Agent"], DEFAULT_USER_AGENT)
            self.assertEqual(headers["Accept"], "application/json")
            self.assertEqual(body["grant_type"], "client_credentials")
            self.assertEqual(body["client_id"], audit_settings.servicetitan_client_id)
            self.assertEqual(body["client_secret"], audit_settings.servicetitan_client_secret)
        finally:
            audit_logger.removeHandler(handler)
            audit_logger.propagate = old_propagate
            logging.disable(logging.CRITICAL)

    def test_service_titan_slack_failure_retries_alert_later(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = FakeServiceTitan([st_job("retry-st", clock_out_at=None)])
        failing_slack = FailingSlack()
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, failing_slack)
        failed = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(failed.alerts_sent, 0)
        violation = self.h.db.get_service_titan_violation("servicetitan:retry-st:2001:tech_clock_out_missing:tech-1")
        self.assertIsNotNone(violation)
        self.assertIsNone(violation["alert_sent_at"])

        audit.slack = self.h.slack
        retried = audit.audit_once(datetime(2026, 5, 15, 16, 5, tzinfo=timezone.utc))
        self.assertEqual(retried.alerts_sent, 1)
        self.assertEqual(len(self.h.slack.messages), 1)

    def test_service_titan_dry_run_evaluates_without_alerts_or_dedupe_writes(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=True,
            slack_alert_channel_id="",
            slack_marketing_ops_channel_id="",
        )
        client = FakeServiceTitan([st_job("dry-run-st", clock_out_at=None)])
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.status, "completed")
        self.assertTrue(summary.dry_run)
        self.assertEqual(summary.jobs_scanned, 1)
        self.assertEqual(summary.alerts_sent, 0)
        self.assertEqual(summary.alerts_would_send, 1)
        self.assertEqual(self.h.slack.messages, [])
        self.assertIsNone(self.h.db.get_service_titan_violation("servicetitan:dry-run-st:2001:tech_clock_out_missing:tech-1"))
        self.assertIsNone(self.h.db.get_kv("servicetitan_audit_last_processed"))

    def test_service_titan_dry_run_does_not_resolve_existing_violations(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=True,
        )
        pass_job = st_job("dry-pass")
        pass_result = _st_rule(audit_settings, "tech_clock_out_missing").run(pass_job, audit_settings)
        not_applicable_job = st_job("dry-not-applicable", status="Canceled", clock_out_at=None, photo_count=0)
        not_applicable_result = _st_rule(audit_settings, "dispatch_photos_missing").run(not_applicable_job, audit_settings)
        self.assertEqual(pass_result.status, RESULT_PASS)
        self.assertEqual(not_applicable_result.status, RESULT_NOT_APPLICABLE)
        for job, result in ((pass_job, pass_result), (not_applicable_job, not_applicable_result)):
            self.h.db.upsert_service_titan_violation(
                violation_key=result.violation_key,
                service_titan_job_id=job.job_id,
                appointment_id=job.appointment_id,
                technician_id=job.technician_id,
                technician_name=job.technician_name,
                dispatcher_id=job.dispatcher_id,
                dispatcher_name=job.dispatcher_name,
                rule_id=result.rule_id,
                ruleset=result.ruleset,
                severity=result.severity,
                title=result.title,
                description=result.explanation,
                recommended_action=result.recommended_action,
                metadata={},
            )

        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([pass_job, not_applicable_job]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertTrue(summary.dry_run)
        for key in (pass_result.violation_key, not_applicable_result.violation_key):
            violation = self.h.db.get_service_titan_violation(key)
            self.assertIsNotNone(violation)
            self.assertEqual(violation["status"], "open")
            self.assertIsNone(violation["resolved_at"])

    def test_service_titan_missing_credentials_return_config_error(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True, servicetitan_client_secret="")
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.status, "config_error")
        self.assertIn("SERVICETITAN_CLIENT_SECRET", summary.config_errors)

    def test_service_titan_live_alert_requires_slack_token_and_channel(self) -> None:
        missing_token = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True, slack_bot_token="")
        token_audit = ServiceTitanAuditService(missing_token, self.h.db, FakeServiceTitan([st_job(clock_out_at=None)]), self.h.slack)
        token_summary = token_audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(token_summary.status, "config_error")
        self.assertIn("SLACK_BOT_TOKEN", token_summary.config_errors)

        missing_channel = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True, slack_alert_channel_id="", slack_marketing_ops_channel_id="COPS")
        channel_audit = ServiceTitanAuditService(missing_channel, self.h.db, FakeServiceTitan([st_job(clock_out_at=None)]), self.h.slack)
        channel_summary = channel_audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(channel_summary.status, "config_error")
        self.assertIn("SLACK_ALERT_CHANNEL_ID", channel_summary.config_errors)

    def test_service_titan_cli_force_can_validate_when_continuous_polling_disabled(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=False, service_titan_audit_dry_run=True)
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([st_job("forced", clock_out_at=None)]), self.h.slack)
        skipped = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        forced = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc), require_enabled=False)
        self.assertEqual(skipped.status, "disabled")
        self.assertEqual(forced.status, "completed")
        self.assertEqual(forced.alerts_would_send, 1)

    def test_service_titan_rulesets_can_be_disabled_independently(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=True,
        )
        rule_ids = {rule.rule_id for rule in active_service_titan_rules(audit_settings)}
        self.assertNotIn("tech_clock_out_missing", rule_ids)
        self.assertIn("dispatch_notes_missing", rule_ids)

    def test_dispatcher_audit_does_not_auto_attach_handbook_rules(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=True,
        )
        rule_ids = {rule.rule_id for rule in active_service_titan_rules(audit_settings)}
        self.assertIn("dispatch_notes_missing", rule_ids)
        self.assertNotIn("missing_hhr_or_service_form", rule_ids)
        self.assertNotIn("po_not_synced_to_service_titan", rule_ids)

    def test_dispatcher_audit_rule_allowlist_limits_dispatch_rules(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=True,
            dispatcher_audit_rule_ids=["dispatch_notes_missing"],
        )
        rule_ids = {rule.rule_id for rule in active_service_titan_rules(audit_settings)}
        self.assertEqual(rule_ids, {"dispatch_notes_missing"})

    def test_dispatcher_alert_uses_dispatcher_channel_when_configured(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=False,
            service_titan_audit_backfill_alerts=True,
            service_titan_audit_max_alerts_per_cycle=1,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=True,
            dispatcher_audit_slack_channel_id="C-DISPATCH",
            dispatcher_audit_rule_ids=["dispatch_notes_missing"],
            slack_alert_channel_id="C-GENERAL",
        )
        job = st_job("dispatch-1", notes="", present_fields={"status", "notes", "business_unit", "job_type"})
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack)

        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))

        self.assertEqual(summary.alerts_sent, 1)
        self.assertEqual(self.h.slack.messages[0][0], "C-DISPATCH")
        self.assertNotEqual(self.h.slack.messages[0][0], "C-GENERAL")

    def test_handbook_rule_matrix_loads_required_metadata(self) -> None:
        matrix = handbook_rule_matrix()
        rule_ids = {rule.rule_id for rule in matrix}
        self.assertIn("missing_hhr_or_service_form", rule_ids)
        self.assertIn("po_not_synced_to_service_titan", rule_ids)
        self.assertIn("defective_part_missing_warranty_claim_data", rule_ids)
        for rule in matrix:
            self.assertTrue(rule.handbook_source)
            self.assertTrue(rule.required_data_fields)
            self.assertTrue(rule.data_sources)
            self.assertIn(rule.current_availability, {"available", "partially_available", "unavailable", "unknown"})
            self.assertTrue(rule.recommended_alert_recipient)
            self.assertEqual(rule.delivery, "immediate")

    def test_service_titan_active_rules_have_scope_metadata(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        for rule in active_service_titan_rules(audit_settings):
            metadata = rule.scope.to_metadata()
            self.assertIn("handbook_source", metadata)
            self.assertIn("applies_to_departments", metadata)
            self.assertIn("applies_to_business_units", metadata)
            self.assertIn("applies_to_trades", metadata)
            self.assertIn("applies_to_job_types", metadata)
            self.assertIn("applies_to_job_statuses", metadata)
            self.assertIn("applies_to_tags", metadata)
            self.assertIn("applies_to_campaigns", metadata)
            self.assertIn("applies_to_roles", metadata)
            self.assertIn("applies_to_workflows", metadata)
            self.assertIn("excludes_job_types", metadata)
            self.assertIn("excludes_statuses", metadata)
            self.assertIn("required_context_fields", metadata)
            self.assertIn("required_data_fields", metadata)
            self.assertIn("alert_routing", metadata)
            self.assertIn("default_enabled", metadata)

    def test_service_titan_rule_scope_config_can_disable_or_narrow_rules(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_rule_scope_config={
                "rules": {
                    "dispatch_notes_missing": {"enabled": "false"},
                    "dispatch_photos_missing": {"applies_to": {"job_types_contains": ["Install"]}},
                }
            },
        )
        self.assertEqual(_st_rule(audit_settings, "dispatch_notes_missing").run(st_job(), audit_settings).status, RESULT_NOT_APPLICABLE)
        self.assertEqual(_st_rule(audit_settings, "dispatch_photos_missing").run(st_job(), audit_settings).status, RESULT_NOT_APPLICABLE)

    def test_sales_rules_return_not_applicable_for_non_sales_job(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        job = st_job()
        for rule_id in ("sales_options_fewer_than_three", "sales_photos_missing", "sales_arrival_after_first_half"):
            self.assertEqual(_st_rule(audit_settings, rule_id).run(job, audit_settings).status, RESULT_NOT_APPLICABLE)

    def test_sales_options_rule_passes_fails_and_handles_missing_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "sales_options_fewer_than_three")
        self.assertEqual(rule.run(sales_job(estimate_count=3), audit_settings).status, RESULT_PASS)

        fail = rule.run(sales_job(estimate_count=2), audit_settings)
        self.assertEqual(fail.status, RESULT_FAIL)
        self.assertEqual(fail.metadata["options_count"], 2)

        insufficient = rule.run(
            sales_job(
                estimate_count=None,
                present_fields={"status", "business_unit", "job_type", "department", "trade", "workflow", "tags"},
            ),
            audit_settings,
        )
        self.assertEqual(insufficient.status, RESULT_INSUFFICIENT)

    def test_sales_photos_rule_passes_fails_and_handles_missing_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "sales_photos_missing")
        self.assertEqual(rule.run(sales_job(photo_count=1), audit_settings).status, RESULT_PASS)

        fail = rule.run(sales_job(photo_count=0), audit_settings)
        self.assertEqual(fail.status, RESULT_FAIL)
        self.assertEqual(fail.metadata["photos_count"], 0)

        insufficient = rule.run(
            sales_job(
                photo_count=None,
                present_fields={"status", "business_unit", "job_type", "department", "trade", "workflow", "tags"},
            ),
            audit_settings,
        )
        self.assertEqual(insufficient.status, RESULT_INSUFFICIENT)

    def test_sales_arrival_first_half_rule_passes_fails_and_handles_missing_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "sales_arrival_after_first_half")
        window_start = datetime(2026, 5, 15, 10, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 15, 12, tzinfo=timezone.utc)

        on_time = rule.run(
            sales_job(arrival_window_start=window_start, arrival_window_end=window_end, arrived_at=datetime(2026, 5, 15, 10, 45, tzinfo=timezone.utc)),
            audit_settings,
        )
        self.assertEqual(on_time.status, RESULT_PASS)

        late = rule.run(
            sales_job(arrival_window_start=window_start, arrival_window_end=window_end, arrived_at=datetime(2026, 5, 15, 11, 1, tzinfo=timezone.utc)),
            audit_settings,
        )
        self.assertEqual(late.status, RESULT_FAIL)
        self.assertIn("arrival_first_half_cutoff", late.metadata)

        missing = rule.run(
            sales_job(
                arrival_window_start=window_start,
                arrival_window_end=None,
                arrived_at=None,
                present_fields={"status", "business_unit", "job_type", "department", "trade", "workflow", "tags"},
            ),
            audit_settings,
        )
        self.assertEqual(missing.status, RESULT_INSUFFICIENT)

    def test_job_left_open_after_visit_rule_is_opt_in(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        self.assertNotIn("job_left_open_after_visit", [rule.rule_id for rule in active_service_titan_rules(audit_settings)])

        enabled = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={"rules": {"job_left_open_after_visit": {"enabled": True}}},
        )
        self.assertIn("job_left_open_after_visit", [rule.rule_id for rule in active_service_titan_rules(enabled)])

    def test_job_left_open_after_visit_rule_results(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={"rules": {"job_left_open_after_visit": {"enabled": True}}},
        )
        rule = _st_rule(audit_settings, "job_left_open_after_visit")
        past_end = datetime.now(timezone.utc) - timedelta(hours=3)
        recent_end = datetime.now(timezone.utc) - timedelta(minutes=30)

        self.assertEqual(rule.run(st_job(status="Completed", arrival_window_end=past_end), audit_settings).status, RESULT_NOT_APPLICABLE)
        self.assertEqual(rule.run(st_job(status="In Progress", arrival_window_end=past_end), audit_settings).status, RESULT_FAIL)
        self.assertEqual(rule.run(st_job(status="In Progress", arrival_window_end=recent_end), audit_settings).status, RESULT_NOT_APPLICABLE)

        missing_end = rule.run(
            st_job(
                status="In Progress",
                arrival_window_end=None,
                present_fields={"status", "business_unit", "job_type", "arrival_window"},
            ),
            audit_settings,
        )
        self.assertEqual(missing_end.status, RESULT_INSUFFICIENT)

        missing_status = rule.run(
            st_job(
                status="",
                arrival_window_end=past_end,
                present_fields={"arrival_window"},
            ),
            audit_settings,
        )
        self.assertEqual(missing_status.status, RESULT_INSUFFICIENT)

    def test_sales_scope_config_supports_business_unit_tags_and_campaign(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_rule_scope_config={
                "rulesets": {
                    "Sales / Comfort Advisor Audit": {
                        "applies_to": {
                            "business_units": ["Replacement Sales"],
                            "tags": ["Comfort Advisor"],
                            "campaigns": ["Retail Lead"],
                        }
                    }
                }
            },
        )
        matching = sales_job(
            business_unit_name="Replacement Sales",
            tag_names=["Comfort Advisor"],
            campaign_name="Retail Lead",
            workflow="Replacement Sales",
        )
        self.assertEqual(_st_rule(audit_settings, "sales_photos_missing").run(matching, audit_settings).status, RESULT_PASS)

        non_matching = sales_job(
            business_unit_name="Replacement Sales",
            tag_names=["Comfort Advisor"],
            campaign_name="Commercial Lead",
            workflow="Replacement Sales",
        )
        self.assertEqual(_st_rule(audit_settings, "sales_photos_missing").run(non_matching, audit_settings).status, RESULT_NOT_APPLICABLE)

    def test_sales_scope_config_can_match_tenant_ids_without_workflow_names(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_rule_scope_config={
                "rulesets": {
                    "Sales / Comfort Advisor Audit": {
                        "applies_to": {
                            "business_unit_ids": ["1809"],
                            "job_type_ids": ["1815"],
                            "tag_ids": ["78"],
                            "workflows": None,
                        }
                    }
                }
            },
        )
        job = sales_job(
            business_unit_id="1809",
            business_unit_name="",
            job_type_id="1815",
            job_type_name="",
            department="",
            trade="",
            workflow="",
            tag_ids=["78"],
            tag_names=[],
            photo_count=1,
            present_fields={
                "status",
                "business_unit",
                "job_type",
                "tags",
                "photos",
                "estimates",
                "arrival_window",
                "arrived_at",
            },
        )
        self.assertEqual(_st_rule(audit_settings, "sales_photos_missing").run(job, audit_settings).status, RESULT_PASS)

    def test_sales_not_applicable_and_insufficient_do_not_send_slack(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        not_applicable = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([st_job("not-sales")]), self.h.slack)
        not_applicable_summary = not_applicable.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(not_applicable_summary.sales_not_applicable, 3)
        self.assertEqual(not_applicable_summary.alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

        insufficient_job = sales_job(
            "sales-insufficient",
            estimate_count=None,
            photo_count=None,
            arrival_window_start=None,
            arrival_window_end=None,
            arrived_at=None,
            present_fields={"status", "business_unit", "job_type", "department", "trade", "workflow", "tags"},
        )
        insufficient = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([insufficient_job]), self.h.slack)
        insufficient_summary = insufficient.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(insufficient_summary.sales_insufficient_data, 3)
        self.assertEqual(insufficient_summary.alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_sales_fail_sends_slack_only_when_live_and_configured(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        job = sales_job(
            "sales-fail",
            business_unit_id="1812",
            business_unit_name="HVAC - Sales",
            job_type_id="1816",
            job_type_name="HVAC Estimate",
            estimate_count=2,
            photo_count=1,
            arrived_at=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
        )
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.sales_fail, 1)
        self.assertEqual(summary.sales_alerts_sent, 1)
        self.assertEqual(len(self.h.slack.messages), 1)
        self.assertEqual(self.h.slack.messages[0][0], "CST")
        alert_text = self.h.slack.messages[0][1]
        self.assertIn("🚨 HIGH - HVAC Sales / Comfort Advisors: Fewer Than 3 Options", alert_text)
        self.assertIn("Job Type: HVAC Estimate", alert_text)
        self.assertIn("Options: 2 of 3 required", alert_text)
        self.assertIn("Open in ServiceTitan:", alert_text)
        for internal_field in ("Rule ID", "Ruleset", "BU ID", "BU Name: <missing>", "Destination", "Delivery", "*Job:*"):
            self.assertNotIn(internal_field, alert_text)

        missing_slack = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            slack_bot_token="",
        )
        missing_summary = ServiceTitanAuditService(missing_slack, self.h.db, FakeServiceTitan([job]), self.h.slack).audit_once(
            datetime(2026, 5, 15, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(missing_summary.status, "config_error")

    def test_servicetitan_alert_business_unit_labels(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([]), self.h.slack)
        rule = _st_rule(audit_settings, "sales_options_fewer_than_three")
        cases = [
            ("1812", "HVAC - Sales", "1816", "HVAC Estimate", "HVAC Sales / Comfort Advisors"),
            ("64326403", "Plumbing - Sales", "54086644", "Water Heater Estimate", "Plumbing Sales"),
            ("64315277", "Plumbing - Service", "112338076", "Plumbing Diagnostic", "Plumbing Service"),
            ("999999", "Other", "other-type", "Other Type", "Unknown Business Unit"),
            ("", "", "", "", "Unknown Business Unit"),
        ]
        for business_unit_id, business_unit_name, job_type_id, job_type_name, expected_label in cases:
            with self.subTest(business_unit_id=business_unit_id or "<missing>"):
                job = sales_job(
                    f"sales-label-{business_unit_id or 'missing'}",
                    business_unit_id=business_unit_id,
                    business_unit_name=business_unit_name,
                    job_type_id=job_type_id,
                    job_type_name=job_type_name,
                    estimate_count=2,
                    photo_count=1,
                )
                result = rule.run(job, audit_settings)
                text = audit._alert_text(job, result)
                display_label = expected_label if expected_label != "Unknown Business Unit" else "Unknown"
                self.assertIn(display_label, text)
                self.assertNotIn("*BU ID:*", text)
                self.assertNotIn("*BU Name:*", text)

    def test_servicetitan_alert_text_is_compact_and_rule_specific(self) -> None:
        hvac_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        audit = ServiceTitanAuditService(hvac_settings, self.h.db, FakeServiceTitan([]), self.h.slack)
        payment_job = hvac_job(
            "130522211",
            business_unit_id="1810",
            business_unit_name="HVAC - Service",
            job_type_id="1933",
            job_type_name="HVAC Diagnostic",
            technician_name="Eduardo Loera-Gaeta",
            invoice_total=115.0,
            invoice_balance=115.0,
            payment_total=0.0,
            payments_count=0,
            invoice_status="",
            customer_name="Private Customer",
            notes="Private raw customer note",
            tag_names=["Private Address 123 Secret St"],
        )
        payment_result = _st_rule(hvac_settings, "hvac_payment_missing_on_completed_job").run(payment_job, hvac_settings)
        payment_text = audit._alert_text(payment_job, payment_result)
        self.assertIn("🚨 HIGH - HVAC Service: Missing Payment", payment_text)
        self.assertIn("Technician: Eduardo Loera-Gaeta", payment_text)
        self.assertIn("Job Type: HVAC Diagnostic", payment_text)
        self.assertIn("Invoice: $115.00 total / $115.00 balance", payment_text)
        self.assertIn("Open in ServiceTitan:", payment_text)
        for forbidden in ("Rule ID", "Ruleset", "BU ID", "BU Name: <missing>", "Destination", "Delivery", "*Job:*"):
            self.assertNotIn(forbidden, payment_text)
        for pii in ("Private Customer", "Private raw customer note", "123 Secret St"):
            self.assertNotIn(pii, payment_text)

        plumbing_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        plumbing_audit = ServiceTitanAuditService(plumbing_settings, self.h.db, FakeServiceTitan([]), self.h.slack)
        options_job = plumbing_job("128859287", estimate_count=1, invoice_total=430.0, invoice_balance=0.0, payment_total=430.0)
        options_result = _st_rule(plumbing_settings, "plumbing_options_fewer_than_three").run(options_job, plumbing_settings)
        options_text = plumbing_audit._alert_text(options_job, options_result)
        self.assertIn("🚨 HIGH - Plumbing Service: Fewer Than 3 Options", options_text)
        self.assertIn("Options: 1 of 3 required", options_text)
        self.assertIn("Invoice: $430.00 total / $0.00 balance", options_text)

    def test_job_left_open_alert_text_has_no_customer_pii(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={"rules": {"job_left_open_after_visit": {"enabled": True}}},
        )
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([]), self.h.slack)
        rule = _st_rule(audit_settings, "job_left_open_after_visit")
        job = st_job(
            "open-job",
            status="In Progress",
            business_unit_id="1810",
            business_unit_name="HVAC - Service",
            job_type_name="HVAC Diagnostic",
            technician_name="Tech One",
            arrival_window_end=datetime.now(timezone.utc) - timedelta(hours=3),
            customer_name="Private Customer",
            notes="Private raw customer note",
            tag_names=["Customer phone 555-1212"],
        )
        result = rule.run(job, audit_settings)
        self.assertEqual(result.status, RESULT_FAIL)
        text = audit._alert_text(job, result)
        self.assertIn("⚠️ MEDIUM - HVAC Service: Job Still Open After Visit", text)
        self.assertIn("Current status: In Progress", text)
        self.assertIn("Open in ServiceTitan:", text)
        for pii in ("Private Customer", "Private raw customer note", "555-1212"):
            self.assertNotIn(pii, text)

    def test_sales_dry_run_summary_includes_business_unit_label_without_slack(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        job = sales_job(
            "sales-dry-run-bu",
            business_unit_id="64326403",
            business_unit_name="Plumbing - Sales",
            job_type_id="54086644",
            job_type_name="Water Heater Estimate",
            estimate_count=2,
            photo_count=1,
        )
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.sales_alerts_would_send, 1)
        self.assertEqual(summary.sales_alerts_sent, 0)
        self.assertEqual(summary.alert_business_unit_counts, {"Plumbing Sales": 1})
        self.assertIn("  - Plumbing Sales: 1", "\n".join(summary.to_lines()))
        self.assertEqual(self.h.slack.messages, [])

    def test_sales_dry_run_sends_no_slack_and_writes_no_violation(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        job = sales_job("sales-dry-run", estimate_count=2)
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertTrue(summary.dry_run)
        self.assertEqual(summary.sales_alerts_would_send, 1)
        self.assertEqual(summary.sales_alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])
        self.assertIsNone(self.h.db.get_service_titan_violation("servicetitan:sales-dry-run:2001:sales_options_fewer_than_three:advisor-1"))

    def test_service_titan_weekly_summary_groups_by_business_unit_rule_severity_and_status(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path)
        _insert_service_titan_violation(
            self.h.db,
            violation_key="weekly-hvac-open",
            rule_id="sales_options_fewer_than_three",
            severity="high",
            metadata={
                "business_unit_id": "1812",
                "business_unit_name": "HVAC - Sales",
                "job_type_id": "1816",
                "job_type_name": "HVAC Estimate",
            },
        )
        _insert_service_titan_violation(
            self.h.db,
            violation_key="weekly-hvac-resolved",
            rule_id="sales_arrival_after_first_half",
            severity="medium",
            metadata={
                "business_unit_id": "1812",
                "business_unit_name": "HVAC - Sales",
                "customer_name": "Private Customer",
                "address": "123 Secret St",
                "email": "private@example.com",
                "raw_notes": "Private raw note",
            },
            resolve=True,
        )
        _insert_service_titan_violation(
            self.h.db,
            violation_key="weekly-unknown-open",
            rule_id="sales_options_fewer_than_three",
            severity="high",
            metadata={},
        )
        _insert_service_titan_violation(
            self.h.db,
            violation_key="weekly-install-open",
            rule_id="install_job_not_marked_complete",
            ruleset="Installer Audit",
            severity="high",
            metadata={"business_unit_id": "1809,64313020", "business_unit_name": "Installer Audit"},
        )
        service = ServiceTitanWeeklySummaryService(audit_settings, self.h.db, self.h.slack)
        summary = service.build_summary(datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 1, tzinfo=timezone.utc))
        text = summary.message_text()

        self.assertEqual(summary.total_violations, 4)
        self.assertEqual(summary.business_unit_counts["HVAC Sales / Comfort Advisors"], 2)
        self.assertEqual(summary.business_unit_counts["Unknown Business Unit"], 1)
        self.assertEqual(summary.business_unit_counts["Installs"], 1)
        self.assertEqual(summary.severity_counts, {"high": 3, "medium": 1})
        self.assertEqual(summary.status_counts, {"open": 3, "resolved": 1})
        self.assertIn("HVAC Sales / Comfort Advisors", text)
        self.assertIn("BU ID: 1812", text)
        self.assertIn("sales_options_fewer_than_three [high] open: 1", text)
        self.assertIn("sales_arrival_after_first_half [medium] resolved: 1", text)
        self.assertIn("Installs", text)
        self.assertIn("BU ID: 1809,64313020", text)
        self.assertIn("Ruleset: Installer Audit", text)
        self.assertIn("install_job_not_marked_complete [high] open: 1", text)
        self.assertIn("Unknown Business Unit", text)
        self.assertNotIn("Private Customer", text)
        self.assertNotIn("123 Secret St", text)
        self.assertNotIn("private@example.com", text)
        self.assertNotIn("Private raw note", text)
        self.assertNotIn("Private Advisor", text)

    def test_service_titan_weekly_summary_dry_run_builds_summary_without_slack(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_dry_run=True,
            service_titan_weekly_summary_enabled=True,
        )
        _insert_service_titan_violation(
            self.h.db,
            violation_key="weekly-dry-run",
            metadata={"business_unit_id": "64326403", "business_unit_name": "Plumbing - Sales"},
        )
        service = ServiceTitanWeeklySummaryService(audit_settings, self.h.db, self.h.slack)
        summary = service.run_once(datetime.now(timezone.utc))
        text = summary.to_text()

        self.assertTrue(summary.dry_run)
        self.assertTrue(summary.slack_skipped_dry_run)
        self.assertFalse(summary.slack_sent)
        self.assertEqual(self.h.slack.messages, [])
        self.assertIn("Plumbing Sales", text)
        self.assertIn("sales_options_fewer_than_three [high] open: 1", text)

    def test_service_titan_weekly_summary_uses_slack_alert_channel_and_dedupes_period(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_weekly_summary_enabled=True,
            service_titan_weekly_summary_lookback_days=7,
        )
        _insert_service_titan_violation(
            self.h.db,
            violation_key="weekly-send",
            metadata={"business_unit_id": "1812", "business_unit_name": "HVAC - Sales"},
        )
        service = ServiceTitanWeeklySummaryService(audit_settings, self.h.db, self.h.slack)
        now = datetime.now(timezone.utc)
        first = service.run_once(now)
        second = service.run_once(now)

        self.assertTrue(first.slack_sent)
        self.assertEqual(len(self.h.slack.messages), 1)
        self.assertEqual(self.h.slack.messages[0][0], "CST")
        self.assertIn("ServiceTitan Weekly Audit Summary", self.h.slack.messages[0][1])
        self.assertTrue(second.slack_skipped_duplicate)
        self.assertEqual(len(self.h.slack.messages), 1)

    def test_service_titan_weekly_summary_disabled_does_nothing_automatically(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_weekly_summary_enabled=False,
        )
        _insert_service_titan_violation(self.h.db, violation_key="weekly-disabled")
        service = ServiceTitanWeeklySummaryService(audit_settings, self.h.db, self.h.slack)
        summary = service.run_once(datetime.now(timezone.utc))

        self.assertEqual(summary.status, "disabled")
        self.assertTrue(summary.slack_skipped_disabled)
        self.assertEqual(self.h.slack.messages, [])
        self.assertFalse(service.should_run_at(datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)))

    def test_service_titan_weekly_summary_schedule_uses_configured_day_and_hour(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_weekly_summary_enabled=True,
            service_titan_weekly_summary_day="MON",
            service_titan_weekly_summary_hour=8,
        )
        service = ServiceTitanWeeklySummaryService(audit_settings, self.h.db, self.h.slack)

        self.assertTrue(service.should_run_at(datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)))
        self.assertFalse(service.should_run_at(datetime(2026, 6, 22, 8, 1, tzinfo=timezone.utc)))
        self.assertFalse(service.should_run_at(datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)))

    def test_service_titan_runtime_diagnostics_masks_config_and_shows_state(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=False,
            service_titan_audit_backfill_alerts=False,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=["sales_photos_missing"],
            service_titan_rule_scope_config={
                "rulesets": {
                    "Sales / Comfort Advisor Audit": {
                        "applies_to": {
                            "business_unit_ids": ["fake-sales-bu-id"],
                            "job_type_ids": ["fake-sales-job-type-id"],
                            "workflows": None,
                            "statuses": ["Completed"],
                        }
                    }
                }
            },
            slack_bot_token="xoxb-secret-token",
            slack_alert_channel_id="C123456789",
        )
        app = AgentApp(audit_settings)
        app.initialize_storage()
        app.db.set_kv("servicetitan_audit_last_processed", "2026-06-15T12:00:00+00:00")
        run_id = app.db.log_run_start("servicetitan_audit")
        app.db.log_run_complete(
            run_id,
            "completed",
            {
                "dry_run": False,
                "jobs_seen": 3,
                "sales_fail": 1,
                "alerts_sent": 1,
                "alerts_would_send": 0,
                "alerts_skipped_dedupe": 0,
                "errors": 0,
            },
        )
        app.db.upsert_service_titan_violation(
            violation_key="servicetitan:job-1:appt-1:sales_options_fewer_than_three:advisor-1",
            service_titan_job_id="job-1",
            appointment_id="appt-1",
            technician_id="advisor-1",
            technician_name="Advisor One",
            dispatcher_id="",
            dispatcher_name="",
            rule_id="sales_options_fewer_than_three",
            ruleset="Sales / Comfort Advisor Audit",
            severity="high",
            title="Closed Sales job has fewer than 3 options",
            description="Closed Sales / Comfort Advisor jobs must show at least three options or estimates.",
            recommended_action="Review the Sales job.",
            metadata={},
        )
        app.db.mark_service_titan_alert_sent("servicetitan:job-1:appt-1:sales_options_fewer_than_three:advisor-1")
        text = app.service_titan_runtime_diagnostics_text()
        self.assertIn("SERVICE_TITAN_AUDIT_ENABLED: True", text)
        self.assertIn("SLACK_BOT_TOKEN present: True", text)
        self.assertIn("SLACK_ALERT_CHANNEL_ID: ***6789", text)
        self.assertIn("sales_photos_missing active: False", text)
        self.assertIn("servicetitan_audit_last_processed: 2026-06-15T12:00:00+00:00", text)
        self.assertIn("sales_fail=1", text)
        self.assertIn("alert_sent: 1", text)
        self.assertNotIn("xoxb-secret-token", text)
        self.assertNotIn("Advisor One", text)

    def test_service_titan_runtime_diagnostics_reports_invalid_json_without_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SERVICE_TITAN_AUDIT_ENABLED": "true",
                "SERVICE_TITAN_AUDIT_DRY_RUN": "false",
                "SERVICE_TITAN_AUDIT_BACKFILL_ALERTS": "false",
                "SALES_COMFORT_ADVISOR_AUDIT_ENABLED": "true",
                "TECHNICIAN_COMPLIANCE_ENABLED": "false",
                "DISPATCHER_AUDIT_ENABLED": "false",
                "SERVICE_TITAN_DISABLED_RULE_IDS_JSON": "[",
                "SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON": "{\"rulesets\":{}}",
                "SLACK_BOT_TOKEN": "xoxb-secret-token",
                "SLACK_ALERT_CHANNEL_ID": "C123456789",
            },
            clear=False,
        ):
            text = _settings_error_diagnostics_text("SERVICE_TITAN_DISABLED_RULE_IDS_JSON must be valid JSON")
        self.assertIn("settings_error: SERVICE_TITAN_DISABLED_RULE_IDS_JSON must be valid JSON", text)
        self.assertIn("SERVICE_TITAN_DISABLED_RULE_IDS_JSON valid list: False", text)
        self.assertIn("SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON valid object: True", text)
        self.assertIn("SLACK_BOT_TOKEN present: True", text)
        self.assertIn("SLACK_ALERT_CHANNEL_ID: ***6789", text)
        self.assertNotIn("xoxb-secret-token", text)

    def test_initial_sales_rollout_keeps_options_and_arrival_enabled_with_photos_disabled(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=["sales_photos_missing"],
        )
        job = sales_job(
            "sales-initial-rollout",
            estimate_count=2,
            photo_count=0,
            arrival_window_start=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
            arrival_window_end=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
            arrived_at=datetime(2026, 5, 15, 11, 5, tzinfo=timezone.utc),
        )
        audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack)
        summary = audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(summary.sales_rules_evaluated, 2)
        self.assertEqual(summary.sales_fail, 2)
        self.assertEqual(summary.sales_alerts_sent, 2)
        self.assertEqual(len(self.h.slack.messages), 2)
        alert_text = "\n".join(message[1] for message in self.h.slack.messages)
        self.assertIn("Fewer Than 3 Options", alert_text)
        self.assertIn("Sales advisor arrived after first half of appointment window", alert_text)
        self.assertNotIn("Closed Sales job is missing required photos", alert_text)

    def test_disabled_sales_photos_rule_does_not_send_alert(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=["sales_photos_missing"],
        )
        job = sales_job(
            "sales-photo-disabled",
            estimate_count=3,
            photo_count=0,
            arrival_window_start=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
            arrival_window_end=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
            arrived_at=datetime(2026, 5, 15, 10, 45, tzinfo=timezone.utc),
        )
        summary = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack).audit_once(
            datetime(2026, 5, 15, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(summary.sales_rules_evaluated, 2)
        self.assertEqual(summary.sales_fail, 0)
        self.assertEqual(summary.sales_alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_hvac_rules_return_not_applicable_for_non_hvac_job(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        job = sales_job()
        for rule_id in (
            "hvac_options_fewer_than_three",
            "hvac_payment_missing_on_completed_job",
            "hvac_diagnosis_form_missing",
            "hvac_required_photos_missing",
            "hvac_arrival_outside_window",
        ):
            self.assertEqual(_st_rule(audit_settings, rule_id).run(job, audit_settings).status, RESULT_NOT_APPLICABLE)

    def test_hvac_options_rule_passes_fails_and_handles_missing_data(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        rule = _st_rule(audit_settings, "hvac_options_fewer_than_three")
        self.assertEqual(rule.run(hvac_job(estimate_count=3), audit_settings).status, RESULT_PASS)

        fail = rule.run(hvac_job(estimate_count=2), audit_settings)
        self.assertEqual(fail.status, RESULT_FAIL)
        self.assertEqual(fail.metadata["options_count"], 2)

        insufficient = rule.run(
            hvac_job(
                estimate_count=None,
                present_fields={
                    "status",
                    "business_unit",
                    "job_type",
                    "department",
                    "trade",
                    "workflow",
                    "payments",
                    "forms",
                    "hhr",
                    "photos",
                    "arrival_window",
                    "arrived_at",
                },
            ),
            audit_settings,
        )
        self.assertEqual(insufficient.status, RESULT_INSUFFICIENT)

    def test_hvac_payment_rule_passes_fails_and_handles_missing_data(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        rule = _st_rule(audit_settings, "hvac_payment_missing_on_completed_job")
        self.assertEqual(rule.run(hvac_job(payment_total=300, invoice_balance=0, invoice_status="Paid"), audit_settings).status, RESULT_PASS)

        fail = rule.run(hvac_job(payment_total=0, payments_count=0, invoice_balance=300, invoice_status="Open"), audit_settings)
        self.assertEqual(fail.status, RESULT_FAIL)
        self.assertEqual(fail.metadata["payment_total"], 0)
        self.assertEqual(fail.metadata["invoice_balance"], 300)

        insufficient = rule.run(
            hvac_job(
                payment_total=None,
                payments_count=None,
                invoice_balance=None,
                invoice_status="",
                invoice_total=300,
                present_fields={
                    "status",
                    "business_unit",
                    "job_type",
                    "department",
                    "trade",
                    "workflow",
                    "estimates",
                    "forms",
                    "hhr",
                    "photos",
                    "arrival_window",
                    "arrived_at",
                },
            ),
            audit_settings,
        )
        self.assertEqual(insufficient.status, RESULT_INSUFFICIENT)

    def test_hvac_diagnosis_form_requires_job_scoped_forms(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        rule = _st_rule(audit_settings, "hvac_diagnosis_form_missing")
        self.assertEqual(rule.run(hvac_job(forms_count=1, hhr_completed=True), audit_settings).status, RESULT_PASS)

        fail = rule.run(hvac_job(forms_count=0, hhr_completed=False), audit_settings)
        self.assertEqual(fail.status, RESULT_FAIL)
        self.assertEqual(fail.metadata["forms_count"], 0)

        unscoped = replace(
            hvac_job(
                forms_count=None,
                hhr_completed=None,
                present_fields={
                    "status",
                    "business_unit",
                    "job_type",
                    "department",
                    "trade",
                    "workflow",
                    "estimates",
                    "payments",
                    "photos",
                    "arrival_window",
                    "arrived_at",
                },
            ),
            missing_data={"forms": "forms/v2 submissions returned an unscoped page"},
        )
        result = rule.run(unscoped, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)
        self.assertIn("unscoped", result.explanation)

    def test_hvac_photos_rule_can_fail_or_stay_insufficient_when_source_unavailable(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        rule = _st_rule(audit_settings, "hvac_required_photos_missing")
        self.assertEqual(rule.run(hvac_job(photo_count=1), audit_settings).status, RESULT_PASS)

        fail = rule.run(hvac_job(photo_count=0), audit_settings)
        self.assertEqual(fail.status, RESULT_FAIL)
        self.assertEqual(fail.metadata["photos_count"], 0)

        unavailable = replace(
            hvac_job(
                photo_count=None,
                present_fields={
                    "status",
                    "business_unit",
                    "job_type",
                    "department",
                    "trade",
                    "workflow",
                    "estimates",
                    "payments",
                    "forms",
                    "hhr",
                    "arrival_window",
                    "arrived_at",
                },
            ),
            missing_data={"photos": "jpm job attachments endpoint returned HTTP 404"},
        )
        result = rule.run(unavailable, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)
        self.assertIn("HTTP 404", result.explanation)

    def test_hvac_arrival_rule_passes_fails_and_handles_missing_data(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        rule = _st_rule(audit_settings, "hvac_arrival_outside_window")
        window_start = datetime(2026, 5, 15, 10, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 15, 12, tzinfo=timezone.utc)

        self.assertEqual(
            rule.run(hvac_job(arrival_window_start=window_start, arrival_window_end=window_end, arrived_at=datetime(2026, 5, 15, 10, 20, tzinfo=timezone.utc)), audit_settings).status,
            RESULT_PASS,
        )

        late = rule.run(
            hvac_job(arrival_window_start=window_start, arrival_window_end=window_end, arrived_at=datetime(2026, 5, 15, 10, 45, tzinfo=timezone.utc)),
            audit_settings,
        )
        self.assertEqual(late.status, RESULT_FAIL)

        missing = rule.run(
            hvac_job(
                arrival_window_start=window_start,
                arrival_window_end=window_end,
                arrived_at=None,
                present_fields={
                    "status",
                    "business_unit",
                    "job_type",
                    "department",
                    "trade",
                    "workflow",
                    "estimates",
                    "payments",
                    "forms",
                    "hhr",
                    "photos",
                    "arrival_window",
                },
            ),
            audit_settings,
        )
        self.assertEqual(missing.status, RESULT_INSUFFICIENT)

    def test_hvac_scope_config_handles_numeric_ids_and_workflows_null(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_rule_scope_config={
                "rulesets": {
                    "HVAC Service Audit": {
                        "applies_to": {
                            "business_unit_ids": ["bu-hvac-numeric"],
                            "job_type_ids": ["jt-hvac-numeric"],
                            "workflows": None,
                            "statuses": ["Completed", "Closed"],
                        }
                    }
                }
            },
        )
        job = hvac_job(
            business_unit_id="bu-hvac-numeric",
            business_unit_name="",
            job_type_id="jt-hvac-numeric",
            job_type_name="",
            department="",
            trade="",
            workflow="",
            estimate_count=3,
            present_fields={
                "status",
                "business_unit",
                "job_type",
                "estimates",
                "payments",
                "forms",
                "hhr",
                "photos",
                "arrival_window",
                "arrived_at",
            },
        )
        self.assertEqual(_st_rule(audit_settings, "hvac_options_fewer_than_three").run(job, audit_settings).status, RESULT_PASS)

    def test_empty_hvac_scope_config_does_not_guess_from_numeric_ids(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        job = hvac_job(
            business_unit_id="bu-hvac-numeric",
            business_unit_name="",
            job_type_id="jt-hvac-numeric",
            job_type_name="",
            department="",
            trade="",
            workflow="",
            present_fields={
                "status",
                "business_unit",
                "job_type",
                "estimates",
                "payments",
                "forms",
                "hhr",
                "photos",
                "arrival_window",
                "arrived_at",
            },
        )
        result = _st_rule(audit_settings, "hvac_options_fewer_than_three").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)
        self.assertIn("workflow", result.explanation)

    def test_disabled_hvac_photo_rule_does_not_send_alert(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=["hvac_required_photos_missing"],
        )
        job = hvac_job("hvac-photo-disabled", estimate_count=3, photo_count=0)
        summary = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack).audit_once(
            datetime(2026, 5, 15, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(summary.hvac_rules_evaluated, 4)
        self.assertEqual(summary.hvac_fail, 0)
        self.assertEqual(summary.hvac_alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_hvac_dry_run_and_non_fail_results_do_not_send_slack(self) -> None:
        dry_run_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_dry_run=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        dry_run = ServiceTitanAuditService(dry_run_settings, self.h.db, FakeServiceTitan([hvac_job("hvac-dry", estimate_count=2)]), self.h.slack).audit_once(
            datetime(2026, 5, 15, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(dry_run.hvac_alerts_would_send, 1)
        self.assertEqual(dry_run.hvac_alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

        live_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        non_hvac = ServiceTitanAuditService(live_settings, self.h.db, FakeServiceTitan([sales_job("non-hvac")]), self.h.slack).audit_once(
            datetime(2026, 5, 15, 17, tzinfo=timezone.utc)
        )
        self.assertEqual(non_hvac.hvac_not_applicable, 5)
        insufficient = replace(
            hvac_job(
                "hvac-insufficient",
                estimate_count=None,
                present_fields={"status", "business_unit", "job_type", "department", "trade", "workflow"},
            ),
            missing_data={"estimates": "sales/v2 estimates endpoint returned HTTP 403"},
        )
        insufficient_summary = ServiceTitanAuditService(live_settings, self.h.db, FakeServiceTitan([insufficient]), self.h.slack).audit_once(
            datetime(2026, 5, 15, 18, tzinfo=timezone.utc)
        )
        self.assertGreaterEqual(insufficient_summary.hvac_insufficient_data, 1)
        self.assertEqual(self.h.slack.messages, [])

    def test_plumbing_service_audit_is_disabled_by_default(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        self.assertFalse(audit_settings.plumbing_service_audit_enabled)
        rule_ids = {rule.rule_id for rule in active_service_titan_rules(audit_settings)}
        self.assertNotIn("plumbing_options_fewer_than_three", rule_ids)
        self.assertNotIn("plumbing_payment_missing_on_completed_job", rule_ids)

    def test_plumbing_rules_return_not_applicable_for_non_plumbing_jobs(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        for job in (
            sales_job(business_unit_id="64326403", job_type_id="54086644"),
            plumbing_job(business_unit_id="64313020", business_unit_name="Plumbing - Install", job_type_id="64570637", job_type_name="Water Heater Installation"),
            hvac_job(business_unit_id="1810", job_type_id="1933"),
            plumbing_job(job_type_id="112630828", job_type_name="Plumbing Repair"),
        ):
            self.assertEqual(_st_rule(audit_settings, "plumbing_options_fewer_than_three").run(job, audit_settings).status, RESULT_NOT_APPLICABLE)

    def test_plumbing_strict_scope_uses_confirmed_ids_only(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        rule = _st_rule(audit_settings, "plumbing_options_fewer_than_three")
        self.assertEqual(rule.scope.applies_to_business_units, ("64315277",))
        self.assertEqual(rule.scope.applies_to_job_types, ("57804592", "64569478", "112338076"))
        self.assertIn("57804592", rule.scope.excludes_job_types)
        self.assertIn("Water Heater Maintenance", rule.scope.excludes_job_types)
        self.assertIn("30209", rule.scope.excludes_job_types)
        self.assertIn("111922608", rule.scope.excludes_job_types)
        self.assertIn("112630828", rule.scope.excludes_job_types)

    def test_plumbing_options_rule_passes_fails_and_handles_missing_data(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        rule = _st_rule(audit_settings, "plumbing_options_fewer_than_three")
        self.assertEqual(rule.run(plumbing_job(estimate_count=3), audit_settings).status, RESULT_PASS)

        fail = rule.run(plumbing_job(estimate_count=2), audit_settings)
        self.assertEqual(fail.status, RESULT_FAIL)
        self.assertEqual(fail.metadata["options_count"], 2)
        self.assertEqual(fail.metadata["invoice_total"], 300.0)

        no_charge = rule.run(
            plumbing_job(
                estimate_count=0,
                invoice_total=0.0,
                invoice_balance=0.0,
                payment_total=0.0,
                payments_count=0,
                invoice_status="",
            ),
            audit_settings,
        )
        self.assertEqual(no_charge.status, RESULT_NOT_APPLICABLE)
        self.assertIn("zero-dollar", no_charge.explanation)

        maintenance = rule.run(
            plumbing_job(
                estimate_count=1,
                job_type_id="57804592",
                job_type_name="Water Heater Maintenance",
            ),
            audit_settings,
        )
        self.assertEqual(maintenance.status, RESULT_NOT_APPLICABLE)

        sold_work_visit = rule.run(
            plumbing_job(
                "131543565",
                estimate_count=0,
                invoice_total=997.20,
                invoice_balance=997.20,
                payment_total=0.0,
                payments_count=0,
                repair_sold=True,
            ),
            audit_settings,
        )
        self.assertEqual(sold_work_visit.status, RESULT_NOT_APPLICABLE)
        self.assertIn("sold/performed repair work", sold_work_visit.explanation)

        insufficient = rule.run(
            plumbing_job(
                estimate_count=None,
                present_fields={
                    "status",
                    "business_unit",
                    "job_type",
                    "payments",
                    "forms",
                    "hhr",
                    "photos",
                    "arrival_window",
                    "arrived_at",
                },
            ),
            audit_settings,
        )
        self.assertEqual(insufficient.status, RESULT_INSUFFICIENT)

        missing_billing = rule.run(
            plumbing_job(
                estimate_count=1,
                invoice_total=None,
                invoice_balance=None,
                payment_total=None,
                payments_count=None,
                invoice_status="",
                present_fields={"status", "business_unit", "job_type", "estimates"},
            ),
            audit_settings,
        )
        self.assertEqual(missing_billing.status, RESULT_INSUFFICIENT)
        self.assertIn("billing context", missing_billing.explanation)

        not_applicable = rule.run(plumbing_job(status="Scheduled"), audit_settings)
        self.assertEqual(not_applicable.status, RESULT_NOT_APPLICABLE)

    def test_disabled_plumbing_photo_rule_does_not_send_alert(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=["plumbing_required_photos_missing"],
        )
        job = plumbing_job("plumbing-photo-disabled", estimate_count=3, photo_count=0)
        summary = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack).audit_once(
            datetime(2026, 5, 15, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(summary.plumbing_rules_evaluated, 4)
        self.assertEqual(summary.plumbing_fail, 0)
        self.assertEqual(summary.plumbing_alerts_sent, 0)
        self.assertEqual(self.h.slack.messages, [])

    def test_plumbing_only_client_skips_unneeded_related_categories_when_disabled_rules_configured(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=[
                "plumbing_payment_missing_on_completed_job",
                "plumbing_required_photos_missing",
                "plumbing_diagnosis_form_missing",
                "plumbing_arrival_outside_window",
            ],
        )
        client = ServiceTitanClient(audit_settings)
        self.assertTrue(client._should_fetch_related_category("appointments"))
        self.assertTrue(client._should_fetch_related_category("appointment_assignments"))
        self.assertTrue(client._should_fetch_related_category("estimates"))
        self.assertTrue(client._should_fetch_related_category("opportunities"))
        self.assertTrue(client._should_fetch_related_category("invoices"))
        self.assertFalse(client._should_fetch_related_category("attachments"))
        self.assertFalse(client._should_fetch_related_category("forms"))
        self.assertFalse(client._should_fetch_related_category("technician_time"))
        self.assertEqual(client._related_skip_reason("forms"), "forms skipped for Plumbing-only enabled rules")

        fully_disabled_settings = settings(
            self.h.settings.sqlite_path,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
            service_titan_disabled_rule_ids=[
                "plumbing_options_fewer_than_three",
                "plumbing_payment_missing_on_completed_job",
                "plumbing_required_photos_missing",
                "plumbing_diagnosis_form_missing",
                "plumbing_arrival_outside_window",
            ],
        )
        fully_disabled_client = ServiceTitanClient(fully_disabled_settings)
        self.assertFalse(fully_disabled_client._should_fetch_related_category("invoices"))

    def test_plumbing_slack_payload_excludes_customer_pii(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            sales_comfort_advisor_audit_enabled=False,
            hvac_service_audit_enabled=False,
            plumbing_service_audit_enabled=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        job = plumbing_job(
            "plumbing-pii",
            customer_name="Sensitive Customer",
            estimate_count=1,
            notes="Customer phone 555-123-4567 email customer@example.com address 123 Main St",
        )
        result = _st_rule(audit_settings, "plumbing_options_fewer_than_three").run(job, audit_settings)
        payload = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([]), self.h.slack)._alert_text(job, result)
        self.assertIn("Plumbing Service: Fewer Than 3 Options", payload)
        self.assertIn("Options: 1 of 3 required", payload)
        self.assertNotIn("Sensitive Customer", payload)
        self.assertNotIn("555-123-4567", payload)
        self.assertNotIn("customer@example.com", payload)
        self.assertNotIn("123 Main St", payload)

    def test_sales_enrichment_with_three_estimates_passes_options_rule(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-options-pass"),
                estimates=[
                    {"id": "est-1", "jobId": "sales-options-pass"},
                    {"id": "est-2", "jobId": "sales-options-pass"},
                    {"id": "est-3", "jobId": "sales-options-pass"},
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_options_fewer_than_three").run(job, audit_settings)
        self.assertEqual(job.estimate_count, 3)
        self.assertEqual(result.status, RESULT_PASS)

    def test_empty_sales_scope_config_does_not_guess_from_numeric_ids(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload(
                    "sales-empty-scope",
                    business_unit_id="bu-tenant-sales",
                    business_unit_name="",
                    job_type_id="jt-tenant-sales",
                    job_type_name="",
                    department_name="",
                    trade="",
                    include_workflow=False,
                ),
                estimates=[
                    {"id": "est-1", "jobId": "sales-empty-scope"},
                    {"id": "est-2", "jobId": "sales-empty-scope"},
                    {"id": "est-3", "jobId": "sales-empty-scope"},
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_options_fewer_than_three").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)
        self.assertIn("workflow", result.explanation)

    def test_sales_enrichment_with_fewer_than_three_estimates_fails_options_rule(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-options-fail"),
                estimates=[
                    {"id": "est-1", "jobId": "sales-options-fail"},
                    {"id": "est-2", "jobId": "sales-options-fail"},
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_options_fewer_than_three").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_FAIL)
        self.assertEqual(result.metadata["options_count"], 2)

    def test_sales_enrichment_uses_job_estimate_ids_when_estimates_endpoint_is_unavailable(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-estimate-ids", estimate_ids=["est-1", "est-2", "est-3"]),
                estimates_response=HttpResponse(403, {"error": "estimate scope missing"}, {}),
                opportunities_response=HttpResponse(403, {"error": "opportunity scope missing"}, {}),
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_options_fewer_than_three").run(job, audit_settings)
        self.assertEqual(job.estimate_count, 3)
        self.assertEqual(result.status, RESULT_PASS)

    def test_sales_enrichment_evaluates_tenant_ids_without_workflow_names(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_rule_scope_config={
                "rulesets": {
                    "Sales / Comfort Advisor Audit": {
                        "applies_to": {
                            "business_unit_ids": ["bu-tenant-sales"],
                            "job_type_ids": ["jt-tenant-sales"],
                            "workflows": None,
                            "statuses": ["Completed"],
                        }
                    }
                }
            },
        )
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload(
                    "sales-id-scope",
                    business_unit_id="bu-tenant-sales",
                    business_unit_name="",
                    job_type_id="jt-tenant-sales",
                    job_type_name="",
                    department_name="",
                    trade="",
                    include_workflow=False,
                ),
                estimates=[
                    {"id": "est-1", "jobId": "sales-id-scope"},
                    {"id": "est-2", "jobId": "sales-id-scope"},
                    {"id": "est-3", "jobId": "sales-id-scope"},
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_options_fewer_than_three").run(job, audit_settings)
        self.assertEqual(job.business_unit_id, "bu-tenant-sales")
        self.assertEqual(job.job_type_id, "jt-tenant-sales")
        self.assertEqual(job.workflow, "")
        self.assertEqual(result.status, RESULT_PASS)

    def test_sales_enrichment_missing_estimate_sources_returns_insufficient_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-options-missing"),
                estimates_response=HttpResponse(403, {"error": "estimate scope missing"}, {}),
                opportunities_response=HttpResponse(403, {"error": "opportunity scope missing"}, {}),
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_options_fewer_than_three").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)
        self.assertIn("sales/v2", result.explanation)

    def test_sales_enrichment_with_photos_or_form_images_passes_photos_rule(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        attachment_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-photo-attachment"),
                attachments=[{"fileName": "comfort-advisor-photo.jpg"}],
            ),
        )
        attachment_job = attachment_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(_st_rule(audit_settings, "sales_photos_missing").run(attachment_job, audit_settings).status, RESULT_PASS)

        form_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-photo-form"),
                attachments_response=HttpResponse(404, {"error": "attachments unavailable"}, {}),
                forms=[{"id": "form-1", "jobId": "sales-photo-form", "attachments": [{"fileName": "form-photo.png"}]}],
            ),
        )
        form_job = form_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(form_job.photo_count, 1)
        self.assertEqual(_st_rule(audit_settings, "sales_photos_missing").run(form_job, audit_settings).status, RESULT_PASS)

    def test_sales_enrichment_without_photos_fails_photos_rule(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(job_payload=sales_job_payload("sales-no-photo"), attachments=[]),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_photos_missing").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_FAIL)
        self.assertEqual(result.metadata["photos_count"], 0)

    def test_sales_enrichment_missing_photos_endpoint_returns_insufficient_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-photo-missing"),
                attachments_response=HttpResponse(404, {"error": "attachments unavailable"}, {}),
                forms=[],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_photos_missing").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)
        self.assertIn("attachments", result.explanation)

    def test_sales_enrichment_arrival_after_first_half_fails_and_before_cutoff_passes(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        late_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-arrival-late"),
                appointments=[
                    {
                        "id": "appt-late",
                        "jobId": "sales-arrival-late",
                        "arrivalWindowStart": "2026-05-15T10:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T12:00:00Z",
                    }
                ],
                assignments=[{"appointmentId": "appt-late", "technicianId": "advisor-1", "arrivedOn": "2026-05-15T11:01:00Z"}],
            ),
        )
        late_job = late_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        late = _st_rule(audit_settings, "sales_arrival_after_first_half").run(late_job, audit_settings)
        self.assertEqual(late.status, RESULT_FAIL)

        on_time_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-arrival-on-time"),
                appointments=[
                    {
                        "id": "appt-on-time",
                        "jobId": "sales-arrival-on-time",
                        "arrivalWindowStart": "2026-05-15T10:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T12:00:00Z",
                    }
                ],
                assignments=[{"appointmentId": "appt-on-time", "technicianId": "advisor-1", "arrivedOn": "2026-05-15T10:45:00Z"}],
            ),
        )
        on_time_job = on_time_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(_st_rule(audit_settings, "sales_arrival_after_first_half").run(on_time_job, audit_settings).status, RESULT_PASS)

    def test_sales_arrival_evaluates_only_first_appointment(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-two-appointments"),
                appointments=[
                    {
                        "id": "appt-follow-up",
                        "jobId": "sales-two-appointments",
                        "arrivalWindowStart": "2026-05-16T10:00:00Z",
                        "arrivalWindowEnd": "2026-05-16T12:00:00Z",
                        "arrivedOn": "2026-05-16T11:30:00Z",
                    },
                    {
                        "id": "appt-first",
                        "jobId": "sales-two-appointments",
                        "arrivalWindowStart": "2026-05-15T10:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T12:00:00Z",
                        "arrivedOn": "2026-05-15T10:30:00Z",
                    },
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_arrival_after_first_half").run(job, audit_settings)
        self.assertEqual(job.appointment_id, "appt-first")
        self.assertEqual(result.status, RESULT_PASS)

    def test_sales_arrival_missing_on_first_appointment_does_not_fallback_to_later_visit(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-first-arrival-missing"),
                appointments=[
                    {
                        "id": "appt-first-missing",
                        "jobId": "sales-first-arrival-missing",
                        "arrivalWindowStart": "2026-05-15T10:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T12:00:00Z",
                    },
                    {
                        "id": "appt-second-late",
                        "jobId": "sales-first-arrival-missing",
                        "arrivalWindowStart": "2026-05-16T10:00:00Z",
                        "arrivalWindowEnd": "2026-05-16T12:00:00Z",
                        "arrivedOn": "2026-05-16T11:30:00Z",
                    },
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_arrival_after_first_half").run(job, audit_settings)
        self.assertEqual(job.appointment_id, "appt-first-missing")
        self.assertEqual(result.status, RESULT_INSUFFICIENT)

    def test_sales_arrival_prefers_explicit_appointment_sequence(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-sequence-appointments"),
                appointments=[
                    {
                        "id": "appt-sequence-two",
                        "sequence": 2,
                        "jobId": "sales-sequence-appointments",
                        "arrivalWindowStart": "2026-05-15T08:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T10:00:00Z",
                        "arrivedOn": "2026-05-15T09:30:00Z",
                    },
                    {
                        "id": "appt-sequence-one",
                        "sequence": 1,
                        "jobId": "sales-sequence-appointments",
                        "arrivalWindowStart": "2026-05-15T12:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T14:00:00Z",
                        "arrivedOn": "2026-05-15T12:15:00Z",
                    },
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "sales_arrival_after_first_half").run(job, audit_settings)
        self.assertEqual(job.appointment_id, "appt-sequence-one")
        self.assertEqual(result.status, RESULT_PASS)

    def test_sales_enrichment_missing_arrival_timestamp_returns_insufficient_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload=sales_job_payload("sales-arrival-missing"),
                appointments=[
                    {
                        "id": "appt-missing",
                        "jobId": "sales-arrival-missing",
                        "arrivalWindowStart": "2026-05-15T10:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T12:00:00Z",
                    }
                ],
                assignments=[{"appointmentId": "appt-missing", "technicianId": "advisor-1"}],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(_st_rule(audit_settings, "sales_arrival_after_first_half").run(job, audit_settings).status, RESULT_INSUFFICIENT)

    def test_sales_debug_field_output_does_not_include_customer_pii(self) -> None:
        logging.disable(logging.NOTSET)
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_debug_fields=True,
            technician_compliance_enabled=False,
            dispatcher_audit_enabled=False,
        )
        audit_logger = logging.getLogger("marketing_os_agent.domain.service_titan_audit")
        old_propagate = audit_logger.propagate
        old_level = audit_logger.level
        records: list[LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: LogRecord) -> None:
                records.append(record)

        handler = Capture()
        audit_logger.addHandler(handler)
        audit_logger.propagate = False
        audit_logger.setLevel(logging.INFO)
        try:
            client = ServiceTitanClient(
                audit_settings,
                st_enrichment_http(
                    job_payload={
                        **sales_job_payload("sales-debug-pii"),
                        "customerName": "Private Customer",
                        "address": "123 Secret St",
                        "phone": "555-1212",
                        "email": "private@example.com",
                        "summary": "Raw private note",
                    },
                    estimates=[{"id": "est-1", "jobId": "sales-debug-pii"}],
                    attachments_response=HttpResponse(404, {"error": "attachments unavailable"}, {}),
                ),
            )
            job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
            audit = ServiceTitanAuditService(audit_settings, self.h.db, FakeServiceTitan([job]), self.h.slack)
            audit.audit_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
            rendered = "\n".join(str(record.__dict__) for record in records)
            self.assertIn("servicetitan_sales_field_availability", rendered)
            self.assertNotIn("Private Customer", rendered)
            self.assertNotIn("123 Secret St", rendered)
            self.assertNotIn("555-1212", rendered)
            self.assertNotIn("private@example.com", rendered)
            self.assertNotIn("Raw private note", rendered)
        finally:
            audit_logger.removeHandler(handler)
            audit_logger.propagate = old_propagate
            audit_logger.setLevel(old_level)
            logging.disable(logging.CRITICAL)

    def test_handbook_arrival_rule_passes_and_fails_with_mapped_appointment_fields(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "first_call_on_time_arrival")
        self.assertEqual(rule.run(st_job(arrived_at=datetime(2026, 5, 15, 9, tzinfo=timezone.utc)), audit_settings).status, RESULT_PASS)
        result = rule.run(st_job(arrived_at=datetime(2026, 5, 15, 9, 1, tzinfo=timezone.utc)), audit_settings)
        self.assertEqual(result.status, RESULT_FAIL)
        missing = st_job(arrival_window_start=None, arrived_at=None, present_fields={"status", "job_type", "business_unit", "department", "trade", "workflow"})
        self.assertEqual(rule.run(missing, audit_settings).status, RESULT_INSUFFICIENT)

    def test_handbook_equipment_hhr_and_options_rules_pass_fail_and_insufficient(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        self.assertEqual(_st_rule(audit_settings, "missing_equipment_registration").run(st_job(equipment_count=0, equipment_complete=False), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "missing_hhr_or_service_form").run(st_job(hhr_completed=False), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "missing_three_repair_options").run(st_job(estimate_count=2), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "missing_home_comfort_plan_option").run(st_job(home_comfort_plan_option_present=False), audit_settings).status, RESULT_FAIL)
        insufficient = st_job(present_fields={"status", "notes"})
        self.assertEqual(_st_rule(audit_settings, "missing_equipment_registration").run(insufficient, audit_settings).status, RESULT_INSUFFICIENT)

    def test_handbook_price_authorization_payment_and_same_day_estimate_rules(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        self.assertEqual(_st_rule(audit_settings, "missing_price_authorization").run(st_job(authorization_count=0), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "missing_payment_on_completed_job").run(st_job(payment_total=0, invoice_balance=300, invoice_status="Open"), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "missing_same_day_estimate").run(st_job(same_day_estimate_present=False), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "missing_payment_on_completed_job").run(st_job(payment_total=None, invoice_balance=None, present_fields={"status", "invoice_line_items"}), audit_settings).status, RESULT_INSUFFICIENT)

    def test_handbook_diagnostic_fee_logic_handles_no_repair_repair_and_unknown_amounts(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        no_repair_missing_fee = st_job(line_items=["Dispatch"], diagnostic_fee_present=False, repair_sold=False)
        self.assertEqual(_st_rule(audit_settings, "missing_diagnostic_fee_when_repair_not_sold").run(no_repair_missing_fee, audit_settings).status, RESULT_FAIL)

        waiver = st_job(line_items=["Dispatch"], diagnostic_fee_present=False, repair_sold=False, notes="Manager approved waiver for diagnostic fee.")
        self.assertEqual(_st_rule(audit_settings, "missing_diagnostic_fee_when_repair_not_sold").run(waiver, audit_settings).status, RESULT_PASS)

        sold_with_charge = st_job(repair_sold=True, diagnostic_fee_present=True, diagnostic_fee_charged=True, diagnostic_fee_waived=False)
        self.assertEqual(_st_rule(audit_settings, "diagnostic_fee_not_waived_when_repair_sold").run(sold_with_charge, audit_settings).status, RESULT_FAIL)

        sold_without_fee = st_job(line_items=["Capacitor"], diagnostic_fee_present=False, repair_sold=True)
        self.assertEqual(_st_rule(audit_settings, "missing_diagnostic_fee_when_repair_not_sold").run(sold_without_fee, audit_settings).status, RESULT_NOT_APPLICABLE)
        self.assertEqual(_st_rule(audit_settings, "tech_invoice_diagnostic_fee_missing").run(sold_without_fee, audit_settings).status, RESULT_NOT_APPLICABLE)

        sold_unknown_amount = st_job(repair_sold=True, diagnostic_fee_present=True, diagnostic_fee_charged=None, diagnostic_fee_waived=False)
        self.assertEqual(_st_rule(audit_settings, "diagnostic_fee_not_waived_when_repair_sold").run(sold_unknown_amount, audit_settings).status, RESULT_INSUFFICIENT)

    def test_handbook_special_order_follow_up_and_lead_turnover_rules(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        special_order = st_job(
            special_order_detected=True,
            special_order_missing_fields=["eta", "supply house"],
            downpayment_recorded=False,
            notes="Special order part needed.",
        )
        self.assertEqual(_st_rule(audit_settings, "special_order_missing_required_notes").run(special_order, audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "missing_downpayment_for_special_order").run(special_order, audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "special_order_missing_service_titan_reminder").run(special_order, audit_settings).status, RESULT_INSUFFICIENT)

        follow_up = st_job(follow_up_needed=True, follow_up_task_present=False, notes="Follow up needed for estimate decision.")
        self.assertEqual(_st_rule(audit_settings, "missing_follow_up_task_when_follow_up_needed").run(follow_up, audit_settings).status, RESULT_FAIL)

        lead = st_job(lead_turnover_required=True, lead_turnover_documented=False, hhr_completed=False)
        self.assertEqual(_st_rule(audit_settings, "lead_turnover_missing_required_documentation").run(lead, audit_settings).status, RESULT_FAIL)

    def test_handbook_po_and_ply_rules_do_not_fake_unavailable_ply_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        po_issue = st_job(
            business_unit_name="Plumbing Service",
            job_type_name="Plumbing Service",
            trade="Plumbing",
            workflow="Plumbing Materials",
            po_received_not_reconciled_count=1,
            po_missing_vendor_document_count=1,
            po_missing_attachment_count=1,
        )
        self.assertEqual(_st_rule(audit_settings, "po_received_not_reconciled").run(po_issue, audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "po_missing_vendor_document").run(po_issue, audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "po_missing_attachments").run(po_issue, audit_settings).status, RESULT_FAIL)
        no_ply = st_job(
            business_unit_name="Plumbing Service",
            job_type_name="Plumbing Service",
            trade="Plumbing",
            workflow="Plumbing Materials",
            ply_data_available=False,
        )
        self.assertEqual(_st_rule(audit_settings, "po_not_synced_to_service_titan").run(no_ply, audit_settings).status, RESULT_INSUFFICIENT)
        self.assertEqual(_st_rule(audit_settings, "ply_st_material_sync_blocked").run(no_ply, audit_settings).status, RESULT_INSUFFICIENT)

    def test_false_positive_scopes_return_not_applicable_instead_of_fail(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        no_po = st_job(
            business_unit_name="Plumbing Service",
            job_type_name="Plumbing Service",
            trade="Plumbing",
            workflow="Plumbing Materials",
            purchase_orders=[],
            purchase_orders_count=0,
            po_received_not_reconciled_count=0,
        )
        self.assertEqual(_st_rule(audit_settings, "po_received_not_reconciled").run(no_po, audit_settings).status, RESULT_NOT_APPLICABLE)

        non_plumbing = st_job(po_received_not_reconciled_count=1)
        self.assertEqual(_st_rule(audit_settings, "po_received_not_reconciled").run(non_plumbing, audit_settings).status, RESULT_NOT_APPLICABLE)

        canceled = st_job(status="Canceled", tag_names=["No Access"], photo_count=0, options_presented=False)
        self.assertEqual(_st_rule(audit_settings, "missing_required_photos").run(canceled, audit_settings).status, RESULT_NOT_APPLICABLE)
        self.assertEqual(_st_rule(audit_settings, "missing_three_repair_options").run(canceled, audit_settings).status, RESULT_NOT_APPLICABLE)

    def test_handbook_escalation_rules_fail_only_when_issue_is_detected(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        self.assertEqual(_st_rule(audit_settings, "scope_change_missing_escalation_note").run(st_job(scope_change_detected=True, scope_change_escalated=False), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "cancellation_after_materials_missing_escalation").run(st_job(cancellation_after_materials_detected=True, cancellation_escalated=False), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "defective_part_missing_warranty_claim_data").run(st_job(defective_part_detected=True, warranty_claim_documented=False), audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "defective_part_missing_warranty_claim_data").run(st_job(defective_part_detected=False), audit_settings).status, RESULT_NOT_APPLICABLE)

    def test_service_titan_disabled_rule_ids_remove_handbook_rules(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_disabled_rule_ids=["missing_hhr_or_service_form"],
        )
        rule_ids = {rule.rule_id for rule in active_service_titan_rules(audit_settings)}
        self.assertNotIn("missing_hhr_or_service_form", rule_ids)

    def test_service_titan_polling_uses_last_processed_overlap(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        self.h.db.set_kv("servicetitan_audit_last_processed", "2026-05-15T16:00:00+00:00")
        client = FakeServiceTitan([])
        audit = ServiceTitanAuditService(audit_settings, self.h.db, client, self.h.slack)
        audit.audit_once(datetime(2026, 5, 15, 16, 10, tzinfo=timezone.utc))
        self.assertEqual(client.since_seen, datetime(2026, 5, 15, 15, 55, tzinfo=timezone.utc))

    def test_service_titan_closed_jobs_are_audited_for_dispatch_quality(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        rule = _st_rule(audit_settings, "dispatch_photos_missing")
        result = rule.run(st_job(photo_count=0), audit_settings)
        self.assertEqual(result.status, RESULT_FAIL)

    def test_service_titan_client_parses_job_payload_and_uses_token(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload={
                    "id": 123,
                    "jobNumber": "J123",
                    "status": "Completed",
                    "modifiedOn": "2026-05-15T16:00:00Z",
                    "technician": {"id": 7, "name": "Tech"},
                    "invoice": {"lineItems": [{"name": "Diagnostic Fee"}]},
                }
            ),
        )
        jobs = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))
        self.assertEqual(jobs[0].job_id, "123")
        self.assertIn("Diagnostic Fee", jobs[0].invoice_line_items)
        self.assertIn("/jpm/v2/tenant/12345/jobs", client.http.calls[1])

    def test_service_titan_enrichment_maps_appointment_arrival_and_time_data(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                appointments=[
                    {
                        "id": "appt-1",
                        "arrivalWindowStart": "2026-05-15T09:00:00Z",
                        "arrivalWindowEnd": "2026-05-15T11:00:00Z",
                    }
                ],
                assignments=[{"technicianId": "tech-7", "technicianName": "Tech Seven", "arrivedOn": "2026-05-15T09:12:00Z"}],
                timesheets=[{"technicianId": "tech-7", "clockInOn": "2026-05-15T09:00:00Z", "clockOutOn": "2026-05-15T15:30:00Z"}],
                non_job_timesheets=[{"name": "Lunch Break", "startedOn": "2026-05-15T12:00:00Z", "endedOn": "2026-05-15T12:30:00Z"}],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(job.appointment_id, "appt-1")
        self.assertEqual(job.technician_id, "tech-7")
        self.assertEqual(job.arrival_window_start, datetime(2026, 5, 15, 9, tzinfo=timezone.utc))
        self.assertEqual(job.arrived_at, datetime(2026, 5, 15, 9, 12, tzinfo=timezone.utc))
        self.assertEqual(job.clock_in_at, datetime(2026, 5, 15, 9, tzinfo=timezone.utc))
        self.assertEqual(job.clock_out_at, datetime(2026, 5, 15, 15, 30, tzinfo=timezone.utc))
        self.assertEqual(job.lunch_break_minutes, 30)
        self.assertEqual(job.related_counts["appointments"], 1)
        self.assertEqual(job.related_counts["technician_time_records"], 2)

    def test_service_titan_enrichment_maps_invoice_items_for_diagnostic_rules(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        pass_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(invoices=[{"id": "inv-1"}], invoice_items=[{"name": "Diagnostic Fee"}]),
        )
        pass_job = pass_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        pass_result = _st_rule(audit_settings, "tech_invoice_diagnostic_fee_missing").run(pass_job, audit_settings)
        self.assertIn("Diagnostic Fee", pass_job.invoice_line_items)
        self.assertEqual(pass_result.status, RESULT_PASS)

        fail_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(invoices=[{"id": "inv-2"}], invoice_items=[{"name": "Dispatch"}]),
        )
        fail_job = fail_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        fail_result = _st_rule(audit_settings, "tech_invoice_diagnostic_fee_missing").run(fail_job, audit_settings)
        self.assertEqual(fail_result.status, RESULT_FAIL)

    def test_service_titan_invoice_export_is_filtered_to_requested_invoice_ids(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_page_size=2,
            service_titan_audit_max_pages=1,
        )
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                invoices=[{"id": "inv-1"}],
                invoice_items=[
                    {"invoiceId": "other-1", "name": "Other Tenant Item"},
                    {"invoiceId": "inv-1", "name": "Diagnostic Fee"},
                    {"invoiceId": "inv-1", "name": "Capacitor"},
                    {"invoiceId": "other-2", "name": "Another Tenant Item"},
                ],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(job.related_counts["invoice_items"], 2)
        self.assertIn("Diagnostic Fee", job.invoice_line_items)
        self.assertIn("Capacitor", job.invoice_line_items)
        self.assertNotIn("Other Tenant Item", job.invoice_line_items)
        self.assertNotIn("Another Tenant Item", job.invoice_line_items)

    def test_service_titan_overbroad_invoice_export_is_treated_as_unavailable(self) -> None:
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_page_size=2,
            service_titan_audit_max_pages=1,
        )
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                invoices=[{"id": "inv-1"}],
                invoice_items=[{"invoiceId": "inv-1", "name": f"Diagnostic Fee {index}"} for index in range(1001)],
            ),
        )
        job = client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(job.related_counts["invoice_items"], 0)
        self.assertNotIn("invoice_line_items", job.present_fields)
        self.assertIn("overbroad", job.missing_data["invoice_line_items"])
        result = _st_rule(audit_settings, "tech_invoice_diagnostic_fee_missing").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)

    def test_service_titan_enrichment_maps_notes_and_photos_for_dispatch_rules(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        pass_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                notes=[{"text": "Completed diagnosis, documented outcome, and next step."}],
                attachments=[{"fileName": "system-photo.jpg"}],
            ),
        )
        pass_job = pass_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(_st_rule(audit_settings, "dispatch_notes_missing").run(pass_job, audit_settings).status, RESULT_PASS)
        self.assertEqual(_st_rule(audit_settings, "dispatch_photos_missing").run(pass_job, audit_settings).status, RESULT_PASS)
        self.assertEqual(_st_rule(audit_settings, "dispatch_supporting_evidence_missing").run(pass_job, audit_settings).status, RESULT_PASS)

        fail_client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(notes=[], attachments=[]),
        )
        fail_job = fail_client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        self.assertEqual(_st_rule(audit_settings, "dispatch_notes_missing").run(fail_job, audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "dispatch_photos_missing").run(fail_job, audit_settings).status, RESULT_FAIL)
        self.assertEqual(_st_rule(audit_settings, "dispatch_supporting_evidence_missing").run(fail_job, audit_settings).status, RESULT_FAIL)

    def test_service_titan_rules_remain_insufficient_when_related_endpoint_unavailable(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=True)
        http = FakeHttp(
            [
                HttpResponse(200, {"access_token": "token", "expires_in": 900}, {}),
                HttpResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": 123,
                                "status": "Completed",
                                "modifiedOn": "2026-05-15T16:00:00Z",
                                "businessUnit": {"id": "bu-service", "name": "HVAC Service"},
                                "jobType": {"id": "jt-diagnostic", "name": "Diagnostic Service"},
                                "departmentName": "Service",
                                "trade": "HVAC",
                                "workflow": "Service Call",
                            }
                        ],
                        "hasMore": False,
                    },
                    {},
                ),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(403, {"error": "invoice scope missing"}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
                HttpResponse(200, {"data": [], "hasMore": False}, {}),
            ]
        )
        job = ServiceTitanClient(audit_settings, http).query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))[0]
        result = _st_rule(audit_settings, "tech_invoice_diagnostic_fee_missing").run(job, audit_settings)
        self.assertEqual(result.status, RESULT_INSUFFICIENT)
        self.assertIn("accounting", result.explanation)

    def test_service_titan_debug_field_mode_does_not_log_secret_or_pii_values(self) -> None:
        logging.disable(logging.NOTSET)
        audit_settings = settings(
            self.h.settings.sqlite_path,
            service_titan_audit_enabled=True,
            service_titan_audit_debug_fields=True,
        )
        service_titan_logger = logging.getLogger("marketing_os_agent.clients.servicetitan")
        old_propagate = service_titan_logger.propagate
        old_level = service_titan_logger.level
        records: list[LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: LogRecord) -> None:
                records.append(record)

        handler = Capture()
        service_titan_logger.addHandler(handler)
        service_titan_logger.propagate = False
        service_titan_logger.setLevel(logging.INFO)
        try:
            client = ServiceTitanClient(
                audit_settings,
                st_enrichment_http(
                    job_payload={
                        "id": 123,
                        "status": "Completed",
                        "modifiedOn": "2026-05-15T16:00:00Z",
                        "customerName": "Private Customer",
                        "address": "123 Secret St",
                        "phone": "555-1212",
                        "email": "private@example.com",
                        "description": "Raw private note",
                    },
                    notes=[{"text": "Private note body"}],
                    attachments=[{"fileName": "unit-photo.jpg"}],
                ),
            )
            client.query_recent_jobs(datetime(2026, 5, 15, 15, tzinfo=timezone.utc))
            rendered = "\n".join(str(record.__dict__) for record in records)
            self.assertIn("servicetitan_field_availability", rendered)
            self.assertNotIn(audit_settings.servicetitan_client_secret, rendered)
            self.assertNotIn("token", rendered)
            self.assertNotIn("Private Customer", rendered)
            self.assertNotIn("123 Secret St", rendered)
            self.assertNotIn("555-1212", rendered)
            self.assertNotIn("private@example.com", rendered)
            self.assertNotIn("Raw private note", rendered)
            self.assertNotIn("Private note body", rendered)
        finally:
            service_titan_logger.removeHandler(handler)
            service_titan_logger.propagate = old_propagate
            service_titan_logger.setLevel(old_level)
            logging.disable(logging.CRITICAL)

    def test_service_titan_scope_discovery_is_sanitized_and_shows_scope_values(self) -> None:
        audit_settings = settings(self.h.settings.sqlite_path, service_titan_audit_enabled=False)
        client = ServiceTitanClient(
            audit_settings,
            st_enrichment_http(
                job_payload={
                    "id": 123,
                    "status": "Completed",
                    "modifiedOn": "2026-05-15T16:00:00Z",
                    "customerName": "Private Customer",
                    "address": "123 Secret St",
                    "phone": "555-1212",
                    "email": "private@example.com",
                    "description": "Raw private note",
                    "businessUnit": {"id": "bu-service", "name": "HVAC Service"},
                    "jobType": {"id": "jt-diagnostic", "name": "Diagnostic Service"},
                    "departmentName": "Service",
                    "trade": "HVAC",
                    "workflow": "Service Call",
                    "tags": [{"id": "tag-1", "name": "Maintenance"}],
                },
                notes=[{"text": "Private note body"}],
            ),
        )
        summary = ServiceTitanScopeDiscovery(audit_settings, client).run_once(datetime(2026, 5, 15, 16, tzinfo=timezone.utc))
        text = "\n".join(summary.to_lines())
        self.assertEqual(summary.status, "completed")
        self.assertIn("HVAC Service", text)
        self.assertIn("Diagnostic Service", text)
        self.assertIn("Maintenance", text)
        self.assertIn("available top-level keys", text)
        self.assertNotIn(audit_settings.servicetitan_client_secret, text)
        self.assertNotIn("Private Customer", text)
        self.assertNotIn("123 Secret St", text)
        self.assertNotIn("555-1212", text)
        self.assertNotIn("private@example.com", text)
        self.assertNotIn("Raw private note", text)
        self.assertNotIn("Private note body", text)

    def test_service_titan_dry_run_summary_lines_show_true_when_enabled(self) -> None:
        lines = ServiceTitanAuditSummary(dry_run=True).to_lines()
        self.assertIn("- dry_run: True", lines)

    def test_notifications_test_validates_without_sending_by_default(self) -> None:
        app_settings = settings(str(Path(self.h.tmp.name) / "notify.sqlite3"), notifications_test_send=False)
        app = AgentApp(app_settings)
        fake_slack = FakeSlack()
        app.slack = fake_slack
        ok, text = app.notifications_test_text()
        self.assertTrue(ok)
        self.assertIn("Slack auth.test: ok", text)
        self.assertIn("Slack test message: skipped", text)
        self.assertEqual(fake_slack.messages, [])

    def test_notifications_test_sends_only_when_explicitly_enabled(self) -> None:
        app_settings = settings(str(Path(self.h.tmp.name) / "notify-send.sqlite3"), notifications_test_send=True)
        app = AgentApp(app_settings)
        fake_slack = FakeSlack()
        app.slack = fake_slack
        ok, text = app.notifications_test_text()
        self.assertTrue(ok)
        self.assertIn("Slack test message: sent", text)
        self.assertEqual(len(fake_slack.messages), 1)
        self.assertIn("[TEST] Marketing OS Agent notification test", fake_slack.messages[0][1])

    def test_synthetic_servicetitan_alert_uses_formatter_and_does_not_write_or_send_by_default(self) -> None:
        app_settings = settings(str(Path(self.h.tmp.name) / "synthetic.sqlite3"), notifications_test_send=False)
        app = AgentApp(app_settings)
        fake_slack = FakeSlack()
        app.slack = fake_slack
        ok, text = app.service_titan_alert_test_text()
        self.assertTrue(ok)
        self.assertIn("ℹ️ TEST - Unknown: [TEST] Synthetic ServiceTitan audit alert", text)
        self.assertIn("[TEST] Synthetic ServiceTitan audit alert", text)
        self.assertIn("writes violation/dedupe records: false", text)
        self.assertIn("would_send: True", text)
        self.assertIn("Slack send: skipped", text)
        self.assertEqual(fake_slack.messages, [])

    def test_synthetic_servicetitan_alert_can_send_when_explicitly_enabled(self) -> None:
        app_settings = settings(str(Path(self.h.tmp.name) / "synthetic-send.sqlite3"), notifications_test_send=True)
        app = AgentApp(app_settings)
        fake_slack = FakeSlack()
        app.slack = fake_slack
        ok, text = app.service_titan_alert_test_text()
        self.assertTrue(ok)
        self.assertIn("Slack send: sent", text)
        self.assertEqual(len(fake_slack.messages), 1)
        self.assertIn("[TEST] Synthetic ServiceTitan audit alert", fake_slack.messages[0][1])

    def test_email_test_documents_smtp_and_no_servicetitan_email_alerting(self) -> None:
        app_settings = settings(str(Path(self.h.tmp.name) / "email.sqlite3"), notifications_test_send=False)
        app = AgentApp(app_settings)
        ok, text = app.email_test_text(["ops@example.com"])
        self.assertFalse(ok)
        self.assertIn("Email subsystem: implemented via SMTP EmailClient", text)
        self.assertIn("ServiceTitan audit email alerts: not implemented", text)
        self.assertIn("Email test message: skipped", text)

    def test_missing_external_credentials_fail_safely(self) -> None:
        missing = settings(
            str(Path(self.h.tmp.name) / "missing.sqlite3"),
            smtp_host="",
            smtp_user="",
            smtp_pass="",
            email_from="",
            slack_bot_token="",
            anthropic_api_key="",
        )
        client = EmailClient(missing)
        self.assertFalse(client.send_email("Subject", "Body", ["tim@example.com"]))
        self.assertIsNone(SlackClient(missing).post_message("COPS", "hello"))
        self.assertIsNone(ClaudeClient(missing).complete("system", "user"))
        self.assertEqual(ClaudeClient(missing).list_models(), [])

    def test_slack_lookup_user_by_email_uses_web_api_lookup(self) -> None:
        client = SlackClient(
            self.h.settings,
            FakeHttp([HttpResponse(200, {"ok": True, "user": {"id": "UEMAIL"}}, {})]),
        )
        self.assertEqual(client.lookup_user_by_email("emil@example.com"), "UEMAIL")
        self.assertIn("/users.lookupByEmail?email=emil%40example.com", client.http.calls[0])

    def test_claude_model_listing_returns_available_ids(self) -> None:
        client = ClaudeClient(
            settings(str(Path(self.h.tmp.name) / "claude.sqlite3"), anthropic_api_key="anthropic"),
            FakeHttp(
                [
                    HttpResponse(
                        200,
                        {"data": [{"id": "claude-test-b"}, {"id": "claude-test-a"}, {"no_id": True}]},
                        {},
                    )
                ]
            ),
        )
        self.assertEqual(client.list_models(), ["claude-test-a", "claude-test-b"])
        self.assertIn("/models", client.http.calls[0])

    def test_dotenv_loader_sets_missing_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("NOTION_API_KEY=from-file\nQUOTED_VALUE=\"hello world\"\n", encoding="utf-8")
            old_notion = os.environ.pop("NOTION_API_KEY", None)
            old_quoted = os.environ.pop("QUOTED_VALUE", None)
            try:
                load_dotenv(str(env_path))
                self.assertEqual(os.environ["NOTION_API_KEY"], "from-file")
                self.assertEqual(os.environ["QUOTED_VALUE"], "hello world")
            finally:
                os.environ.pop("NOTION_API_KEY", None)
                os.environ.pop("QUOTED_VALUE", None)
                if old_notion is not None:
                    os.environ["NOTION_API_KEY"] = old_notion
                if old_quoted is not None:
                    os.environ["QUOTED_VALUE"] = old_quoted

    def test_poll_skips_cleanly_on_notion_api_setup_error(self) -> None:
        self.h.notion = BrokenNotion()
        self.h.processor.notion = self.h.notion
        self.assertEqual(self.h.processor.poll_once(), 0)

    def test_notion_validation_error_returns_report_without_logging_key_error(self) -> None:
        logging.disable(logging.NOTSET)
        notion_logger = logging.getLogger("marketing_os_agent.clients.notion")
        old_propagate = notion_logger.propagate
        handler = logging.NullHandler()
        notion_logger.addHandler(handler)
        notion_logger.propagate = False
        try:
            client = NotionClient(
                self.h.settings,
                FakeHttp(
                    [
                        HttpResponse(
                            400,
                            {
                                "code": "validation_error",
                                "message": "Database does not contain any data sources accessible by this API bot.",
                            },
                            {},
                        )
                    ]
                ),
            )
            report = client.validate_databases()
            self.assertFalse(report.ok)
            self.assertIn("no accessible data source", report.errors[0])
        finally:
            notion_logger.removeHandler(handler)
            notion_logger.propagate = old_propagate
            logging.disable(logging.CRITICAL)

    def test_notion_queries_configured_data_source_id(self) -> None:
        client = NotionClient(self.h.settings, FakeHttp([HttpResponse(200, {"results": [], "has_more": False}, {})]))
        self.assertEqual(client.query_tasks_modified_since(None), [])
        self.assertIn("/data_sources/tasks-source/query", client.http.calls[0])

    def test_parses_existing_task_manager_schema(self) -> None:
        client = NotionClient(self.h.settings)
        page = {
            "id": "existing-task",
            "url": "https://notion.so/existing-task",
            "created_time": "2026-05-01T00:00:00Z",
            "last_edited_time": "2026-05-15T00:00:00Z",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Create a client base"}]},
                "Person": {
                    "type": "people",
                    "people": [{"id": "owner-1", "name": "Meerim Chukaeva", "person": {"email": "meerim@example.com"}}],
                },
                "Due date": {"type": "date", "date": {"start": "2026-05-21"}},
                "Status": {"type": "status", "status": {"name": "In progress"}},
                "Priority": {"type": "select", "select": {"name": "Urgent"}},
                "Type": {"type": "select", "select": {"name": "SEO"}},
            },
        }
        parsed = client.parse_task(page)
        self.assertEqual(parsed.name, "Create a client base")
        self.assertEqual(parsed.owner_name, "Meerim Chukaeva")
        self.assertEqual(parsed.deadline, date(2026, 5, 21))
        self.assertEqual(parsed.status, "In Progress")
        self.assertEqual(parsed.priority, "Critical")
        self.assertEqual(parsed.department, "SEO")
        self.assertIsNone(parsed.deadline_at)

    def test_parses_task_deadline_time_and_optional_reminder_state(self) -> None:
        reminder_settings = settings(
            str(Path(self.h.tmp.name) / "reminder-parse.sqlite3"),
            notion_task_last_reminder_sent_property="Last Reminder Sent At",
        )
        client = NotionClient(reminder_settings)
        page = {
            "id": "timed-task",
            "url": "https://notion.so/timed-task",
            "created_time": "2026-05-01T00:00:00Z",
            "last_edited_time": "2026-05-15T00:00:00Z",
            "properties": {
                "Task name": {"type": "title", "title": [{"plain_text": "Timed task"}]},
                "Owner": {
                    "type": "people",
                    "people": [{"id": "owner-1", "name": "Emil", "person": {"email": "emil@example.com"}}],
                },
                "Deadline": {"type": "date", "date": {"start": "2026-05-15T15:00:00+00:00"}},
                "Status": {"type": "status", "status": {"name": "In progress"}},
                "Last Reminder Sent At": {"type": "date", "date": {"start": "2026-05-15T14:05:00+00:00"}},
            },
        }
        parsed = client.parse_task(page)
        self.assertEqual(parsed.deadline, date(2026, 5, 15))
        self.assertEqual(parsed.deadline_at, datetime(2026, 5, 15, 15, tzinfo=timezone.utc))
        self.assertEqual(parsed.last_reminder_sent_at, datetime(2026, 5, 15, 14, 5, tzinfo=timezone.utc))

    def test_validation_accepts_existing_fixed_database_schema_with_optional_warnings(self) -> None:
        fixed_settings = settings(
            str(Path(self.h.tmp.name) / "fixed.sqlite3"),
            notion_task_name_property="Name",
            notion_task_owner_property="Person",
            notion_task_deadline_property="Due date",
            notion_task_department_property="Type",
            notion_task_original_deadline_property="",
            notion_task_campaign_property="",
            notion_task_deliverable_property="",
            notion_task_notes_property="",
            notion_task_needs_from_others_property="",
            notion_workbooks_page_id="",
            notion_workbooks_database_id="",
            notion_workbooks_data_source_id="",
        )
        task_props = {
            "Name": {"type": "title"},
            "Person": {"type": "people"},
            "Due date": {"type": "date"},
            "Status": {"type": "status"},
            "Priority": {"type": "select"},
            "Type": {"type": "select"},
        }
        campaign_props = {
            "Campaign name": {"type": "title"},
            "Trade": {"type": "multi_select"},
            "Channel": {"type": "multi_select"},
            "Start Date": {"type": "date"},
            "End Date": {"type": "date"},
            "Owner": {"type": "people"},
            "Status": {"type": "status"},
            "Planned Spend": {"type": "number"},
            "Expected Leads": {"type": "number"},
            "Expected CPL": {"type": "formula"},
            "Expected ROI": {"type": "number"},
            "Actual Spend": {"type": "number"},
            "Actual Leads": {"type": "number"},
            "Actual CPL": {"type": "formula"},
            "Actual ROI": {"type": "number"},
        }
        client = NotionClient(
            fixed_settings,
            FakeHttp(
                [
                    HttpResponse(200, {"properties": task_props}, {}),
                    HttpResponse(200, {"properties": campaign_props}, {}),
                ]
            ),
        )
        report = client.validate_databases()
        self.assertTrue(report.ok, report.to_text())
        self.assertTrue(any("No Workbooks" in warning for warning in report.warnings))

    def test_logging_extras_do_not_use_reserved_log_record_fields(self) -> None:
        reserved = set(LogRecord("x", 20, __file__, 1, "msg", (), None).__dict__)
        text = "\n".join(path.read_text(encoding="utf-8") for path in Path("marketing_os_agent").rglob("*.py"))
        for match in re.finditer(r"extra=\{([^}]*)\}", text, re.DOTALL):
            keys = re.findall(r"['\"]([^'\"]+)['\"]\s*:", match.group(1))
            forbidden = sorted(set(keys) & reserved)
            self.assertEqual(forbidden, [], f"Reserved logging extra field(s): {forbidden}")

    def test_poll_initializes_full_baseline_before_detecting_transitions(self) -> None:
        baseline_tasks = [
            task("base-1", "Not Started", edited=datetime(2026, 5, 15, 10, tzinfo=timezone.utc)),
            task("base-2", "In Progress", edited=datetime(2026, 5, 15, 10, tzinfo=timezone.utc)),
        ]
        changed = task("base-1", "Completed", deliverable_link="https://drive.google.com/file", edited=datetime(2026, 5, 15, 11, tzinfo=timezone.utc))
        self.h.notion = SequencedNotion(baseline_tasks, [changed])
        self.h.processor.notion = self.h.notion

        self.assertEqual(self.h.processor.poll_once(), 0)
        self.assertEqual(self.h.db.count_task_states(), 2)
        self.assertEqual(self.h.processor.poll_once(), 1)

        status_posts = [message for message in self.h.slack.messages if "Task:" in message[1]]
        self.assertEqual(len(status_posts), 1)

    def test_poll_uses_overlap_when_querying_modified_tasks(self) -> None:
        self.h.db.set_kv("notion_tasks_baseline_initialized", "true")
        self.h.db.set_kv("notion_tasks_last_processed", "2026-05-15T14:44:00+00:00")
        item = task("overlap-1", "Not Started", edited=datetime(2026, 5, 15, 14, tzinfo=timezone.utc))
        self.h.save_previous(item, status="Not Started")

        class CapturingNotion(FakeNotion):
            def __init__(self) -> None:
                super().__init__()
                self.since_seen: str | None = None

            def query_tasks_modified_since(self, since: str | None) -> list[Task]:
                self.since_seen = since
                return []

        notion = CapturingNotion()
        self.h.processor.notion = notion
        self.h.processor.poll_once()
        self.assertEqual(notion.since_seen, "2026-05-15T13:44:00+00:00")

    def test_process_pending_transitions_scans_all_tasks(self) -> None:
        item = task("pending-1", "Completed", deliverable_link="https://drive.google.com/file", edited=datetime(2026, 5, 15, 14, 22, tzinfo=timezone.utc))
        self.h.save_previous(item, status="Not Started")
        self.h.notion.tasks = [item]
        processed = self.h.processor.process_pending_transitions()
        self.assertEqual(processed, 1)
        self.assertEqual(self.h.db.get_task_state("pending-1")["status"], "Completed")

    def test_slack_failure_keeps_transition_pending(self) -> None:
        item = task("slack-fail-1", "Completed", deliverable_link="https://drive.google.com/file")
        self.h.save_previous(item, status="Not Started")
        self.h.notion.tasks = [item]
        self.h.slack = FailingSlack()
        self.h.processor.slack = self.h.slack
        processed = self.h.processor.process_pending_transitions()
        self.assertEqual(processed, 0)
        self.assertEqual(self.h.db.get_task_state("slack-fail-1")["status"], "Not Started")
        self.assertEqual(self.h.db.get_transitions_missing_slack(), [])

    def test_repost_missing_slack_updates_posts_unthreaded_transition(self) -> None:
        item = task("missing-slack-1", "Completed", deliverable_link="https://drive.google.com/file")
        self.h.notion.tasks = [item]
        dedupe_key = "task-status:missing-slack-1:Completed:2026-05-15T00:00:00+00:00"
        self.h.db.record_status_transition(
            task_id=item.id,
            from_status="Not Started",
            to_status="Completed",
            notion_last_edited_time="2026-05-15T00:00:00+00:00",
            dedupe_key=dedupe_key,
        )
        self.assertEqual(self.h.processor.repost_missing_slack_updates(), 1)
        self.assertEqual(len(self.h.db.get_transitions_missing_slack()), 0)

    def test_repeated_completion_counts_when_intermediate_state_observed(self) -> None:
        first_done = task("repeat-1", "Completed", deliverable_link="https://drive.google.com/file", edited=datetime(2026, 5, 15, 10, tzinfo=timezone.utc))
        in_progress = task("repeat-1", "In Progress", deliverable_link="", edited=datetime(2026, 5, 15, 11, tzinfo=timezone.utc))
        second_done = task("repeat-1", "Completed", deliverable_link="https://drive.google.com/file", edited=datetime(2026, 5, 15, 12, tzinfo=timezone.utc))

        self.h.save_previous(first_done, status="Not Started")
        self.assertTrue(self.h.processor.process_status_change(first_done, "Not Started"))
        self.h.db.upsert_task_state(
            task_id=in_progress.id,
            name=in_progress.name,
            owner_name=in_progress.owner_name,
            owner_notion_user_id=in_progress.owner_notion_user_id,
            owner_email=in_progress.owner_email,
            status=in_progress.status,
            deadline=in_progress.deadline.isoformat() if in_progress.deadline else None,
            original_deadline=in_progress.original_deadline.isoformat() if in_progress.original_deadline else None,
            last_edited_time=in_progress.last_edited_time.isoformat(),
        )
        self.assertTrue(self.h.processor.process_status_change(second_done, "In Progress"))

        completed_counts = [
            row for row in self.h.db.get_status_transition_counts()
            if row["task_id"] == "repeat-1" and row["to_status"] == "Completed"
        ]
        self.assertEqual(completed_counts[0]["transition_count"], 2)

    def test_transition_counts_output_explains_observed_only(self) -> None:
        from marketing_os_agent.app import AgentApp

        app = AgentApp(self.h.settings)
        app.db = self.h.db
        app.notion = self.h.notion
        text = app.transition_counts_text()
        self.assertIn("only transitions observed", text)

    def test_debug_tasks_shows_notion_and_local_status(self) -> None:
        item = task("debug-1", "In Progress", name="Debug Task")
        self.h.notion.tasks = [item]
        self.h.save_previous(item, status="Not Started")
        from marketing_os_agent.app import AgentApp

        app = AgentApp(self.h.settings)
        app.db = self.h.db
        app.notion = self.h.notion
        debug_text = app.debug_tasks_text()
        self.assertIn("Debug Task", debug_text)
        self.assertIn("notion_status=In Progress", debug_text)
        self.assertIn("local_status=Not Started", debug_text)

    def test_send_test_email_uses_explicit_recipients(self) -> None:
        from marketing_os_agent.app import AgentApp

        app = AgentApp(self.h.settings)
        fake_email = FakeEmail()
        app.email = fake_email
        app.notion = self.h.notion
        app.claude = self.h.claude
        self.h.notion.tasks = [
            task("email-preview-1", "Completed", name="Finished task", deadline=date.today()),
            task("email-preview-2", "Blocked", name="Blocked task", needs="Need Emil"),
        ]
        sent, recipients = app.send_test_email(["one@example.com,two@example.com", "one@example.com"])
        self.assertTrue(sent)
        self.assertEqual(recipients, ["one@example.com", "two@example.com"])
        self.assertEqual(fake_email.sent[0][0], "[Test] Friday Marketing Roundup Preview")
        self.assertEqual(fake_email.sent[0][2], recipients)
        self.assertIn("TEST PREVIEW", fake_email.sent[0][1])
        self.assertIn("Completed (1)", fake_email.sent[0][1])
        self.assertIsNotNone(fake_email.sent[0][3])
        self.assertIn("<html>", fake_email.sent[0][3] or "")
        self.assertNotIn("##", fake_email.sent[0][1])


if __name__ == "__main__":
    unittest.main()
