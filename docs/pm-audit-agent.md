# PM Audit Agent V1

The Project Management Audit Agent is a read-only ServiceTitan audit path for PM install projects. It is disabled by default, separate from the continuous ServiceTitan operations audit loop, and can run manually or from the main app scheduler.

## Command

```bash
python3 -m marketing_os_agent pm-audit-once
```

Default behavior:

- `PM_AUDIT_ENABLED=false`, so the command reports `disabled`.
- `PM_AUDIT_DRY_RUN=true`, so validation sends no Slack.
- Passes stay silent.
- Skips stay silent.
- Only failures are included in the printed or Slack summary.
- No ServiceTitan write actions are implemented.

## Environment

```env
PM_AUDIT_ENABLED=false
PM_AUDIT_SCHEDULE_ENABLED=false
PM_AUDIT_RUN_ON_STARTUP=false
PM_AUDIT_DRY_RUN=true
PM_AUDIT_STATUS_STALE_DAYS=14
PM_AUDIT_TASK_OVERDUE_DAYS=3
PM_AUDIT_PM_ASSIGNMENT_GRACE_HOURS=24
PM_AUDIT_TASK_TEMPLATE_GRACE_HOURS=48
PM_AUDIT_RUN_HOUR=8
PM_AUDIT_RUN_MINUTE=0
PM_AUDIT_WEEKDAYS_ONLY=true
PM_AUDIT_PROJECT_PAGE_SIZE=50
PM_AUDIT_MAX_PROJECTS=100
PM_AUDIT_MAX_TASKS=500
PM_AUDIT_ENABLED_RULE_IDS_JSON=[]
PM_AUDIT_INSTALL_BUSINESS_UNIT_IDS_JSON=["1809","64313020","64569731"]
PM_AUDIT_INSTALL_BUSINESS_UNIT_NAMES_JSON=["HVAC - Install","Plumbing - Install","Electrical - Install"]
PM_AUDIT_INCLUDE_CLIENT_NAME=false
PM_AUDIT_SOLD_BY_FIELD_NAMES=["Sold By","Sold by","Comfort Advisor","Sold By CA"]
PM_AUDIT_PERMIT_FIELD_NAMES=["PERMIT","Permit","Permit Number","Permit #","Permit Status"]
PM_AUDIT_HOA_FIELD_NAMES=["HOA Approval","Under HOA","HOA Status","HOA"]
PM_AUDIT_HOA_ZIP_LIST=[]
PM_AUDIT_ASBESTOS_FIELD_NAMES=["Asbestos","Asbestos Status","Asbestos Check"]
PM_AUDIT_ASBESTOS_YEAR_CUTOFF=
PM_AUDIT_REVIEW_REQUESTED_FIELD_NAMES=["Review Requested","Review request","Review Sent"]
PM_AUDIT_ON_HOLD_MAX_DAYS=30
PM_AUDIT_ON_HOLD_REASON_FIELD_NAMES=["On Hold Reason","Hold Reason","Hold Notes"]
PM_AUDIT_HOMEOWNER_AUTH_WITHIN_HOURS=2
PM_AUDIT_HOMEOWNER_AUTH_FORM_NAMES=["Homeowner Authorization","Homeowner Authorization Form"]
PM_AUDIT_COMPLETION_REPORT_FORM_NAMES=["Installation Completion Report","Completion Report"]
PM_AUDIT_EQUIPMENT_FIELD_NAMES=["Equipment Registered","Equipment Status","Equipment Registration"]
PM_AUDIT_DEPOSIT_BEFORE_INSTALL_DAYS=7
PM_AUDIT_DEPOSIT_LINE_ITEM_NAMES=["Deposit","Project Deposit","Installation Deposit"]
PM_AUDIT_PERMIT_BEFORE_INSTALL_DAYS=10
PM_AUDIT_PROJECT_LEFT_OPEN_DAYS=7
PM_AUDIT_REBATE_FIELD_NAMES=["Rebate","Rebate Status","Rebate Approval"]
PM_AUDIT_CREW_FIELD_NAMES=["Crew","Install Crew","Team"]
PM_AUDIT_CHANGE_ORDER_FIELD_NAMES=["Change Order","Change Order Approval","Additional Work Approval","Written Approval"]
PM_AUDIT_SLACK_CHANNEL_ID=
PM_AUDIT_TEST_SEND=false
SERVICE_TITAN_PROJECT_URL_TEMPLATE=https://go.servicetitan.com/#/project/{project_id}
```

Live PM Slack sends require `PM_AUDIT_SLACK_CHANNEL_ID`. PM does not send to `SLACK_ALERT_CHANNEL_ID`.

```env
SLACK_BOT_TOKEN=...
PM_AUDIT_SLACK_CHANNEL_ID=C0BDZ5E4GJU
```

Use this PM-only test command with synthetic project data:

```bash
python3 -m marketing_os_agent pm-audit-test-slack
```

It sends only when `PM_AUDIT_TEST_SEND=true`, `PM_AUDIT_SLACK_CHANNEL_ID` is set, and the bot token is configured.

Use `PM_AUDIT_DRY_RUN=true` for validation. Set `PM_AUDIT_DRY_RUN=false` only for approved PM live sends to `PM_AUDIT_SLACK_CHANNEL_ID`.

`PM_AUDIT_DRY_RUN=true` is separate from `SERVICE_TITAN_AUDIT_DRY_RUN=true`. For PM-only dry-run validation in Render, set `PM_AUDIT_ENABLED=true` and keep `PM_AUDIT_DRY_RUN=true`; do not change the live ServiceTitan audit dry-run setting unless intentionally pausing Sales/HVAC/Plumbing ServiceTitan alerts.

WARNING: Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` only for one-off ServiceTitan audit validation commands or a deliberately paused dry-run environment. Do not set it globally on the live Render service unless intentionally stopping live ServiceTitan alerts.

## Rule Allowlist

`PM_AUDIT_ENABLED_RULE_IDS_JSON` controls which implemented PM rules run:

- Empty or unset: evaluate all implemented PM rules.
- Non-empty: evaluate only listed rule IDs.

Supported IDs:

```json
["R1","R3","R4","R6","R7","R8","R9","R10","R11","R13","R15","R16","R17","R18","R19","R20","R21","R22","R23","R24","R25","R26","R27","R28"]
```

First scheduled live set:

```env
PM_AUDIT_ENABLED_RULE_IDS_JSON=["R1","R13","R17"]
```

Run this allowlist in dry-run before enabling or changing the app scheduler. R4 and the new R8-R28 rules are implemented for dry-run validation, but they should stay out of first scheduled live messages until Jane confirms alert quality and field availability.

Do not include these in the first scheduled PM messages:

- R3 PM assigned: currently noisy and needs more review.
- R6 Sold By: re-pointed to Project Details, but needs another dry-run to confirm realistic empty rates.
- R7 Permit: re-pointed to Project Details PERMIT, but needs another dry-run to confirm field availability.
- R8-R10, R16, and R18-R28: implemented skip-safe for dry-run validation only.
- R11 Tasks applied: task-count proxy is useful, but not in Jane's first live set.
- R15 No stale tasks: useful later, but not in Jane's first live set.

## Scope

In-scope project types:

- `63812999` / `Standard Install`
- `63813000` / `Construction & Remodel`

Out of scope:

- Service Call
- Warranty
- Recall
- Home Care Plan / Comfort Plan / Comfort Club maintenance
- Free diagnostic
- Internal / R&D

PM Audit also filters projects to install business units before loading tasks or invoices. Defaults are:

- `1809` / HVAC - Install
- `64313020` / Plumbing - Install
- `64569731` / Electrical - Install

This prevents Service, Sales, Warranty, Recall, Maintenance, and other non-install projects from skewing PM reports when ServiceTitan marks them with a broad project type such as Standard Install. If ServiceTitan only exposes business-unit names, `PM_AUDIT_INSTALL_BUSINESS_UNIT_NAMES_JSON` is used as a fallback. Projects with no matching install BU are skipped before task/invoice enrichment.

The v1 implementation uses project type first. It fetches project metadata, filters to the PM install project types, then loads tasks only for the bounded in-scope project set. Use `PM_AUDIT_MAX_PROJECTS` and `PM_AUDIT_MAX_TASKS` to keep dry-run validation fast and predictable. If data is missing or uncertain, rules return `skip` rather than failing.

## Rules Implemented

| Rule | Name | V1 behavior |
| --- | --- | --- |
| R1 | Project type set and valid | Fails missing/invalid project type when the project type field is available. |
| R3 | PM assigned | Fails when no PM is assigned at all; passes when a PM is assigned. The grace-period setting is retained for future SLA logic but is not required for the basic no-PM check. |
| R4 | Status set and current | Fails when project status is empty. Passes when status exists. If a status last-updated timestamp is available and older than `PM_AUDIT_STATUS_STALE_DAYS` while the project has open tasks, fails as stale; if that timestamp is missing, it does not guess. |
| R6 | Comfort Advisor / Sold By set | Reads Project Details `Sold By` using `PM_AUDIT_SOLD_BY_FIELD_NAMES`; skips when none of the configured fields exist, fails when a configured field exists but is empty. Valid non-empty values include Comfort Advisor names, `HVAC Service`, and `Plumbing Service`. Keep dry-run until revalidation confirms realistic empty rates. |
| R7 | Permit field present | Reads Project Details `PERMIT` using `PM_AUDIT_PERMIT_FIELD_NAMES`; skips when the PERMIT section/fields are unavailable, fails when configured permit data exists but is empty. Does not use the separate ServiceTitan Permits module in v1.1. |
| R8 | HOA approval status set | Uses configured HOA fields only. Passes when HOA is not required or approval/status is present; fails only when a structured field shows HOA is required and approval/status is blank; skips when requirement/status cannot be determined. |
| R9 | Asbestos check recorded | Requires `PM_AUDIT_ASBESTOS_YEAR_CUTOFF`, replacement signal, structure/system age, and configured asbestos field. Missing or ambiguous data skips. |
| R10 | Review-requested flag set | Fails completed/closed projects only when a configured Review Requested field exists and is false/blank; skips when the field/status is unavailable. |
| R11 | Tasks applied / task count present | Uses task count as a v1 proxy for task-template application; fails when no project tasks exist after `PM_AUDIT_TASK_TEMPLATE_GRACE_HOURS`; skips when the timestamp needed for the grace period is unavailable. |
| R13 | Every task has an assignee | Fails when any project task has no assignee. |
| R15 | No stale open tasks | Fails when an open task with a due date is more than `PM_AUDIT_TASK_OVERDUE_DAYS` past due. Open tasks without due dates are ignored for stale-task failure. |
| R16 | On-hold has a reason | Fails only when status is On Hold beyond `PM_AUDIT_ON_HOLD_MAX_DAYS` and a structured on-hold reason field exists but is blank. Raw notes are ignored. |
| R17 | Completed projects are closed out | Fails when a completed project still has open tasks. |
| R18 | Payment order | Uses structured payment milestone fields when available. Skips when deposit/first/final milestones or timestamps cannot be identified safely. |
| R19 | Homeowner Authorization timing | Uses structured crew-arrival and Homeowner Authorization timestamps. Skips when forms/timestamps are not safely project-scoped. |
| R20 | Installation Completion Report green | Checks structured completion-report status for completed installs. Skips when the report is unavailable or not safely scoped. |
| R21 | Equipment registered | Checks configured structured equipment registration fields on completed projects. Skips when unavailable. |
| R22 | Deposit before install | Uses linked project/job invoices only. Passes when structured payment is confirmed before install, fails when scoped invoice data shows no payment inside the configured window, and skips if invoice/payment/date linkage is unclear. |
| R23 | Permit before install | Uses Project Details PERMIT fields inside the configured pre-install window. Customer-pulled permit is honored only when structured permit-owner data says customer. |
| R24 | Equipment confirmed before scheduling | Uses configured equipment fields for scheduled installs. Skips when the field is unavailable. |
| R25 | Rebate confirmed before scheduling | Uses configured rebate fields. Skips when applicability or status is unavailable. |
| R26 | Crew assigned before scheduling | Uses configured crew fields for scheduled installs. Skips when the field is unavailable. |
| R27 | Project left open too long | Uses structured completion/final-payment signals only. Skips when those signals are unavailable. |
| R28 | Change order missing written approval | Uses configured structured change-order fields only. Raw notes are ignored. |

First live candidates after validation are R1 project type, R13 task assignee, and R17 completed-with-open-tasks. R3, R4, R6, R7, R8-R10, R11, R15, and R16-R28 should stay dry-run pending revalidation.

R4 stale-status failures are intentionally not part of the first live allowlist because dry-run validation showed stale status can be noisy when ServiceTitan status data is inconsistent.

## Main App Scheduler

PM Audit is registered in the main app runner. The existing Render command stays:

```bash
python -m marketing_os_agent run
```

No Render Cron Job is required.

Working PM Audit Render env:

```env
PM_AUDIT_ENABLED=true
PM_AUDIT_SCHEDULE_ENABLED=true
PM_AUDIT_RUN_ON_STARTUP=true
PM_AUDIT_DRY_RUN=false
PM_AUDIT_SLACK_CHANNEL_ID=C0BDZ5E4GJU
PM_AUDIT_TEST_SEND=false
PM_AUDIT_ENABLED_RULE_IDS_JSON=["R1","R13","R17"]
PM_AUDIT_INSTALL_BUSINESS_UNIT_IDS_JSON=["1809","64313020","64569731"]
PM_AUDIT_INCLUDE_CLIENT_NAME=false
PM_AUDIT_MAX_PROJECTS=50
PM_AUDIT_PROJECT_PAGE_SIZE=50
PM_AUDIT_MAX_TASKS=500
PM_AUDIT_RUN_HOUR=8
PM_AUDIT_RUN_MINUTE=0
PM_AUDIT_WEEKDAYS_ONLY=true
```

`PM_AUDIT_RUN_ON_STARTUP=true` runs PM Audit shortly after deploy/restart. `PM_AUDIT_SCHEDULE_ENABLED=true` runs it at the configured daily time. A persisted daily marker prevents repeated automatic sends from restart loops; manual `pm-audit-once` remains allowed. Do not set `SERVICE_TITAN_AUDIT_DRY_RUN=true` for PM Audit; it is unrelated and would pause live Sales/HVAC/Plumbing ServiceTitan alerts.

## Project Links

PM Audit links to ServiceTitan Project pages, not Job pages.

```env
SERVICE_TITAN_PROJECT_URL_TEMPLATE=https://go.servicetitan.com/#/project/{project_id}
```

The alert text displays the project number, but the URL is built from the actual ServiceTitan project ID. If the project ID is missing, PM Audit omits the link instead of guessing with a linked job ID. PM Audit does not use `SERVICETITAN_JOB_URL_TEMPLATE`.

## Projects View Scope Notes

Jane's manual report flow is:

```text
Projects -> date range -> Install -> Electrical/HVAC/Plumbing -> Apply -> Search
```

Read-only API discovery found these project-level fields in the Projects endpoint:

- `projectTypeId`
- `status` / `statusId`
- `createdOn`
- `modifiedOn`
- `startDate`
- `businessUnitIds`
- `jobIds`

The Projects endpoint sample did not expose embedded trade, department, job type, or business-unit names directly. Business-unit names can be joined from `/settings/v2/tenant/{tenant}/business-units`; linked job type and job business-unit details can be joined from bounded `/jpm/v2/tenant/{tenant}/jobs` samples.

Current PM scope still uses project type first: `63812999` Standard Install and `63813000` Construction & Remodel. PM Audit also skips records with explicit Service, Sales, Warranty, Recall, Maintenance, Internal, or R&D labels when those classification labels are present. No hard-coded install business-unit allowlist was added with the project-link fix.

Recommended future scope tightening, after Jane confirms it matches the Projects report view:

- project type is Standard Install or Construction & Remodel;
- project `businessUnitIds` includes only install BUs:
  - `1809` HVAC - Install;
  - `64313020` Plumbing - Install;
  - `64569731` Electrical - Install;
- exclude Service, Sales, Warranty, Recall, Maintenance, Internal/R&D.

Do not add automatic classification/tagging writes without explicit write-mode approval.

## Deposit Tracking Discovery

Discovery date: 2026-06-30.

Business rule under review:

- deposit is required before install scheduling;
- expected deposit is `$1,000` or `10%` of job total, whichever is less;
- deposit is created as a project invoice in ServiceTitan;
- after payment arrives, PM removes the Deposit service line and moves payment to the installation invoice.

Read-only ServiceTitan discovery found a partially reliable structured path:

```text
project -> project.jobIds -> accounting invoices filtered by jobId
```

Trusted data observed from `/accounting/v2/tenant/{tenant}/invoices?jobId=...`:

- invoice ID;
- invoice `projectId`;
- linked job ID;
- invoice total;
- invoice balance;
- `paidOn` when fully paid;
- `depositedOn` when available;
- invoice date;
- nested invoice item names/totals.

Unreliable or unsafe for v1:

- `/accounting/v2/tenant/{tenant}/invoices?projectId=...` returned rows, but sampled rows did not match the requested project ID.
- `/accounting/v2/tenant/{tenant}/export/invoice-items?invoiceIds=...` returned overbroad pages and ignored filters in the sample.
- `/accounting/v2/tenant/{tenant}/payments` and `/accounting/v2/tenant/{tenant}/export/payments` returned payment-shaped rows, but sampled `invoiceId` / `projectId` filters did not produce reliable invoice-linked matches.
- No safe structured signal was found for "payment moved from deposit invoice to install invoice."

What can be inferred safely today:

- A linked project/job invoice with `total > balance` indicates some payment has been applied.
- A fully paid invoice may expose `paidOn` and sometimes `depositedOn`.
- A partial deposit may appear only as `invoice total - invoice balance`; the payment date may not be available from the scoped invoice row.
- Deposit line-item detection is not reliable after PM removes the Deposit service line.

Recommended R22 dry-run behavior:

- evaluate only when project install date exists;
- evaluate only when linked job invoices can be fetched by `jobId`;
- pass if structured invoice data shows payment applied before the configured deposit deadline;
- skip if invoice relationship, payment amount, or payment date cannot be determined safely;
- do not fail based on unscoped payment/export endpoints.

Recommended deposit-received notification behavior:

- keep disabled until a reliable payment event source is mapped;
- trigger only when a newly detected payment can be linked to a project or invoice;
- use PM Slack channel only;
- include project number, invoice ID, amount, status/date if available, and project link;
- do not include customer name, address, phone, email, or raw notes.

Email and office-number notifications:

- This repo currently has outbound SMTP email support for reports/tests.
- No Gmail inbox reader, Dialpad integration, SMS parser, or office-number ingestion path was found.
- If deposit notifications must come from email/SMS, implement that as a separate read-only integration with strict PII filtering and message dedupe.

## Dry-Run Only Rules

These are implemented as skip-safe rules, but should remain out of scheduled live allowlists until dry-run validation confirms field availability and Jane approves alert quality:

- R8 HOA approval status set
- R9 Asbestos check recorded
- R10 Review-requested flag set
- R16 On-hold has a reason
- R18 Payment order
- R19 Homeowner Authorization timing
- R20 Installation Completion Report green
- R21 Equipment registered
- R22 Deposit before install
- R23 Permit before install
- R24 Equipment confirmed before scheduling
- R25 Rebate confirmed before scheduling
- R26 Crew assigned before scheduling
- R27 Project left open too long
- R28 Change order missing written approval

## Slack / Dry-Run Output

PM output is grouped by PM and excludes customer names, addresses, phone numbers, emails, raw notes, raw descriptions, and customer summaries.

If PM leadership approves client names in the private PM audit channel, set `PM_AUDIT_INCLUDE_CLIENT_NAME=true`. This adds a single `Client:` line to PM failure blocks. It remains `false` by default.

Example:

```text
📋 PM Audit — Jun 24

Jane
• Project #127623147 — Missing permit field
  Field: Permit
  Action: Fill Project Details PERMIT information
  Link: https://go.servicetitan.com/#/project/127623147

Gerson
• Project #127623148 — Task overdue
  Field: Task #884
  Action: Update or close overdue task
  Due: Jun 20
  Link: https://go.servicetitan.com/#/project/127623148

Summary: Jane 1 issue · Gerson 1 issue
Totals: 25 projects evaluated · 2 fails · 6 skips
Top fail: R15 No stale open tasks (1).
```

The PM agent can use `PM_AUDIT_SLACK_CHANNEL_ID` for PM-only alerts/tests. The synthetic PM test command never sends to `SLACK_ALERT_CHANNEL_ID` by fallback.
Dry-run summaries also include total projects evaluated, total failures/skips, top fail rules, and top skip reasons to help tune field mappings without sending Slack.
