from __future__ import annotations

import copy
import io
import os
import tempfile
import threading
import unittest
import urllib.error
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from marketing_os_agent.app import AgentApp
from marketing_os_agent.__main__ import main
from marketing_os_agent.clients.http import HttpClient, HttpResponse
from marketing_os_agent.clients.notion import NotionApiError, NotionClient
from marketing_os_agent.clients.slack import SlackClient, SlackDeliveryError
from marketing_os_agent.config import Settings
from marketing_os_agent.domain.recurring_report import REPORT_NAME, RecurringReportService
from marketing_os_agent.persistence import Persistence
from marketing_os_agent.scheduler import ScheduledJob, Scheduler

OWNER_ID = "a5274c27-110e-4cb9-9fa4-62fe5c46eccc"
LA = ZoneInfo("America/Los_Angeles")
FRIDAY = datetime(2026, 10, 16, 16, tzinfo=LA)
MONDAY = datetime(2026, 10, 19, 8, tzinfo=LA)


def config(path: str, **overrides: object) -> Settings:
    with patch.dict(os.environ, {"TIMEZONE": "America/Los_Angeles"}, clear=True):
        settings = Settings.from_env()
    return replace(settings, sqlite_path=path, notion_api_key="test", slack_bot_token="test",
                   notion_tasks_data_source_id="source", marketing_performance_report_enabled=True,
                   marketing_performance_report_dry_run=False,
                   marketing_performance_report_owner_notion_user_id=OWNER_ID, **overrides)


class FakeNotion:
    def __init__(self) -> None:
        self.schema = {
            "Name": {"type": "title"}, "Person": {"type": "people"}, "Due date": {"type": "date"},
            "Status": {"type": "select", "select": {"options": [{"name": "Not started"}, {"name": "Done"}]}},
            "Deadline rule": {"type": "rich_text"},
            "Frequency": {"type": "select", "select": {"options": [{"name": "Weekly"}]}},
            "Department": {"type": "select", "select": {"options": [{"name": "Marketing"}]}},
        }
        self.pages: list[dict] = []
        self.calls = []
        self.create_error = None
        self.query_error = None
        self.user = {"id": OWNER_ID, "name": "Emil Sultanaliev", "type": "person", "person": {"email": "emil@irbishvac.com"}}

    def tasks_data_source_id(self):
        return "source"

    def retrieve_user(self, user_id):
        self.calls.append(("user", user_id))
        return self.user

    def retrieve_data_source(self, source_id):
        return {"properties": self.schema}

    def query_data_source(self, source_id, body):
        self.calls.append(("query", body))
        if self.query_error:
            error, self.query_error = self.query_error, None
            raise error

        def matches(page, filt):
            if "or" in filt:
                return any(matches(page, item) for item in filt["or"])
            if "and" in filt:
                return all(matches(page, item) for item in filt["and"])
            prop = page["properties"].get(filt["property"], {})
            if "people" in filt:
                return filt["people"]["contains"] in [user["id"] for user in prop.get("people", [])]
            if "date" in filt:
                return (prop.get("date") or {}).get("start", "")[:10] == filt["date"]["equals"]
            kind = "title" if "title" in filt else "rich_text"
            value = "".join(item.get("text", {}).get("content", "") for item in prop.get(kind, []))
            return value == filt[kind]["equals"]

        return [page for page in self.pages if matches(page, body["filter"])]

    def create_page(self, parent, properties, children=None, *, retry_on_failure=True):
        self.calls.append(("create", parent, copy.deepcopy(properties), retry_on_failure))
        if self.create_error:
            error, self.create_error = self.create_error, None
            raise error
        page = {"id": f"page-{len(self.pages)+1}", "properties": copy.deepcopy(properties), "url": "https://notion.so/report"}
        self.pages.append(page)
        return page


class FakeSlack:
    def __init__(self):
        self.available = True
        self.lookups = []
        self.dms = []
        self.recipient = "UEMIL"
        self.error = None

    def lookup_user_by_email(self, email):
        self.lookups.append(email)
        return self.recipient

    def dm_user_once(self, user_id, text, client_msg_id):
        if self.error:
            error, self.error = self.error, None
            raise error
        self.dms.append((user_id, text, client_msg_id))
        return "123.456"


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.settings = config(self.path)
        self.db = Persistence(self.path)
        self.db.initialize()
        self.notion, self.slack = FakeNotion(), FakeSlack()
        self.service = RecurringReportService(self.settings, self.db, self.notion, self.slack)

    def service_with(self, **overrides):
        return RecurringReportService(replace(self.settings, **overrides), self.db, self.notion, self.slack)

    def test_friday_creates_monday_task_assigned_to_emil(self):
        self.assertEqual(self.service.create_task(FRIDAY)["status"], "created")
        page = self.notion.pages[0]
        self.assertEqual(page["properties"]["Due date"], {"date": {"start": "2026-10-19"}})
        self.assertEqual(page["properties"]["Person"], {"people": [{"id": OWNER_ID}]})
        self.assertEqual(page["properties"]["Status"], {"select": {"name": "Not started"}})
        self.assertEqual(page["properties"]["Frequency"], {"select": {"name": "Weekly"}})
        self.assertFalse(next(call for call in self.notion.calls if call[0] == "create")[3])
        self.assertEqual(self.slack.dms, [])

    def test_restarts_and_repeated_calls_do_not_create_duplicates(self):
        self.service.create_task(FRIDAY)
        db = Persistence(self.path)
        db.initialize()
        restarted = RecurringReportService(self.settings, db, self.notion, self.slack)
        for minute in [1, 15, 30, 120]:
            self.assertEqual(restarted.create_task(FRIDAY + timedelta(minutes=minute))["status"], "suppressed")
        self.assertEqual(len(self.notion.pages), 1)
        self.assertEqual(restarted.create_task(FRIDAY + timedelta(days=7))["status"], "created")
        self.assertEqual(self.notion.pages[1]["properties"]["Due date"]["date"]["start"], "2026-10-26")

    def test_notion_key_survives_local_state_loss_rename_and_deadline_edit(self):
        self.service.create_task(FRIDAY)
        page = self.notion.pages[0]
        page["properties"]["Name"]["title"][0]["text"]["content"] = "Renamed report"
        page["properties"]["Due date"]["date"]["start"] = "2026-10-20"
        fresh = Persistence(str(Path(self.temp.name) / "new.sqlite3"))
        fresh.initialize()
        service = RecurringReportService(self.settings, fresh, self.notion, self.slack)
        self.assertEqual(service.create_task(FRIDAY)["status"], "existing")
        self.assertEqual(len(self.notion.pages), 1)

    def test_existing_manual_report_is_adopted(self):
        self.service.create_task(FRIDAY)
        self.notion.pages[0]["properties"]["Name"]["title"][0]["text"]["content"] = REPORT_NAME
        self.notion.pages[0]["properties"]["Deadline rule"] = {"rich_text": []}
        fresh = Persistence(str(Path(self.temp.name) / "new.sqlite3"))
        fresh.initialize()
        service = RecurringReportService(self.settings, fresh, self.notion, self.slack)
        self.assertEqual(service.create_task(FRIDAY)["status"], "existing")
        self.assertEqual(len(self.notion.pages), 1)

    def test_monday_dm_contains_link_and_complete_and_send(self):
        self.service.create_task(FRIDAY)
        self.assertEqual(self.service.remind(MONDAY)["status"], "sent")
        user, text, message_id = self.slack.dms[0]
        self.assertEqual(user, "UEMIL")
        self.assertIn("complete and send", text)
        self.assertIn("https://notion.so/report", text)
        self.assertEqual(len(message_id), 36)
        self.assertEqual(self.slack.lookups, ["emil@irbishvac.com"])
        restarted = RecurringReportService(self.settings, Persistence(self.path), self.notion, self.slack)
        self.assertEqual(restarted.remind(MONDAY + timedelta(minutes=15))["status"], "suppressed")
        self.assertEqual(len(self.slack.dms), 1)

    def test_terminal_reports_are_silent(self):
        self.service.create_task(FRIDAY)
        for status in ["Done", "Completed", "Cancelled", "Canceled", "Archived"]:
            with self.subTest(status=status):
                fresh = Persistence(str(Path(self.temp.name) / f"{status}.sqlite3"))
                fresh.initialize()
                self.notion.pages[0]["properties"]["Status"]["select"]["name"] = status
                service = RecurringReportService(self.settings, fresh, self.notion, self.slack)
                self.assertEqual(service.remind(MONDAY)["status"], "closed")
        self.assertEqual(self.slack.dms, [])

    def test_moved_or_reassigned_reports_do_not_remind_emil(self):
        self.service.create_task(FRIDAY)
        self.notion.pages[0]["properties"]["Due date"]["date"]["start"] = "2026-10-20"
        self.assertEqual(self.service.remind(MONDAY)["status"], "rescheduled_or_reassigned")
        self.assertEqual(self.slack.dms, [])

    def test_unknown_owner_or_slack_identity_never_falls_back_to_tim(self):
        self.service.create_task(FRIDAY)
        self.slack.recipient = None
        self.assertEqual(self.service.remind(MONDAY)["status"], "failed")
        self.assertEqual(self.slack.dms, [])
        self.notion.user = {"id": "wrong-id", "type": "person"}
        self.assertEqual(self.service.validate(FRIDAY)["status"], "config_error")

    def test_validation_requires_slack_credentials_even_with_id_mapping(self):
        self.slack.available = False
        service = self.service_with(owner_slack_map={OWNER_ID: "UEMIL"})
        self.assertEqual(service.validate(FRIDAY)["status"], "config_error")
        self.assertEqual(self.slack.dms, [])

    def test_id_mapping_works_when_notion_email_is_unavailable(self):
        self.service.create_task(FRIDAY)
        self.notion.user["person"] = {}
        service = self.service_with(owner_slack_map={OWNER_ID: "UEMIL"})
        self.assertEqual(service.remind(MONDAY)["status"], "sent")
        self.assertEqual(self.slack.lookups, [])

    def test_missing_report_never_creates_on_monday(self):
        self.assertEqual(self.service.remind(MONDAY)["status"], "failed")
        self.assertEqual(self.notion.pages, [])
        self.assertEqual(self.slack.dms, [])

    def test_dry_run_and_validation_write_no_state_or_messages(self):
        service = self.service_with(marketing_performance_report_dry_run=True)
        self.assertEqual(service.create_task(FRIDAY)["status"], "dry_run")
        self.assertEqual(service.validate(FRIDAY)["status"], "validated")
        self.assertIsNone(self.db.get_recurring_report_action(service.action_key("create", date(2026, 10, 19))))
        self.assertEqual(self.notion.pages, [])
        self.service.create_task(FRIDAY)
        self.assertEqual(service.remind(MONDAY)["status"], "dry_run")
        self.assertEqual(self.slack.dms, [])
        self.assertIsNone(self.db.get_recurring_report_action(service.action_key("remind", MONDAY.date())))

    def test_disabled_workflow_does_nothing(self):
        service = self.service_with(marketing_performance_report_enabled=False)
        self.assertFalse(service.should_create(FRIDAY))
        self.assertFalse(service.should_remind(MONDAY))
        self.assertEqual(service.create_task(FRIDAY)["status"], "outside_schedule")
        self.assertEqual(self.notion.calls, [])

    def test_schema_status_type_is_preserved(self):
        self.notion.schema["Status"] = {"type": "status", "status": {"options": [{"name": "Not Started"}]}}
        self.assertEqual(self.service.create_task(FRIDAY)["status"], "created")
        self.assertEqual(self.notion.pages[0]["properties"]["Status"], {"status": {"name": "Not Started"}})

    def test_incompatible_schema_is_retryable_before_any_write(self):
        self.notion.schema.pop("Deadline rule")
        self.assertEqual(self.service.create_task(FRIDAY)["status"], "failed")
        self.assertEqual(self.notion.pages, [])
        self.notion.schema["Deadline rule"] = {"type": "rich_text"}
        self.assertEqual(self.service.create_task(FRIDAY + timedelta(minutes=15))["status"], "created")

    def test_duplicate_remote_matches_block_send(self):
        self.service.create_task(FRIDAY)
        self.notion.pages.append(copy.deepcopy(self.notion.pages[0]))
        self.assertEqual(self.service.remind(MONDAY)["status"], "failed")
        self.assertEqual(self.slack.dms, [])

    def test_known_notion_rejection_retries_after_backoff(self):
        self.notion.create_error = NotionApiError(429, {"code": "rate_limited"})
        self.assertEqual(self.service.create_task(FRIDAY)["status"], "failed")
        self.assertEqual(self.service.create_task(FRIDAY + timedelta(minutes=1))["status"], "suppressed")
        self.assertEqual(self.service.create_task(FRIDAY + timedelta(minutes=15))["status"], "created")
        self.assertEqual(len(self.notion.pages), 1)

    def test_query_failure_retries_without_burning_period(self):
        self.notion.query_error = TimeoutError("read timed out")
        self.assertEqual(self.service.create_task(FRIDAY)["status"], "failed")
        self.assertEqual(self.service.create_task(FRIDAY + timedelta(minutes=15))["status"], "created")

    def test_ambiguous_create_reconciles_without_a_second_create(self):
        original = self.notion.create_page

        def accepted_then_timeout(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError("response lost")

        self.notion.create_page = accepted_then_timeout
        self.assertEqual(self.service.create_task(FRIDAY)["status"], "uncertain")
        self.assertEqual(self.service.create_task(FRIDAY + timedelta(minutes=15))["status"], "existing")
        self.assertEqual(len(self.notion.pages), 1)
        self.assertEqual(sum(call[0] == "create" for call in self.notion.calls), 1)

    def test_ambiguous_create_without_visible_page_does_not_resend(self):
        self.notion.create_error = TimeoutError("unknown")
        self.assertEqual(self.service.create_task(FRIDAY)["status"], "uncertain")
        self.assertEqual(self.service.create_task(FRIDAY + timedelta(minutes=15))["status"], "uncertain")
        self.assertEqual(sum(call[0] == "create" for call in self.notion.calls), 1)

    def test_explicit_slack_rejection_retries_but_timeout_does_not(self):
        self.service.create_task(FRIDAY)
        self.slack.error = SlackDeliveryError("channel unavailable", uncertain=False)
        self.assertEqual(self.service.remind(MONDAY)["status"], "failed")
        self.slack.error = SlackDeliveryError("response lost", uncertain=True)
        self.assertEqual(self.service.remind(MONDAY + timedelta(minutes=15))["status"], "uncertain")
        self.assertEqual(self.service.remind(MONDAY + timedelta(minutes=30))["status"], "suppressed")
        self.assertEqual(self.slack.dms, [])

    def test_utc_calendar_day_and_dst_follow_system_timezone(self):
        self.assertTrue(self.service.should_create(datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)))
        self.assertFalse(self.service.should_create(FRIDAY.replace(hour=15)))
        self.assertFalse(self.service.should_create(FRIDAY + timedelta(days=1)))
        self.assertEqual(self.service.next_monday(datetime(2026, 12, 31, 16, tzinfo=LA)), date(2027, 1, 4))
        for utc, expected in [(datetime(2026, 11, 2, 16, tzinfo=timezone.utc), True),
                              (datetime(2026, 11, 2, 15, tzinfo=timezone.utc), False),
                              (datetime(2026, 3, 9, 15, tzinfo=timezone.utc), True)]:
            with self.subTest(utc=utc):
                self.assertEqual(self.service.should_remind(utc), expected)
        self.assertFalse(self.service.should_remind(MONDAY.replace(hour=12)))
        with self.assertRaises(ValueError):
            self.service.should_create(datetime(2026, 10, 16, 16))

    def test_scheduler_isolation_and_new_job_retry(self):
        scheduler = Scheduler(self.settings, self.db)
        legacy = []
        scheduler.register(ScheduledJob("legacy", lambda _: True, lambda now: legacy.append(now)))
        app = AgentApp(self.settings)
        app.recurring_report = self.service
        app.register_recurring_report_jobs(scheduler)
        self.notion.query_error = TimeoutError("read failed")
        scheduler.run_pending(FRIDAY)
        scheduler.run_pending(FRIDAY)
        scheduler.run_pending(FRIDAY + timedelta(minutes=15))
        self.assertEqual(len(legacy), 2)
        self.assertEqual(len(self.notion.pages), 1)
        self.assertEqual(self.slack.dms, [])
        self.assertFalse(self.db.has_dedupe("schedule:marketing_performance_report_create:2026-10-16-16-00"))
        disabled_app = AgentApp(replace(self.settings, marketing_performance_report_enabled=False))
        other = Scheduler(disabled_app.settings, self.db)
        disabled_app.register_recurring_report_jobs(other)
        self.assertEqual(other.jobs, [])

    def test_sqlite_claims_are_atomic_between_independent_workers(self):
        key = self.service.action_key("create", MONDAY.date())
        barrier = threading.Barrier(2)
        claims = []

        def claim():
            db = Persistence(self.path)
            barrier.wait()
            claims.append(db.claim_recurring_report_action(key, FRIDAY))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        token = next(claim[0] for claim in claims if claim)
        with self.assertRaises(RuntimeError):
            self.db.dispatch_recurring_report_action(key, token, FRIDAY + timedelta(minutes=6))
        new_token, uncertain = self.db.claim_recurring_report_action(key, FRIDAY + timedelta(minutes=15))
        self.assertFalse(uncertain)
        with self.assertRaises(RuntimeError):
            self.db.finish_recurring_report_action(key, token, "succeeded")
        self.db.dispatch_recurring_report_action(key, new_token, FRIDAY + timedelta(minutes=15))
        self.assertIsNone(self.db.claim_recurring_report_action(key, FRIDAY + timedelta(minutes=30)))
        _, uncertain = self.db.claim_recurring_report_action(key, FRIDAY + timedelta(minutes=30), reconcile=True)
        self.assertTrue(uncertain)


class ClientSafetyTests(unittest.TestCase):
    def test_http_no_retry_for_unsafe_write(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("unknown")) as opened, patch("time.sleep") as sleep:
            with self.assertRaises(RuntimeError):
                HttpClient().request_json("POST", "https://example.com", body={}, retries=0)
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()

    def test_http_zero_retry_returns_explicit_429(self):
        error = urllib.error.HTTPError("https://example.com", 429, "rate", {}, io.BytesIO(b'{"error":"rate"}'))
        with patch("urllib.request.urlopen", side_effect=error) as opened:
            response = HttpClient().request_json("POST", "https://example.com", retries=0)
            self.assertEqual(response.status, 429)
            self.assertEqual(opened.call_count, 1)

    def test_notion_creation_uses_zero_retries(self):
        http = unittest.mock.Mock()
        http.request_json.return_value = HttpResponse(200, {"id": "created"}, {})
        client = NotionClient(config("unused"), http)
        self.assertEqual(client.create_page({"data_source_id": "source"}, {}, retry_on_failure=False)["id"], "created")
        self.assertEqual(http.request_json.call_args.kwargs["retries"], 0)

    def test_slack_sends_to_opened_dm_with_stable_id_and_no_retry(self):
        http = unittest.mock.Mock()
        http.request_json.side_effect = [HttpResponse(200, {"ok": True, "channel": {"id": "DEMIL"}}, {}),
                                        HttpResponse(200, {"ok": True, "ts": "ts"}, {})]
        client = SlackClient(config("unused"), http)
        self.assertEqual(client.dm_user_once("UEMIL", "report", "stable-id"), "ts")
        self.assertEqual(http.request_json.call_args.kwargs["retries"], 0)
        self.assertEqual(http.request_json.call_args.kwargs["body"], {"channel": "DEMIL", "text": "report", "client_msg_id": "stable-id"})

    def test_slack_unknown_response_and_known_rejection_are_distinguished(self):
        for response, uncertain in [(TimeoutError(), True), (HttpResponse(503, {}, {}), True),
                                    (HttpResponse(200, {"ok": False, "error": "internal_error"}, {}), True),
                                    (HttpResponse(200, {"ok": False, "error": "restricted_action"}, {}), False),
                                    (HttpResponse(429, {"ok": False}, {}), False)]:
            with self.subTest(response=response):
                http = unittest.mock.Mock()
                http.request_json.side_effect = [HttpResponse(200, {"ok": True, "channel": {"id": "D"}}, {}), response]
                with self.assertRaises(SlackDeliveryError) as error:
                    SlackClient(config("unused"), http).dm_user_once("U", "report", "id")
                self.assertEqual(error.exception.uncertain, uncertain)

    def test_cli_preview_needs_no_credentials_or_storage(self):
        with patch.dict(os.environ, {"TIMEZONE": "America/Los_Angeles"}, clear=True), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["marketing-performance-report", "preview", "--at", "2026-10-16T23:00:00Z"]), 0)
            self.assertIn('"due_date": "2026-10-19"', output.getvalue())
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
            main(["marketing-performance-report", "create", "--at", "2026-10-16T23:00:00Z"])

    def test_configuration_defaults_are_disabled_and_dry_run(self):
        with patch.dict(os.environ, {"TIMEZONE": "America/Los_Angeles"}, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.marketing_performance_report_enabled)
        self.assertTrue(settings.marketing_performance_report_dry_run)
        with patch.dict(os.environ, {"MARKETING_PERFORMANCE_REPORT_ENABLED": "true", "MARKETING_PERFORMANCE_REPORT_DRY_RUN": "false",
                                   "MARKETING_PERFORMANCE_REPORT_OWNER_NOTION_USER_ID": OWNER_ID}, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.marketing_performance_report_enabled)
        self.assertFalse(settings.marketing_performance_report_dry_run)
        self.assertEqual(settings.marketing_performance_report_owner_notion_user_id, OWNER_ID)


if __name__ == "__main__":
    unittest.main()
