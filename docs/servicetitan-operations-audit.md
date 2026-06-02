# ServiceTitan Operations Audit Agent

The ServiceTitan Operations Audit Agent continuously audits recent ServiceTitan jobs for operational compliance issues. It is part of the existing `marketing_os_agent` process and is disabled unless `SERVICE_TITAN_AUDIT_ENABLED=true`.

It does not replace Agent 1. Notion task dispatching, Slack task reminders, scheduled reports, and campaign health checks continue to run through their existing code paths.

## Architecture

- `marketing_os_agent/clients/servicetitan.py` handles OAuth client credentials, `ST-App-Key`, API calls, pagination, token caching, and conservative ServiceTitan job parsing.
- `marketing_os_agent/domain/service_titan_rules.py` contains the rule engine and the two independent rulesets.
- `marketing_os_agent/domain/service_titan_audit.py` coordinates polling, rule execution, durable violation storage, Slack alerting, and retry behavior.
- `marketing_os_agent/persistence.py` stores audit violations in SQLite.
- `AgentApp` starts a separate ServiceTitan audit thread only when the feature is enabled.

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

Each rule returns one of:

- `pass`
- `fail`
- `insufficient_data`
- `error`

Only `fail` results create Slack alerts. `insufficient_data` is logged and skipped to avoid false positives when a ServiceTitan endpoint does not expose a required field yet.

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
SERVICE_TITAN_AUDIT_POLL_INTERVAL_SECONDS=300
SERVICE_TITAN_AUDIT_LOOKBACK_MINUTES=240
SERVICE_TITAN_AUDIT_OVERLAP_SECONDS=300
SERVICE_TITAN_AUDIT_PAGE_SIZE=100
SERVICE_TITAN_AUDIT_MAX_PAGES=5
TECHNICIAN_COMPLIANCE_ENABLED=true
DISPATCHER_AUDIT_ENABLED=true
SERVICE_TITAN_ARRIVAL_GRACE_MINUTES=30
SERVICE_TITAN_MIN_LUNCH_BREAK_MINUTES=30
SERVICE_TITAN_LUNCH_REQUIRED_AFTER_HOURS=5
SERVICE_TITAN_MIN_NOTE_LENGTH=15
SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME=false
SERVICE_TITAN_DIAGNOSTIC_FEE_KEYWORDS_JSON=["diagnostic"]
SERVICE_TITAN_REQUIRED_PHASES_JSON=[]
SERVICE_TITAN_REQUIRED_OPERATIONAL_FIELDS_JSON=[]
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
- `/jpm/v2/tenant/{tenant}/jobs/{job_id}/history`
- `/sales/v2/tenant/{tenant}/estimates`
- `/sales/v2/tenant/{tenant}/opportunities`

If any related endpoint is unavailable or does not expose a required field, the affected rules remain `insufficient_data` and include source notes in logs/summary context.

## Slack Alerts

Alerts go to `SLACK_ALERT_CHANNEL_ID` or, if blank, `SLACK_MARKETING_OPS_CHANNEL_ID`.

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
- ServiceTitan link when `SERVICETITAN_JOB_URL_TEMPLATE` is configured.

Customer names are omitted by default. Set `SERVICE_TITAN_ALERT_INCLUDE_CUSTOMER_NAME=true` only if the alert channel is appropriate for that data.

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
- Notes scanned.
- Photos scanned.
- Technician time records scanned.
- Rules evaluated.
- Violations detected.
- `insufficient_data` count by rule.
- Missing data category counts.
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

The same violation is not alerted repeatedly. If Slack fails, the violation remains with `alert_sent_at=NULL`, so a later cycle can retry the alert. If a rule later passes for a previously open violation, the record is marked `resolved`.

## Commands

Run one audit cycle:

```bash
python3 -m marketing_os_agent servicetitan-audit-once
```

The one-time command runs exactly one cycle and exits. It intentionally does not require `SERVICE_TITAN_AUDIT_ENABLED=true`, so you can validate real ServiceTitan data without enabling continuous polling. Use `SERVICE_TITAN_AUDIT_DRY_RUN=true` until you are ready to send Slack alerts.

Run the long-lived service:

```bash
python3 -m marketing_os_agent run
```

## Adding A Rule

1. Add an `AuditRule` in `service_titan_rules.py`.
2. Keep it inside the correct ruleset builder.
3. Declare `rule_id`, `ruleset`, `severity`, `title`, `description`, `required_fields`, and recommended action.
4. Return `insufficient_data` when required fields are unavailable.
5. Add unit tests for pass, fail, insufficient data, and alert dedupe if the rule creates a new violation class.

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

## Render Deployment Notes

- Add ServiceTitan env vars in Render only after the app is connected and scopes are approved.
- Keep `SERVICE_TITAN_AUDIT_ENABLED=false` until credentials and Slack alert channel are ready.
- Set `SERVICE_TITAN_AUDIT_DRY_RUN=true` for the first production validation run.
- Run `python3 -m marketing_os_agent init-db` after deploy if the SQLite file is new.
- Run `python3 -m marketing_os_agent servicetitan-audit-once` with a short lookback for first validation.
- Watch the command summary and logs for `servicetitan_audit_completed`, `servicetitan_rule_insufficient_data`, `servicetitan_alert_dry_run`, `servicetitan_duplicate_alert_suppressed`, and `servicetitan_alert_sent`.
- After dry-run results look correct, set `SERVICE_TITAN_AUDIT_DRY_RUN=false`, confirm `SLACK_ALERT_CHANNEL_ID` or `SLACK_MARKETING_OPS_CHANNEL_ID`, run one one-time live alert cycle if desired, then set `SERVICE_TITAN_AUDIT_ENABLED=true` to start continuous polling.

## Known Limitations

- Some requested checks depend on fields that may not be returned by the ServiceTitan Jobs endpoint in every tenant.
- Payroll/time-entry, invoice detail, forms/options, photos, notes, and supporting evidence may require additional ServiceTitan endpoints or report/export APIs.
- The first version records `insufficient_data` in logs rather than guessing.
- Resolution tracking is rule-based: a stored violation is marked resolved when the same deterministic rule key later evaluates as `pass`.
