# Operations

## Daily Operation

Ilias monitors service health:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz
python3 -m marketing_os_agent health-check
```

Check logs:

```bash
docker compose logs -f marketing-os-agent
sudo journalctl -u marketing-os-agent -f
```

Expected daily automation:

- Notion polling every `NOTION_POLL_INTERVAL_SECONDS`.
- Daily campaign health scan at 7 AM.
- Status changes to Completed, Delayed, Blocked, and Canceled posted to `#marketing-ops`.

## Weekly Operation

Monday 8 AM:

- Each owner receives a DM with open tasks due that week, tasks not completed last week, and tasks moved into the week.
- `#marketing-ops` receives a summary by owner plus carry-over and moved-task details.

Friday 4 PM:

- `#marketing-ops` receives the Friday roundup, including not-completed tasks that need rollover.
- Tim and Vadim receive the same roundup by email.

Emil reviews the Friday roundup for marketing output and follow-up.

## Monthly and Quarterly Operation

First day of month at 9 AM:

- Monthly kickoff briefing posts campaigns starting that month.

First day of quarter at 9 AM:

- Quarterly kickoff briefing posts campaigns starting that quarter.

Each briefing includes campaign name, owner, dates, channel, planned spend, expected leads, expected CPL, and expected ROI.

## Tim Alerts

Tim receives Slack DMs only for:

- Task delayed twice.
- Budget overrun flagged.
- Campaign is over the configured window threshold and under the task-completion threshold.
- Agent cannot reach a task owner.
- Same completed task has been verification-flagged twice.

Tim is not notified for routine on-time completions with deliverables attached.

## Troubleshooting

Notion failures:

1. Check `NOTION_API_KEY` and database IDs.
2. Confirm the Notion integration has access to each database.
3. Run `python3 -m marketing_os_agent validate-notion`.

Slack failures:

1. Check bot token and channel/user IDs.
2. Confirm the app is installed in the workspace.
3. Confirm the bot can post to `#marketing-ops`.

Email failures:

1. Check SMTP host, port, user, password, and from address.
2. Confirm the SMTP provider allows app passwords or SMTP auth.
3. The service logs and continues if SMTP is missing or unavailable.

Claude failures:

1. Check `ANTHROPIC_API_KEY`.
2. Check Anthropic billing and usage dashboard.
3. The service falls back to deterministic text when Claude is unavailable.

SQLite failures:

1. Check `SQLITE_PATH`.
2. Confirm the service user can write to the data directory.
3. Back up the SQLite file before manual repair.

Duplicate posts:

- The service stores status transition dedupe keys in SQLite.
- If a duplicate is suppressed, logs include `duplicate_status_transition_suppressed`.

Manual catch-up after downtime:

```bash
python3 -m marketing_os_agent poll-once
python3 -m marketing_os_agent campaign-health-scan
```
