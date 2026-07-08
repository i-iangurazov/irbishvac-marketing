from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading

from .app import AgentApp
from .config import Settings, load_dotenv
from .logging_config import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Marketing OS Agent")
    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("run", help="Run the long-lived service")
    sub.add_parser("init-db", help="Initialize SQLite persistence")
    sub.add_parser("validate-notion", help="Validate configured Notion databases")
    sub.add_parser("seed-workbooks", help="Create missing required workbook records in the configured Workbooks database")
    sub.add_parser("poll-once", help="Run one Notion polling cycle")
    sub.add_parser("rebuild-task-baseline", help="Rebuild local task baseline from all Notion tasks without posting transitions")
    sub.add_parser("process-pending-transitions", help="Scan all Notion tasks once and post any status transitions vs local baseline")
    sub.add_parser("repost-missing-slack-updates", help="Retry transition Slack posts that previously failed before a Slack timestamp was stored")
    sub.add_parser("servicetitan-audit-once", help="Run one ServiceTitan operations audit cycle")
    sub.add_parser("servicetitan-discover-scopes", help="Print sanitized ServiceTitan scope values for rule configuration")
    sub.add_parser("servicetitan-runtime-diagnostics", help="Print masked ServiceTitan runtime config and audit state")
    sub.add_parser("servicetitan-weekly-summary-once", help="Build and optionally send one ServiceTitan weekly violation summary")
    sub.add_parser("pm-audit-once", help="Run one Project Management audit cycle")
    sub.add_parser("pm-audit-test-slack", help="Validate and optionally send a synthetic PM Audit Slack test message")
    sub.add_parser("install-audit-once", help="Run one Installer Audit cycle")
    sub.add_parser("install-audit-test-slack", help="Validate and optionally send a synthetic Installer Audit Slack test message")
    sub.add_parser("notifications-test", help="Validate notification configuration and optionally send a Slack test message")
    sub.add_parser("servicetitan-alert-test", help="Build a synthetic ServiceTitan alert and optionally send it to Slack")
    email_test = sub.add_parser("email-test", help="Validate SMTP configuration and optionally send a safe test email")
    email_test.add_argument("--to", action="append", default=[], help="Recipient email. May be repeated or comma-separated. Defaults to TIM_EMAIL and VADIM_EMAIL.")
    sub.add_parser("health-check", help="Run local integration health checks")
    sub.add_parser("monday-push", help="Run Monday push immediately")
    sub.add_parser("friday-roundup", help="Run Friday roundup immediately")
    sub.add_parser("monthly-kickoff", help="Run monthly kickoff immediately")
    sub.add_parser("quarterly-kickoff", help="Run quarterly kickoff immediately")
    sub.add_parser("campaign-health-scan", help="Run campaign health checks immediately")
    sub.add_parser("smoke-test", help="Read connected Notion data and print a non-destructive integration summary")
    sub.add_parser("debug-tasks", help="Print current Notion task statuses next to local baseline statuses")
    sub.add_parser("transition-counts", help="Print observed status transition counts from local history")
    sub.add_parser("list-claude-models", help="List Claude model IDs available to the configured Anthropic API key")
    test_email = sub.add_parser("test-email", help="Send a simple SMTP test email without running a roundup")
    test_email.add_argument("--to", action="append", default=[], help="Recipient email. May be repeated or comma-separated. Defaults to TIM_EMAIL and VADIM_EMAIL.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        if command == "servicetitan-runtime-diagnostics":
            load_dotenv()
            configure_logging(os.getenv("LOG_LEVEL", "INFO"))
            print(_settings_error_diagnostics_text(str(exc)))
            return 1
        raise
    configure_logging(settings.log_level)
    app = AgentApp(settings)

    if command == "init-db":
        app.initialize_storage()
        logging.getLogger(__name__).info("database_initialized", extra={"path": settings.sqlite_path})
        return 0

    if command == "validate-notion":
        app.initialize_storage()
        report = app.validate_notion()
        print(report.to_text())
        return 0 if report.ok else 1

    if command == "seed-workbooks":
        app.initialize_storage()
        created = app.seed_workbooks()
        print(f"Created {len(created)} workbook(s)")
        for name in created:
            print(f"- {name}")
        return 0

    if command == "poll-once":
        app.initialize_storage()
        app.poll_once()
        return 0

    if command == "rebuild-task-baseline":
        app.initialize_storage()
        count = app.rebuild_task_baseline()
        print(f"Rebuilt task baseline with {count} task(s)")
        return 0

    if command == "process-pending-transitions":
        app.initialize_storage()
        count = app.process_pending_transitions()
        print(f"Processed {count} pending transition(s)")
        return 0

    if command == "repost-missing-slack-updates":
        app.initialize_storage()
        count = app.repost_missing_slack_updates()
        print(f"Reposted {count} missing Slack update(s)")
        return 0

    if command == "servicetitan-audit-once":
        app.initialize_storage()
        summary = app.run_service_titan_audit_once(force=True)
        print("\n".join(summary.to_lines()))
        return 1 if summary.status in {"config_error", "api_error"} else 0

    if command == "servicetitan-discover-scopes":
        app.initialize_storage()
        summary = app.run_service_titan_scope_discovery()
        print("\n".join(summary.to_lines()))
        return 1 if summary.status in {"config_error", "api_error"} else 0

    if command == "servicetitan-runtime-diagnostics":
        app.initialize_storage()
        print(app.service_titan_runtime_diagnostics_text())
        return 0

    if command == "servicetitan-weekly-summary-once":
        app.initialize_storage()
        summary = app.run_service_titan_weekly_summary(force=True)
        print(summary.to_text())
        return 1 if summary.status in {"config_error", "slack_error"} else 0

    if command == "pm-audit-once":
        app.initialize_storage()
        summary = app.run_pm_audit_once()
        print("\n".join(summary.to_lines()))
        return 1 if summary.status in {"config_error", "api_error", "slack_error"} else 0

    if command == "pm-audit-test-slack":
        ok, text = app.pm_audit_slack_test_text()
        print(text)
        return 0 if ok else 1

    if command == "install-audit-once":
        app.initialize_storage()
        summary = app.run_install_audit_once()
        print("\n".join(summary.to_lines()))
        return 1 if summary.status in {"config_error", "api_error", "slack_error"} else 0

    if command == "install-audit-test-slack":
        ok, text = app.install_audit_slack_test_text()
        print(text)
        return 0 if ok else 1

    if command == "notifications-test":
        ok, text = app.notifications_test_text()
        print(text)
        return 0 if ok else 1

    if command == "servicetitan-alert-test":
        ok, text = app.service_titan_alert_test_text()
        print(text)
        return 0 if ok else 1

    if command == "email-test":
        ok, text = app.email_test_text(args.to)
        print(text)
        return 0 if ok else 1

    if command == "health-check":
        app.initialize_storage()
        report = app.health_check()
        print(report.to_text())
        return 0 if report.ok else 1

    if command == "monday-push":
        app.initialize_storage()
        app.run_monday_push()
        return 0

    if command == "friday-roundup":
        app.initialize_storage()
        app.run_friday_roundup()
        return 0

    if command == "monthly-kickoff":
        app.initialize_storage()
        app.run_monthly_kickoff()
        return 0

    if command == "quarterly-kickoff":
        app.initialize_storage()
        app.run_quarterly_kickoff()
        return 0

    if command == "campaign-health-scan":
        app.initialize_storage()
        app.run_campaign_health_scan()
        return 0

    if command == "smoke-test":
        app.initialize_storage()
        print(app.smoke_test_text())
        return 0

    if command == "debug-tasks":
        app.initialize_storage()
        print(app.debug_tasks_text())
        return 0

    if command == "transition-counts":
        app.initialize_storage()
        print(app.transition_counts_text())
        return 0

    if command == "list-claude-models":
        app.initialize_storage()
        print(app.claude_models_text())
        return 0

    if command == "test-email":
        app.initialize_storage()
        sent, recipients = app.send_test_email(args.to)
        if sent:
            print("Test email sent to:")
            for recipient in recipients:
                print(f"- {recipient}")
            return 0
        print("Test email failed. Check SMTP_* and EMAIL_FROM in .env, then inspect the email_failure log.")
        if recipients:
            print("Attempted recipients:")
            for recipient in recipients:
                print(f"- {recipient}")
        return 1

    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        logging.getLogger(__name__).info("shutdown_signal_received", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    app.run(stop_event)
    return 0


def _settings_error_diagnostics_text(error: str) -> str:
    disabled_raw = os.getenv("SERVICE_TITAN_DISABLED_RULE_IDS_JSON", "").strip()
    scope_raw = os.getenv("SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON", "").strip()
    channel = os.getenv("SLACK_ALERT_CHANNEL_ID", "")
    return "\n".join(
        [
            "ServiceTitan runtime diagnostics",
            "- sanitized: true",
            f"- settings_error: {error}",
            "- runtime config could not be fully parsed; fix the error above, then rerun diagnostics",
            f"- SERVICE_TITAN_AUDIT_ENABLED raw: {os.getenv('SERVICE_TITAN_AUDIT_ENABLED', '<unset>')}",
            f"- SERVICE_TITAN_AUDIT_DRY_RUN raw: {os.getenv('SERVICE_TITAN_AUDIT_DRY_RUN', '<unset>')}",
            f"- SERVICE_TITAN_AUDIT_BACKFILL_ALERTS raw: {os.getenv('SERVICE_TITAN_AUDIT_BACKFILL_ALERTS', '<unset>')}",
            f"- SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE raw: {os.getenv('SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE', '<unset>')}",
            f"- SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE raw: {os.getenv('SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE', '<unset>')}",
            f"- SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED raw: {os.getenv('SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED', '<unset>')}",
            f"- SERVICE_TITAN_WEEKLY_SUMMARY_DAY raw: {os.getenv('SERVICE_TITAN_WEEKLY_SUMMARY_DAY', '<unset>')}",
            f"- SERVICE_TITAN_WEEKLY_SUMMARY_HOUR raw: {os.getenv('SERVICE_TITAN_WEEKLY_SUMMARY_HOUR', '<unset>')}",
            f"- SERVICE_TITAN_WEEKLY_SUMMARY_LOOKBACK_DAYS raw: {os.getenv('SERVICE_TITAN_WEEKLY_SUMMARY_LOOKBACK_DAYS', '<unset>')}",
            f"- SALES_COMFORT_ADVISOR_AUDIT_ENABLED raw: {os.getenv('SALES_COMFORT_ADVISOR_AUDIT_ENABLED', '<unset>')}",
            f"- HVAC_SERVICE_AUDIT_ENABLED raw: {os.getenv('HVAC_SERVICE_AUDIT_ENABLED', '<unset>')}",
            f"- PLUMBING_SERVICE_AUDIT_ENABLED raw: {os.getenv('PLUMBING_SERVICE_AUDIT_ENABLED', '<unset>')}",
            f"- TECHNICIAN_COMPLIANCE_ENABLED raw: {os.getenv('TECHNICIAN_COMPLIANCE_ENABLED', '<unset>')}",
            f"- DISPATCHER_AUDIT_ENABLED raw: {os.getenv('DISPATCHER_AUDIT_ENABLED', '<unset>')}",
            f"- SERVICE_TITAN_DISABLED_RULE_IDS_JSON valid list: {_raw_json_valid(disabled_raw, expected='list')}",
            f"- SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON valid object: {_raw_json_valid(scope_raw, expected='object')}",
            f"- SLACK_BOT_TOKEN present: {bool(os.getenv('SLACK_BOT_TOKEN'))}",
            f"- SLACK_ALERT_CHANNEL_ID: {_mask_channel(channel)}",
        ]
    )


def _raw_json_valid(raw: str, *, expected: str) -> bool:
    if not raw:
        return True
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if expected == "list":
        return isinstance(parsed, list)
    if expected == "object":
        return isinstance(parsed, dict)
    return True


def _mask_channel(value: str) -> str:
    if not value:
        return "<missing>"
    return f"***{value[-4:]}"


if __name__ == "__main__":
    sys.exit(main())
