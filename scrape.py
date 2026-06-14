#!/usr/bin/env python3
"""Scrape the TRIMARC RSS feed into an append-only CSV log.

For each <item> in the feed we record (title, link, description, pubDate),
plus the incident id parsed from the title and a UTC ``first_seen`` timestamp.

Update policy "C" (append every change): the CSV is append-only. A new row is
written the first time an incident is seen, and again every time any tracked
field changes -- so the file preserves the full edit history of every notice,
including edits the feed itself eventually drops as items roll off.

Only stdlib is used so the GitHub Action needs no pip install. The feed is
fetched and fully parsed before anything is written to disk, so a failed fetch
or malformed XML leaves the existing data untouched.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEED_URL = "https://www.trimarc.org/rss/trimarcrss.xml"

DATA_DIR = Path(__file__).resolve().parent / "data"
CSV_PATH = DATA_DIR / "trimarc.csv"
XML_PATH = DATA_DIR / "trimarcrss.xml"

# Column order written to the CSV.
FIELDNAMES = ["incident_id", "title", "link", "description", "pubDate", "first_seen"]
# Fields compared to decide whether an incident has a new version.
TRACKED = ["title", "link", "description", "pubDate"]

USER_AGENT = "trimarc-git-scraper (+https://github.com/markschaver/TRIMARC)"


def fetch(url: str) -> bytes:
    """Return the raw feed bytes, or raise on a network/HTTP error."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def extract_incident_id(title: str) -> str:
    """The leading number in the title is TRIMARC's stable incident id."""
    match = re.match(r"\s*(\d+)", title)
    return match.group(1) if match else ""


def match_key(record: dict[str, str]) -> str:
    """Key used to track an incident across runs (id, or title as a fallback)."""
    return record["incident_id"] or record["title"]


def parse_items(raw: bytes) -> list[dict[str, str]]:
    """Parse <item> elements into records of the tracked fields + incident id."""
    root = ET.fromstring(raw)
    items: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        record = {field: (item.findtext(field) or "").strip() for field in TRACKED}
        record["incident_id"] = extract_incident_id(record["title"])
        items.append(record)
    return items


def load_existing(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def latest_versions(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Map each incident key to its most recently recorded row."""
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        latest[match_key(row)] = row
    return latest


def main() -> int:
    raw = fetch(FEED_URL)
    items = parse_items(raw)  # raises on malformed XML before we touch disk

    existing = load_existing(CSV_PATH)
    latest = latest_versions(existing)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_rows: list[dict[str, str]] = []
    for record in items:
        key = match_key(record)
        previous = latest.get(key)
        if previous is None or any(record[f] != previous.get(f, "") for f in TRACKED):
            row = {field: record.get(field, "") for field in FIELDNAMES}
            row["first_seen"] = now
            new_rows.append(row)
            # Track within this run too, so a duplicate id in one feed
            # doesn't produce two rows.
            latest[key] = record

    if not new_rows:
        print(f"No changes: {len(items)} items in feed, {len(existing)} rows on file.")
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    # Snapshot the raw feed alongside the CSV as the source of truth. It is
    # only rewritten when something changed, so unchanged fetches (whose only
    # difference is the feed's lastBuildDate) don't create noise commits.
    XML_PATH.write_bytes(raw)

    print(f"Appended {len(new_rows)} row(s); {len(items)} items in feed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # surface the failure in the Action log, commit nothing
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
