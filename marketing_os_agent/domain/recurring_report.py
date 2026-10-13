from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..clients.notion import NotionApiError, NotionClient
from ..clients.slack import SlackClient, SlackDeliveryError
from ..config import Settings
from ..persistence import Persistence
from ..scheduler import friday_4pm, monday_8am

logger = logging.getLogger(__name__)
REPORT_NAME = "Marketing Performance Report"


class RecurringReportService:
    """One weekly report, using the existing Notion/Slack clients and shared state."""

    def __init__(self, settings: Settings, db: Persistence, notion: NotionClient, slack: SlackClient) -> None:
        self.settings, self.db, self.notion, self.slack = settings, db, notion, slack

    @property
    def tz(self) -> ZoneInfo:
        # Disabled workflow must not change startup/diagnostics for existing commands.
        return ZoneInfo(self.settings.timezone)

    def local_now(self, now: datetime | None = None) -> datetime:
        if now is None:
            return datetime.now(self.tz)
        if now.tzinfo is None:
            raise ValueError("Report scheduling requires a timezone-aware datetime")
        return now.astimezone(self.tz)

    def next_monday(self, now: datetime) -> date:
        local = self.local_now(now).date()
        return local + timedelta(days=(7 - local.weekday()) % 7 or 7)

    def marker(self, due: date) -> str:
        return f"marketing-performance-report:{self.settings.marketing_performance_report_owner_notion_user_id}:{due.isoformat()}"

    def action_key(self, action: str, due: date) -> str:
        return f"{self.notion.tasks_data_source_id()}:{self.marker(due)}:{action}"

    def should_create(self, now: datetime) -> bool:
        now = self.local_now(now)
        return self.settings.marketing_performance_report_enabled and friday_4pm(now.replace(hour=16, minute=0)) and now.hour >= 16

    def should_remind(self, now: datetime) -> bool:
        now = self.local_now(now)
        return self.settings.marketing_performance_report_enabled and monday_8am(now.replace(hour=8, minute=0)) and 8 <= now.hour < 12

    def preview(self, now: datetime | None = None) -> dict[str, Any]:
        now = self.local_now(now)
        due = self.next_monday(now)
        return {
            "status": "preview", "enabled": self.settings.marketing_performance_report_enabled,
            "dry_run": self.settings.marketing_performance_report_dry_run, "timezone": self.settings.timezone,
            "create_schedule": "Friday 16:00 (catch up until local midnight)",
            "reminder_schedule": "Monday 08:00 (catch up before local noon)",
            "title": f"{REPORT_NAME} — {due.isoformat()}", "due_date": due.isoformat(),
            "owner_notion_user_id": self.settings.marketing_performance_report_owner_notion_user_id,
            "key_property": self.settings.marketing_performance_report_key_property,
            "marker": self.marker(due), "delivery": "Slack DM, resolved by Notion owner's email",
        }

    def _prepare(self, due: date) -> dict[str, Any]:
        owner_id = self.settings.marketing_performance_report_owner_notion_user_id
        if not owner_id:
            raise ValueError("MARKETING_PERFORMANCE_REPORT_OWNER_NOTION_USER_ID is missing")
        owner = self.notion.retrieve_user(owner_id)
        if owner.get("id") != owner_id or owner.get("type") != "person":
            raise ValueError("Report owner must be the configured Notion person")
        source_id = self.notion.tasks_data_source_id()
        schema = self.notion.retrieve_data_source(source_id).get("properties", {})

        def prop(configured: str, fallback: str, types: set[str]) -> str:
            for name in dict.fromkeys([configured, fallback]):
                if name and schema.get(name, {}).get("type") in types:
                    return name
            raise ValueError(f"Report requires {configured or fallback!r} property of type {sorted(types)}")

        title = prop(self.settings.notion_task_name_property, "Name", {"title"})
        person = prop(self.settings.notion_task_owner_property, "Person", {"people"})
        deadline = prop(self.settings.notion_task_deadline_property, "Due date", {"date"})
        status = prop(self.settings.notion_task_status_property, "Status", {"select", "status"})
        key = prop(self.settings.marketing_performance_report_key_property, "", {"rich_text"})
        status_type = schema[status]["type"]
        options = schema[status].get(status_type, {}).get("options", [])
        initial_status = next((item["name"] for item in options if item["name"].casefold() == "not started"), None)
        if not initial_status:
            raise ValueError("Report status property has no Not started option")
        name = f"{REPORT_NAME} — {due.isoformat()}"
        properties = {
            title: {"title": [{"text": {"content": name}}]}, person: {"people": [{"id": owner_id}]},
            deadline: {"date": {"start": due.isoformat()}}, status: {status_type: {"name": initial_status}},
            key: {"rich_text": [{"text": {"content": self.marker(due)}}]},
        }
        for field, value in [("Frequency", "Weekly"), ("Department", "Marketing")]:
            spec = schema.get(field, {})
            if spec.get("type") == "select" and any(item["name"] == value for item in spec.get("select", {}).get("options", [])):
                properties[field] = {"select": {"name": value}}
        return {"source_id": source_id, "owner": owner, "title": title, "person": person, "deadline": deadline,
                "status": status, "key": key, "properties": properties, "name": name}

    def _find(self, prepared: dict[str, Any], due: date) -> dict[str, Any] | None:
        # The durable remote key survives renames/due-date edits and local DB rebuilds.
        # Also adopt an existing manual report for this owner and due date.
        pages = self.notion.query_data_source(prepared["source_id"], {"filter": {"or": [
            {"property": prepared["key"], "rich_text": {"equals": self.marker(due)}},
            {"and": [
                {"property": prepared["title"], "title": {"equals": prepared["name"]}},
                {"property": prepared["person"], "people": {"contains": prepared["owner"]["id"]}},
            ]},
            {"and": [
                {"property": prepared["title"], "title": {"equals": REPORT_NAME}},
                {"property": prepared["deadline"], "date": {"equals": due.isoformat()}},
                {"property": prepared["person"], "people": {"contains": prepared["owner"]["id"]}},
            ]},
        ]}})
        pages = [page for page in pages if not page.get("archived") and not page.get("in_trash")]
        if len(pages) > 1:
            raise ValueError("Multiple weekly reports match this period; reconcile them before sending")
        return pages[0] if pages else None

    def _recipient(self, prepared: dict[str, Any]) -> str:
        if not self.slack.available:
            raise ValueError("SLACK_BOT_TOKEN is missing")
        owner = prepared["owner"]
        email = owner.get("person", {}).get("email", "")
        recipient = self.slack.lookup_user_by_email(email) if email else None
        # Only stable identity/email mappings; never route by a similar name or to Tim.
        recipient = recipient or self.settings.owner_slack_map.get(owner["id"]) or self.settings.owner_slack_map.get(email)
        if not recipient:
            raise ValueError("Cannot resolve report owner's Slack identity by Notion email or explicit ID mapping")
        return recipient

    def validate(self, now: datetime | None = None) -> dict[str, Any]:
        """Read-only validation: schema, owner, existing task and Slack email lookup."""
        now = self.local_now(now)
        due = self.next_monday(now)
        try:
            prepared = self._prepare(due)
            page = self._find(prepared, due)
            recipient = self._recipient(prepared)
            return {"status": "validated", "timezone": self.settings.timezone, "due_date": due.isoformat(),
                    "source_id": prepared["source_id"], "owner": prepared["owner"]["name"],
                    "slack_user_id": recipient, "existing_task_id": page["id"] if page else None,
                    "properties": prepared["properties"]}
        except Exception as exc:
            logger.exception("marketing_performance_report_validation_failed")
            return {"status": "config_error", "error": str(exc)}

    def create_task(self, now: datetime | None = None) -> dict[str, Any]:
        now = self.local_now(now)
        if not self.should_create(now):
            return {"status": "outside_schedule"}
        return self._run("create", self.next_monday(now), now)

    def remind(self, now: datetime | None = None) -> dict[str, Any]:
        now = self.local_now(now)
        if not self.should_remind(now):
            return {"status": "outside_schedule"}
        return self._run("remind", now.date(), now)

    def _run(self, action: str, due: date, now: datetime) -> dict[str, Any]:
        started = time.monotonic()
        dry_run = self.settings.marketing_performance_report_dry_run
        key, token, uncertain, dispatched = "", "", False, False
        try:
            key = self.action_key(action, due)
            if not dry_run:
                claim = self.db.claim_recurring_report_action(key, now, reconcile=action == "create")
                if claim is None:
                    return {"status": "suppressed"}
                token, uncertain = claim
            prepared = self._prepare(due)
            page = self._find(prepared, due)
            if action == "create":
                if page:
                    result = {"status": "existing", "task_id": page["id"], "url": page.get("url", "")}
                elif uncertain:
                    result = {"status": "uncertain", "error": "Earlier create may have been accepted; no automatic second create"}
                elif dry_run:
                    result = {"status": "dry_run", "properties": prepared["properties"]}
                else:
                    self.db.dispatch_recurring_report_action(key, token, now + timedelta(seconds=time.monotonic() - started))
                    dispatched = True
                    children = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
                        {"type": "text", "text": {"content": f"Complete and send the weekly {REPORT_NAME} by Monday, {due.isoformat()}."}},
                    ]}}]
                    page = self.notion.create_page({"data_source_id": prepared["source_id"]}, prepared["properties"], children, retry_on_failure=False)
                    if not page.get("id"):
                        raise RuntimeError("Notion create response missing page ID")
                    result = {"status": "created", "task_id": page["id"], "url": page.get("url", "")}
            else:
                if not page:
                    # No new task is created on Monday; Friday's failure stays visible.
                    raise ValueError("No report task found for this Monday; check Friday creation")
                props = page["properties"]
                status_spec = props[prepared["status"]]
                status = (status_spec.get("select") or status_spec.get("status") or {}).get("name", "").casefold()
                owners = [person["id"] for person in props[prepared["person"]].get("people", [])]
                deadline = (props[prepared["deadline"]].get("date") or {}).get("start", "")
                if status in {"done", "completed", "complete", "cancelled", "canceled", "archived"}:
                    result = {"status": "closed", "task_id": page["id"]}
                elif deadline[:10] != due.isoformat() or prepared["owner"]["id"] not in owners:
                    result = {"status": "rescheduled_or_reassigned", "task_id": page["id"]}
                else:
                    recipient = self._recipient(prepared)
                    url = page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}"
                    text = f"Reminder: please complete and send the {REPORT_NAME} today (Monday, {due.isoformat()}).\nTask: {url}"
                    if dry_run:
                        result = {"status": "dry_run", "slack_user_id": recipient, "text": text}
                    else:
                        self.db.dispatch_recurring_report_action(key, token, now + timedelta(seconds=time.monotonic() - started))
                        dispatched = True
                        ts = self.slack.dm_user_once(recipient, text, str(uuid.uuid5(uuid.NAMESPACE_URL, key)))
                        result = {"status": "sent", "task_id": page["id"], "slack_ts": ts}
            if token:
                self.db.finish_recurring_report_action(key, token, "uncertain" if result["status"] == "uncertain" else "succeeded", result)
            logger.info("marketing_performance_report_completed", extra={"action": action, "due_date": due.isoformat(), **result})
            return result
        except Exception as exc:
            rejected = (isinstance(exc, NotionApiError) and exc.status < 500 and exc.status != 408) or (isinstance(exc, SlackDeliveryError) and not exc.uncertain)
            unknown = uncertain or (dispatched and not rejected)
            result = {"status": "uncertain" if unknown else "failed", "error": str(exc)}
            if token:
                self.db.finish_recurring_report_action(key, token, "uncertain" if unknown else "ready", result)
            logger.exception("marketing_performance_report_failed", extra={"action": action, "due_date": due.isoformat(), "outcome": result["status"]})
            return result
