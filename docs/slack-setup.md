# Slack Setup

Slack is the notification and verification surface. Notion remains the source of truth. Telegram remains human chat and is unchanged.

## Required Channels

Create these channels:

- `#marketing-ops`
- `#marketing-content`
- `#marketing-ads`
- `#marketing-general`

The service posts automation output to `#marketing-ops`. Other channels can be used by humans, but important decisions must be mirrored into Notion.

## Slack App

Create a Slack app for the workspace and install it with a bot token.

Recommended bot scopes:

- `chat:write`
- `im:write`
- `users:read`
- `users:read.email` if owner mapping by email is needed
- `channels:read`
- `groups:read` if private channels are used

Copy values into `.env`:

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `SLACK_MARKETING_OPS_CHANNEL_ID`
- `SLACK_TIM_USER_ID`

Find channel/user IDs from Slack profile details or by copying links.

## Webhooks and Events

The service exposes:

- `POST /webhooks/slack`
- `POST /webhooks/notion`

Slack request signatures are verified with `SLACK_SIGNING_SECRET`.

The current automation does not depend on Slack Workflow Builder. Notion status-change automation is handled by polling, because Notion native webhook availability varies by workspace/API setup.

## Owner Mapping

The service must be able to map Notion owners to Slack users for Monday DMs and escalation routing.

Use `OWNER_SLACK_MAP_JSON`:

```json
{"Emil":"U00000000","Tim":"U00000001","Vadim":"U00000002","emil@example.com":"U00000000"}
```

Keys can be:

- Notion user ID
- Notion display name
- Notion email

Values must be Slack user IDs.

If the agent cannot reach an owner, it DMs Tim when `SLACK_TIM_USER_ID` is configured.

## Free Tier Constraints

The setup respects Slack free-tier limits:

- 90-day message history
- 10 app limit
- 5 GB file storage
- no Slack Workflow Builder dependency

Rules for the team:

- Do not upload heavy files to Slack.
- Link files from Google Drive or Notion.
- Put final task state and important decisions in Notion.
- Use Slack for notifications and lightweight verification only.

