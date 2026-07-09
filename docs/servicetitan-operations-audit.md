# ServiceTitan Operations Audit Agent

The ServiceTitan Operations Audit Agent continuously audits recent ServiceTitan jobs for operational compliance issues. It is part of the existing `marketing_os_agent` process and is disabled unless `SERVICE_TITAN_AUDIT_ENABLED=true`.

It does not replace Agent 1. Notion task dispatching, Slack task reminders, scheduled reports, and campaign health checks continue to run through their existing code paths.

The audit architecture is moving away from one large generic dispatcher audit that tries to apply every rule to every job. The production path is now split into smaller scoped business-unit agents/rulesets:

- Sales / Comfort Advisor Audit.
- HVAC Service Audit.
- Plumbing Service Audit.
- Project Management / Install Audit.

Sales / Comfort Advisor Audit is implemented for initial production monitoring. HVAC Service Audit is implemented as the next scoped ruleset for dry-run validation and reviewed rollout. The previous Technician Compliance, Dispatcher / Job Quality, and handbook-backed rule families remain available behind explicit flags for continued testing, but they should stay disabled during Sales-only or HVAC-only production validation unless the scope has been reviewed.

## Architecture

- `marketing_os_agent/clients/servicetitan.py` handles OAuth client credentials, `ST-App-Key`, API calls, pagination, token caching, and conservative ServiceTitan job parsing.
- `marketing_os_agent/domain/service_titan_rules.py` contains the rule engine, the Sales / Comfort Advisor ruleset, legacy audit rules, and handbook-backed rule evaluators.
- `marketing_os_agent/domain/service_titan_handbook.py` contains the handbook-backed rule matrix: rule ID, handbook source, business reason, data requirements, current availability, routing, delivery mode, and default enabled state.
- `marketing_os_agent/domain/service_titan_audit.py` coordinates polling, rule execution, durable violation storage, Slack alerting, and retry behavior.
- `marketing_os_agent/persistence.py` stores audit violations in SQLite.
- `AgentApp` starts a separate ServiceTitan audit thread only when the feature is enabled.

## Safe Env Patterns

WARNING: Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` only for one-off validation commands or a deliberately paused dry-run environment. Do not set this globally on the live Render service unless you intentionally want to stop live Sales/HVAC/Plumbing ServiceTitan alerts.

`SERVICE_TITAN_AUDIT_DRY_RUN=true` affects the ServiceTitan audit send path globally. It suppresses ServiceTitan Slack alerts, violation writes, dedupe writes, and checkpoint advancement. `PM_AUDIT_DRY_RUN=true` is separate and applies only to the PM Audit command.

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

For PM-only dry-run in Render, prefer `PM_AUDIT_ENABLED=true` and `PM_AUDIT_DRY_RUN=true` while leaving existing ServiceTitan live audit dry-run settings unchanged. Do not copy local validation env blindly into Render.

Current rollout status:

- Sales / Comfort Advisor Audit: live with strict Render scope.
- HVAC Service Audit: implemented and disabled by default; only the payment rule is a controlled-rollout candidate.
- Plumbing Service Audit: implemented and disabled by default; the options rule is a controlled-rollout candidate.
- Installer Audit: implemented as a separate read-only v3 ruleset, disabled and dry-run by default.
- PM Audit: implemented, disabled by default, and dry-run first only.
- Weekly Summary: implemented and disabled by default.

Do not do:

- Do not set `SERVICE_TITAN_AUDIT_DRY_RUN=true` on the live Render service unless intentionally pausing live ServiceTitan alerts.
- Do not enable `PM_AUDIT_ENABLED=true` with `PM_AUDIT_DRY_RUN=false` until Jane approves live PM alerts.
- Do not enable photos/forms/arrival rules until API data is confirmed reliable.
- Do not remove Sales scope from `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` when adding HVAC or Plumbing scope.

## Sales / Comfort Advisor Audit

The Sales / Comfort Advisor Audit applies only to jobs that match Sales scope. It is controlled by `SALES_COMFORT_ADVISOR_AUDIT_ENABLED`, which defaults to `true`.

The default Sales scope uses generic Sales/Comfort Advisor workflow language such as `sales`, `comfort advisor`, `advisor`, `estimate`, `consultation`, and `replacement`. Production tenants should run scope discovery and configure exact business unit, job type, tag, campaign/lead source, department, or workflow values in `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON`.

Some ServiceTitan tenants expose only numeric `businessUnitId` / `jobTypeId` values on job payloads and do not expose workflow names. In that case, Sales rules intentionally stay `insufficient_data` until `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` is configured with the discovered Sales IDs and `"workflows": null`. This prevents the agent from guessing that every completed numeric-ID job is a Sales job.

Sales rules:

- `sales_options_fewer_than_three`: closed Sales jobs must show at least three estimate/option records. If estimate data is unavailable, the result is `insufficient_data`.
- `sales_photos_missing`: closed Sales jobs must include photos or image attachments. If the photos/attachments source is unavailable, the result is `insufficient_data`.
- `sales_arrival_after_first_half`: Sales appointments should arrive before the first half of the appointment window ends. If the appointment window or arrival time is unavailable, the result is `insufficient_data`.

Sales data mapping:

- Options use `/sales/v2/tenant/{tenant}/estimates`, `/sales/v2/tenant/{tenant}/opportunities`, or job-level `estimateIds` when ServiceTitan exposes them on the job payload. The rule counts real estimate/opportunity/option records only; it does not infer options from invoice line items or notes.
- Photos use job attachments when the job attachment endpoint is available. Form submission image attachments can count only when the form records are safely scoped to the job. Broad tenant-level form pages are treated as unavailable instead of evidence.
- Arrival uses `/jpm/v2/tenant/{tenant}/appointments` for arrival-window start/end and `/dispatch/v2/tenant/{tenant}/appointment-assignments` or payroll job timesheets for actual `arrived_at` timestamps.
- With `SERVICE_TITAN_AUDIT_DEBUG_FIELDS=true`, Sales jobs log sanitized `servicetitan_sales_field_availability` entries with job ID/job number, scope labels, related-record counts, availability booleans, and per-rule `insufficient_data` reasons. Customer names, addresses, phone numbers, emails, raw notes, secrets, and tokens are not logged.

Sales rules return `not_applicable` for HVAC Service, Plumbing Service, Project Management, install, admin, canceled, internal, or otherwise non-Sales jobs. `not_applicable` and `insufficient_data` never send Slack alerts.

Sales alerts are immediate per audit cycle when all of these are true:

- `SERVICE_TITAN_AUDIT_ENABLED=true` for continuous polling, or the one-time command is run.
- `SERVICE_TITAN_AUDIT_DRY_RUN=false`.
- The Sales rule result is `fail`.
- Slack is configured.
- The violation has not already been successfully alerted.

Sales alerts are not Friday-only or weekly-only. Email alerts are not implemented for ServiceTitan audits.

## Weekly Violation Summary

The weekly ServiceTitan violation summary is separate from immediate operational alerts. Immediate alerts still send during each audit polling cycle when a new real `fail` violation is detected. The weekly summary is an additional rollup of already persisted audit violations.

The weekly summary:

- Is disabled by default.
- Reads existing `service_titan_audit_violations` SQLite records.
- Does not call ServiceTitan.
- Does not re-run the audit rules.
- Posts only to `SLACK_ALERT_CHANNEL_ID`.
- Groups counts by Business Unit label, Business Unit ID/name, ruleset, rule ID, severity, and violation status.
- Does not include customer names, addresses, phone numbers, emails, or raw notes.
- Uses a durable dedupe key for the period so the same weekly period is not sent repeatedly.

Configure it with:

```env
SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED=false
SERVICE_TITAN_WEEKLY_SUMMARY_DAY=MON
SERVICE_TITAN_WEEKLY_SUMMARY_HOUR=8
SERVICE_TITAN_WEEKLY_SUMMARY_LOOKBACK_DAYS=7
```

Manual dry-run test:

```bash
SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-weekly-summary-once
```

WARNING: Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` here as a command-level one-off override. Do not set it globally on the live Render service unless intentionally pausing live ServiceTitan alerts.

The manual command prints the summary. When `SERVICE_TITAN_AUDIT_DRY_RUN=true`, it does not send Slack and does not mark the weekly summary as sent. When dry-run is false and weekly summary sending is enabled or the command is forced manually, Slack delivery uses the same `SLACK_ALERT_CHANNEL_ID` as immediate ServiceTitan alerts.

Example summary:

```text
ServiceTitan Weekly Audit Summary
Period: 2026-06-10 -> 2026-06-17

HVAC Sales / Comfort Advisors
BU ID: 1812
BU Name: HVAC - Sales
Ruleset: Sales / Comfort Advisor Audit
- sales_options_fewer_than_three [high] open: 12
- sales_arrival_after_first_half [medium] open: 3

Plumbing Sales
BU ID: 64326403
BU Name: Plumbing - Sales
Ruleset: Sales / Comfort Advisor Audit
- sales_options_fewer_than_three [high] open: 8
- sales_arrival_after_first_half [medium] open: 2

Installs
BU ID: 1809,64313020
BU Name: Installer Audit
Ruleset: Installer Audit
- install_job_not_marked_complete [high] open: 1
- install_completion_form_not_completed [high] open: 1

Totals:
- Violations: 27
- High: 22
- Medium: 5
- open: 27
```

## Installer Audit

The Installer Audit is a separate read-only v3 ruleset. It does not run Sales options rules, Sales first-half arrival rules, PM permit rules, or generic dispatcher rules. It uses `INSTALL_AUDIT_DRY_RUN`; do not use `SERVICE_TITAN_AUDIT_DRY_RUN` for Installer Audit validation.

Scope is intentionally strict because scope was the main v3 risk. A job is in scope only when the job type name or business unit name contains `Installation`, case-insensitive, or when the job business unit ID is one of the configured install IDs. Scope uses only job type and business unit fields; it does not use free text, notes, form names, customer summaries, or the presence of an `Installation Completion Form`.

Default scope:

```env
INSTALL_AUDIT_JOB_TYPE_MATCH_KEYWORDS=["Installation"]
INSTALL_AUDIT_BUSINESS_UNIT_IDS=["1809","64313020"]
ST_BU_INSTALLERS=1809,64313020
```

Excluded by scope: Service Call, Maintenance, Warranty, Recall, Sales/Estimate, standby, internal placeholders, and non-install jobs. If job type and business unit fields are missing and the configured BU IDs do not match, Installer Audit skips the job instead of guessing.

Rules:

- `I1 / install_job_not_marked_complete` high: final install work appears done, progress is 100%, completion form is done, or full payment is in, but job status is not Completed.
- `I2 / install_completion_form_not_completed` high: final install day is done or job is complete, but Installation Completion Form is missing or not completed. Skips when form status is unavailable.
- `I3 / install_authorization_form_not_completed` high: installation has begun, but Homeowner Authorization Form is missing or not completed. Skips when form status or crew-start signal is unavailable.
- `I4 / install_arrival_not_marked` medium: scheduled start has passed or job is in progress/complete, but no arrival timestamp is recorded. Skips when the arrival field is unavailable.
- `I5 / install_arrived_late` medium: arrival is more than `INSTALL_AUDIT_ARRIVAL_GRACE_MIN` after scheduled start. Missing arrival is handled by I4.
- `I6 / install_meal_break_not_taken` high: per technician/day CA compliance check for shifts over 5 hours without a 30-minute meal break, or over 10 hours without a second 30-minute break. Skips with `timesheet_breaks_unavailable` when time/break data is unavailable.
- `I7 / install_deposit_not_collected_before_day1` reminder: install starts today/tomorrow and no structured deposit payment is recorded, unless financed or deposit-waived/customer-arranged. Skips when payment/deposit relationship is unclear.
- `I8 / install_payment_milestone_short` high/medium: final day unpaid balance or day-1 50% milestone short. Skips financed jobs, same-day end-of-day money windows, and unclear invoice/payment relationships.
- `I9 / install_photos_missing` medium: completed install has fewer than `INSTALL_AUDIT_COMPLETION_PHOTOS_MIN` attached photos. Skips when photo/attachment count is unavailable.
- `I10 / install_materials_not_scanned` medium: completed install has no scanned materials. Skips when material/Ply data is unavailable or the job is bare-labor/no-material.
- `I11 / install_equipment_not_registered` medium: completed install has missing equipment registration/labels. Skips when equipment registration data is unavailable.
- `I12 / install_review_not_requested` low: completed install has no review-requested flag. Skips when the review-requested field is unavailable.

`INSTALL_AUDIT_RULE_IDS_JSON=[]` runs all v3 rules I1-I12. Use explicit values like `["I1"]` or `["I1","I2","I3"]` for targeted validation.

Render env for a dry-run validation:

```env
INSTALL_AUDIT_ENABLED=true
INSTALL_AUDIT_DRY_RUN=true
INSTALL_AUDIT_RUN_ON_STARTUP=false
INSTALL_AUDIT_SCHEDULE_ENABLED=false
INSTALL_AUDIT_SLACK_CHANNEL_ID=
INSTALL_AUDIT_RULE_IDS_JSON=[]
INSTALL_AUDIT_MAX_APPOINTMENTS=100
INSTALL_AUDIT_LOOKBACK_DAYS=14
INSTALL_AUDIT_LOOKAHEAD_DAYS=2
INSTALL_AUDIT_RUN_HOUR=8
INSTALL_AUDIT_RUN_MINUTE=0
INSTALL_AUDIT_WEEKDAYS_ONLY=true
```

Installer Audit can run manually with:

```bash
python3 -m marketing_os_agent install-audit-once
python3 -m marketing_os_agent install-audit-test-slack
```

It is wired into `python -m marketing_os_agent run` when `INSTALL_AUDIT_ENABLED=true`. `INSTALL_AUDIT_RUN_ON_STARTUP=true` runs one startup audit after the app starts. `INSTALL_AUDIT_SCHEDULE_ENABLED=true` runs at `INSTALL_AUDIT_RUN_HOUR` / `INSTALL_AUDIT_RUN_MINUTE`, weekdays only when `INSTALL_AUDIT_WEEKDAYS_ONLY=true`. Automatic runs dedupe by local date; manual `install-audit-once` is independent.

Slack alert shape:

```text
HIGH - Installs: Job Not Marked Complete
Technician: <crew lead>
Appointment: <date, window>
Arrived: <time or unavailable>
Invoice: $<total> total / $<balance> balance
Issue: <one concrete sentence naming the value>
Action: <what to check / who to coach>
Open in ServiceTitan: https://go.servicetitan.com/#/Job/Index/<job_id>
```

Known skip-safe fields from current validation work: form status, timesheet/break data, job-scoped photo/attachment counts, material/Ply scan data, equipment registration, and review-requested fields may be unavailable depending on ServiceTitan tenant access. When unavailable, the rule returns `skip` with clear reasons such as `form_status_unavailable`, `timesheet_breaks_unavailable`, `photo_count_unavailable`, `materials_scan_unavailable`, `equipment_registration_unavailable`, or `review_requested_field_unavailable`; it does not send Slack.

## HVAC Service Audit

The HVAC Service Audit applies only to jobs that match HVAC Service scope. It is controlled by `HVAC_SERVICE_AUDIT_ENABLED`, which defaults to `false`.

The default HVAC scope uses visible HVAC/service workflow context such as `hvac`, `heating`, `cooling`, `air conditioning`, `service`, `diagnostic`, `repair`, `maintenance`, and `tune up`. Production tenants should run scope discovery and configure exact business unit, job type, tag, department, trade, or workflow values in `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON`.

If a tenant exposes only numeric `businessUnitId` / `jobTypeId` values and no workflow or name context, HVAC rules intentionally stay `insufficient_data` until reviewed HVAC IDs are configured and `"workflows": null` is set. This prevents the agent from guessing which numeric jobs are HVAC Service.

HVAC rules:

- `hvac_options_fewer_than_three`: closed HVAC Service jobs must show at least three estimate/option records. If estimate data is unavailable, the result is `insufficient_data`.
- `hvac_payment_missing_on_completed_job`: closed HVAC Service jobs with a positive invoice total must show payment, paid invoice status, or zero balance. If invoice/payment data is unavailable, the result is `insufficient_data`.
- `hvac_diagnosis_form_missing`: closed HVAC Service jobs must include a completed diagnosis/service form only when job-scoped form submission data is available. Unscoped tenant-level form pages stay `insufficient_data`.
- `hvac_required_photos_missing`: closed HVAC Service jobs must include required photos only when job-scoped photos/attachments are available. This rule should stay disabled until dry-run proves the tenant exposes scoped job photos.
- `hvac_arrival_outside_window`: HVAC Service appointments should arrive within the configured first-window threshold. If arrival-window or actual-arrival data is unavailable, the result is `insufficient_data`.

HVAC data mapping:

- Options use the same safe ServiceTitan option sources as Sales: `/sales/v2/tenant/{tenant}/estimates`, `/sales/v2/tenant/{tenant}/opportunities`, or job-level `estimateIds`.
- Payment uses invoice/payment fields from `/accounting/v2/tenant/{tenant}/invoices`. It passes when payment is visible, invoice balance is zero or less, or invoice status indicates paid.
- Diagnosis forms use `/forms/v2/tenant/{tenant}/submissions?jobId=...` only when the response is safely job-scoped. Broad or unscoped form pages are not treated as evidence.
- Photos use `/jpm/v2/tenant/{tenant}/jobs/{job}/attachments` or scoped form image attachments only when those sources are available and job-scoped.
- Arrival uses `/jpm/v2/tenant/{tenant}/appointments` and `/dispatch/v2/tenant/{tenant}/appointment-assignments` when those endpoints expose arrival timestamps. Payroll timesheets remain a generic enrichment source outside HVAC-only optimized validation, but HVAC-only mode does not depend on them.
- With `SERVICE_TITAN_AUDIT_DEBUG_FIELDS=true`, HVAC jobs log sanitized `servicetitan_hvac_field_availability` entries with job ID/job number, scope labels, related-record counts, availability booleans, payment/form/photo counts, and per-rule `insufficient_data` reasons. Customer names, addresses, phone numbers, emails, raw notes, secrets, and tokens are not logged.

HVAC Service validation found this strict candidate scope in this tenant:

```env
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rulesets":{"HVAC Service Audit":{"applies_to":{"business_unit_ids":["1810"],"job_type_ids":["1933","6807409","7020890","7131342"],"workflows":null,"statuses":["Completed","Closed"]}}}}
```

Those IDs mean `1810 / HVAC - Service` with `1933 / HVAC Diagnostic`, `6807409 / Tune Up`, `7020890 / HVAC Repair`, and `7131342 / Diagnostics Members`. Keep `1812 / 1816` HVAC Sales, `1809 / 1815` HVAC Install, `64326403 / 54086644 / 123562931` Plumbing Sales, and `64315277 / 112338076` Plumbing Service excluded from HVAC Service scope.

This is future dry-run configuration, not live activation. Keep live HVAC alerts disabled until the business confirms whether those four HVAC Service job types require three options and whether the payment rule is acceptable for live alerts. If HVAC dry-run testing is enabled, keep these rules disabled:

```env
SERVICE_TITAN_DISABLED_RULE_IDS_JSON=["sales_photos_missing","hvac_required_photos_missing","hvac_diagnosis_form_missing","hvac_arrival_outside_window"]
```

Enable `hvac_required_photos_missing` later only after dry-run proves scoped job photos are available. Enable `hvac_diagnosis_form_missing` later only after dry-run proves scoped job forms are available. Enable `hvac_arrival_outside_window` later only after ServiceTitan exposes a reliable job/appointment arrival timestamp; the targeted sample had appointment windows but no `arrived_at` values.

## Plumbing Service Audit

The Plumbing Service Audit applies only to jobs that match Plumbing Service scope. It is controlled by `PLUMBING_SERVICE_AUDIT_ENABLED`, which defaults to `false`. It is implemented for dry-run validation only and is not approved for live alerts yet.

Plumbing Service validation found this strict candidate scope in this tenant:

```env
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rulesets":{"Plumbing Service Audit":{"applies_to":{"business_unit_ids":["64315277"],"job_type_ids":["57804592","64569478","112338076"],"workflows":null,"statuses":["Completed","Closed"]}}}}
```

Those IDs mean `64315277 / Plumbing - Service` with `57804592 / Water Heater Maintenance`, `64569478 / Water Heater Diagnostic`, and `112338076 / Plumbing Diagnostic`. Keep `64326403 / 54086644 / 64569350 / 123562931` Plumbing Sales or estimate-only jobs, `64313020 / 64569604 / 64570637` Plumbing Install jobs, and HVAC Service/Sales/Install/Maintenance jobs excluded from Plumbing Service scope.

Do not include cross-BU job types `30209 / Full HVAC/Water heater Maintenance`, `111922608 / Water Heater Repair`, or `112630828 / Plumbing Repair` until the business confirms whether they belong in Plumbing Service audit scope.

Plumbing rules:

- `plumbing_options_fewer_than_three`: closed Plumbing Service diagnostic/estimate jobs must show at least three estimate/option records only after the business confirms this expectation for positive-invoice diagnostic work. The rule excludes `57804592 / Water Heater Maintenance`, structured zero-dollar/no-charge jobs where invoice total and balance are both `0`, and structured sold/performed repair-work visits where invoice line items show the work was already sold. If billing context is unavailable or does not show a positive charge, the rule returns `insufficient_data` rather than failing.
- `plumbing_payment_missing_on_completed_job`: closed Plumbing Service jobs with a positive invoice total must show payment, paid invoice status, or zero balance when structured invoice/payment data is available. In discovery, invoice/payment data was missing for all matched Plumbing Service sample jobs, so keep this disabled.
- `plumbing_diagnosis_form_missing`: closed Plumbing Service jobs must include a completed diagnosis/service form only when job-scoped form submission data is available. Unscoped tenant-level form pages stay `insufficient_data`; keep this disabled.
- `plumbing_required_photos_missing`: closed Plumbing Service jobs must include required photos only when job-scoped photos/attachments are available. Keep this disabled until dry-run proves the tenant exposes scoped job photos.
- `plumbing_arrival_outside_window`: Plumbing Service appointments should arrive within the configured first-window threshold only when actual-arrival timestamps are available. Discovery found appointment windows but no `arrived_at` values, so keep this disabled.

If Plumbing dry-run testing is enabled, use reviewed scope IDs and keep live alerts disabled. Recommended future dry-run disabled rules:

```env
SERVICE_TITAN_DISABLED_RULE_IDS_JSON=["sales_photos_missing","hvac_options_fewer_than_three","hvac_required_photos_missing","hvac_diagnosis_form_missing","hvac_arrival_outside_window","plumbing_payment_missing_on_completed_job","plumbing_required_photos_missing","plumbing_diagnosis_form_missing","plumbing_arrival_outside_window"]
```

## Current-State Report

Current data flow:

1. The one-time command and the audit loop call `ServiceTitanClient.query_recent_jobs(modified_on_or_after)`.
2. The client fetches `/jpm/v2/tenant/{tenant}/jobs` using `modifiedOnOrAfter`, pagination, `ST-App-Key`, and OAuth client credentials.
3. Each job is parsed into a conservative `ServiceTitanJob`, then enriched with related ServiceTitan records where the tenant and scopes expose them.
4. Rule applicability is checked before the rule body runs. If a job is outside the configured scope, the rule returns `not_applicable`.
5. Rules evaluate only mapped fields. If a required source is absent, unavailable, or applicability cannot be determined, the rule returns `insufficient_data`.
6. Only `fail` results can create stored violations and Slack alerts. Dry-run does not write violations, send Slack, or advance the checkpoint.

## Rule Scoping

False positives usually happen when a valid handbook rule is applied to the wrong job type, business unit, workflow, status, tag, or missing ServiceTitan context. The audit agent prevents that with a scope/applicability layer in front of every rule.

A rule can return `fail` only when all of the following are true:

- The rule is enabled.
- The job matches the rule scope.
- Required applicability context is available.
- Required rule data is available.
- The actual violation condition is true.

If the rule does not apply to the job, it returns `not_applicable`. If the agent cannot safely determine whether it applies, or the data source needed to evaluate it is unavailable, it returns `insufficient_data`. Neither status sends Slack.

Every rule carries scope metadata:

- `rule_id`
- `ruleset`
- `title`
- `handbook_source`
- `applies_to_departments`
- `applies_to_business_units`
- `applies_to_trades`
- `applies_to_job_types`
- `applies_to_job_statuses`
- `applies_to_tags`
- `applies_to_campaigns`
- `applies_to_roles`
- `applies_to_workflows`
- `excludes_job_types`
- `excludes_statuses`
- `excludes_tags`
- `excludes_cancellation_reasons`
- `required_context_fields`
- `required_data_fields`
- `alert_routing`
- `default_enabled`

The code also supports exclusion tags and cancellation reasons so canceled, no-access, callback, warranty, no-charge, admin/internal, material-only, and other exception jobs do not generate service-call false positives.

Runtime guardrails:

- `insufficient_data` never alerts.
- `not_applicable` never alerts.
- Missing scope context never becomes `fail`.
- PO/Ply/material rules do not apply when no PO/Ply/material context exists.
- Diagnostic fee rules do not fail unless repair-sold status is known and the diagnostic-fee branch actually applies.
- Photo, HHR, equipment, and options rules do not apply to canceled/no-access jobs.
- Service-call rules do not apply to admin/internal/material-only jobs.
- Plumbing/Ply rules do not apply to non-plumbing/non-material workflows unless configured.

Use `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` after discovery to narrow or disable rules without code changes. Sales rules can be configured at the ruleset level so all Sales checks share the same tenant scope. Example:

```env
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rulesets":{"Sales / Comfort Advisor Audit":{"applies_to":{"business_units":["Replacement Sales"],"job_types":["Comfort Advisor"],"tags":["Comfort Advisor"],"campaigns":["Retail Lead"],"workflows":["Sales Consultation"],"statuses":["Completed","Closed"]},"excludes":{"job_types":["Install","Project Management","Plumbing Service","HVAC Service"],"tags":["Internal","No Access"]},"alert":{"channel":"sales/comfort advisor audit channel"}}}}
```

If discovery shows only numeric ServiceTitan IDs and no workflow names, scope by IDs and clear the default workflow-name filter:

```env
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rulesets":{"Sales / Comfort Advisor Audit":{"applies_to":{"business_unit_ids":["fake-sales-bu-id"],"job_type_ids":["fake-comfort-advisor-job-type-id"],"tag_ids":["fake-sales-tag-id"],"workflows":null,"statuses":["Completed","Closed"]}}}}
```

For Sales-only rollout, leave `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={}` for the first discovery command, then replace it with reviewed Sales business unit/job type/tag IDs before using dry-run results for a live-alert decision.

Initial Sales / Comfort Advisor production rollout should keep only rules with proven data sources enabled. Use the existing disabled-rule env to keep the photo rule off until a scoped job photo source is available:

```env
SERVICE_TITAN_DISABLED_RULE_IDS_JSON=["sales_photos_missing"]
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rulesets":{"Sales / Comfort Advisor Audit":{"applies_to":{"business_unit_ids":["fake-sales-bu-id"],"job_type_ids":["fake-comfort-advisor-job-type-id"],"workflows":null,"statuses":["Completed","Closed"]}}}}
```

This leaves `sales_options_fewer_than_three` and `sales_arrival_after_first_half` enabled while disabling `sales_photos_missing`. Empty scope config is intentionally conservative: if ServiceTitan provides only numeric IDs and no workflow names, the agent returns `insufficient_data` rather than guessing which jobs are Sales jobs.

Enable `sales_photos_missing` later by removing it from `SERVICE_TITAN_DISABLED_RULE_IDS_JSON` only after dry-run proves that ServiceTitan exposes scoped job photos or scoped form image attachments. Broad tenant-level form pages and unavailable attachment endpoints must remain `insufficient_data` and should not be used as photo evidence.

Initial HVAC Service validation should use reviewed HVAC IDs and keep form/photo/arrival rules disabled until the tenant exposes scoped sources and reliable arrival timestamps. This is a future dry-run setup only; do not use it for live alerts until business review is complete:

```env
# WARNING: validation-only. Do not put SERVICE_TITAN_AUDIT_DRY_RUN=true on live Render unless intentionally pausing live ServiceTitan alerts.
HVAC_SERVICE_AUDIT_ENABLED=true
SALES_COMFORT_ADVISOR_AUDIT_ENABLED=false
TECHNICIAN_COMPLIANCE_ENABLED=false
DISPATCHER_AUDIT_ENABLED=false
SERVICE_TITAN_AUDIT_DRY_RUN=true
SERVICE_TITAN_DISABLED_RULE_IDS_JSON=["sales_photos_missing","hvac_required_photos_missing","hvac_diagnosis_form_missing","hvac_arrival_outside_window"]
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rulesets":{"HVAC Service Audit":{"applies_to":{"business_unit_ids":["1810"],"job_type_ids":["1933","6807409","7020890","7131342"],"workflows":null,"statuses":["Completed","Closed"]}}}}
```

This leaves `hvac_options_fewer_than_three` and `hvac_payment_missing_on_completed_job` available for dry-run validation while suppressing photo/form/arrival false positives. In the targeted sample, `hvac_options_fewer_than_three` failed 14 of 20 matched jobs and needs business confirmation before live alerts. `hvac_payment_missing_on_completed_job` mostly evaluated cleanly but the single fail should be reviewed before live alerts.

Initial Plumbing Service validation should use reviewed Plumbing IDs and keep payment/photo/form/arrival rules disabled until structured ServiceTitan data is available and business expectations are confirmed. This is a future dry-run setup only; do not use it for live alerts until business review is complete:

```env
# WARNING: validation-only. Do not put SERVICE_TITAN_AUDIT_DRY_RUN=true on live Render unless intentionally pausing live ServiceTitan alerts.
PLUMBING_SERVICE_AUDIT_ENABLED=true
SALES_COMFORT_ADVISOR_AUDIT_ENABLED=false
HVAC_SERVICE_AUDIT_ENABLED=false
TECHNICIAN_COMPLIANCE_ENABLED=false
DISPATCHER_AUDIT_ENABLED=false
SERVICE_TITAN_AUDIT_DRY_RUN=true
SERVICE_TITAN_DISABLED_RULE_IDS_JSON=["sales_photos_missing","hvac_options_fewer_than_three","hvac_required_photos_missing","hvac_diagnosis_form_missing","hvac_arrival_outside_window","plumbing_payment_missing_on_completed_job","plumbing_required_photos_missing","plumbing_diagnosis_form_missing","plumbing_arrival_outside_window"]
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rulesets":{"Plumbing Service Audit":{"applies_to":{"business_unit_ids":["64315277"],"job_type_ids":["57804592","64569478","112338076"],"workflows":null,"statuses":["Completed","Closed"]}}}}
```

This leaves `plumbing_options_fewer_than_three` available for dry-run validation while suppressing payment/photo/form/arrival false positives. The options rule itself excludes Water Heater Maintenance, zero-dollar/no-charge jobs, and sold/performed repair-work follow-up visits; positive-invoice Plumbing Diagnostic and Water Heater Diagnostic estimate/diagnostic visits with fewer than three options still require business review before live alerts. Do not enable Plumbing live alerts until the business confirms whether scoped Plumbing Service diagnostic job types require three options and ServiceTitan exposes reliable structured data for the other rule candidates.

Per-rule overrides still work. Example:

```env
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={"rules":{"missing_diagnostic_fee_when_repair_not_sold":{"enabled":true,"applies_to":{"business_units":["HVAC Service","Plumbing Service"],"job_types_contains":["Diagnostic","Service","Tune Up"],"statuses":["Completed","Closed"]},"excludes":{"tags_contains":["Warranty","Callback","No Charge"],"cancellation_reasons_contains":["Wrong Equipment","No Access","Safety Concern"]},"alert":{"channel":"accounting/operations channel"}}}}
```

If exact names are not known, run `servicetitan-discover-scopes` first and keep live Slack disabled until the scope config reflects production values.

Why appointments scanned may be `0`:

- The `/jpm/v2/tenant/{tenant}/appointments?jobId=...` endpoint can return no records for jobs where the appointment is embedded differently, outside the lookback, cancelled, or hidden by scope/tenant permissions.
- If the endpoint returns HTTP 400/401/403/404/405, the related category is disabled for that process and arrival rules return `insufficient_data`.
- If appointments exist but do not expose arrival-window or arrival timestamps, appointment counts may be nonzero while arrival rules still return `insufficient_data`.

Why many handbook rules can still return `insufficient_data`:

- ServiceTitan tenant scopes may not expose invoice items, payments, forms, installed equipment, purchase orders, reminders/tasks, signatures, or detailed attachments.
- Some handbook requirements need Ply data. There is no Ply API/client/config in this repository, so Ply-only checks are intentionally `insufficient_data`.
- Several checks require positive evidence, not inference. For example, a diagnostic fee line name without an amount is not enough to fail the repair-sold waiver rule.
- The matrix is based on the handbook-backed rule definitions already in this repository and the detailed excerpts in the request. If handbook text changes, reconcile the matrix before enabling live alerts for the affected rules.

## Rulesets

Sales / Comfort Advisor Audit:

- Closed Sales job has fewer than 3 options.
- Closed Sales job is missing required photos.
- Sales advisor arrived after the first half of the appointment window.

HVAC Service Audit:

- Closed HVAC Service job has fewer than 3 options.
- Closed HVAC Service job is missing payment.
- Closed HVAC Service job is missing diagnosis/service form.
- Closed HVAC Service job is missing required photos.
- HVAC Service arrival is outside the configured window threshold.

Opt-in status cleanup rule:

- `job_left_open_after_visit` detects jobs left open after the appointment end plus `SERVICE_TITAN_OPEN_JOB_GRACE_MINUTES`. It is disabled by default and only runs when enabled through `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON`.

Legacy Technician Compliance, disabled by default for the Sales-first phase:

- Technician clock-in missing.
- Technician clock-out missing.
- Lunch break missing or too short when the shift duration requires one.
- Invoice missing a diagnostic fee line item.
- Required job phases incomplete.
- Required operational data incomplete.

Legacy Dispatcher / Job Quality Audit, disabled by default for the Sales-first phase:

- Technician arrival outside the first configured minutes of the arrival window.
- Diagnostic fee not reflected.
- Required job options not presented.
- Job notes missing or incomplete.
- Required job photos missing.
- Supporting evidence missing.

Handbook-backed audit:

- First call on-time arrival.
- Arrival outside configured window start threshold.
- Missing or too-short completion notes.
- Missing required photos.
- Missing equipment registration.
- Missing HHR or service form.
- Missing three repair options.
- Missing Home Comfort Plan option.
- Missing same-day estimate.
- Missing price authorization.
- Missing diagnostic fee when no repair is sold.
- Diagnostic fee not waived when repair is sold.
- Missing payment on completed job.
- Missing follow-up task when follow-up is needed.
- Special-order notes missing required fields.
- Special-order missing ServiceTitan reminder. This remains `insufficient_data` until reminders/tasks are integrated.
- Missing downpayment for special order.
- Lead turnover missing documentation.
- PO received but not reconciled.
- PO missing vendor document.
- PO missing attachments.
- PO not synced to ServiceTitan. This remains `insufficient_data` until Ply is integrated.
- Ply/ST material sync blocked. This remains `insufficient_data` until Ply is integrated.
- Scope change missing escalation note.
- Cancellation after materials missing escalation.
- Defective part missing warranty claim data.

Each rule returns one of:

- `pass`
- `fail`
- `insufficient_data`
- `not_applicable`
- `error`

Only `fail` results create Slack alerts. `insufficient_data` and `not_applicable` are logged and skipped to avoid false positives when a ServiceTitan endpoint does not expose a required field yet or the job is outside the rule scope.

## Required Environment

Required for continuous polling when `SERVICE_TITAN_AUDIT_ENABLED=true`, or for the one-time audit command when you force a validation run:

```env
SERVICE_TITAN_AUDIT_ENABLED=true
SERVICETITAN_CLIENT_ID=
SERVICETITAN_CLIENT_SECRET=
SERVICETITAN_TENANT_ID=
SERVICETITAN_APP_KEY=
```

Slack alert routing is required only when alerts can be sent. If `SERVICE_TITAN_AUDIT_DRY_RUN=true`, no Slack channel is required. If dry-run is false, set `SLACK_ALERT_CHANNEL_ID`. ServiceTitan audit alerts do not fall back to `SLACK_MARKETING_OPS_CHANNEL_ID`.

## Optional Environment

```env
SERVICETITAN_ENVIRONMENT=production
SERVICETITAN_BASE_URL=https://api.servicetitan.io
SERVICETITAN_AUTH_URL=https://auth.servicetitan.io/connect/token
SERVICETITAN_JOB_URL_TEMPLATE=
SERVICE_TITAN_AUDIT_DRY_RUN=false
SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false
SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE=false
SERVICE_TITAN_AUDIT_DEBUG_FIELDS=false
NOTIFICATIONS_TEST_SEND=false
SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS=300
SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS=300
SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES=240
SERVICE_TITAN_AUDIT_OVERLAP_SECONDS=300
SERVICE_TITAN_AUDIT_PAGE_SIZE=100
SERVICE_TITAN_AUDIT_MAX_PAGES=5
SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE=25
SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED=false
SERVICE_TITAN_WEEKLY_SUMMARY_DAY=MON
SERVICE_TITAN_WEEKLY_SUMMARY_HOUR=8
SERVICE_TITAN_WEEKLY_SUMMARY_LOOKBACK_DAYS=7
SALES_COMFORT_ADVISOR_AUDIT_ENABLED=true
HVAC_SERVICE_AUDIT_ENABLED=false
PLUMBING_SERVICE_AUDIT_ENABLED=false
TECHNICIAN_COMPLIANCE_ENABLED=false
DISPATCHER_AUDIT_ENABLED=false
SERVICE_TITAN_FIRST_CALL_GRACE_MINUTES=0
SERVICE_TITAN_ARRIVAL_GRACE_MINUTES=30
SERVICE_TITAN_OPEN_JOB_GRACE_MINUTES=120
SERVICE_TITAN_MIN_LUNCH_BREAK_MINUTES=30
SERVICE_TITAN_LUNCH_REQUIRED_AFTER_HOURS=5
SERVICE_TITAN_MIN_NOTE_LENGTH=30
SERVICE_TITAN_REQUIRE_HHR=true
SERVICE_TITAN_REQUIRE_EQUIPMENT_REGISTRATION=true
SERVICE_TITAN_MIN_REPAIR_OPTIONS=3
SERVICE_TITAN_REQUIRE_HOME_COMFORT_PLAN_OPTION=true
SERVICE_TITAN_PO_RECONCILE_WITHIN_HOURS=24
SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME=false
SERVICE_TITAN_DIAGNOSTIC_FEE_KEYWORDS_JSON=["diagnostic"]
SERVICE_TITAN_HOME_COMFORT_PLAN_KEYWORDS_JSON=["home comfort plan","comfort plan","membership","maintenance plan"]
SERVICE_TITAN_HHR_KEYWORDS_JSON=["home health report","hhr","report card"]
SERVICE_TITAN_SPECIAL_ORDER_REQUIRED_NOTE_FIELDS_JSON=["purchase order number","ordering date","employee ordered","eta","supply house"]
SERVICE_TITAN_DISABLED_RULE_IDS_JSON=[]
SERVICE_TITAN_REQUIRED_PHASES_JSON=[]
SERVICE_TITAN_REQUIRED_OPERATIONAL_FIELDS_JSON=[]
SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={}
```

Use `SERVICETITAN_BASE_URL=https://api-integration.servicetitan.io` for the integration environment. Set `SERVICETITAN_AUTH_URL` to the environment-specific token URL provided by ServiceTitan if it differs from production.

`SERVICETITAN_JOB_URL_TEMPLATE` may include `{job_id}` and `{job_number}`. Example:

```env
SERVICETITAN_JOB_URL_TEMPLATE=https://go.servicetitan.com/#/Job/Index/{job_id}
```

## ServiceTitan Setup

The ServiceTitan API requires:

- Client ID.
- Client Secret.
- App Key, sent as `ST-App-Key`.
- Tenant ID.
- OAuth client credentials token access.

Minimum expected read scopes:

- Job Planning and Management -> Jobs.
- Job Planning and Management -> Appointments, if appointment timing is not embedded in job payloads.
- Dispatch -> Appointment Assignments, if actual arrival time is stored there.
- Sales -> Estimates or Opportunities, for Sales option count.
- Job Planning and Management -> Attachments, for Sales photo checks.
- Any invoice, payroll, forms, notes, photos, or attachment scopes needed for your tenant to expose the fields used by the rules.

The audit starts from recent jobs, then enriches each job with related records. If a related endpoint or required field is unavailable in the tenant, the corresponding rules return `insufficient_data`.

The enrichment pass reads related ServiceTitan records when available:

- `/jpm/v2/tenant/{tenant}/appointments`
- `/dispatch/v2/tenant/{tenant}/appointment-assignments`
- `/accounting/v2/tenant/{tenant}/invoices`
- `/accounting/v2/tenant/{tenant}/export/invoice-items`
- `/payroll/v2/tenant/{tenant}/jobs/timesheets`
- `/payroll/v2/tenant/{tenant}/non-job-timesheets`
- `/jpm/v2/tenant/{tenant}/jobs/{job_id}/notes`
- `/jpm/v2/tenant/{tenant}/jobs/{job_id}/attachments`
- `/forms/v2/tenant/{tenant}/submissions`
- `/equipments/v2/tenant/{tenant}/installed-equipment`
- `/inventory/v2/tenant/{tenant}/purchase-orders`
- `/jpm/v2/tenant/{tenant}/jobs/{job_id}/history`
- `/sales/v2/tenant/{tenant}/estimates`
- `/sales/v2/tenant/{tenant}/opportunities`

If any related endpoint is unavailable, ignores requested pagination, returns overbroad tenant-level data for a job-scoped request, or does not expose a required field, the affected rules remain `insufficient_data` and include source notes in logs/summary context. Overbroad related endpoints are disabled for the current process after detection so one bad export path does not dominate every job in the audit cycle.

## Scope Discovery

Before enabling live alerts, run:

```bash
python3 -m marketing_os_agent servicetitan-discover-scopes
```

The command fetches recent ServiceTitan jobs using the configured lookback and prints sanitized discovery output:

- Job statuses.
- Appointment statuses.
- Business units.
- Job types.
- Departments.
- Trades.
- Workflows.
- Tags.
- Advisor/technician and dispatcher identifiers.
- Invoice statuses.
- Cancellation reasons.
- PO/material context counts.
- Related-record counts.
- Available top-level payload keys by category.

It does not print customer names, addresses, phone numbers, emails, raw notes, secrets, access tokens, or client secrets. Use these values to fill `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON`, then rerun `servicetitan-audit-once` in dry-run mode before live Slack alerts.

## Handbook Rule Matrix Summary

The full structured matrix is in `marketing_os_agent/domain/service_titan_handbook.py`. Every entry includes `rule_id`, title, ruleset/category, handbook source, business reason, severity, required fields, data sources, current availability, evaluation logic, pass/fail/insufficient conditions, alert recipient, recommended action, delivery mode, and default enabled state.

| Rule ID | Handbook source | Availability | Default | Destination | Delivery |
| --- | --- | --- | --- | --- | --- |
| `first_call_on_time_arrival` | Service Call arrival protocol | partially_available | enabled | dispatcher channel | immediate |
| `arrival_outside_window_start_threshold` | Service Call arrival protocol | partially_available | enabled | dispatcher channel | immediate |
| `missing_job_completion_notes` | Service Call post-call | available | enabled | dispatcher channel | immediate |
| `job_notes_too_short` | Service Call post-call | available | enabled | dispatcher channel | immediate |
| `missing_required_photos` | Service Call photos/videos | partially_available | enabled | technician manager | immediate |
| `missing_equipment_registration` | Service Call equipment registration | partially_available | enabled | technician manager | immediate |
| `missing_hhr_or_service_form` | Service Call Home Health Report | partially_available | enabled | technician manager | immediate |
| `missing_three_repair_options` | Service Call repair options | partially_available | enabled | dispatcher channel | immediate |
| `missing_home_comfort_plan_option` | Service Call repair options | partially_available | enabled | dispatcher channel | immediate |
| `missing_same_day_estimate` | Service Call repair options | partially_available | enabled | dispatcher channel | immediate |
| `missing_price_authorization` | Service Call price authorization | partially_available | enabled | accounting/office | immediate |
| `missing_diagnostic_fee_when_repair_not_sold` | Service Call diagnostic fee/payments | partially_available | enabled | accounting/office | immediate |
| `diagnostic_fee_not_waived_when_repair_sold` | Service Call diagnostic fee/payments | partially_available | enabled | accounting/office | immediate |
| `missing_payment_on_completed_job` | Service Call diagnostic fee/payments | partially_available | enabled | accounting/office | immediate |
| `missing_follow_up_task_when_follow_up_needed` | Service Call post-call | partially_available | enabled | dispatcher channel | immediate |
| `special_order_missing_required_notes` | Service Call special-order/future work | partially_available | enabled | warehouse/parts | immediate |
| `special_order_missing_service_titan_reminder` | Service Call special-order/future work | unknown | enabled | warehouse/parts | immediate |
| `missing_downpayment_for_special_order` | Service Call special-order/future work | partially_available | enabled | accounting/office | immediate |
| `lead_turnover_missing_required_documentation` | Service Call lead turnover | partially_available | enabled | technician manager | immediate |
| `po_received_not_reconciled` | Plumbing Dispatcher reconciliation | partially_available | enabled | warehouse/parts | immediate |
| `po_missing_vendor_document` | Plumbing Dispatcher PO receiving | partially_available | enabled | warehouse/parts | immediate |
| `po_missing_attachments` | Plumbing Dispatcher PO receiving | partially_available | enabled | warehouse/parts | immediate |
| `po_not_synced_to_service_titan` | Plumbing Dispatcher ST sync | unavailable | enabled | warehouse/parts | immediate |
| `ply_st_material_sync_blocked` | Plumbing Dispatcher escalations | unavailable | enabled | Ali/operations escalation | immediate |
| `scope_change_missing_escalation_note` | Plumbing Dispatcher escalations | partially_available | enabled | dispatcher channel | immediate |
| `cancellation_after_materials_missing_escalation` | Plumbing Dispatcher escalations | partially_available | enabled | Ali/operations escalation | immediate |
| `defective_part_missing_warranty_claim_data` | Plumbing Dispatcher escalations | partially_available | enabled | warehouse/parts | immediate |

Production-ready now:

- The Sales / Comfort Advisor rules are production-ready when Sales scope values are configured from discovery and the tenant exposes estimates/opportunities, photos/attachments, appointment windows, and actual arrival timestamps.
- Rules using notes, attachments/photos, appointment windows, invoice line items, invoice payment fields, estimates/opportunities, forms, equipment, and ServiceTitan purchase orders are production-ready only when those ServiceTitan endpoints are scoped and return the needed fields.
- Rules intentionally fail only when the source is available and the mapped fact is missing or noncompliant.

Still needs data access:

- Ply-only checks need a real Ply API/client/config before they can pass or fail.
- ServiceTitan reminder/task verification for special-order parts needs a reminder/task endpoint or another approved task source.
- Fine-grained photo category checks, ductwork/insulation inspection fields, signature timing before work start, remote authorization email delivery, customer-not-home email status, and customer-sent HHR/photos/temperature readings need tenant-specific ServiceTitan fields or reports before they can be enforced without false positives.

Rule readiness interpretation:

- Production-ready now when source fields are present: `missing_job_completion_notes`, `job_notes_too_short`.
- Partially-ready and able to produce real `fail` results when the related ServiceTitan endpoint is scoped and returns usable fields: arrival rules, photos, equipment registration, HHR/service form, repair options, Home Comfort Plan option, same-day estimate, price authorization, diagnostic fee/payment, follow-up task indicator, special-order notes/downpayment, lead turnover documentation, PO reconciliation, PO vendor document, PO attachments, scope-change escalation, cancellation-after-materials escalation, and defective part warranty claim data.
- Insufficient-data only until additional integration exists: `po_not_synced_to_service_titan`, `ply_st_material_sync_blocked`, and `special_order_missing_service_titan_reminder`.
- Every handbook rule is enabled by default unless listed in `SERVICE_TITAN_DISABLED_RULE_IDS_JSON`.
- Every `fail` result is routed as an immediate ServiceTitan Slack alert. There is no ServiceTitan digest sender in this codebase.

## Slack Alerts

Alerts go to `SLACK_ALERT_CHANNEL_ID` only. Business Unit labels are included in the message text for grouping, but they do not route alerts to separate channels.

### Production Double-Check

Overall readiness:

- Production-ready for Slack alerting after ServiceTitan scopes, Slack token, alert channel, bot channel membership, and SQLite persistence are configured.
- Ready for dry-run validation before live Slack alerting.
- Not production-ready for Ply-only pass/fail checks until a real Ply API/client/config exists.
- Not production-ready for ServiceTitan audit email alerts because email alerting for ServiceTitan is not implemented.

Exact ServiceTitan alert timing:

- Real violations are evaluated on every `servicetitan-audit-once` run and on every continuous audit polling cycle.
- Continuous polling uses `ServiceTitanAuditLoop` and `SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS`.
- Continuous polling waits `SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS` before the first startup cycle, so Render can finish boot and health checks before related ServiceTitan records are fetched.
- On the first live run with no checkpoint, `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false` initializes a baseline checkpoint and sends no historical Slack alerts.
- Set `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=true` only when you intentionally want the first live run to evaluate and alert on the configured lookback window.
- `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE` caps live Slack send attempts per cycle. Dry-run summaries are not capped, so backfill dry-runs can still show the full would-send count. Use `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE=1` for controlled one-real-alert validation.
- Slack sends immediately when a rule returns `fail`, dry-run is false, Slack config is present, and the exact violation was not already successfully alerted.
- There is no Friday-only or weekly-only ServiceTitan violation alert path.
- The `delivery` field in the handbook matrix is metadata included in Slack text; ServiceTitan violation alerts are immediate because no ServiceTitan digest sender exists.

Scheduler inventory:

| Scheduler/path | Timing | Purpose | Controls ServiceTitan alerts? |
| --- | --- | --- | --- |
| `PollingLoop` | `NOTION_POLL_INTERVAL_SECONDS` | Agent 1 Notion task polling/reminders | no |
| `Scheduler` / `monday_push` | Monday 8 AM | Scheduled marketing task summary | no |
| `Scheduler` / `friday_roundup` | Friday 4 PM | Weekly marketing Slack/email report | no |
| `Scheduler` / `monthly_kickoff` | first day 9 AM | Monthly campaign kickoff report | no |
| `Scheduler` / `quarterly_kickoff` | first day of quarter 9 AM | Quarterly campaign kickoff report | no |
| `Scheduler` / `campaign_health_scan` | daily 7 AM | Campaign health scan/DMs | no |
| `ServiceTitanAuditLoop` | `SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS`, then `SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS` | ServiceTitan operations audit | yes |
| `servicetitan-audit-once` | manual one-time command | One ServiceTitan audit cycle | yes |

Friday/weekly behavior exists only for marketing reports. It does not gate urgent ServiceTitan audit violations.

Runtime mode behavior:

- `SERVICE_TITAN_AUDIT_ENABLED=false`: continuous ServiceTitan polling does not start. The one-time `servicetitan-audit-once` command can still run because it forces a validation cycle.
- `SERVICE_TITAN_AUDIT_DRY_RUN=true`: real ServiceTitan data may be fetched and rules may be evaluated, but Slack alerts, violation writes, dedupe writes, and checkpoint advancement are skipped. The CLI summary prints `dry_run: True`.
- `SERVICE_TITAN_AUDIT_DRY_RUN=false` and `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false`: the first run with no checkpoint creates a baseline and sends nothing. Later cycles fetch updated jobs using the checkpoint plus overlap.
- `SERVICE_TITAN_AUDIT_DRY_RUN=false` and `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=true`: the first run uses `SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES` and can alert historical failures. Use only for explicit backfill.
- Live backfill should always include `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE`. The initial production default is 25; set it to 1 for a one-alert validation run.
- `SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE=true`: honored only by one-time forced commands, only when backfill is true, and in live mode only when `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE=1`. This is for controlled historical validation after a checkpoint already exists. Continuous polling ignores it.
- After a checkpoint exists, `fail` results create/open violations and immediately call Slack. `alert_sent_at` is set only after Slack returns a timestamp.
- Slack failure does not crash the audit cycle and leaves `alert_sent_at=NULL`, so the same violation can retry later. If Slack fails or the per-cycle alert cap is reached, checkpoint advancement is skipped so pending alertable violations are not left behind.
- `pass` and `not_applicable` can resolve an existing open violation for the same deterministic key.
- `insufficient_data` is logged and counted in summaries but does not alert by default.
- `not_applicable` is logged and counted in summaries but does not alert by default.

### Notification Architecture Report

ServiceTitan audit Slack alerts are created only in `ServiceTitanAuditService._record_and_alert`.

Required conditions for a live ServiceTitan Slack alert:

1. The audit command/loop runs and ServiceTitan credentials are valid.
2. The rule engine returns at least one `fail` result.
3. `SERVICE_TITAN_AUDIT_DRY_RUN=false`.
4. `SLACK_BOT_TOKEN` is configured.
5. `SLACK_ALERT_CHANNEL_ID` is configured.
6. The Slack bot is installed and has permission to post in the configured channel. For private channels, invite the bot to the channel.
7. The deterministic violation key has not already been alerted with `alert_sent_at` set.

Conditions that prevent Slack alerting:

- All rule results are `pass`, `insufficient_data`, `not_applicable`, or `error`.
- `violations_detected=0`.
- `SERVICE_TITAN_AUDIT_DRY_RUN=true`.
- `SLACK_BOT_TOKEN` is missing.
- `SLACK_ALERT_CHANNEL_ID` is missing.
- Slack rejects the post, commonly because the bot is not in the channel, the token is invalid, or the channel ID is wrong.
- A matching stored violation already has `alert_sent_at` set.

Important behavior:

- A `fail` rule result is required for Slack alerting.
- `insufficient_data` and `not_applicable` do not alert by design. They appear in logs and the CLI summary to avoid production false positives.
- Dry-run blocks Slack, violation writes, dedupe writes, and checkpoint advancement.
- Dedupe suppresses only already-sent alerts. If Slack fails, `alert_sent_at` remains `NULL`, so the alert can retry later.
- `SLACK_ALERT_CHANNEL_ID` is the only ServiceTitan alert channel.
- `SLACK_BOT_TOKEN` is loaded from env through `Settings.from_env()`.
- ServiceTitan audit email alerts are not implemented. The repo has SMTP email support for report emails and email test commands, but ServiceTitan audit does not call `EmailClient`.

Exact reason for the latest described run:

- The run had jobs scanned and rules evaluated, but `violations_detected=0`, `fail=0`, and `insufficient_data>0`.
- That is expected to send no Slack alerts. The alert path is not reached unless a rule result is `fail`.
- Do not send `insufficient_data` to operations channels by default. Use the CLI summary and debug logs, or build a separate opt-in digest later if needed.

Each alert includes:

- Ruleset.
- Rule.
- Severity.
- Job number or job ID.
- Technician and dispatcher when available.
- Arrival window and arrival time when relevant.
- Invoice total when available.
- Explanation.
- Recommended action.
- Intended alert destination and delivery mode from the handbook matrix.
- ServiceTitan link when `SERVICETITAN_JOB_URL_TEMPLATE` is configured.

Customer names, addresses, phone numbers, emails, and raw notes are not included in ServiceTitan Slack alerts.

## Notification Test Commands

Validate Slack notification config without sending:

```bash
python3 -m marketing_os_agent notifications-test
```

This prints:

- `SERVICE_TITAN_AUDIT_DRY_RUN` status.
- Slack token presence.
- `SLACK_ALERT_CHANNEL_ID` presence.
- Effective Slack channel.
- `auth.test` result when a token is present.
- Whether a Slack test message would be sent.
- A reminder that ServiceTitan audit email alerts are not implemented.

Send a safe Slack test message:

```bash
NOTIFICATIONS_TEST_SEND=true python3 -m marketing_os_agent notifications-test
```

Message text:

```text
[TEST] Marketing OS Agent notification test. If you see this, Slack alert delivery works.
```

Build a synthetic ServiceTitan alert without calling ServiceTitan, writing violations, or touching dedupe:

```bash
python3 -m marketing_os_agent servicetitan-alert-test
```

Send that synthetic ServiceTitan alert to Slack only when explicitly enabled:

```bash
NOTIFICATIONS_TEST_SEND=true python3 -m marketing_os_agent servicetitan-alert-test
```

Validate SMTP/email config without sending:

```bash
python3 -m marketing_os_agent email-test --to you@example.com
```

Send a safe SMTP test email only when explicitly enabled:

```bash
NOTIFICATIONS_TEST_SEND=true python3 -m marketing_os_agent email-test --to you@example.com
```

The existing `test-email` command still sends the Friday-roundup preview immediately. Use `email-test` for safer notification diagnostics.

## Render Notification Env Vars

Required for live ServiceTitan Slack alert delivery:

```env
SERVICE_TITAN_AUDIT_DRY_RUN=false
SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false
SLACK_BOT_TOKEN=
SLACK_ALERT_CHANNEL_ID=
```

Optional alert labels for grouping in the same Slack channel:

```env
SERVICE_TITAN_BUSINESS_UNIT_LABELS_JSON={"1810":"HVAC Service","1812":"HVAC Sales / Comfort Advisors","64326403":"Plumbing Sales","64315277":"Plumbing Service"}
```

Optional and normally false:

```env
NOTIFICATIONS_TEST_SEND=false
SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME=false
SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED=false
```

When `SERVICE_TITAN_WEEKLY_SUMMARY_ENABLED=true`, the weekly report still uses `SLACK_ALERT_CHANNEL_ID`; no separate Slack channel is required.

Required only for SMTP email/report delivery, not ServiceTitan audit alerts:

```env
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
EMAIL_FROM=
TIM_EMAIL=
VADIM_EMAIL=
```

## Dry Run Mode

Set:

```env
SERVICE_TITAN_AUDIT_DRY_RUN=true
```

WARNING: Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` only for one-off validation commands or a deliberately paused dry-run environment. Do not set this globally on the live Render service unless you intentionally want to stop live Sales/HVAC/Plumbing ServiceTitan alerts.

Dry-run mode fetches real ServiceTitan data and evaluates every enabled rule, but it does not send Slack alerts, does not create violation records, and does not advance the ServiceTitan audit checkpoint. The run still writes normal run logs and prints a summary for one-time runs.

The summary includes:

- Jobs scanned.
- Appointments scanned.
- Invoices scanned.
- Invoice items scanned.
- Estimates scanned.
- Notes scanned.
- Photos scanned.
- Forms scanned.
- Equipment records scanned.
- Purchase orders scanned.
- Technician time records scanned.
- Rules evaluated.
- Pass/fail/insufficient_data/not_applicable/error counts.
- Violations detected.
- `insufficient_data` count by rule.
- `not_applicable` count by rule.
- Rules skipped due to `not_applicable`.
- False-positive prevention summary.
- Missing data category counts.
- Alert destinations.
- Alerts sent.
- Alerts that would have been sent.
- Alerts skipped due to dedupe.
- Errors.

This is the recommended first validation mode before live ServiceTitan alerts. If Sales alerts are already live, do not change the live Render service to `SERVICE_TITAN_AUDIT_DRY_RUN=true`; instead, run a one-off command with command-level env overrides in a shell. You may keep `SERVICE_TITAN_AUDIT_ENABLED=false` in a long-running validation service and run `python3 -m marketing_os_agent servicetitan-audit-once` as a one-off command with `SERVICE_TITAN_AUDIT_DRY_RUN=true`.

For sanitized field introspection, set:

```env
SERVICE_TITAN_AUDIT_DEBUG_FIELDS=true
```

Debug field mode logs only top-level key names, related-record counts, present field names, and missing data categories. It does not log customer names, addresses, phone numbers, emails, raw notes, access tokens, or client secrets.

## Deduplication

Violations are stored in `service_titan_audit_violations`.

The deterministic key is:

```text
service_titan_job_id + appointment_id + rule_id + relevant_actor_id
```

The same violation is not alerted repeatedly. If Slack fails, the violation remains with `alert_sent_at=NULL`, so a later cycle can retry the alert. If a rule later passes or becomes `not_applicable` for a previously open violation, the record is marked `resolved`.

## Commands

Run one audit cycle:

```bash
python3 -m marketing_os_agent servicetitan-audit-once
```

Discover sanitized ServiceTitan scope values:

```bash
python3 -m marketing_os_agent servicetitan-discover-scopes
```

The one-time command runs exactly one cycle and exits. It intentionally does not require `SERVICE_TITAN_AUDIT_ENABLED=true`, so you can validate real ServiceTitan data without enabling continuous polling. Use command-level `SERVICE_TITAN_AUDIT_DRY_RUN=true` until you are ready to send Slack alerts. Do not set `SERVICE_TITAN_AUDIT_DRY_RUN=true` globally on an already-live Render service unless intentionally pausing live ServiceTitan alerts.
The discovery command is read-only and prints sanitized scope values for configuring business-unit, job-type, status, tag, and workflow filters.

Validate notification delivery paths:

```bash
python3 -m marketing_os_agent notifications-test
python3 -m marketing_os_agent servicetitan-alert-test
python3 -m marketing_os_agent email-test --to you@example.com
```

Set `NOTIFICATIONS_TEST_SEND=true` only when you want the test command to send a real Slack or SMTP test message.

Run the long-lived service:

```bash
python3 -m marketing_os_agent run
```

With `SERVICE_TITAN_AUDIT_ENABLED=true`, the long-lived service starts `ServiceTitanAuditLoop`. It waits `SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS`, then runs an audit cycle every `SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS`. Each cycle fetches recently updated jobs using the durable checkpoint plus `SERVICE_TITAN_AUDIT_OVERLAP_SECONDS`, evaluates scoped rules, and immediately sends Slack only for new real `fail` results. It does not batch ServiceTitan violations into daily, Friday, or weekly reports.

Startup logs to confirm continuous mode:

- `servicetitan_continuous_audit_enabled`: emitted by the main process before the ServiceTitan audit thread starts. Includes dry-run, backfill, poll interval, lookback, overlap, max-alert cap, enabled rulesets, disabled rules, and whether a Slack channel is configured.
- `servicetitan_audit_loop_started`: emitted by the ServiceTitan audit thread when continuous polling starts.
- `servicetitan_audit_startup_delay`: emitted when a startup delay is configured.

Per-cycle logs:

- `servicetitan_audit_cycle_started`: emitted before fetching ServiceTitan jobs. Includes dry-run, backfill, since timestamp, and max-alert cap.
- `servicetitan_audit_cycle_completed`: compact per-cycle summary with jobs scanned, Sales jobs scanned, Sales pass/fail, Sales would-send, Sales sent, dedupe skipped, cap skipped, failed alerts, dry-run, and backfill.
- `servicetitan_audit_completed`: detailed cycle summary with all related record counts and rule result counts.
- `servicetitan_controlled_backfill_alert_attempt`: emitted before a live backfill Slack attempt. If `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE=1`, `manual_validation=true`.
- `servicetitan_alert_skipped_max_per_cycle`: emitted when the live cap prevents an additional Slack send.

## Adding A Rule

1. Add a `HandbookRuleDefinition` in `service_titan_handbook.py` when the rule comes from a handbook.
2. Add an `AuditRule` or handbook evaluator in `service_titan_rules.py`.
3. Keep it inside the correct ruleset builder.
4. Declare `rule_id`, `ruleset`, `severity`, `title`, `description`, `required_fields`, recommended action, alert recipient, and delivery mode.
5. Keep thresholds, keyword lists, and noisy rule controls in config/env when the value may change.
6. Return `insufficient_data` when required fields are unavailable.
7. Add unit tests for pass, fail, insufficient data, and alert dedupe if the rule creates a new violation class.

Do not:

- Infer violations from absent sources.
- Add Ply pass/fail checks without a real Ply data source.
- Include customer names, addresses, phone numbers, emails, raw notes, secrets, or tokens in debug logs.

## Manual Dry-Run Command

Recommended first validation:

```bash
SERVICE_TITAN_AUDIT_DRY_RUN=true SERVICE_TITAN_AUDIT_DEBUG_FIELDS=true python3 -m marketing_os_agent servicetitan-audit-once
```

WARNING: This is a local or shell-level one-off validation override. Do not copy `SERVICE_TITAN_AUDIT_DRY_RUN=true` into the live Render service unless intentionally pausing live Sales/HVAC/Plumbing alerts.

Confirm the output includes:

```text
- dry_run: True
```

Review:

- Jobs, appointments, invoices, invoice items, estimates, notes, forms, equipment, purchase orders, and technician time counts.
- Rule result counts.
- Sales jobs scanned.
- Sales rules evaluated.
- Sales pass/fail/insufficient_data/not_applicable counts.
- Sales alerts that would have been sent and Sales alerts sent.
- HVAC jobs scanned.
- HVAC rules evaluated.
- HVAC pass/fail/insufficient_data/not_applicable counts.
- HVAC alerts that would have been sent and HVAC alerts sent.
- `insufficient_data by rule`.
- `not_applicable by rule`.
- Missing data category counts.
- Alerts that would have been sent.
- Alert destinations.

Keep dry-run enabled only in the validation command/environment until notification delivery and routing are separately verified. If production Sales alerts are already live, leave the live Render ServiceTitan dry-run setting unchanged.

## Safe Historical Alert Validation

Normal production mode avoids historical flood:

```bash
SERVICE_TITAN_AUDIT_ENABLED=true \
SERVICE_TITAN_AUDIT_DRY_RUN=false \
SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false \
SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE=25 \
SALES_COMFORT_ADVISOR_AUDIT_ENABLED=true \
TECHNICIAN_COMPLIANCE_ENABLED=false \
DISPATCHER_AUDIT_ENABLED=false \
SERVICE_TITAN_DISABLED_RULE_IDS_JSON='["sales_photos_missing"]' \
python3 -m marketing_os_agent run
```

Safe one-real-historical-Sales-alert validation:

```bash
SERVICE_TITAN_AUDIT_ENABLED=false \
SERVICE_TITAN_AUDIT_DRY_RUN=false \
SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=true \
SERVICE_TITAN_AUDIT_IGNORE_CHECKPOINT_ONCE=true \
SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES=4320 \
SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE=1 \
SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME=false \
SALES_COMFORT_ADVISOR_AUDIT_ENABLED=true \
TECHNICIAN_COMPLIANCE_ENABLED=false \
DISPATCHER_AUDIT_ENABLED=false \
SERVICE_TITAN_DISABLED_RULE_IDS_JSON='["sales_photos_missing"]' \
python3 -m marketing_os_agent servicetitan-audit-once
```

This sends at most one real historical Sales alert to Slack, even when a production checkpoint already exists. `SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES=4320` scans the last three days; lower or raise it based on the dry-run window you already validated. It respects `SERVICE_TITAN_DISABLED_RULE_IDS_JSON`, does not enable `sales_photos_missing`, and logs `servicetitan_controlled_backfill_alert_attempt` with `manual_validation=true`. If additional historical violations are found, they are recorded without `alert_sent_at`, the max-alert log is emitted, and checkpoint advancement is skipped so nothing is silently lost.

When setting JSON env vars manually in a shell, quote them so the shell does not strip the JSON quotes:

```bash
export SERVICE_TITAN_DISABLED_RULE_IDS_JSON='["sales_photos_missing"]'
```

Do not use `SERVICE_TITAN_DISABLED_RULE_IDS_JSON=["sales_photos_missing"]` as a standalone shell assignment; many shells pass that to child processes as `[sales_photos_missing]`, which is invalid JSON.

Continuous loop verification:

```bash
python3 -m marketing_os_agent servicetitan-runtime-diagnostics
```

Then check Render logs for:

```text
servicetitan_continuous_audit_enabled
servicetitan_audit_loop_started
servicetitan_audit_cycle_started
servicetitan_audit_cycle_completed
```

## Manual QA Checklist

1. Job with proper technician clock-in/out: no violation.
2. Job missing clock-out: violation created and Slack alert sent.
3. Job missing diagnostic fee: violation created and Slack alert sent.
4. Same job on next audit cycle: no duplicate alert.
5. Job missing notes/photos: dispatcher audit violation created.
6. Job with missing required fields from API: `insufficient_data`, no false alert.
7. ServiceTitan API failure: logged, audit cycle exits cleanly, next cycle can recover.
8. Slack failure: logged, violation remains unalerted and retries later.
9. Agent restart: existing alerted violations are not re-alerted.
10. Disabled ruleset: rules from that module do not run.
11. Handbook matrix loads and includes source, availability, routing, action, and delivery metadata.
12. Appointment arrival rules pass/fail using mapped appointment or assignment fields.
13. HHR, equipment, option count, Home Comfort Plan, authorization, payment, and same-day estimate rules pass/fail only when their sources are available.
14. Diagnostic fee rules do not fail repair-sold waiver checks when amount data is unknown.
15. Special-order, follow-up, scope-change, cancellation-after-materials, and defective-part rules trigger only when notes/PO text indicate the condition applies.
16. Ply-only rules remain `insufficient_data` until Ply access exists.
17. Jobs with no PO return `not_applicable` for PO rules.
18. Non-plumbing jobs return `not_applicable` for plumbing/Ply rules.
19. Canceled/no-access jobs return `not_applicable` for photos/HHR/options rules.
20. `SERVICE_TITAN_DISABLED_RULE_IDS_JSON` disables a noisy rule without changing code.
21. `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` narrows a rule without changing code.
22. Non-Sales jobs return `not_applicable` for all Sales rules.
23. Closed Sales jobs with 3 or more estimates pass the option rule.
24. Closed Sales jobs with fewer than 3 estimates fail only when estimate data is available.
25. Sales photo checks fail only when the attachment/photo source is available and contains no photos.
26. Sales arrival checks fail only when arrival window and arrival time are both available and arrival is after the first-half cutoff.

## Render Deployment Notes

- Add ServiceTitan env vars in Render only after the app is connected and scopes are approved.
- Keep `SERVICE_TITAN_AUDIT_ENABLED=false` until credentials and Slack alert channel are ready.
- Set `SERVICE_TITAN_AUDIT_DRY_RUN=true` only for a first validation service/run before live alerts, or as a command-level one-off override. If the live Render service is already sending Sales alerts, do not change its global `SERVICE_TITAN_AUDIT_DRY_RUN` value unless intentionally pausing live ServiceTitan alerts.
- Keep `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false` unless you explicitly want historical first-run alerts.
- Keep `SERVICE_TITAN_AUDIT_MAX_ALERTS_PER_CYCLE` set. Use `1` only for controlled one-alert historical validation.
- Set `SALES_COMFORT_ADVISOR_AUDIT_ENABLED=true`.
- Set `HVAC_SERVICE_AUDIT_ENABLED=true` only for HVAC dry-run validation or reviewed HVAC rollout.
- Set `PLUMBING_SERVICE_AUDIT_ENABLED=true` only for Plumbing dry-run validation or reviewed Plumbing rollout.
- Keep `TECHNICIAN_COMPLIANCE_ENABLED=false` and `DISPATCHER_AUDIT_ENABLED=false` during Sales-only validation.
- Set `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={}` until discovery confirms tenant-specific scope narrowing is needed.
- Run `python3 -m marketing_os_agent init-db` after deploy if the SQLite file is new.
- Run `python3 -m marketing_os_agent servicetitan-discover-scopes` to collect sanitized production scope names/IDs.
- Configure `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` when the discovered business units/job types/statuses/tags need tenant-specific narrowing.
- Run `SERVICE_TITAN_AUDIT_ENABLED=false SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-audit-once` with a short lookback for first validation as a command-level override. Do not paste that override into a live Render service unless deliberately pausing live alerts.
- Watch the command summary and logs for `servicetitan_continuous_audit_enabled`, `servicetitan_audit_loop_started`, `servicetitan_audit_cycle_started`, `servicetitan_audit_cycle_completed`, `servicetitan_audit_completed`, `servicetitan_rule_insufficient_data`, `servicetitan_alert_dry_run`, `servicetitan_duplicate_alert_suppressed`, `servicetitan_alert_skipped_max_per_cycle`, and `servicetitan_alert_sent`.
- After dry-run results look correct, set `SERVICE_TITAN_AUDIT_DRY_RUN=false`, keep `SERVICE_TITAN_AUDIT_BACKFILL_ALERTS=false`, confirm `SLACK_ALERT_CHANNEL_ID`, then set `SERVICE_TITAN_AUDIT_ENABLED=true` to start continuous polling. The first live cycle establishes a baseline; later cycles alert only new/updated violations.

Render-safe command checklist:

```bash
python3 -m marketing_os_agent init-db
python3 -m marketing_os_agent servicetitan-runtime-diagnostics
python3 -m marketing_os_agent servicetitan-discover-scopes
SERVICE_TITAN_AUDIT_ENABLED=false SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-audit-once
python3 -m marketing_os_agent servicetitan-alert-test
NOTIFICATIONS_TEST_SEND=true python3 -m marketing_os_agent servicetitan-alert-test
```

The `SERVICE_TITAN_AUDIT_DRY_RUN=true` line in this checklist is a one-off command override. It is not a recommended persistent live Render setting when Sales/HVAC/Plumbing alerts should continue sending.

The `servicetitan-runtime-diagnostics` command prints masked runtime config, JSON parsing status, rule enablement, checkpoint state, recent audit-cycle summaries, and durable violation/alert dedupe counts. It does not call ServiceTitan or Slack and does not print customer PII or secrets.

The default `servicetitan-alert-test` validates configuration and formats a synthetic ServiceTitan alert without sending it. The `NOTIFICATIONS_TEST_SEND=true` variant sends only the synthetic alert to the configured Slack alert channel.

## Known Limitations

- Some requested checks depend on fields that may not be returned by the ServiceTitan Jobs endpoint in every tenant.
- Payroll/time-entry, invoice detail, forms/options, photos, notes, installed equipment, purchase orders, and supporting evidence may require additional ServiceTitan scopes or report/export APIs.
- Ply-only checks are not pass/fail capable yet because there is no Ply API/client/config in this repository.
- ServiceTitan reminders/tasks are not integrated yet, so special-order reminder verification remains `insufficient_data`.
- Fine-grained photo requirements are currently enforced as photo presence. Per-category validation needs attachment tags, form fields, or image classification that is not currently available.
- The actual handbook PDFs were not included with the provided attachment; reconcile the matrix against the PDFs before treating it as final legal/operations policy.
- The agent records `insufficient_data` in logs rather than guessing.
- Resolution tracking is rule-based: a stored violation is marked resolved when the same deterministic rule key later evaluates as `pass`.
