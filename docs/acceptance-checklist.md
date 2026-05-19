# Acceptance Checklist

This checklist separates implemented service capability from external setup that requires Notion, Slack, Claude, SMTP, or campaign data access.

| Criterion | Status |
| --- | --- |
| Tasks DB live/configured/populated/usable by marketing team members | Manual external setup required: schema validation and docs implemented |
| Marketing Calendar live with at least 30 entries through end of 2026 | Manual data required: CSV import support implemented |
| Budget fields integrated into Marketing Calendar | Complete |
| Each campaign has planned spend, expected leads, CPL, ROI support | Complete |
| Workbooks collected into Notion structure with owner and last-reviewed metadata support | Manual external setup required: Workbooks DB validation/seeding implemented |
| Slack workspace/channel integration for `#marketing-ops` | Manual external setup required: client/docs implemented |
| Notion/Slack status changes post in real time or near-real-time | Complete via polling |
| Claude AI agent runs on chosen hosting | Complete, credentials required |
| Claude API billing/config visibility documented | Complete |
| Monday push tested and working | Complete in automated tests |
| Friday roundup tested and working | Complete in automated tests |
| Monthly kickoff tested and working | Complete in automated tests |
| Completed-without-deliverable test case gets flagged | Complete in automated tests |
| Simulated double-delay sends Tim DM | Complete in automated tests |
| One-page user guide exists and is ready to pin in Notion | Complete |
| Telegram remains chat; Slack remains task updates; Notion remains source of truth | Complete in docs |
| No Slack Workflow Builder dependency | Complete |
| Slack free-tier limitations respected | Complete in docs and architecture |
| No hardcoded credentials | Complete |
| Tests pass | Complete |
| Deployment docs are complete | Complete |

