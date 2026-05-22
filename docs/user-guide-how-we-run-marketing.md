# How We Run Marketing

## The Rule

If it is not in Notion, it did not happen.

## Which Tool to Use

- Telegram = human chat.
- Slack = task updates and automation notices.
- Notion = source of truth for work, campaigns, deadlines, and proof.

Slack does not replace Notion. Important Slack decisions must be copied into the related Notion task or campaign.

## Updating Tasks

Every task needs:

- Owner
- Deadline
- Status
- Priority
- Department
- Linked Campaign when relevant
- Notes / Issues when there is a problem

## When You Complete a Task

Set Status to `Completed`.

Add one of:

- Deliverable link
- Google Drive link
- Notion link
- clear completion note in `Notes / Issues`

The agent checks for proof. If proof is missing, it comments on the task and marks it for verification.

## When a Task Is Delayed

Set Status to `Delayed`.

Also update:

- Deadline
- Notes / Issues with the reason

A second delay on the same task is escalated to Tim.

## Monday Update

The Monday update comes from Notion task data. It includes:

- Open tasks due this week
- Tasks not completed last week
- Tasks moved to this week

Each owner receives a Slack DM with their tasks. `#marketing-ops` receives the team summary so everyone can see carry-over work.

## When a Task Is Blocked

Set Status to `Blocked`.

Also fill:

- Notes / Issues
- Needs From Others, including the person or team needed

The agent posts the blocker to `#marketing-ops`.

## Friday Roundup

The Friday roundup comes from Notion task and campaign data. It includes:

- Completed
- Delayed
- Blocked
- Not completed, needs rollover
- Canceled
- Coming next week

Keep Notion current before Friday afternoon.
