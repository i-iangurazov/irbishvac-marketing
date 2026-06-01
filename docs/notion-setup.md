# Notion Setup

Notion is the source of truth. Slack mirrors task updates; it does not replace Notion.

## Required Pages and Databases

Create a top-level page named `Workbooks`. Under it, create or place:

- Tasks database
- Marketing Calendar database
- Workbooks database

Share all three databases with the Notion integration used by `NOTION_API_KEY`.

Current Notion API versions query data sources, not the database container directly. Keep the database IDs in `.env`, and also set the matching data source IDs when possible:

- `NOTION_TASKS_DATA_SOURCE_ID`
- `NOTION_MARKETING_CALENDAR_DATA_SOURCE_ID`
- `NOTION_WORKBOOKS_DATA_SOURCE_ID`

To copy a data source ID in Notion: open the database settings menu, choose `Manage data sources`, click the data source `...` menu, then copy the data source ID. If a database is only a linked view of another database, share the original source database with the integration.

## Using Existing Fixed Databases

If Tasks or Marketing Calendar already exist and you cannot rename/add properties, configure the app to use the existing property names. The app supports this through `.env` mapping variables and built-in aliases.

For the Tasks database shown in the current `Task Manager` screen, use:

```env
NOTION_TASK_NAME_PROPERTY=Name
NOTION_TASK_OWNER_PROPERTY=Person
NOTION_TASK_DEADLINE_PROPERTY=Due date
NOTION_TASK_STATUS_PROPERTY=Status
NOTION_TASK_PRIORITY_PROPERTY=Priority
NOTION_TASK_DEPARTMENT_PROPERTY=Type

# Leave these blank if the fixed database does not have those properties.
NOTION_TASK_ORIGINAL_DEADLINE_PROPERTY=
NOTION_TASK_CAMPAIGN_PROPERTY=
NOTION_TASK_DELIVERABLE_PROPERTY=
NOTION_TASK_NOTES_PROPERTY=
NOTION_TASK_NEEDS_FROM_OTHERS_PROPERTY=
NOTION_TASK_CREATED_PROPERTY=
NOTION_TASK_LAST_EDITED_PROPERTY=
NOTION_TASK_LAST_REMINDER_SENT_PROPERTY=
```

For the Marketing Calendar screen shown in the current workspace, these names already match the app defaults:

```env
NOTION_CAMPAIGN_NAME_PROPERTY=Campaign name
NOTION_CAMPAIGN_TRADE_PROPERTY=Trade
NOTION_CAMPAIGN_CHANNEL_PROPERTY=Channel
NOTION_CAMPAIGN_START_DATE_PROPERTY=Start Date
NOTION_CAMPAIGN_END_DATE_PROPERTY=End Date
NOTION_CAMPAIGN_OWNER_PROPERTY=Owner
NOTION_CAMPAIGN_STATUS_PROPERTY=Status
NOTION_CAMPAIGN_PLANNED_SPEND_PROPERTY=Planned Spend
NOTION_CAMPAIGN_EXPECTED_LEADS_PROPERTY=Expected Leads
NOTION_CAMPAIGN_EXPECTED_CPL_PROPERTY=Expected CPL
NOTION_CAMPAIGN_EXPECTED_ROI_PROPERTY=Expected ROI
NOTION_CAMPAIGN_ACTUAL_SPEND_PROPERTY=Actual Spend
NOTION_CAMPAIGN_ACTUAL_LEADS_PROPERTY=Actual Leads
NOTION_CAMPAIGN_ACTUAL_CPL_PROPERTY=Actual CPL
NOTION_CAMPAIGN_ACTUAL_ROI_PROPERTY=Actual ROI
```

If the fixed Marketing Calendar does not have task/workbook relations or notes, leave these blank:

```env
NOTION_CAMPAIGN_LINKED_TASKS_PROPERTY=
NOTION_CAMPAIGN_LINKED_WORKBOOK_PROPERTY=
NOTION_CAMPAIGN_NOTES_PROPERTY=
```

Status values are normalized internally. For the current Tasks database:

```env
TASK_STATUS_MAP_JSON={"Not started":"Not Started","In progress":"In Progress","Done":"Completed"}
TASK_PRIORITY_MAP_JSON={"Urgent":"Critical"}
```

When optional fields are missing, validation shows warnings instead of blocking startup. The affected automations are limited:

- no `Deliverable link` property means completed-task proof checks cannot pass by URL unless a notes/link property is mapped
- no `Notes / Issues` property means delayed/blocker reason checks cannot read a reason
- no `Linked Tasks` relation means campaign progress risk cannot calculate completion from linked tasks
- no `Needs Verification` checkbox means flags are logged/commented but not stored as a Notion checkbox
- no `Last Reminder Sent At` date property means reminder duplicate prevention still works from SQLite, but sent reminder state is not visible in Notion

## Tasks Database Schema

Required properties:

| Property | Type | Requirement |
| --- | --- | --- |
| Task name | Title | Required |
| Owner | Person | Required, one person only; email is used for automatic Slack reminder lookup when Notion exposes it |
| Deadline | Date | Required |
| Original Deadline | Date | Service sets from Deadline when missing |
| Status | Select | Required |
| Priority | Select | Required |
| Department | Select | Required |
| Linked Campaign | Relation | Relation to Marketing Calendar |
| Deliverable link | URL | Required when Status is Completed |
| Notes / Issues | Text | Required when Status is Delayed or Blocked |
| Needs From Others | Text | Required when Status is Blocked |
| Created | Created time | Required |
| Last Edited | Last edited time | Required |
| Needs Verification | Checkbox | Recommended service-managed flag |
| Last Reminder Sent At | Date | Optional service-managed reminder audit field |

Status options:

- Not Started
- In Progress
- Blocked
- Needs Review
- Completed
- Delayed
- Canceled

Priority options:

- Low
- Medium
- High
- Critical

Department options:

- Marketing
- HVAC
- Plumbing
- Electrical
- Ops
- IT

Required views to create manually:

- My Tasks
- This Week
- Overdue
- Blocked
- Needs Verification
- By Department
- By Campaign
- Completed Archive

Notion API support for creating database views is limited, so views are documented for manual creation.

## Marketing Calendar Schema

Required properties:

| Property | Type | Requirement |
| --- | --- | --- |
| Campaign name | Title | Required |
| Trade | Multi-select | Required |
| Channel | Multi-select | Required |
| Start Date | Date | Required |
| End Date | Date | Required |
| Owner | Person | Required |
| Status | Select | Required |
| Planned Spend | Number, dollar | Required for budget reporting |
| Expected Leads | Number | Required for expected CPL |
| Expected CPL | Formula | Planned Spend / Expected Leads |
| Expected ROI | Number, percent | Required when known |
| Actual Spend | Number, dollar | Updated during campaign |
| Actual Leads | Number | Updated during campaign |
| Actual CPL | Formula | Actual Spend / Actual Leads |
| Actual ROI | Number, percent | Updated during campaign |
| Linked Tasks | Relation | Relation to Tasks DB |
| Linked Workbook | Relation | Relation to Workbooks DB |
| Notes | Text | Optional details |

Trade options:

- HVAC
- Plumbing
- Electrical
- Cross-trade

Channel options:

- Google Ads
- SEO
- YouTube
- Billboard
- Email
- SMS
- Direct Mail
- Event
- Co-op

Status options:

- Planned
- In Flight
- Completed
- Canceled

Suggested Notion formulas:

```text
Expected CPL = if(or(empty(prop("Expected Leads")), prop("Expected Leads") == 0), 0, prop("Planned Spend") / prop("Expected Leads"))
Actual CPL = if(or(empty(prop("Actual Leads")), prop("Actual Leads") == 0), 0, prop("Actual Spend") / prop("Actual Leads"))
```

Required views to create manually:

- Annual Timeline grouped by Trade
- This Month
- This Quarter
- By Channel
- Budget Roll-up with sum of Planned Spend
- Performance: Actual vs Expected

Campaign import:

```bash
python3 scripts/import_campaigns_csv.py docs/campaign-import-template.csv --dry-run
python3 scripts/import_campaigns_csv.py path/to/2026-campaigns.csv
```

The CSV supports `Owner Notion User ID` and semicolon-separated multi-select values for `Trade` and `Channel`. The service provides import support. Emil must supply the real campaign calendar if at least 30 entries through end of 2026 are required.

## Workbooks Database

If the workspace does not already have a Workbooks database, create it manually first. In the screenshot you shared, the right place is under `Marketing 2.0` -> `DATABASES`, beside `Tasks` and `Marketing Calendar`.

### Create the Workbooks Database Step by Step

1. In the Notion sidebar, click `Marketing 2.0`.

2. Click the `DATABASES` page or section.

3. Create a new full-page database:
   - Click `+` near the page list, or type `/table` inside the `DATABASES` page.
   - Choose `Table - Full page` or `Table database - Full page`.
   - Do not choose a linked database view. The agent needs the original source database.

4. Name the new database exactly:

   ```text
   Workbooks
   ```

5. Rename the default title property:
   - Notion usually creates a first column named `Name`.
   - Click the `Name` column header.
   - Rename it to:

   ```text
   Workbook name
   ```

6. Add the required properties:
   - Click `+` at the right side of the table headers.
   - Add each property from the table below.
   - Match the names exactly, including spaces and capitalization.

7. Set each property type:
   - `Owner` -> `Person`
   - `Last Updated` -> `Date`
   - `Last Reviewed By` -> `Person`
   - `Current Version` -> `Text`
   - `Quarterly review reminder` -> `Date`

8. Share the database with the Notion integration:
   - Open the `Workbooks` database as a full page.
   - Click the `...` menu in the top-right.
   - Click `Connections`.
   - Search for `Marketing Operating System connection`.
   - Add it.
   - If Notion asks for confirmation, confirm access.

9. Copy the Workbooks database ID:
   - Open the `Workbooks` database in the browser.
   - Copy the URL.
   - The long ID in the URL is the database ID.
   - Put it in `.env`:

   ```env
   NOTION_WORKBOOKS_DATABASE_ID=your_workbooks_database_id
   ```

10. Copy the Workbooks data source ID:
    - Open the `Workbooks` database as a full page.
    - Open database settings.
    - Click `Manage data sources`.
    - Click the `...` menu for the Workbooks data source.
    - Choose `Copy data source ID`.
    - Put it in `.env`:

    ```env
    NOTION_WORKBOOKS_DATA_SOURCE_ID=your_workbooks_data_source_id
    ```

11. Decide what to use for `NOTION_WORKBOOKS_PAGE_ID`:
    - If you created a parent page specifically named `Workbooks`, use that page ID.
    - If `Workbooks` is only a database under `DATABASES`, you can use the `DATABASES` page ID.
    - If you do not need parent-page automation right now, leave it blank:

    ```env
    NOTION_WORKBOOKS_PAGE_ID=
    ```

12. Validate the setup:

    ```bash
    python3 -m marketing_os_agent validate-notion
    ```

13. Seed the required workbook rows:

    ```bash
    python3 -m marketing_os_agent seed-workbooks
    ```

14. After seeding, open the Workbooks database and fill owner/review metadata:
    - `Owner`
    - `Last Updated`
    - `Last Reviewed By`
    - `Current Version`
    - `Quarterly review reminder`

If you want to validate Tasks and Marketing Calendar before creating Workbooks, leave these blank temporarily:

```env
NOTION_WORKBOOKS_PAGE_ID=
NOTION_WORKBOOKS_DATABASE_ID=
NOTION_WORKBOOKS_DATA_SOURCE_ID=
```

Required properties:

| Property | Type |
| --- | --- |
| Workbook name | Title |
| Owner | Person |
| Last Updated | Date |
| Last Reviewed By | Person |
| Current Version | Text |
| Quarterly review reminder | Date |

Required workbooks:

- Google Ads
- Google LSAs
- SEO technical + content
- YouTube
- Email / Hatch
- SMS / Hatch + 10DLC compliance
- Billboard
- Direct Mail / Dream Home Guide
- Referral program
- Co-op marketing / Carrier / Sigler
- Rebate program tracking: SVCE, SJCE, PCE, TECH Clean CA, BAAQMD
- Reviews & reputation
- Web / Webflow CMS
- AI/Chat: ChatGPT, Perplexity, Gemini, Google AI Overviews
- Reporting workbook: 1–14, 15–31, monthly, quarterly, annual templates

Seed missing workbook records:

```bash
python3 -m marketing_os_agent seed-workbooks
```

The seeder creates missing workbook rows in `NOTION_WORKBOOKS_DATABASE_ID`. Owners and review metadata must be assigned by Emil after seeding.

## Validation

Run:

```bash
python3 -m marketing_os_agent validate-notion
```

The validator checks required database properties and required workbook names when `NOTION_WORKBOOKS_DATABASE_ID` is configured. It does not silently assume schema exists.
