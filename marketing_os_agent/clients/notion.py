from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from ..config import Settings
from ..models import Campaign, Owner, Task, ValidationReport, parse_notion_date, parse_notion_datetime
from .http import HttpClient


logger = logging.getLogger(__name__)


class NotionApiError(RuntimeError):
    def __init__(self, status: int, data: dict[str, Any]) -> None:
        self.status = status
        self.data = data
        self.code = str(data.get("code", ""))
        self.message = str(data.get("message", data))
        super().__init__(f"Notion API error {status} ({self.code}): {self.message}")


class NotionClient:
    base_url = "https://api.notion.com/v1"

    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.http = http or HttpClient()
        self._resolved_data_source_ids: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return bool(self.settings.notion_api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.notion_api_key}",
            "Notion-Version": self.settings.notion_api_version,
        }

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("Notion API key is not configured")
        response = self.http.request_json(method, f"{self.base_url}{path}", headers=self._headers(), body=body)
        if response.status >= 400:
            raise NotionApiError(response.status, response.data)
        return response.data

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{database_id}")

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self._request("GET", f"/data_sources/{data_source_id}")

    def query_data_source(self, data_source_id: str, body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            request_body = dict(body or {})
            if cursor:
                request_body["start_cursor"] = cursor
            data = self._request("POST", f"/data_sources/{data_source_id}/query", request_body)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")

    def validate_databases(self) -> ValidationReport:
        report = ValidationReport(ok=True)
        if not self.settings.notion_api_key:
            return ValidationReport(ok=False, errors=["NOTION_API_KEY is missing"])
        if not self.settings.notion_tasks_database_id:
            report.ok = False
            report.errors.append("NOTION_TASKS_DATABASE_ID is missing")
        if not self.settings.notion_marketing_calendar_database_id:
            report.ok = False
            report.errors.append("NOTION_MARKETING_CALENDAR_DATABASE_ID is missing")
        if not report.ok:
            return report
        try:
            tasks_source = self.retrieve_data_source(self.tasks_data_source_id())
            campaigns_source = self.retrieve_data_source(self.marketing_calendar_data_source_id())
        except NotionApiError as exc:
            logger.warning("notion_database_validation_failed", extra={"status": exc.status, "code": exc.code, "notion_message": exc.message})
            return ValidationReport(ok=False, errors=[_friendly_notion_error(exc)])
        except Exception as exc:
            logger.exception("notion_database_validation_failed")
            return ValidationReport(ok=False, errors=[f"Could not retrieve Notion database: {exc}"])
        report.merge(_validate_mapped_properties("Tasks", tasks_source.get("properties", {}), self._task_property_specs()))
        report.merge(
            _validate_mapped_properties(
                "Marketing Calendar",
                campaigns_source.get("properties", {}),
                self._campaign_property_specs(),
            )
        )
        if self.settings.notion_needs_verification_property and self.settings.notion_needs_verification_property not in tasks_source.get("properties", {}):
            report.warnings.append(
                f"Tasks DB is missing optional service-managed checkbox property "
                f"{self.settings.notion_needs_verification_property!r}; flags will still be logged/commented."
            )
        if not self.settings.notion_workbooks_page_id and not self.settings.notion_workbooks_database_id:
            report.warnings.append("No Workbooks page/database ID configured; workbook validation can only be documented.")
        if self.settings.notion_workbooks_database_id:
            report.merge(self.validate_workbooks_database())
        return report

    def validate_workbooks_database(self) -> ValidationReport:
        if not self.settings.notion_workbooks_database_id:
            return ValidationReport(ok=False, errors=["NOTION_WORKBOOKS_DATABASE_ID is missing"])
        try:
            data_source = self.retrieve_data_source(self.workbooks_data_source_id())
            report = _validate_properties("Workbooks", data_source.get("properties", {}), WORKBOOKS_SCHEMA)
            existing_names = set(self.query_workbook_names())
            missing = [name for name in REQUIRED_WORKBOOKS if name not in existing_names]
            if missing:
                report.ok = False
                report.errors.append("Workbooks DB missing required workbook pages: " + ", ".join(missing))
            return report
        except Exception as exc:
            logger.exception("notion_workbooks_validation_failed")
            return ValidationReport(ok=False, errors=[f"Could not validate Workbooks DB: {exc}"])

    def query_workbook_names(self) -> list[str]:
        if not self.settings.notion_workbooks_database_id and not self.settings.notion_workbooks_data_source_id:
            return []
        pages = self.query_data_source(self.workbooks_data_source_id(), {"page_size": 100})
        return [_title(page.get("properties", {}).get("Workbook name")) for page in pages]

    def seed_missing_workbooks(self) -> list[str]:
        if not self.settings.notion_workbooks_database_id:
            raise RuntimeError("NOTION_WORKBOOKS_DATABASE_ID is required to seed workbooks")
        existing_names = set(self.query_workbook_names())
        created: list[str] = []
        for name in REQUIRED_WORKBOOKS:
            if name in existing_names:
                continue
            self.create_page(
                {"data_source_id": self.workbooks_data_source_id()},
                {
                    "Workbook name": {"title": [{"text": {"content": name}}]},
                    "Current Version": {"rich_text": [{"text": {"content": "v1"}}]},
                },
            )
            created.append(name)
        return created

    def create_page(self, parent: dict[str, Any], properties: dict[str, Any], children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        return self._request("POST", "/pages", body)

    def query_tasks_modified_since(self, since_iso: str | None) -> list[Task]:
        body: dict[str, Any] = {"sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]}
        if since_iso:
            body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since_iso}}
        pages = self.query_data_source(self.tasks_data_source_id(), body)
        return [self.parse_task(page) for page in pages]

    def query_all_tasks(self) -> list[Task]:
        pages = self.query_data_source(
            self.tasks_data_source_id(),
            {"sorts": [{"property": self.settings.notion_task_deadline_property or "Deadline", "direction": "ascending"}]},
        )
        return [self.parse_task(page) for page in pages]

    def query_campaigns_starting_between(self, start: date, end: date) -> list[Campaign]:
        body = {
            "filter": {
                "and": [
                    {"property": self.settings.notion_campaign_start_date_property or "Start Date", "date": {"on_or_after": start.isoformat()}},
                    {"property": self.settings.notion_campaign_start_date_property or "Start Date", "date": {"on_or_before": end.isoformat()}},
                ]
            },
            "sorts": [{"property": self.settings.notion_campaign_start_date_property or "Start Date", "direction": "ascending"}],
        }
        pages = self.query_data_source(self.marketing_calendar_data_source_id(), body)
        return [self.parse_campaign(page) for page in pages]

    def query_all_campaigns(self) -> list[Campaign]:
        pages = self.query_data_source(
            self.marketing_calendar_data_source_id(),
            {"sorts": [{"property": self.settings.notion_campaign_start_date_property or "Start Date", "direction": "ascending"}]},
        )
        return [self.parse_campaign(page) for page in pages]

    def create_campaign(self, campaign: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.notion_marketing_calendar_database_id:
            raise RuntimeError("NOTION_MARKETING_CALENDAR_DATABASE_ID is required")
        props: dict[str, Any] = {
            self.settings.notion_campaign_name_property: {"title": [{"text": {"content": campaign["Campaign name"]}}]},
            self.settings.notion_campaign_trade_property: {"multi_select": [{"name": item.strip()} for item in campaign.get("Trade", "").split(";") if item.strip()]},
            self.settings.notion_campaign_channel_property: {"multi_select": [{"name": item.strip()} for item in campaign.get("Channel", "").split(";") if item.strip()]},
            self.settings.notion_campaign_status_property: {"select": {"name": campaign.get("Status") or "Planned"}},
        }
        if self.settings.notion_campaign_notes_property:
            props[self.settings.notion_campaign_notes_property] = {"rich_text": [{"text": {"content": campaign.get("Notes", "")}}]}
        if campaign.get("Owner Notion User ID") and self.settings.notion_campaign_owner_property:
            props[self.settings.notion_campaign_owner_property] = {"people": [{"id": campaign["Owner Notion User ID"]}]}
        if campaign.get("Start Date"):
            props[self.settings.notion_campaign_start_date_property] = {"date": {"start": campaign["Start Date"]}}
        if campaign.get("End Date"):
            props[self.settings.notion_campaign_end_date_property] = {"date": {"start": campaign["End Date"]}}
        for source, prop_name in {
            "Planned Spend": self.settings.notion_campaign_planned_spend_property,
            "Expected Leads": self.settings.notion_campaign_expected_leads_property,
            "Expected ROI": self.settings.notion_campaign_expected_roi_property,
            "Actual Spend": self.settings.notion_campaign_actual_spend_property,
            "Actual Leads": self.settings.notion_campaign_actual_leads_property,
            "Actual ROI": self.settings.notion_campaign_actual_roi_property,
        }.items():
            raw = campaign.get(source, "")
            if raw != "" and prop_name:
                props[prop_name] = {"number": float(raw)}
        return self.create_page({"data_source_id": self.marketing_calendar_data_source_id()}, props)

    def tasks_data_source_id(self) -> str:
        return self.resolve_data_source_id(
            "tasks",
            self.settings.notion_tasks_database_id,
            self.settings.notion_tasks_data_source_id,
        )

    def marketing_calendar_data_source_id(self) -> str:
        return self.resolve_data_source_id(
            "marketing_calendar",
            self.settings.notion_marketing_calendar_database_id,
            self.settings.notion_marketing_calendar_data_source_id,
        )

    def workbooks_data_source_id(self) -> str:
        return self.resolve_data_source_id(
            "workbooks",
            self.settings.notion_workbooks_database_id,
            self.settings.notion_workbooks_data_source_id,
        )

    def resolve_data_source_id(
        self,
        label: str,
        database_id: str,
        configured_data_source_id: str = "",
        database: dict[str, Any] | None = None,
    ) -> str:
        if configured_data_source_id:
            return configured_data_source_id
        if not database_id:
            raise RuntimeError(f"NOTION_{label.upper()}_DATABASE_ID is missing")
        cache_key = f"{label}:{database_id}"
        if cache_key in self._resolved_data_source_ids:
            return self._resolved_data_source_ids[cache_key]
        database = database or self.retrieve_database(database_id)
        data_sources = database.get("data_sources") or []
        if not data_sources:
            raise RuntimeError(
                f"Notion database {database_id} has no accessible data sources. "
                "Share the original database with the integration, or set the matching NOTION_*_DATA_SOURCE_ID from "
                "Notion database settings -> Manage data sources -> Copy data source ID."
            )
        if len(data_sources) > 1:
            names = ", ".join(f"{item.get('name', 'unnamed')}={item.get('id')}" for item in data_sources)
            raise RuntimeError(
                f"Notion database {database_id} has multiple data sources. Set the matching NOTION_*_DATA_SOURCE_ID. "
                f"Available data sources: {names}"
            )
        data_source_id = data_sources[0]["id"]
        self._resolved_data_source_ids[cache_key] = data_source_id
        return data_source_id

    def set_original_deadline_if_missing(self, task: Task) -> None:
        if task.original_deadline or not task.deadline or not self.settings.notion_task_original_deadline_property:
            return
        try:
            self.update_page_properties(
                task.id,
                {self.settings.notion_task_original_deadline_property: {"date": {"start": task.deadline.isoformat()}}},
            )
        except Exception:
            logger.warning("notion_original_deadline_update_failed", exc_info=True, extra={"task_id": task.id})

    def set_needs_verification(self, task_id: str, value: bool) -> None:
        property_name = self.settings.notion_needs_verification_property
        if not property_name:
            return
        try:
            self.update_page_properties(task_id, {property_name: {"checkbox": value}})
        except NotionApiError as exc:
            logger.warning(
                "notion_needs_verification_update_failed",
                extra={"task_id": task_id, "property": property_name, "status": exc.status, "code": exc.code, "notion_message": exc.message},
            )
        except Exception:
            logger.warning(
                "notion_needs_verification_update_failed",
                exc_info=True,
                extra={"task_id": task_id, "property": property_name},
            )

    def update_page_properties(self, page_id: str, properties: dict[str, Any]) -> None:
        self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def add_task_comment(self, task: Task, text: str, mention_owner: bool = False) -> None:
        rich_text: list[dict[str, Any]] = []
        if mention_owner and task.owner_notion_user_id:
            rich_text.append({"type": "mention", "mention": {"type": "user", "user": {"id": task.owner_notion_user_id}}})
            rich_text.append({"type": "text", "text": {"content": " "}})
        rich_text.append({"type": "text", "text": {"content": text}})
        self._request("POST", "/comments", {"parent": {"page_id": task.id}, "rich_text": rich_text})

    def parse_task(self, page: dict[str, Any]) -> Task:
        props = page.get("properties", {})
        owner = _first_person(props.get("Owner"))
        owner = _first_person(_prop(props, self.settings.notion_task_owner_property, ["Person"])) or owner
        status = _normalize_mapped_value(
            _select_name(_prop(props, self.settings.notion_task_status_property, [])),
            self.settings.task_status_map,
        )
        priority = _normalize_mapped_value(
            _select_name(_prop(props, self.settings.notion_task_priority_property, [])),
            self.settings.task_priority_map,
        )
        return Task(
            id=page["id"],
            name=_title(_prop(props, self.settings.notion_task_name_property, ["Name"])),
            owner=owner,
            deadline=parse_notion_date(_date_start(_prop(props, self.settings.notion_task_deadline_property, ["Due date", "Due Date"]))),
            original_deadline=parse_notion_date(_date_start(_prop(props, self.settings.notion_task_original_deadline_property, []))),
            status=status,
            priority=priority,
            department=_select_name(_prop(props, self.settings.notion_task_department_property, ["Type"])) or "Marketing",
            linked_campaign_ids=_relation_ids(_prop(props, self.settings.notion_task_campaign_property, [])),
            deliverable_link=_url(_prop(props, self.settings.notion_task_deliverable_property, ["Deliverable", "Link"])),
            notes_issues=_rich_text(_prop(props, self.settings.notion_task_notes_property, ["Notes", "Issues", "Comments"])),
            needs_from_others=_rich_text(_prop(props, self.settings.notion_task_needs_from_others_property, ["Needs from others", "Blocked by"])),
            created_time=parse_notion_datetime(_created_time(_prop(props, self.settings.notion_task_created_property, [])) or page.get("created_time")),
            last_edited_time=parse_notion_datetime(_last_edited_time(_prop(props, self.settings.notion_task_last_edited_property, [])) or page.get("last_edited_time")),
            url=page.get("url", ""),
            child_task_ids=_relation_ids(props.get(self.settings.notion_child_tasks_property)),
            dependency_task_ids=_relation_ids(props.get(self.settings.notion_dependencies_property)),
        )

    def parse_campaign(self, page: dict[str, Any]) -> Campaign:
        props = page.get("properties", {})
        owner = _first_person(_prop(props, self.settings.notion_campaign_owner_property, []))
        return Campaign(
            id=page["id"],
            name=_title(_prop(props, self.settings.notion_campaign_name_property, [])),
            trade=_select_or_multi_select_names(_prop(props, self.settings.notion_campaign_trade_property, [])),
            channel=_select_or_multi_select_names(_prop(props, self.settings.notion_campaign_channel_property, [])),
            start_date=parse_notion_date(_date_start(_prop(props, self.settings.notion_campaign_start_date_property, []))),
            end_date=parse_notion_date(_date_start(_prop(props, self.settings.notion_campaign_end_date_property, []))),
            owner=owner,
            status=_select_name(_prop(props, self.settings.notion_campaign_status_property, [])),
            planned_spend=_number(_prop(props, self.settings.notion_campaign_planned_spend_property, ["Planned Budget"])),
            expected_leads=_number(_prop(props, self.settings.notion_campaign_expected_leads_property, [])),
            expected_roi=_number(_prop(props, self.settings.notion_campaign_expected_roi_property, [])),
            actual_spend=_number(_prop(props, self.settings.notion_campaign_actual_spend_property, [])),
            actual_leads=_number(_prop(props, self.settings.notion_campaign_actual_leads_property, [])),
            actual_roi=_number(_prop(props, self.settings.notion_campaign_actual_roi_property, [])),
            linked_task_ids=_relation_ids(_prop(props, self.settings.notion_campaign_linked_tasks_property, [])),
            linked_workbook_ids=_relation_ids(_prop(props, self.settings.notion_campaign_linked_workbook_property, [])),
            notes=_rich_text(_prop(props, self.settings.notion_campaign_notes_property, [])),
            url=page.get("url", ""),
        )

    def _task_property_specs(self) -> list[PropertySpec]:
        return [
            PropertySpec("task name", self.settings.notion_task_name_property, ("title",), True, ("Name",)),
            PropertySpec("owner", self.settings.notion_task_owner_property, ("people",), True, ("Person",)),
            PropertySpec("deadline", self.settings.notion_task_deadline_property, ("date",), True, ("Due date", "Due Date")),
            PropertySpec("status", self.settings.notion_task_status_property, ("select", "status"), True, ()),
            PropertySpec("priority", self.settings.notion_task_priority_property, ("select", "status"), False, ()),
            PropertySpec("department/category", self.settings.notion_task_department_property, ("select", "multi_select", "rich_text"), False, ("Type",)),
            PropertySpec("original deadline", self.settings.notion_task_original_deadline_property, ("date",), False, ()),
            PropertySpec("linked campaign", self.settings.notion_task_campaign_property, ("relation",), False, ()),
            PropertySpec("deliverable link", self.settings.notion_task_deliverable_property, ("url", "files", "rich_text"), False, ("Deliverable", "Link")),
            PropertySpec("notes/issues", self.settings.notion_task_notes_property, ("rich_text",), False, ("Notes", "Issues", "Comments")),
            PropertySpec("needs from others", self.settings.notion_task_needs_from_others_property, ("rich_text",), False, ("Needs from others", "Blocked by")),
        ]

    def _campaign_property_specs(self) -> list[PropertySpec]:
        return [
            PropertySpec("campaign name", self.settings.notion_campaign_name_property, ("title",), True, ()),
            PropertySpec("trade", self.settings.notion_campaign_trade_property, ("multi_select", "select"), True, ()),
            PropertySpec("channel", self.settings.notion_campaign_channel_property, ("multi_select", "select"), True, ()),
            PropertySpec("start date", self.settings.notion_campaign_start_date_property, ("date",), True, ()),
            PropertySpec("end date", self.settings.notion_campaign_end_date_property, ("date",), True, ()),
            PropertySpec("owner", self.settings.notion_campaign_owner_property, ("people",), True, ()),
            PropertySpec("status", self.settings.notion_campaign_status_property, ("select", "status"), True, ()),
            PropertySpec("planned spend", self.settings.notion_campaign_planned_spend_property, ("number", "formula", "rollup"), True, ("Planned Budget",)),
            PropertySpec("expected leads", self.settings.notion_campaign_expected_leads_property, ("number", "formula", "rollup"), True, ()),
            PropertySpec("expected CPL", self.settings.notion_campaign_expected_cpl_property, ("number", "formula", "rollup"), False, ()),
            PropertySpec("expected ROI", self.settings.notion_campaign_expected_roi_property, ("number", "formula", "rollup"), False, ()),
            PropertySpec("actual spend", self.settings.notion_campaign_actual_spend_property, ("number", "formula", "rollup"), False, ()),
            PropertySpec("actual leads", self.settings.notion_campaign_actual_leads_property, ("number", "formula", "rollup"), False, ()),
            PropertySpec("actual CPL", self.settings.notion_campaign_actual_cpl_property, ("number", "formula", "rollup"), False, ()),
            PropertySpec("actual ROI", self.settings.notion_campaign_actual_roi_property, ("number", "formula", "rollup"), False, ()),
            PropertySpec("linked tasks", self.settings.notion_campaign_linked_tasks_property, ("relation",), False, ()),
            PropertySpec("linked workbook", self.settings.notion_campaign_linked_workbook_property, ("relation",), False, ()),
            PropertySpec("notes", self.settings.notion_campaign_notes_property, ("rich_text",), False, ()),
        ]


WORKBOOKS_SCHEMA = {
    "Workbook name": "title",
    "Owner": "people",
    "Last Updated": "date",
    "Last Reviewed By": "people",
    "Current Version": "rich_text",
    "Quarterly review reminder": "date",
}


class PropertySpec:
    def __init__(self, label: str, name: str, expected_types: tuple[str, ...], required: bool, aliases: tuple[str, ...]) -> None:
        self.label = label
        self.name = name
        self.expected_types = expected_types
        self.required = required
        self.aliases = aliases

REQUIRED_WORKBOOKS = [
    "Google Ads",
    "Google LSAs",
    "SEO technical + content",
    "YouTube",
    "Email / Hatch",
    "SMS / Hatch + 10DLC compliance",
    "Billboard",
    "Direct Mail / Dream Home Guide",
    "Referral program",
    "Co-op marketing / Carrier / Sigler",
    "Rebate program tracking: SVCE, SJCE, PCE, TECH Clean CA, BAAQMD",
    "Reviews & reputation",
    "Web / Webflow CMS",
    "AI/Chat: ChatGPT, Perplexity, Gemini, Google AI Overviews",
    "Reporting workbook: 1–14, 15–31, monthly, quarterly, annual templates",
]


def _validate_properties(name: str, properties: dict[str, Any], required: dict[str, str]) -> ValidationReport:
    report = ValidationReport(ok=True)
    for prop_name, prop_type in required.items():
        prop = properties.get(prop_name)
        if not prop:
            report.ok = False
            report.errors.append(f"{name} DB missing property {prop_name!r} ({prop_type})")
            continue
        actual = prop.get("type")
        if actual != prop_type:
            report.ok = False
            report.errors.append(f"{name} DB property {prop_name!r} has type {actual!r}; expected {prop_type!r}")
    return report


def _validate_mapped_properties(name: str, properties: dict[str, Any], specs: list[PropertySpec]) -> ValidationReport:
    report = ValidationReport(ok=True)
    for spec in specs:
        if not spec.name:
            if spec.required:
                report.ok = False
                report.errors.append(f"{name} DB mapping for {spec.label} is blank")
            continue
        prop = _prop(properties, spec.name, spec.aliases)
        configured_or_alias = _matched_property_name(properties, spec.name, spec.aliases)
        if not prop:
            message = f"{name} DB missing {'required' if spec.required else 'optional'} property for {spec.label}: expected {spec.name!r}"
            if spec.aliases:
                message += f" or one of {list(spec.aliases)!r}"
            if spec.required:
                report.ok = False
                report.errors.append(message)
            else:
                report.warnings.append(message)
            continue
        actual = prop.get("type")
        if actual not in spec.expected_types:
            message = (
                f"{name} DB property {configured_or_alias!r} for {spec.label} has type {actual!r}; "
                f"expected one of {list(spec.expected_types)!r}"
            )
            if spec.required:
                report.ok = False
                report.errors.append(message)
            else:
                report.warnings.append(message)
    return report


def _friendly_notion_error(exc: NotionApiError) -> str:
    if exc.status == 404 and exc.code == "object_not_found":
        return (
            f"Notion could not find or access the configured database. {exc.message} "
            "Check that the ID is a database ID, not a page/view ID, and share the database with the Notion integration."
        )
    if exc.status == 400 and exc.code == "validation_error" and "data sources" in exc.message:
        return (
            f"Notion can see the database container but no accessible data source is available. {exc.message} "
            "Open the original Notion database, share it with the integration, then set the matching "
            "NOTION_*_DATA_SOURCE_ID from database settings -> Manage data sources -> Copy data source ID."
        )
    if exc.status == 401:
        return "Notion rejected NOTION_API_KEY. Create/copy a valid internal integration secret and update .env."
    return str(exc)


def _prop(props: dict[str, Any], configured_name: str, aliases: tuple[str, ...] | list[str]) -> dict[str, Any] | None:
    name = _matched_property_name(props, configured_name, aliases)
    return props.get(name) if name else None


def _matched_property_name(props: dict[str, Any], configured_name: str, aliases: tuple[str, ...] | list[str]) -> str | None:
    candidates = [configured_name, *aliases]
    for candidate in candidates:
        if candidate and candidate in props:
            return candidate
    lower_map = {key.lower(): key for key in props.keys()}
    for candidate in candidates:
        if candidate and candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None


def _normalize_mapped_value(value: str, mapping: dict[str, str]) -> str:
    if not value:
        return ""
    return mapping.get(value, mapping.get(value.lower(), value))


def _title(prop: dict[str, Any] | None) -> str:
    return "".join(part.get("plain_text", "") for part in (prop or {}).get("title", [])).strip()


def _rich_text(prop: dict[str, Any] | None) -> str:
    return "".join(part.get("plain_text", "") for part in (prop or {}).get("rich_text", [])).strip()


def _select_name(prop: dict[str, Any] | None) -> str:
    select = (prop or {}).get("select")
    if select:
        return select.get("name", "")
    status = (prop or {}).get("status")
    return status.get("name", "") if status else ""


def _multi_select_names(prop: dict[str, Any] | None) -> list[str]:
    return [item.get("name", "") for item in (prop or {}).get("multi_select", []) if item.get("name")]


def _select_or_multi_select_names(prop: dict[str, Any] | None) -> list[str]:
    multi = _multi_select_names(prop)
    if multi:
        return multi
    selected = _select_name(prop)
    return [selected] if selected else []


def _date_start(prop: dict[str, Any] | None) -> str | None:
    date_obj = (prop or {}).get("date")
    return date_obj.get("start") if date_obj else None


def _url(prop: dict[str, Any] | None) -> str:
    return (prop or {}).get("url") or ""


def _number(prop: dict[str, Any] | None) -> float | None:
    value = (prop or {}).get("number")
    if isinstance(value, (int, float)):
        return float(value)
    formula = (prop or {}).get("formula") or {}
    value = formula.get("number")
    if isinstance(value, (int, float)):
        return float(value)
    rollup = (prop or {}).get("rollup") or {}
    value = rollup.get("number")
    return float(value) if isinstance(value, (int, float)) else None


def _relation_ids(prop: dict[str, Any] | None) -> list[str]:
    return [item["id"] for item in (prop or {}).get("relation", []) if "id" in item]


def _created_time(prop: dict[str, Any] | None) -> str | None:
    return (prop or {}).get("created_time")


def _last_edited_time(prop: dict[str, Any] | None) -> str | None:
    return (prop or {}).get("last_edited_time")


def _first_person(prop: dict[str, Any] | None) -> Owner | None:
    people = (prop or {}).get("people", [])
    if not people:
        return None
    person = people[0]
    person_info = person.get("person") or {}
    return Owner(notion_user_id=person.get("id", ""), name=person.get("name", ""), email=person_info.get("email", ""))


def last_edited_or_now(task: Task) -> str:
    return (task.last_edited_time or datetime.now(timezone.utc)).isoformat()
