#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from marketing_os_agent.clients.notion import NotionClient
from marketing_os_agent.config import Settings
from marketing_os_agent.logging_config import configure_logging


REQUIRED_COLUMNS = {
    "Campaign name",
    "Owner Notion User ID",
    "Trade",
    "Channel",
    "Start Date",
    "End Date",
    "Status",
    "Planned Spend",
    "Expected Leads",
    "Expected ROI",
    "Actual Spend",
    "Actual Leads",
    "Actual ROI",
    "Notes",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Marketing Calendar campaigns from CSV into Notion.")
    parser.add_argument("csv_path", help="CSV file with Marketing Calendar columns.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print rows without writing to Notion.")
    args = parser.parse_args()

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    notion = NotionClient(settings)

    with open(args.csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV missing required columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    logging.getLogger(__name__).info("campaign_csv_loaded", extra={"rows": len(rows), "dry_run": args.dry_run})
    if args.dry_run:
        for row in rows:
            print(row["Campaign name"])
        return 0

    for row in rows:
        notion.create_campaign(row)
        logging.getLogger(__name__).info("campaign_imported", extra={"campaign": row["Campaign name"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
