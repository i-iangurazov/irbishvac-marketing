# ServiceTitan Operations Audit Agent

The ServiceTitan Operations Audit Agent continuously audits recent ServiceTitan jobs for operational compliance issues. It is part of the existing `marketing_os_agent` process and is disabled unless `SERVICE_TITAN_AUDIT_ENABLED=true`.

It does not replace Agent 1. Notion task dispatching, Slack task reminders, scheduled reports, and campaign health checks continue to run through their existing code paths.

## Architecture

- `marketing_os_agent/clients/servicetitan.py` handles OAuth client credentials, `ST-App-Key`, API calls, pagination, token caching, and conservative ServiceTitan job parsing.
- `marketing_os_agent/domain/service_titan_rules.py` contains the rule engine, legacy audit rules, and handbook-backed rule evaluators.
- `marketing_os_agent/domain/service_titan_handbook.py` contains the handbook-backed rule matrix: rule ID, handbook source, business reason, data requirements, current availability, routing, delivery mode, and default enabled state.
- `marketing_os_agent/domain/service_titan_audit.py` coordinates polling, rule execution, durable violation storage, Slack alerting, and retry behavior.
- `marketing_os_agent/persistence.py` stores audit violations in SQLite.
- `AgentApp` starts a separate ServiceTitan audit thread only when the feature is enabled.

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

Use `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` after discovery to narrow or disable rules without code changes. Example:

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

Technician Compliance:

- Technician clock-in missing.
- Technician clock-out missing.
- Lunch break missing or too short when the shift duration requires one.
- Invoice missing a diagnostic fee line item.
- Required job phases incomplete.
- Required operational data incomplete.

Dispatcher / Job Quality Audit:

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

Slack alert routing is required only when alerts can be sent. If `SERVICE_TITAN_AUDIT_DRY_RUN=true`, no Slack channel is required. If dry-run is false, set `SLACK_ALERT_CHANNEL_ID` or let the agent fall back to `SLACK_MARKETING_OPS_CHANNEL_ID`.

## Optional Environment

```env
SERVICETITAN_ENVIRONMENT=production
SERVICETITAN_BASE_URL=https://api.servicetitan.io
SERVICETITAN_AUTH_URL=https://auth.servicetitan.io/connect/token
SERVICETITAN_JOB_URL_TEMPLATE=
SERVICE_TITAN_AUDIT_DRY_RUN=false
SERVICE_TITAN_AUDIT_DEBUG_FIELDS=false
NOTIFICATIONS_TEST_SEND=false
SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS=300
SERVICE_TITAN_AUDIT_STARTUP_DELAY_SECONDS=300
SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES=240
SERVICE_TITAN_AUDIT_OVERLAP_SECONDS=300
SERVICE_TITAN_AUDIT_PAGE_SIZE=100
SERVICE_TITAN_AUDIT_MAX_PAGES=5
TECHNICIAN_COMPLIANCE_ENABLED=true
DISPATCHER_AUDIT_ENABLED=true
SERVICE_TITAN_FIRST_CALL_GRACE_MINUTES=0
SERVICE_TITAN_ARRIVAL_GRACE_MINUTES=30
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
- Business units.
- Job types.
- Departments.
- Trades.
- Workflows.
- Tags.
- Technician and dispatcher identifiers.
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

Alerts go to `SLACK_ALERT_CHANNEL_ID` or, if blank, `SLACK_MARKETING_OPS_CHANNEL_ID`.

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
- `SERVICE_TITAN_AUDIT_DRY_RUN=false`: `fail` results create/open violations and immediately call Slack. `alert_sent_at` is set only after Slack returns a timestamp.
- Slack failure does not crash the audit cycle and leaves `alert_sent_at=NULL`, so the same violation can retry later.
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
5. `SLACK_ALERT_CHANNEL_ID` is configured, or `SLACK_MARKETING_OPS_CHANNEL_ID` is configured as fallback.
6. The Slack bot is installed and has permission to post in the configured channel. For private channels, invite the bot to the channel.
7. The deterministic violation key has not already been alerted with `alert_sent_at` set.

Conditions that prevent Slack alerting:

- All rule results are `pass`, `insufficient_data`, `not_applicable`, or `error`.
- `violations_detected=0`.
- `SERVICE_TITAN_AUDIT_DRY_RUN=true`.
- `SLACK_BOT_TOKEN` is missing.
- Both `SLACK_ALERT_CHANNEL_ID` and `SLACK_MARKETING_OPS_CHANNEL_ID` are missing.
- Slack rejects the post, commonly because the bot is not in the channel, the token is invalid, or the channel ID is wrong.
- A matching stored violation already has `alert_sent_at` set.

Important behavior:

- A `fail` rule result is required for Slack alerting.
- `insufficient_data` and `not_applicable` do not alert by design. They appear in logs and the CLI summary to avoid production false positives.
- Dry-run blocks Slack, violation writes, dedupe writes, and checkpoint advancement.
- Dedupe suppresses only already-sent alerts. If Slack fails, `alert_sent_at` remains `NULL`, so the alert can retry later.
- `SLACK_ALERT_CHANNEL_ID` is preferred. If blank, `SLACK_MARKETING_OPS_CHANNEL_ID` is used.
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

Customer names are omitted by default. Set `SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME=true` only if the alert channel is appropriate for that data.

## Notification Test Commands

Validate Slack notification config without sending:

```bash
python3 -m marketing_os_agent notifications-test
```

This prints:

- `SERVICE_TITAN_AUDIT_DRY_RUN` status.
- Slack token presence.
- `SLACK_ALERT_CHANNEL_ID` and fallback channel presence.
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
SLACK_BOT_TOKEN=
SLACK_ALERT_CHANNEL_ID=
```

Fallback if no dedicated ServiceTitan alert channel is used:

```env
SLACK_MARKETING_OPS_CHANNEL_ID=
```

Optional and normally false:

```env
NOTIFICATIONS_TEST_SEND=false
SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME=false
```

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

This is the recommended first production validation mode. You may keep `SERVICE_TITAN_AUDIT_ENABLED=false` in the long-running Render service and run `python3 -m marketing_os_agent servicetitan-audit-once` as a one-off command with `SERVICE_TITAN_AUDIT_DRY_RUN=true`.

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

The one-time command runs exactly one cycle and exits. It intentionally does not require `SERVICE_TITAN_AUDIT_ENABLED=true`, so you can validate real ServiceTitan data without enabling continuous polling. Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` until you are ready to send Slack alerts.
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

Confirm the output includes:

```text
- dry_run: True
```

Review:

- Jobs, appointments, invoices, invoice items, estimates, notes, forms, equipment, purchase orders, and technician time counts.
- Rule result counts.
- `insufficient_data by rule`.
- `not_applicable by rule`.
- Missing data category counts.
- Alerts that would have been sent.
- Alert destinations.

Keep dry-run enabled until notification delivery and routing are separately verified.

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

## Render Deployment Notes

- Add ServiceTitan env vars in Render only after the app is connected and scopes are approved.
- Keep `SERVICE_TITAN_AUDIT_ENABLED=false` until credentials and Slack alert channel are ready.
- Set `SERVICE_TITAN_AUDIT_DRY_RUN=true` for the first production validation run.
- Set `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON={}` until discovery confirms tenant-specific scope narrowing is needed.
- Run `python3 -m marketing_os_agent init-db` after deploy if the SQLite file is new.
- Run `python3 -m marketing_os_agent servicetitan-discover-scopes` to collect sanitized production scope names/IDs.
- Configure `SERVICE_TITAN_RULE_SCOPE_CONFIG_JSON` when the discovered business units/job types/statuses/tags need tenant-specific narrowing.
- Run `SERVICE_TITAN_AUDIT_ENABLED=false SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-audit-once` with a short lookback for first validation.
- Watch the command summary and logs for `servicetitan_audit_completed`, `servicetitan_rule_insufficient_data`, `servicetitan_alert_dry_run`, `servicetitan_duplicate_alert_suppressed`, and `servicetitan_alert_sent`.
- After dry-run results look correct, set `SERVICE_TITAN_AUDIT_DRY_RUN=false`, confirm `SLACK_ALERT_CHANNEL_ID` or `SLACK_MARKETING_OPS_CHANNEL_ID`, run one one-time live alert cycle if desired, then set `SERVICE_TITAN_AUDIT_ENABLED=true` to start continuous polling.

Render-safe command checklist:

```bash
python3 -m marketing_os_agent init-db
python3 -m marketing_os_agent servicetitan-discover-scopes
SERVICE_TITAN_AUDIT_ENABLED=false SERVICE_TITAN_AUDIT_DRY_RUN=true python3 -m marketing_os_agent servicetitan-audit-once
python3 -m marketing_os_agent servicetitan-alert-test
NOTIFICATIONS_TEST_SEND=true python3 -m marketing_os_agent servicetitan-alert-test
```

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
