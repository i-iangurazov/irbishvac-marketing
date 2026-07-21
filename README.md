# Marketing OS Agent

Production service for the Irbis marketing operating system. It keeps Notion as the source of truth, posts task-status updates to Slack, uses Claude for bounded summary/comment drafting, sends the Friday roundup by email, and persists state in SQLite for idempotent automation.

There is no admin UI because this repo started empty and the assignment focuses on the integration/service layer.

## Architecture

- `marketing_os_agent/config.py` loads environment configuration and safe defaults.
- `marketing_os_agent/persistence.py` stores task state, status history, counters, Slack thread mappings, owner mappings, campaign flags, and run logs in SQLite.
- `marketing_os_agent/clients/` contains Notion, Slack, Claude, and SMTP clients with retry/error logging.
- `marketing_os_agent/domain/` contains deterministic business logic for status verification, campaign health, owner mapping, scheduled reports, and ServiceTitan operations audit rules.
- `marketing_os_agent/scheduler.py` runs timezone-aware scheduled jobs.
- `marketing_os_agent/http_server.py` exposes `/healthz`, `/readyz`, and Slack/Notion webhook endpoints.

Claude is used only for language drafting. Deterministic checks remain in code: status, deadlines, missing deliverables, missing notes, missing blockers, delay counts, budget thresholds, and campaign progress risk.

## Local Setup

Requires Python 3.11+.

```bash
cp .env.example .env
python3 -m marketing_os_agent init-db
python3 -m marketing_os_agent validate-notion
python3 -m marketing_os_agent run
```

Health endpoints:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz
```

## Configuration

All secrets come from `.env`; none are stored in SQLite.
The service loads `.env` automatically from the current working directory. Existing shell environment variables take precedence over `.env` values.

Required for full production operation:

- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`
- `NOTION_API_KEY`, `NOTION_TASKS_DATABASE_ID`, `NOTION_MARKETING_CALENDAR_DATABASE_ID`
- `NOTION_WORKBOOKS_PAGE_ID`, `NOTION_WORKBOOKS_DATABASE_ID`
- Optional but recommended for current Notion API: `NOTION_TASKS_DATA_SOURCE_ID`, `NOTION_MARKETING_CALENDAR_DATA_SOURCE_ID`, `NOTION_WORKBOOKS_DATA_SOURCE_ID`
- `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_MARKETING_OPS_CHANNEL_ID`, `SLACK_TIM_USER_ID`
- Required for live ServiceTitan audit alerts: `SLACK_ALERT_CHANNEL_ID`; not required while `SERVICE_TITAN_AUDIT_DRY_RUN=true`
- Required only when continuous ServiceTitan audit is enabled or when running `servicetitan-audit-once`: `SERVICETITAN_CLIENT_ID`, `SERVICETITAN_CLIENT_SECRET`, `SERVICETITAN_TENANT_ID`, `SERVICETITAN_APP_KEY`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `TIM_EMAIL`, `VADIM_EMAIL`
- `OWNER_SLACK_MAP_JSON` only for fallback Slack routing exceptions; deadline reminders normally resolve users from Notion owner email via Slack lookup.

Defaults:

- `TIMEZONE=America/Los_Angeles`
- `NOTION_API_VERSION=2026-03-11`
- `NOTION_POLL_INTERVAL_SECONDS=120`
- `TASK_REMINDER_MINUTES_BEFORE=60`
- `TASK_DATE_ONLY_DEADLINE_HOUR=17`, used in `TIMEZONE` when a Notion deadline has a date but no time.
- `SERVICE_TITAN_AUDIT_ENABLED=false`
- `SERVICE_TITAN_AUDIT_DRY_RUN=false`
- `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false`
- `SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE=false`
- `SERVICE_TITAN_AUDIT_DEBUG_FIELDS=false`
- `SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS=300`
- `SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS=300`
- `SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES=240`
- `SERVICE_TITAN_AUDIT_OVERLAP_SECONDS=300`
- `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE=25`
- `SALES_COMFORT_ADVISOR_AUDIT_ENABLED=true`
- `HVAC_SERVICE_AUDIT_ENABLED=false`
- `PLUMBING_SERVICE_AUDIT_ENABLED=false`
- `TECHNICIAN_COMPLIANCE_ENABLED=false`
- `DISPATCHER_AUDIT_ENABLED=false`
- `DISPATCHER_AUDIT_SLACK_CHANNEL_ID=`
- `DISPATCHER_AUDIT_RULE_IDS_JSON=[]`
- `INSTALL_AUDIT_ENABLED=false`
- `INSTALL_AUDIT_DRY_RUN=true`
- `INSTALL_AUDIT_JOB_TYPE_MATCH_KEYWORDS=["Installation"]`
- `INSTALL_AUDIT_BUSINESS_UNIT_IDS=["1809","64313020"]`
- `ST_BU_INSTALLERS=1809,64313020`
- `INSTALL_AUDIT_RULE_IDS_JSON=[]`
- `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={}`
- `SERVICE_TITAN_BUSINESS_UNIT_LABELS_JSON={"1809":"HVAC Install","1810":"HVAC Service","1812":"HVAC Sales / Comfort Advisors","64313020":"Plumbing Install","64326403":"Plumbing Sales","64315277":"Plumbing Service"}`
- `BUDGET_OVERRUN_THRESHOLD_PERCENT=0`, which flags any actual spend at or over plan.
- `CAMPAIGN_RISK_WINDOW_PERCENT=80`
- `CAMPAIGN_RISK_TASK_COMPLETION_PERCENT=20`

## Safe Env Patterns

WARNING: Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` only for one-off validation commands or a deliberately paused dry-run environment. Do not set this globally on the live Render service unless you intentionally want to stop live Sales/HVAC/Plumbing ServiceTitan alerts.

`SERVICE_TITAN_AUDIT_DRY_RUN=true` affects the ServiceTitan audit send path globally. It suppresses immediate ServiceTitan Slack alerts, violation writes, dedupe writes, and checkpoint advancement. `PM_AUDIT_DRY_RUN=true` is separate and applies only to the PM Audit command.

Local one-off ServiceTitan validation:

```bash
SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-audit-once
```

Live production ServiceTitan audit:

```env
SERVICE_TITAN_AUDIT_DRY_RUN=false
SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false
```

PM Audit dry-run only:

```env
PM_AUDIT_ENABLED=true
PM_AUDIT_DRY_RUN=true
```

PM Audit disabled:

```env
PM_AUDIT_ENABLED=false
```

Do not copy local validation env blindly into Render. For PM-only dry-run validation in Render, prefer `PM_AUDIT_ENABLED=true` and `PM_AUDIT_DRY_RUN=true` while leaving existing ServiceTitan live audit dry-run settings unchanged.

## Do Not Do

- Do not set `SERVICE_TITAN_AUDIT_DRY_RUN=true` on the live Render service unless intentionally pausing live ServiceTitan alerts.
- Do not enable `PM_AUDIT_ENABLED=true` with `PM_AUDIT_DRY_RUN=false` until Jane approves live PM alerts.
- Do not enable photos/forms/arrival rules until API data is confirmed reliable.
- Do not remove Sales scope from `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` when adding HVAC or Plumbing scope.

Optional reminder state mirror:

- `NOTION_TASK_LAST_REMINDER_SENT_PROPERTY`, for example `Last Reminder Sent At`, can point to a Notion Date property. SQLite remains the required duplicate-prevention store; this Notion field gives visible state and prevents duplicate reminders if local state is rebuilt or lost.

If email or external credentials are missing, the service logs clear warnings and skips only the affected external action.

For Claude model errors like `model: ... not_found_error`, run:

```bash
python3 -m marketing_os_agent list-claude-models
```

Set `CLAUDE_MODEL` to one of the returned model IDs. Model access can vary by Anthropic account, key, and region, so do not guess from examples if the API returns 404.

For email warnings, configure an SMTP mailbox:

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=mailbox@example.com
SMTP_PASS=app-password-or-smtp-password
EMAIL_FROM=marketing-agent@example.com
TIM_EMAIL=tim@example.com
VADIM_EMAIL=vadim@example.com
```

Until those values are set, Friday Slack roundups still run, but Friday email delivery is skipped with a warning.

## Commands

```bash
python3 -m marketing_os_agent init-db
python3 -m marketing_os_agent validate-notion
python3 -m marketing_os_agent rebuild-task-baseline
python3 -m marketing_os_agent process-pending-transitions
python3 -m marketing_os_agent repost-missing-slack-updates
python3 -m marketing_os_agent servicetitan-audit-once
python3 -m marketing_os_agent servicetitan-discover-scopes
python3 -m marketing_os_agent install-audit-once
python3 -m marketing_os_agent install-audit-test-slack
python3 -m marketing_os_agent seed-workbooks
python3 -m marketing_os_agent poll-once
python3 -m marketing_os_agent monday-push
python3 -m marketing_os_agent friday-roundup
python3 -m marketing_os_agent monthly-kickoff
python3 -m marketing_os_agent quarterly-kickoff
python3 -m marketing_os_agent campaign-health-scan
python3 -m marketing_os_agent health-check
python3 -m marketing_os_agent smoke-test
python3 -m marketing_os_agent debug-tasks
python3 -m marketing_os_agent transition-counts
python3 -m marketing_os_agent list-claude-models
python3 -m marketing_os_agent test-email
```

Use `smoke-test` after configuring Notion. It reads Tasks and Marketing Calendar and prints a non-destructive summary without posting to Slack or changing Notion.
Use `rebuild-task-baseline` after changing database IDs, data source IDs, or field mappings. It reads all current tasks into SQLite without posting old statuses to Slack.
Use `process-pending-transitions` when `debug-tasks` shows `notion_status` differs from `local_status`; it scans all tasks once and posts pending transitions.
Use `repost-missing-slack-updates` after fixing Slack channel membership if an earlier transition was recorded before a Slack timestamp was stored.
Use `servicetitan-audit-once` after configuring ServiceTitan credentials to run one operations audit cycle without waiting for the background interval.
Use `servicetitan-discover-scopes` to print sanitized ServiceTitan business units, job types, statuses, tags, material context, related-record counts, and payload key availability before narrowing production rule scopes.
Use `install-audit-once` after configuring ServiceTitan credentials and Installer Audit scope to run one read-only Installer Audit cycle. It stays dry-run unless `INSTALL_AUDIT_DRY_RUN=false`.
Use `pm-audit-once` after configuring ServiceTitan credentials to run one disabled-by-default Project Management audit cycle. It stays dry-run unless `PM_AUDIT_ENABLED=true` and `PM_AUDIT_DRY_RUN=false`.
Use `debug-tasks` when a Notion edit is being read but no transition posts. It prints current Notion status next to the saved local baseline status.
Use `transition-counts` to inspect observed status transitions, including repeated completions that the service actually saw.
Use `list-claude-models` when Anthropic returns a model 404. It prints model IDs available to the configured `ANTHROPIC_API_KEY`.
Use `test-email` after configuring SMTP. It sends a live HTML Friday-roundup preview using current Notion task data without posting to Slack. By default it sends to `TIM_EMAIL` and `VADIM_EMAIL`; pass `--to you@example.com` to send only to a specific address.

Marketing Calendar import support:

```bash
python3 scripts/import_campaigns_csv.py docs/campaign-import-template.csv --dry-run
python3 scripts/import_campaigns_csv.py path/to/2026-campaigns.csv
```

The importer needs Emil’s real campaign data. It does not invent campaign entries.

## Automation Behavior

- Polls Notion Tasks for status transitions to `Completed`, `Delayed`, `Blocked`, or `Canceled`.
- Posts structured updates to `#marketing-ops`.
- Comments back on the Notion task for verification gaps.
- Flags `Needs Verification` when the configured checkbox exists.
- DMs Tim only for double-delay, repeated verification flags, campaign budget overrun, campaign progress risk, or unreachable owners.
- DMs task assignees once when an open assigned task is inside the configured deadline reminder window, default 1 hour before due time.
- Sends Monday owner DMs and a channel summary at Monday 8 AM, including due-this-week, carry-over, and moved-to-this-week tasks.
- Sends Friday roundup to Slack and email at Friday 4 PM, including not-completed tasks that need rollover.
- Sends monthly and quarterly campaign kickoff briefings at 9 AM on the first day.
- Runs daily campaign health scan at 7 AM.
- When enabled, continuously audits recent ServiceTitan jobs for Technician Compliance and Dispatcher / Job Quality violations, dedupes findings in SQLite, and sends actionable Slack alerts.

## ServiceTitan Operations Audit

The ServiceTitan Operations Audit Agent is disabled by default and runs in the same process as the Task Dispatcher when `SERVICE_TITAN_AUDIT_ENABLED=true`. It uses one shared ServiceTitan client, one shared SQLite store, and one Slack alert path for independently enabled scoped rulesets:

- Sales / Comfort Advisor Audit: three options, Sales photos when scoped photos are available, and first-half appointment-window arrival.
- HVAC Service Audit: three options, payment on completed jobs, diagnosis/service form, photos when scoped photos are available, and arrival-window checks.
- Plumbing Service Audit: three options, payment on completed jobs, diagnosis/service form, photos when scoped photos are available, and arrival-window checks. It is disabled by default and not approved for live alerts yet. The Plumbing options rule excludes Water Heater Maintenance, zero-dollar/no-charge jobs, and structured sold/performed repair-work visits; positive-invoice diagnostic/estimate visits still need business confirmation before live alerts.
- `job_left_open_after_visit` is available as an opt-in rule for jobs left open after an appointment end time plus `SERVICE_TITAN_OPEN_JOB_GRACE_MINUTES`; it is disabled unless enabled in `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON`.
- Legacy Technician Compliance and Dispatcher / Job Quality Audit remain behind explicit flags and should stay disabled until reviewed.

Rules return `pass`, `fail`, `insufficient_data`, `not_applicable`, or `error`. `insufficient_data` and `not_applicable` are logged and not alerted, so unavailable ServiceTitan fields and out-of-scope jobs do not create false positives.

Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` for command-level one-off validation only, or for a deliberately paused dry-run environment. Dry-run fetches real ServiceTitan jobs, evaluates rules, prints the one-time run summary, and skips Slack alerts and audit dedupe writes. The `servicetitan-audit-once` command runs one cycle and exits, even when continuous polling is still disabled. Do not set `SERVICE_TITAN_AUDIT_DRY_RUN=true` globally on a live Render service unless intentionally pausing live Sales/HVAC/Plumbing alerts. Set `SERVICE_TITAN_AUDIT_DEBUG_FIELDS=true` only when you need sanitized field availability logs for ServiceTitan payloads.

Sales/HVAC/Plumbing ServiceTitan alerts use `SLACK_ALERT_CHANNEL_ID`. Dispatcher Audit can use `DISPATCHER_AUDIT_SLACK_CHANNEL_ID`; if it is empty, Dispatcher falls back to `SLACK_ALERT_CHANNEL_ID`. PM Audit and Installer Audit do not use `SLACK_ALERT_CHANNEL_ID`; they use `PM_AUDIT_SLACK_CHANNEL_ID` and `INSTALL_AUDIT_SLACK_CHANNEL_ID`. Business Unit labels from `SERVICE_TITAN_BUSINESS_UNIT_LABELS_JSON` are included in compact alert text for grouping; they do not route alerts to separate channels. Normal Slack alerts omit internal scope/debug fields and customer PII.

Dispatcher Audit is disabled by default. When `DISPATCHER_AUDIT_ENABLED=true`, only the dedicated Dispatcher rules are attached; old handbook matrix rules are not enabled automatically. Use `DISPATCHER_AUDIT_RULE_IDS_JSON` for a narrow production allowlist and keep `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false` to prevent historical floods.

Weekly ServiceTitan violation summaries are disabled by default. Set `SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED=true` plus `SERVICE_TITAN_WEEKLY_SUMMARY_DAY`, `SERVICE_TITAN_WEEKLY_SUMMARY_HOUR`, and `SERVICE_TITAN_WEEKLY_SUMMARY_LOOKBACK_DAYS` to post a grouped stored-violation summary to `SLACK_ALERT_CHANNEL_ID`. The summary reads existing SQLite violation records; it does not fetch ServiceTitan or re-run the audit. Use command-level `SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-weekly-summary-once` to print the summary without sending Slack; do not copy that override into live Render unless intentionally pausing ServiceTitan sends.

Use `python3 -m marketing_os_agent servicetitan-runtime-diagnostics` on Render to confirm masked runtime config, parsed rule JSON, checkpoint state, recent audit cycles, and durable alert dedupe state. Live Slack sends are capped by `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE`; set it to `1` with `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=true` and `SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE=true` only for controlled one-real-historical-alert validation.

## Installer Audit Agent

The Installer Audit Agent is separate from PM Audit and the Sales/HVAC/Plumbing ServiceTitan operations audit. It is disabled by default, read-only, and uses `INSTALL_AUDIT_DRY_RUN` instead of `SERVICE_TITAN_AUDIT_DRY_RUN`.

```bash
python3 -m marketing_os_agent install-audit-once
python3 -m marketing_os_agent install-audit-test-slack
```

Installer Audit v4 only evaluates real install jobs. Both scope gates must pass: the normalized business-unit name must exactly match Electrical - Install, HVAC - Install, or Plumbing - Install, and the job-type name must contain `Installation` case-insensitively. Known IDs in `INSTALL_AUDIT_BUSINESS_UNIT_IDS` / `ST_BU_INSTALLERS` are used only to avoid enriching obviously unrelated raw jobs; an ID never bypasses the final BU-name gate. Notes, customer summaries, form names, Service Call, Maintenance, Warranty, Recall, Sales/Estimate, standby, internal placeholders, City Inspection, and other non-install jobs cannot make a job in scope.

```env
INSTALL_AUDIT_JOB_TYPE_MATCH_KEYWORDS=["Installation"]
INSTALL_AUDIT_BUSINESS_UNIT_NAMES=["Electrical - Install","HVAC - Install","Plumbing - Install"]
INSTALL_AUDIT_BUSINESS_UNIT_IDS=["1809","64313020"]
ST_BU_INSTALLERS=1809,64313020
INSTALL_AUDIT_RULE_IDS_JSON=[]
```

`INSTALL_AUDIT_RULE_IDS_JSON=[]` runs all active v4 rules: I1-I9, I11, and I12. Use an explicit list such as `["I1"]` or `["I1","I2","I3"]` for controlled rollout. I10 is retired and is not reused.

Implemented rules are I1 job not marked complete, I2 completion form not completed, I3 authorization form not completed, I4 arrival not marked, I5 late arrival, I6 meal break not recorded, I7 deposit reminder, I8 payment milestone short, I9 photos missing, I11 equipment not registered, and I12 review not requested. I10 materials not scanned was removed by v4. Rules with unavailable ServiceTitan fields return `skip` with a reason such as `form_status_unavailable`, `timesheet_breaks_unavailable`, `photo_count_unavailable`, `equipment_registration_unavailable`, or `review_requested_field_unavailable`; they do not create fake failures. Keep I2/I3 held until job-scoped form status is confirmed readable.

Install alerts send only to `INSTALL_AUDIT_SLACK_CHANNEL_ID`; there is no fallback to `PM_AUDIT_SLACK_CHANNEL_ID` or `SLACK_ALERT_CHANNEL_ID`. Slack/log output omits customer addresses, phone numbers, emails, raw notes, and raw customer summaries.

Installer Slack alerts use the existing operations style:

```text
HIGH - Installs: Job Not Marked Complete
Technician: <crew lead>
Appointment: <date, window>
Arrived: <time or unavailable>
Invoice: $<total> total / $<balance> balance
Issue: <concrete issue>
Action: <what to check>
Open in ServiceTitan: https://go.servicetitan.com/#/Job/Index/<job_id>
```

Installer violations are persisted with ruleset `Installer Audit`, so the weekly ServiceTitan summary includes Installs counts alongside Sales/HVAC/Plumbing counts.

Installer Audit can run from the main app scheduler with `INSTALL_AUDIT_ENABLED=true` plus `INSTALL_AUDIT_SCHEDULE_ENABLED=true`; `INSTALL_AUDIT_RUN_ON_STARTUP=true` runs once after startup. Automatic runs dedupe by local day. Manual `install-audit-once` ignores automatic daily dedupe and can be used for dry-run validation while automatic runs remain disabled.

## PM Audit Agent

The Project Management Audit Agent is implemented as a read-only PM install audit path. It is disabled by default and can run either manually or from the main app scheduler:

```bash
python3 -m marketing_os_agent pm-audit-once
```

For validation, use `PM_AUDIT_ENABLED=true` and `PM_AUDIT_DRY_RUN=true`. For approved PM live sends, set `PM_AUDIT_DRY_RUN=false`, `PM_AUDIT_SLACK_CHANNEL_ID` to the PM channel, and a narrow `PM_AUDIT_ENABLED_RULE_IDS_JSON` allowlist. PM audits only PM install projects with project type `Standard Install` or `Construction & Remodel` and install BU scope `PM_AUDIT_INSTALL_BUSINESS_UNIT_IDS_JSON=["1809","64313020","64569731"]`, then loads tasks/invoices only for the bounded in-scope project set. It implements R1, R3, R4, R6-R11, R13, R15-R28 from [docs/pm-audit-agent-discovery.md](docs/pm-audit-agent-discovery.md). R22 deposit before install is amount-based and can run explicitly with `PM_AUDIT_ENABLED_RULE_IDS_JSON=["R22"]`; it uses linked project/job invoices, configured deposit line-item names, and structured payment/balance/status data, and skips when linkage is unclear. Other R8-R28 rules are skip-safe dry-run validation rules and should stay out of scheduled live allowlists until field availability is confirmed. First scheduled live messages should remain limited to `PM_AUDIT_ENABLED_RULE_IDS_JSON=["R1","R13","R17"]` unless R22 is deliberately added for controlled deposit rollout; R4 stale status stays dry-run because ServiceTitan status data has been noisy. Passes and skips stay silent; dry-run prints grouped failures by PM and sends no Slack. `PM_AUDIT_INCLUDE_CLIENT_NAME=false` keeps client names out by default; R22 deposit failures may include the client name in the PM audit channel as approved for deposit tracking.

PM Audit can use a separate test Slack channel with `PM_AUDIT_SLACK_CHANNEL_ID` and `PM_AUDIT_TEST_SEND=true` via `python3 -m marketing_os_agent pm-audit-test-slack`. The PM test command uses synthetic project numbers only and does not fall back to the live ServiceTitan audit channel.

PM Audit can run from the main app scheduler when `PM_AUDIT_SCHEDULE_ENABLED=true`. The existing Render command stays `python -m marketing_os_agent run`; no Render Cron Job is required. Set `PM_AUDIT_RUN_ON_STARTUP=true` to run once after deploy/restart, then use `PM_AUDIT_RUN_HOUR`, `PM_AUDIT_RUN_MINUTE`, and `PM_AUDIT_WEEKDAYS_ONLY` for daily scheduling. PM Audit sends only to `PM_AUDIT_SLACK_CHANNEL_ID`; it does not use `SLACK_ALERT_CHANNEL_ID`.

PM project links use `SERVICE_TITAN_PROJECT_URL_TEMPLATE`, which defaults to `https://go.servicetitan.com/#/project/{project_id}`. PM Audit does not use `SERVICETITAN_JOB_URL_TEMPLATE`.

See [docs/servicetitan-operations-audit.md](docs/servicetitan-operations-audit.md) for setup, required scopes, dedupe behavior, adding rules, manual QA, known field limitations, and Render deployment notes.

## Manual Testing

1. Confirm the service can read Notion:

   ```bash
   python3 -m marketing_os_agent smoke-test
   ```

2. Start the service:

   ```bash
   python3 -m marketing_os_agent run
   ```

3. In Notion, change one mapped task status from `Not started` to `Done`, `Blocked`, or another terminal/status-update value.

4. Wait up to `NOTION_POLL_INTERVAL_SECONDS`.

5. Expected result:
   - logs show `notion_poll_completed`
   - `transitions` becomes `1`
   - Slack receives a task update in `SLACK_MARKETING_OPS_CHANNEL_ID`

Polling limitation: the Notion API returns the current page state, not every intermediate status edit. If a task is changed `Done -> In progress -> Done` between two polls, the service may only see `Done` before and after and cannot infer the hidden intermediate change. For toggle testing, set `NOTION_POLL_INTERVAL_SECONDS=10` and wait for one poll after each status change.

Deadline reminder checklist:

1. Create or choose an open task whose Notion Owner has an email matching a Slack user, Deadline more than 1 hour away, then run `poll-once`; no DM should be sent.
2. Set Deadline within the next hour, including a time if possible, then run `poll-once`; the owner should receive one Slack DM.
3. Run `poll-once` again with the same deadline; no duplicate DM should be sent.
4. Mark the task Completed or Canceled; no reminder should be sent.
5. Remove the owner; no reminder should be sent.
6. Use an owner without a Notion email, Slack email lookup result, or fallback mapping; logs should show `task_reminder_skipped_unmapped_owner` and polling should continue.
7. Temporarily break Slack credentials in a non-production environment; logs should show a Slack failure and polling should continue.

Run scheduled jobs manually:

```bash
python3 -m marketing_os_agent monday-push
python3 -m marketing_os_agent friday-roundup
python3 -m marketing_os_agent monthly-kickoff
python3 -m marketing_os_agent campaign-health-scan
SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-weekly-summary-once
python3 -m marketing_os_agent test-email --to ilias@example.com
```

Those commands may post to Slack and, for Friday roundup, attempt email. The `SERVICE_TITAN_AUDIT_DRY_RUN=true` weekly-summary example is a command-level one-off override only; do not set it globally on live Render unless intentionally pausing ServiceTitan audit sends. `test-email` reads Notion and only sends SMTP email; it does not post to Slack.

## Tests

```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m unittest discover -s tests
```

The tests use fake clients and do not require Notion, Slack, Claude, or SMTP credentials.

## VPS Deployment

Docker:

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f marketing-os-agent
```

Systemd:

1. Copy the repo to `/opt/marketing-os-agent`.
2. Create a `marketing-os-agent` Linux user.
3. Put production secrets in `/opt/marketing-os-agent/.env`.
4. Run `python3 -m marketing_os_agent init-db`.
5. Copy `deploy/marketing-os-agent.service` to `/etc/systemd/system/`.
6. Run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now marketing-os-agent
sudo journalctl -u marketing-os-agent -f
```

## Logs and Recovery

Logs are JSON lines on stdout or journald. Key events include scheduled job starts/completions, API failures, email failures, Claude token usage, verification flags, Tim escalations, and duplicate suppression.

Recovery steps:

1. Check `/readyz`.
2. Inspect logs for `notion_api_failure`, `slack_api_failure`, `claude_api_failure`, or `email_failure`.
3. Re-run `validate-notion` after Notion schema changes.
4. Re-run `poll-once` after fixing credentials.
5. SQLite state is in `SQLITE_PATH`; back it up before manual DB surgery.

Claude billing visibility is through Anthropic’s dashboard. The service logs token usage per successful Claude call.
