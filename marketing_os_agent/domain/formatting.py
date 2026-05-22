from __future__ import annotations

from datetime import date
from html import escape
from typing import Iterable

from ..models import Campaign, Task


def task_line(task: Task) -> str:
    campaign = f" | Campaign: {', '.join(task.linked_campaign_names or task.linked_campaign_ids)}" if task.linked_campaign_ids else ""
    return f"- {task.name} | {task.status} | due {task.deadline_iso}{campaign}"


def status_update_text(task: Task) -> str:
    lines = [
        f"*Task:* {task.name}",
        f"*Owner:* {task.owner_name}",
        f"*New status:* {task.status}",
        f"*Deadline:* {task.deadline_iso}",
    ]
    if task.linked_campaign_names or task.linked_campaign_ids:
        lines.append(f"*Linked campaign:* {', '.join(task.linked_campaign_names or task.linked_campaign_ids)}")
    if task.status in {"Delayed", "Blocked"} and task.notes_issues:
        lines.append(f"*Reason / notes:* {task.notes_issues}")
    if task.status == "Completed" and task.deliverable_link:
        lines.append(f"*Deliverable:* {task.deliverable_link}")
    if task.url:
        lines.append(f"*Notion:* {task.url}")
    return "\n".join(lines)


def task_status_blocks(task: Task) -> list[dict[str, object]]:
    text = status_update_text(task)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def format_money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def format_cpl(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def campaign_brief_line(campaign: Campaign) -> str:
    return (
        f"- {campaign.name} | owner {campaign.owner_name} | "
        f"{_date(campaign.start_date)} to {_date(campaign.end_date)} | "
        f"channel {', '.join(campaign.channel) or 'n/a'} | "
        f"planned {format_money(campaign.planned_spend)} | "
        f"expected leads {campaign.expected_leads or 'n/a'} | "
        f"expected CPL {format_cpl(campaign.expected_cpl())} | "
        f"expected ROI {format_percent(campaign.expected_roi)}"
    )


def section(title: str, lines: Iterable[str]) -> str:
    line_list = list(lines)
    return f"{title}\n" + "\n".join(line_list or ["- None"])


def format_friday_roundup_email(
    sections: dict[str, list[str]],
    week_start: date,
    week_end: date,
    *,
    preview: bool = False,
) -> tuple[str, str]:
    title = "Friday Marketing Roundup"
    subtitle = f"Week of {_display_date(week_start)} to {_display_date(week_end)}"
    section_order = [
        "Completed",
        "Delayed, with new deadline and reason",
        "Blocked",
        "Not completed, needs rollover",
        "Canceled",
        "Coming next week",
    ]
    counts = {name: len(sections.get(name, [])) for name in section_order}
    labels = {
        "Completed": "Completed",
        "Delayed, with new deadline and reason": "Delayed",
        "Blocked": "Blocked",
        "Not completed, needs rollover": "Needs rollover",
        "Canceled": "Canceled",
        "Coming next week": "Coming next week",
    }
    summary = " | ".join(f"{labels[name]}: {counts[name]}" for name in section_order)

    text_lines = []
    if preview:
        text_lines.extend(["TEST PREVIEW - no Slack message was posted.", ""])
    text_lines.extend([title, subtitle, "", "Summary", summary])
    for name in section_order:
        text_lines.extend(["", f"{name} ({counts[name]})"])
        lines = [_clean_task_line(item) for item in sections.get(name, [])]
        text_lines.extend(lines or ["None"])

    html_sections = []
    for name in section_order:
        items = sections.get(name, [])
        if items:
            item_html = "".join(f"<li>{escape(_clean_task_line(item))}</li>" for item in items)
            list_html = f"<ul>{item_html}</ul>"
        else:
            list_html = '<p class="empty">None</p>'
        html_sections.append(
            f"""
            <section>
              <h2>{escape(name)} <span>{counts[name]}</span></h2>
              {list_html}
            </section>
            """
        )

    preview_html = '<div class="banner">Test preview. No Slack message was posted.</div>' if preview else ""
    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <style>
      body {{
        margin: 0;
        padding: 0;
        background: #f6f7f9;
        color: #1f2933;
        font-family: Arial, Helvetica, sans-serif;
        line-height: 1.45;
      }}
      .wrap {{
        max-width: 720px;
        margin: 0 auto;
        padding: 28px 20px;
      }}
      .card {{
        background: #ffffff;
        border: 1px solid #d9dee6;
        border-radius: 8px;
        overflow: hidden;
      }}
      .header {{
        background: #0f172a;
        color: #ffffff;
        padding: 24px 28px;
      }}
      .header h1 {{
        margin: 0 0 6px;
        font-size: 24px;
        font-weight: 700;
      }}
      .header p {{
        margin: 0;
        color: #cbd5e1;
        font-size: 14px;
      }}
      .banner {{
        margin: 0 0 16px;
        padding: 10px 12px;
        background: #fff7ed;
        border: 1px solid #fed7aa;
        border-radius: 6px;
        color: #9a3412;
        font-size: 14px;
      }}
      .content {{
        padding: 24px 28px 28px;
      }}
      .summary {{
        margin: 0 0 20px;
        padding: 14px 16px;
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 6px;
        font-size: 14px;
      }}
      section {{
        border-top: 1px solid #e5e7eb;
        padding: 18px 0 2px;
      }}
      section:first-of-type {{
        border-top: 0;
      }}
      h2 {{
        margin: 0 0 10px;
        font-size: 16px;
        color: #111827;
      }}
      h2 span {{
        display: inline-block;
        min-width: 22px;
        margin-left: 8px;
        padding: 1px 7px;
        border-radius: 999px;
        background: #e5e7eb;
        color: #374151;
        font-size: 12px;
        text-align: center;
      }}
      ul {{
        margin: 0;
        padding-left: 20px;
      }}
      li {{
        margin: 0 0 8px;
      }}
      .empty {{
        margin: 0 0 8px;
        color: #6b7280;
      }}
      .footer {{
        margin-top: 22px;
        color: #6b7280;
        font-size: 12px;
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <div class="header">
          <h1>{escape(title)}</h1>
          <p>{escape(subtitle)}</p>
        </div>
        <div class="content">
          {preview_html}
          <div class="summary"><strong>Summary:</strong> {escape(summary)}</div>
          {''.join(html_sections)}
          <div class="footer">Generated from Notion task data by Marketing OS Agent.</div>
        </div>
      </div>
    </div>
  </body>
</html>"""
    return "\n".join(text_lines), html


def _date(value: date | None) -> str:
    return value.isoformat() if value else "n/a"


def _display_date(value: date) -> str:
    return f"{value:%b} {value.day}, {value:%Y}"


def _clean_task_line(value: str) -> str:
    line = value.strip()
    if line.startswith("- "):
        line = line[2:]
    return line.replace(" | ", " - ")
