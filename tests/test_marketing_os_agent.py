from __future__ import annotations

import tempfile
import unittest
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from logging import LogRecord

from marketing_os_agent.clients.email_client import EmailClient
from marketing_os_agent.clients.claude import ClaudeClient
from marketing_os_agent.clients.http import HttpResponse
from marketing_os_agent.clients.notion import NotionApiError, NotionClient
from marketing_os_agent.clients.slack import SlackClient
from marketing_os_agent.config import Settings, load_dotenv
from marketing_os_agent.domain.campaign_health import CampaignHealthService
from marketing_os_agent.domain.owner_mapping import OwnerResolver
from marketing_os_agent.domain.reports import ReportService, month_bounds, select_campaigns_starting_between, week_bounds
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
    owner_name: str = "Emil",
    deadline: date | None = date(2026, 5, 15),
    deliverable_link: str = "",
    notes: str = "",
    needs: str = "",
    edited: datetime | None = None,
    campaigns: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        name=name,
        owner=owner(owner_name),
        deadline=deadline,
        original_deadline=deadline,
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


class FakeNotion:
    def __init__(self) -> None:
        self.available = True
        self.comments: list[tuple[str, str, bool]] = []
        self.flags: list[tuple[str, bool]] = []
        self.tasks: list[Task] = []
        self.campaigns: list[Campaign] = []
        self.original_deadlines: list[str] = []

    def add_task_comment(self, task: Task, text: str, mention_owner: bool = False) -> None:
        self.comments.append((task.id, text, mention_owner))

    def set_needs_verification(self, task_id: str, value: bool) -> None:
        self.flags.append((task_id, value))

    def set_original_deadline_if_missing(self, task: Task) -> None:
        self.original_deadlines.append(task.id)

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

    def request_json(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: dict[str, object] | None = None) -> HttpResponse:
        self.calls.append(url)
        return self.responses.pop(0)


class FakeSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str | None]] = []
        self.dms: list[tuple[str, str]] = []
        self.counter = 0

    def post_message(self, channel: str, text: str, blocks: object | None = None, thread_ts: str | None = None) -> str:
        self.counter += 1
        self.messages.append((channel, text, thread_ts))
        return f"{self.counter}.000"

    def reply(self, channel: str, thread_ts: str, text: str) -> str:
        return self.post_message(channel, text, thread_ts=thread_ts)

    def dm_user(self, user_id: str, text: str) -> str:
        self.dms.append((user_id, text))
        return f"dm-{len(self.dms)}"


class FailingSlack(FakeSlack):
    def post_message(self, channel: str, text: str, blocks: object | None = None, thread_ts: str | None = None) -> str | None:
        self.messages.append((channel, text, thread_ts))
        return None


class FakeClaude:
    def draft_monday_owner_message(self, owner_name: str, task_lines: list[str]) -> str:
        return f"Monday for {owner_name}\n" + "\n".join(task_lines)

    def draft_friday_roundup(self, structured_sections: dict[str, list[str]]) -> str:
        return "\n".join(f"{key}: {len(value)}" for key, value in structured_sections.items())

    def draft_verification_comment(self, status: str, issues: list[str]) -> str:
        return f"Marked {status.lower()} — " + " ".join(issues)


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[str]]] = []

    def send_email(self, subject: str, body: str, recipients: list[str]) -> bool:
        self.sent.append((subject, body, recipients))
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


class MarketingOsAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

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
            ],
            week_start,
            week_end,
            week_end.replace(day=week_end.day + 1),
            week_end.replace(day=week_end.day + 7),
        )
        self.assertEqual(len(sections["Completed"]), 1)
        self.assertEqual(len(sections["Delayed, with new deadline and reason"]), 1)
        self.assertEqual(len(sections["Blocked"]), 1)
        self.assertEqual(len(sections["Canceled"]), 1)
        self.assertEqual(len(sections["Coming next week"]), 1)

    def test_monday_push_groups_tasks_by_owner(self) -> None:
        grouped = self.h.reports.monday_push(
            [
                task("m1", "Not Started", owner_name="Emil", deadline=date(2026, 5, 11)),
                task("m2", "In Progress", owner_name="Vadim", deadline=date(2026, 5, 12)),
                task("m3", "Completed", owner_name="Emil", deadline=date(2026, 5, 13)),
            ],
            datetime(2026, 5, 11, 8, tzinfo=timezone.utc),
        )
        self.assertEqual(set(grouped), {"Emil", "Vadim"})
        self.assertEqual(len(self.h.slack.dms), 2)

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
            task("email-preview-1", "Completed", name="Finished task", deadline=date(2026, 5, 15)),
            task("email-preview-2", "Blocked", name="Blocked task", needs="Need Emil"),
        ]
        sent, recipients = app.send_test_email(["one@example.com,two@example.com", "one@example.com"])
        self.assertTrue(sent)
        self.assertEqual(recipients, ["one@example.com", "two@example.com"])
        self.assertEqual(fake_email.sent[0][0], "[Test] Friday Marketing Roundup Preview")
        self.assertEqual(fake_email.sent[0][2], recipients)
        self.assertIn("live preview of the Friday roundup", fake_email.sent[0][1])
        self.assertIn("Completed:", fake_email.sent[0][1])


if __name__ == "__main__":
    unittest.main()
