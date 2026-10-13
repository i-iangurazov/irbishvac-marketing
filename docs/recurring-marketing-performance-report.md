# Weekly Marketing Performance Report

Source requirement: [Recurring Tasks Set Up](https://app.notion.com/p/3f7c710dca6e80729506e709c404103b).

The existing `python -m marketing_os_agent run` process creates a report task each Friday at 16:00 and sends its owner a Slack DM each Monday at 08:00. Both times use the existing `TIMEZONE` configuration, including daylight-saving changes. No separate service or cron job is required.

## Behavior

- Friday: create `Marketing Performance Report — YYYY-MM-DD`, where the date is the following Monday. Assign the task to the configured Notion person, set a date-only Monday deadline and the existing `Not started` status. Set `Frequency=Weekly` and `Department=Marketing` when those options exist. The body says to complete and send the report.
- Monday: resolve that same person's email from Notion and look up their Slack identity. Send one personal reminder with the task link. An explicit `OWNER_SLACK_MAP_JSON` entry keyed by the exact Notion user ID/email can serve as a fallback. Similar names and Tim's escalation route are never used.
- Completed, canceled, archived, rescheduled or reassigned reports receive no dedicated Monday reminder. If Friday's task is missing, log a failure; do not create a replacement on Monday.
- Keep existing Monday pushes, Friday roundups, deadline reminders and all ServiceTitan/PM/Installer jobs unchanged. The existing general Monday summary/deadline reminder may also include the report.
- Catch up after a missed scheduler minute on Friday from 16:00 until local midnight, and on Monday from 08:00 until local noon. Known rejections and read/configuration failures retry after 15 minutes within those windows. Entirely missed windows are not backfilled.

## Configuration and read-only validation

This workflow is disabled and dry-run by default. Preserve the production service's current environment; add only these variables:

```env
MARKETING_PERFORMANCE_REPORT_ENABLED=true
MARKETING_PERFORMANCE_REPORT_DRY_RUN=true
MARKETING_PERFORMANCE_REPORT_OWNER_NOTION_USER_ID=a5274c27-110e-4cb9-9fa4-62fe5c46eccc
MARKETING_PERFORMANCE_REPORT_KEY_PROPERTY=Deadline rule
```

The owner above was verified as Emil Sultanaliev (`emil@irbishvac.com`) in the source workspace. Verify it again with the command below in the deployed environment. Do not substitute an integration/bot user.

The source Task Manager data source is `360c710d-ca6e-80dc-bf27-000b9f07047c`, under database `360c710d-ca6e-8096-ab76-edd067481d9b`. Confirm that the existing `NOTION_TASKS_DATA_SOURCE_ID`/`NOTION_TASKS_DATABASE_ID` target this source. If production currently targets another database, stop and resolve the target; do not repoint the shared polling process casually. The report uses the existing configured task source and property mappings, with `Name`, `Person`, and `Due date` schema fallbacks. It supports both select and status properties for Status.

Required access: the service's existing Notion integration must read the source schema/users/query and create pages in Task Manager; Slack needs `users:read.email`, `im:write`, and `chat:write`. Notion must expose the owner's email, or an exact ID mapping must already be configured.

```bash
# Offline preview: no credentials, API calls or SQLite writes.
python -m marketing_os_agent marketing-performance-report preview
# Optional timezone-aware reference time for preview/validation only.
python -m marketing_os_agent marketing-performance-report preview --at 2026-10-16T23:00:00Z
# Read-only API validation: schema/options, owner, duplicate scan, Slack email lookup.
python -m marketing_os_agent marketing-performance-report validate
```

Expect `status=validated`, owner Emil, the correct task source and a resolved Slack ID. Validation does not open a DM, send messages, create pages, initialize SQLite, or touch dedupe state. Never expose tokens when copying diagnostics.

## Rollout

1. Run the full tests: `python -m unittest discover -s tests`.
2. Back up the existing SQLite database using SQLite's backup facility. Keep its durable disk and the existing `SQLITE_PATH`.
3. Deploy the reviewed PR with the new workflow enabled and its dry-run still true. Keep the start command `python -m marketing_os_agent run`. Storage initialization adds one table without changing existing records.
4. Run the read-only validation above with the service's actual environment. Check the resolved owner/source, `TIMEZONE`, and the planned Monday date.
5. Switch only `MARKETING_PERFORMANCE_REPORT_DRY_RUN=false` when validation succeeds. Restart/redeploy the same service. Existing global audit dry-run and notification settings must retain their values.
6. At the next Friday window, check `marketing_performance_report_completed` with `status=created` or `existing`, then read the task back in Notion. At the next Monday morning window, verify one DM and its `status=sent` log. Manual `create`/`remind` commands obey the same windows, enablement, dry-run and dedupe as scheduled calls; `--at` is intentionally prohibited for writes.

```bash
python -m marketing_os_agent marketing-performance-report create
python -m marketing_os_agent marketing-performance-report remind
```

For `TIMEZONE=America/Los_Angeles`, the first upcoming pair after the October 13 setup is Friday October 16 at 16:00 PDT, with Monday October 19 as the deadline; the reminder is October 19 at 08:00 PDT. In Bishkek these are October 17 at 05:00 and October 19 at 21:00 respectively. These are examples, not a change to the production timezone.

Rollback: set only `MARKETING_PERFORMANCE_REPORT_ENABLED=false` and redeploy. Preserve the action state so re-enabling cannot repeat completed periods. Leave existing jobs and their environment untouched.

## Duplicate prevention and recovery

- `recurring_report_actions` keys include the task source, owner's stable Notion ID, Monday date and action. SQLite claims are atomic across workers sharing the same database; leases and a 15-minute backoff avoid concurrent/repeated attempts. Success is recorded after the confirmed external result. Old scheduler jobs keep their existing dedupe behavior.
- New tasks store a durable weekly marker in the existing text property `Deadline rule`. A targeted Notion query checks that marker before creating, so renames/deadline edits and a local-state rebuild do not duplicate an automatically created task. Existing manually created tasks with the report title/owner/date are adopted; their properties are not overwritten. Multiple matches block the action for operator review.
- Notion page creation and the dedicated Slack message send use a single HTTP attempt. Slack also receives a stable `client_msg_id`. Normal clients retain their current retry behavior.
- A dispatch is recorded as uncertain before writing externally. On a lost Notion response, later Friday attempts reconcile the remote marker and mark success when the task becomes visible. They do not submit another create while the outcome remains uncertain.
- A lost Slack response, HTTP 5xx/408, or Slack internal/fatal error leaves the reminder uncertain and suppresses further automatic sends. Exactly-once external delivery cannot be guaranteed transactionally; this favors avoiding duplicate messages over silently resending. Inspect Emil's DM/Notion and logs before manually resolving an uncertain action.
- Keep one durable SQLite state shared by service workers. Separate deployments with separate databases can race on Notion creation; they are not supported for this workflow. Losing reminder state can permit a repeat DM. Backups and persistent storage remain necessary.
- An adopted manual task without the durable marker depends on its original title/owner/date for remote recovery. Renaming it after losing SQLite state removes that fallback; use the automatic task's marker for durable identification.

For uncertain actions, first inspect the external system. If the write occurred, retain the state (Notion creation reconciles automatically during the Friday window). If absence is positively confirmed, pause this workflow, back up SQLite, and reset **only that exact action key** to `ready` with `lease_until=0` using an administrative SQLite connection. Keep other tables/periods intact, then re-enable within the appropriate window. Do not reset uncertain state based solely on a timeout or temporary search absence.

## Validation performed for this change

Tests cover Friday creation, Monday assignment/date/DM, local-time and DST boundaries, year rollover, restarts, a local-state rebuild, remote keys/manual adoption, terminal/rescheduled tasks, missing identities/schema, two-worker claim races, expired claims, safe retries, ambiguous responses, dry-run, scheduler coexistence and client-level no-retry behavior. All tests use fakes/mocks; no credentials, live tasks or messages are required.

Runtime deployment and actual delivery require the service's production environment. A successful fake-client test or repository PR is not evidence of live deployment.
