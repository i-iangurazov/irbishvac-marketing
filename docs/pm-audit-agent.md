# PM Audit Agent V1

The Project Management Audit Agent is a read-only ServiceTitan audit path for PM install projects. It is disabled by default and does not run in the continuous ServiceTitan operations audit loop.

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
PM_AUDIT_DRY_RUN=true
PM_AUDIT_STATUS_STALE_DAYS=14
PM_AUDIT_TASK_OVERDUE_DAYS=3
PM_AUDIT_PM_ASSIGNMENT_GRACE_HOURS=24
PM_AUDIT_TASK_TEMPLATE_GRACE_HOURS=48
PM_AUDIT_PROJECT_PAGE_SIZE=50
PM_AUDIT_MAX_PROJECTS=100
PM_AUDIT_MAX_TASKS=500
PM_AUDIT_SOLD_BY_FIELD_NAMES=["Sold by","Sold By","Comfort Advisor","Sold By CA"]
PM_AUDIT_PERMIT_FIELD_NAMES=["Permit","Permit Number","Permit #","Permit Status"]
```

Live Slack requires the existing single ServiceTitan Slack channel setup:

```env
SLACK_BOT_TOKEN=...
SLACK_ALERT_CHANNEL_ID=...
```

Do not set `PM_AUDIT_DRY_RUN=false` until Jane has reviewed dry-run output.

`PM_AUDIT_DRY_RUN=true` is separate from `SERVICE_TITAN_AUDIT_DRY_RUN=true`. For PM-only dry-run validation in Render, set `PM_AUDIT_ENABLED=true` and keep `PM_AUDIT_DRY_RUN=true`; do not change the live ServiceTitan audit dry-run setting unless intentionally pausing Sales/HVAC/Plumbing ServiceTitan alerts.

WARNING: Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` only for one-off ServiceTitan audit validation commands or a deliberately paused dry-run environment. Do not set it globally on the live Render service unless intentionally stopping live ServiceTitan alerts.

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

The v1 implementation uses project type first. It fetches project metadata, filters to the PM install project types, then loads tasks only for the bounded in-scope project set. Use `PM_AUDIT_MAX_PROJECTS` and `PM_AUDIT_MAX_TASKS` to keep dry-run validation fast and predictable. If data is missing or uncertain, rules return `skip` rather than failing.

## Rules Implemented

| Rule | Name | V1 behavior |
| --- | --- | --- |
| R1 | Project type set and valid | Fails missing/invalid project type when the project type field is available. |
| R3 | PM assigned | Fails when no PM is assigned after `PM_AUDIT_PM_ASSIGNMENT_GRACE_HOURS`; skips when project created timestamp is unavailable. |
| R6 | Comfort Advisor / Sold By set | Uses `PM_AUDIT_SOLD_BY_FIELD_NAMES`; skips when none of the configured fields exist, fails when a configured field exists but is empty. Requires Jane field confirmation before live use. |
| R7 | Permit field present | Uses `PM_AUDIT_PERMIT_FIELD_NAMES`; skips when none of the configured fields exist, fails when a configured field exists but is empty. Requires Jane field confirmation before live use. |
| R11 | Tasks applied / task count present | Uses task count as a v1 proxy for task-template application; fails when no project tasks exist after `PM_AUDIT_TASK_TEMPLATE_GRACE_HOURS`; skips when the timestamp needed for the grace period is unavailable. |
| R13 | Every task has an assignee | Fails when any project task has no assignee. |
| R15 | No stale open tasks | Fails when an open task is more than `PM_AUDIT_TASK_OVERDUE_DAYS` past due. Open tasks without due dates are skipped and counted in the summary rather than treated as failures. |
| R17 | Completed projects are closed out | Fails when a completed project still has open tasks. |

R13 and R17 are structurally the best live candidates from the first dry-run, but PM Audit should remain dry-run until Jane reviews more validation output. R1/R3/R6/R7/R11/R15 need more data confirmation before live alerts.

## Rules Intentionally Skipped

These are not implemented in v1 because discovery found missing, unsafe, or business-unconfirmed data:

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

## Slack / Dry-Run Output

PM output is grouped by PM and excludes customer names, addresses, phone numbers, emails, raw notes, raw descriptions, and customer summaries.

Example:

```text
PM Audit, 2026-06-24

Jane:
Project #127623147 | Missing permit field | Permit | install Jun 28 | https://go.servicetitan.com/#/Project/Index/127623147

Gerson:
Project #127623148 | Task overdue | Task #884 | due Jun 20 | https://go.servicetitan.com/#/Project/Index/127623148

Summary: 25 projects evaluated, 2 fails, 6 skips.
Top fail: R15 No stale open tasks (1).
Top skip: Project created timestamp unavailable. (4).
```

The PM agent uses `SLACK_ALERT_CHANNEL_ID` only. It does not create or require BU-specific Slack channels.
Dry-run summaries also include total projects evaluated, total failures/skips, top fail rules, and top skip reasons to help tune field mappings without sending Slack.
