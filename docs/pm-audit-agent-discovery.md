# PM Audit Agent Discovery

Discovery date: 2026-06-24

Scope: read-only ServiceTitan discovery for a future Project Management Audit Agent. No ServiceTitan writes, Slack sends, Render env changes, or live PM rules were made.

Implementation follow-up: PM Audit Agent v1 is documented in `docs/pm-audit-agent.md`. It implements only the data-available v1 rules from this discovery and remains disabled/dry-run by default.

PM dry-run uses `PM_AUDIT_DRY_RUN=true`, which is separate from `SERVICE_TITAN_AUDIT_DRY_RUN=true`. Do not set `SERVICE_TITAN_AUDIT_DRY_RUN=true` globally on a live Render service for PM validation unless intentionally pausing live ServiceTitan Sales/HVAC/Plumbing alerts.

## Existing Extension Points

- Config: add future `PM_AUDIT_ENABLED=false` and timing thresholds in `marketing_os_agent/config.py`.
- ServiceTitan client: add project/task-safe fetchers in `marketing_os_agent/clients/servicetitan.py`; reuse current auth, pagination, field sanitization, and endpoint-disable patterns.
- Domain rules: add a separate scoped PM ruleset rather than changing Sales, HVAC, or Plumbing rule behavior.
- CLI/app: add discovery and one-time dry-run commands in `marketing_os_agent/__main__.py` and `marketing_os_agent/app.py`.
- Persistence: reuse or extend existing ServiceTitan violation storage in `marketing_os_agent/persistence.py` if PM violations share dedupe/Slack semantics.
- Slack: reuse the existing single-channel ServiceTitan alert formatter and keep messages PII-safe.
- Tests/docs: add PM rules, scope, client mapping, and no-PII tests before any live rollout.

## Endpoints Checked

| Endpoint | Result | PM relevance |
| --- | --- | --- |
| `/settings/v2/tenant/{tenant}/business-units` | Available | Internal BU scope/context. |
| `/jpm/v2/tenant/{tenant}/job-types` | Available | Job type scope/exclusions. |
| `/jpm/v2/tenant/{tenant}/projects` | Available | Core PM project data. |
| `/jpm/v2/tenant/{tenant}/project-types` | Available | Exact project type IDs/names. |
| `/jpm/v2/tenant/{tenant}/project-statuses` | Available | Project status IDs/names. |
| `/taskmanagement/v2/tenant/{tenant}/tasks` | Available | Project tasks; `projectId` filter appears usable. |
| `/jpm/v2/tenant/{tenant}/project-tasks` | 404 | Not available. |
| `/jpm/v2/tenant/{tenant}/tasks` | 404 | Not available. |
| `/jpm/v2/tenant/{tenant}/permits` | 404 | Not available as a standalone endpoint. |
| `/forms/v2/tenant/{tenant}/submissions` | Available, but owner filters appeared unscoped | Do not use as rule evidence until scoped owner filtering is proven. |
| `/equipments/v2/tenant/{tenant}/installed-equipment` | 404 | Not available for PM equipment-registration proof. |
| `/accounting/v2/tenant/{tenant}/invoices` | Available | Some rows expose `projectId`; project filters were not reliable in the sample. |
| `/accounting/v2/tenant/{tenant}/payments` | Available | Payment rows did not expose `projectId` in the sample. |
| `/accounting/v2/tenant/{tenant}/export/invoice-items` | Available, but filtered sample returned overbroad pages | Do not use as PM deposit evidence until scoped filtering is proven. |
| `/accounting/v2/tenant/{tenant}/export/payments` | Available, but filtered sample returned overbroad pages | Do not use as PM deposit evidence until scoped filtering is proven. |
| `/jpm/v2/tenant/{tenant}/jobs` | Available | Linked install jobs expose `projectId`, `businessUnitId`, `jobTypeId`, `invoiceId`, `soldById`. |
| `/settings/v2/tenant/{tenant}/technicians` | Available | Internal person ID/name mapping. |
| `/settings/v2/tenant/{tenant}/employees` | Available | Internal person ID/name mapping. |
| `/jpm/v2/tenant/{tenant}/projects/{id}/tasks|jobs|forms|appointments` | 404 | Project subresources not available through these guessed paths. |

## Data Fields Found

Project type values:

| ID | Name |
| --- | --- |
| `63812999` | Standard Install |
| `63813000` | Construction & Remodel |

Project status values:

| ID | Name |
| --- | --- |
| `22936526` | Pending Scheduling |
| `22936527` | Scheduled |
| `22936528` | In Progress |
| `22936529` | Completed |
| `22936530` | Hold |
| `22936531` | Canceled |
| `63812996` | Bid |

Project fields observed:

- `id`, `number`
- `projectTypeId`
- `status`, `statusId`, `subStatus`, `subStatusId`
- `businessUnitIds`
- `projectManagerIds`
- `jobIds`
- `createdOn`, `modifiedOn`
- `startDate`, `targetCompletionDate`, `actualCompletionDate`
- `customFields`

Useful project custom field names observed:

- `Sold by`
- `Permit`
- `City Inspection`
- `Equipment Status`
- `ECC`
- `Under HOA`
- `HOA Approval`
- `Asbestos EXISTS in OLD system`
- `Asbestos Abatement`
- `Review Requested`
- `Team`

Task fields observed:

- `id`, `taskNumber`, `projectId`, `jobId`, `jobNumber`
- `name`, `description`
- `assignedToId`, `involvedEmployeeIdList`
- `completeBy`, `createdOn`, `modifiedOn`, `closedOn`
- `isClosed`, `status`, `active`
- `employeeTaskTypeId`, `employeeTaskResolutionId`, `employeeTaskSourceId`
- `comments`, `attachments`, `subTasksData`

Task keyword signals found in the bounded sample:

- permit tasks: 10
- payment tasks: 7
- install tasks: 24
- deposit tasks: 1
- HOA tasks: 3
- equipment tasks: 2
- rebate tasks: 6

## Missing Or Unsafe Data

- PM assigned timestamp was not found. `projectManagerIds` is available, but the assignment-change time is not.
- Status last-modified timestamp was not found separately from project `modifiedOn`.
- Standalone permit endpoint was not available.
- Forms endpoint returned records, but project owner filtering appeared unscoped; do not use Homeowner Authorization or Installation Completion Report forms as rule evidence yet.
- Installed equipment endpoint returned 404; equipment registration cannot be verified directly.
- Payments did not expose `projectId` in the sample. Payment-order rules need more work through invoices/linked jobs.
- Invoice project filtering appeared unreliable; broad invoice records sometimes include `projectId`, but project-filtered calls returned unscoped pages.
- Job-filtered invoice calls were reliable in a bounded deposit probe. The safe relationship is project `jobIds` -> `/accounting/invoices?jobId=...`.
- Export invoice-item and payment endpoints returned overbroad pages in a bounded deposit probe and should not be used for PM rule evidence yet.
- Scoped invoice rows expose total, balance, invoice date, `paidOn` when fully paid, and sometimes `depositedOn`. Partial payments can be inferred from `total - balance`, but partial payment date was not reliably available.
- Payment moved from a deposit invoice to an install invoice was not detectable from the safe sampled fields.
- Task template applied was not found directly. Task presence can be checked, but template identity cannot.
- On-hold reason text was not found. `Hold` status and `subStatusId` exist, but reason semantics need confirmation.

## Sanitized Project Sample

No customer names, addresses, phones, emails, or raw notes were collected.

| Project # | Type | Status | PM assigned | BU IDs | Jobs | Tasks | Open tasks | Custom signals |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |
| 48399388 | Standard Install | Completed | Yes | 1809 | 3 | 2 | 1 | Permit, City Inspection, Equipment Status, Sold by |
| 64209810 | Standard Install | Completed | Yes | 1809 | 5 | 6 | 2 | Permit, HOA, Asbestos, Sold by |
| 67212510 | Standard Install | Completed | Yes | 1809 | 8 | 4 | 2 | Permit, HOA, Asbestos, Sold by |
| 112727079 | Standard Install | Completed | Yes | 1809, 64313020, 64569731 | 7 | 0 | 0 | Permit, HOA, Review Requested, Sold by |
| 131013978 | Standard Install | Pending Scheduling | Yes | 1809 | 2 | not sampled | not sampled | Sold by |
| 130980562 | Standard Install | Scheduled | Yes | none shown | 2 | not sampled | not sampled | none shown |

Install-like job context seen in recent raw jobs:

- HVAC Install BU: `1809`
- Plumbing Install BU: `64313020`
- Electrical Install BU: `64569731`
- HVAC Installation job type: `1815`
- Water Heater Installation job type: `64570637`
- Plumbing Installation job type: `130772825`
- Install jobs often carry `projectId` and `invoiceId`.

## PM Rules Matrix

| Rule | Name | Core | Data needed | Endpoint/field found | Readiness | False-positive risk | MVP recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | Project type set and valid | Yes | Project type ID/name; approved labels | `/jpm/projects.projectTypeId`, `/jpm/project-types` | Ready, needs Jane confirmation on exact labels | Low after exact IDs are configured | Implement in v1 |
| R3 | PM assigned | Yes | Assigned PM | `/jpm/projects.projectManagerIds` | Ready for presence check; assignment timestamp missing | Low for presence, medium for grace-window timing | Implement presence in v1; defer timestamp SLA |
| R4 | Status set and current | Yes | Status/status age | `/jpm/projects.status/statusId`; status-specific last-updated field when present | Ready for status presence; stale check only when status timestamp is available | Low for presence, medium for stale check if timestamp semantics vary | First live allowlist candidate |
| R6 | Comfort Advisor set | Yes | Project Details `Sold By` | Project Details fields parsed via `PM_AUDIT_SOLD_BY_FIELD_NAMES` | Re-pointed in v1.1 after Jane confirmation; needs another dry-run to confirm realistic empty rates | Medium until revalidated | Keep dry-run until Project Details Sold By empty rate is reviewed |
| R7 | Permit number present | Yes | Project Details `PERMIT` section | Project Details PERMIT fields parsed via `PM_AUDIT_PERMIT_FIELD_NAMES` | Re-pointed in v1.1 after Jane confirmation; separate ServiceTitan Permits module is not used | Medium/high until revalidated and permit-owner exception is defined | Keep skip/dry-run until Project Details PERMIT empty rate is reviewed |
| R8 | HOA approval status set | No | HOA applicability and approval | Project custom fields `Under HOA`, `HOA Approval` | Partial, needs Jane confirmation | Medium; only applies to HOA projects/zip list | Dry-run only |
| R9 | Asbestos check recorded | No | Asbestos applicability/status | Project custom fields `Asbestos EXISTS in OLD system`, `Asbestos Abatement` | Partial, needs `asbestos_year_cutoff` | Medium | Dry-run only |
| R10 | Review-requested flag set | No | Review flag | Project custom field `Review Requested` | Ready if field expectation is confirmed | Medium | Needs business confirmation |
| R11 | Task template or tasks applied | Yes | Template or task list | `/taskmanagement/tasks?projectId=...`; template ID not found | Partial | Low for task-count check, medium for template identity | Implement task-count check in v1 |
| R13 | Every task has assignee | Yes | Task assignee | `/taskmanagement/tasks.assignedToId` | Ready | Low | Implement in v1 |
| R15 | No stale tasks | Yes | Task due/status/closed | `/taskmanagement/tasks.completeBy/isClosed/status/closedOn` | Ready | Low/medium depending overdue threshold | Implement in v1 |
| R16 | On-hold has a reason | No | Hold status and reason | Project status `Hold`; subStatusId found, reason text not found | Partial | Medium/high without reason mapping | Skip for now |
| R17 | Completed projects are closed out | Yes | Completed status, completion date, closeout evidence | `/jpm/projects.status/actualCompletionDate`; forms unsafe | Partial | Medium if closeout requires ICR/form | Implement simple status/date check dry-run |
| R18 | Payment order | Yes | Invoice/payment sequence | Invoices available; payments lack projectId; project invoice filters unreliable | Partial | High | Skip for now or dry-run after job-linked invoice mapping |
| R19 | Homeowner Authorization timing | Yes | Authorization form timestamp | Form name observed, but forms unscoped | Not available safely | High | Skip for now |
| R20 | Installation Completion Report green | Yes | ICR status | Form name observed, but forms unscoped | Not available safely | High | Skip for now |
| R21 | Equipment registered | No | Equipment registration/status | Project custom field `Equipment Status`; installed equipment endpoint 404 | Partial | Medium/high | Skip for now |
| R22 | Deposit before install | Yes | Deposit/payment amount/date, install date, linked invoices | Project `jobIds` -> `/accounting/invoices?jobId=...` gives invoice total/balance and sometimes `paidOn`/`depositedOn`; payment/export filters were not reliable | Partial | Medium/high because partial payment date and moved-payment history may be missing | Dry-run only; skip when invoice/payment linkage is unclear |
| R23 | Permit before install | Yes | Permit date/status and install date | Project custom field `Permit`; project `startDate` | Partial | Medium until permit field value/date semantics are checked | Dry-run only |

## Recommended PM Audit V1 Scope

Implemented rules remain available for dry-run validation:

1. R1 `pm_project_type_invalid_or_missing`
2. R3 `pm_missing_project_manager`
3. R4 `pm_status_set_and_current`
4. R6 `pm_missing_comfort_advisor`
5. R7 `pm_missing_permit_number`
6. R11 `pm_task_template_or_tasks_missing`
7. R13 `pm_task_missing_assignee`
8. R15 `pm_stale_open_task`
9. R17 `pm_completed_project_not_closed_out`

First scheduled live allowlist:

```json
["R1","R4","R13","R17"]
```

Keep R3/R6/R7/R11/R15 out of scheduled live messages until Jane reviews more dry-run output.

Skip for initial live rollout:

- R8 HOA approval until HOA applicability rules and zip list are confirmed.
- R9 asbestos until cutoff year and field semantics are confirmed.
- R10 review-requested until Jane confirms when the flag is required.
- R16 on-hold reason until substatus/reason mapping is confirmed.
- R18 payment order until reliable project-linked payment mapping exists.
- R19 Homeowner Authorization timing until forms are safely project/job-scoped.
- R20 Installation Completion Report green until forms are safely project/job-scoped.
- R21 equipment registered until ServiceTitan exposes reliable equipment registration or Jane accepts `Equipment Status` as evidence.
- R22 deposit before install until project/job-linked invoice/payment mapping is proven.
- R23 permit before install until permit field value/date semantics are confirmed.

## Future Slack Summary Proposal

Do not send Slack during discovery. PM Slack output should be compact, grouped by PM, and PII-safe:

```text
📋 PM Audit — Jun 24

Jane
• Project #127623147 — Missing permit number
  Field: Permit
  Action: Fill Project Details PERMIT information

Gerson
• Project #127623148 — PM Pre-Scheduling overdue
  Field: Task #884
  Action: Update or close overdue task
  Due: Jun 20

Summary: Jane 1 issue · Gerson 1 issue
```

Allowed identifiers:

- project ID / project number
- internal PM/employee name
- task number
- short rule issue
- field/task name
- due date or install date
- ServiceTitan link

Do not include customer names, addresses, phone numbers, emails, raw notes, raw descriptions, or form comments.

## Questions For Jane

1. Confirm project type IDs/names for in-scope PM installs: `63812999 / Standard Install` and `63813000 / Construction & Remodel`.
2. Confirm exact out-of-scope business units/job types for service, warranty, recall, maintenance, free diagnostic, and internal/R&D.
3. Revalidate Project Details `Sold By` empty rates after the v1.1 field re-point.
4. Revalidate Project Details `PERMIT` empty rates, and confirm whether permit presence/status/date is enough for R7.
5. Confirm whether `Under HOA` and `HOA Approval` define HOA applicability/status, and provide `hoa_zip_list` if zip-based logic is required.
6. Provide `asbestos_year_cutoff` and define how `Asbestos EXISTS in OLD system` / `Asbestos Abatement` should be interpreted.
7. Define when `Review Requested` is required.
8. Confirm whether task count is an acceptable proxy for task template applied.
9. Define on-hold reason source: status, substatus, task, or a custom field.
10. Define completed project closeout: project `Completed`, `actualCompletionDate`, Installation Completion Report, equipment status, or all of these.
11. Confirm whether payment/deposit rules should wait until invoices/payments can be reliably linked to projects.
