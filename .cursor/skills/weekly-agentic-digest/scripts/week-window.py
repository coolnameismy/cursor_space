#!/usr/bin/env python3
"""Print the UTC window, ISO week, and existing weekly digest paths."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def iso_week_bounds(iso_year: int, iso_week: int) -> tuple[date, date]:
    start = date.fromisocalendar(iso_year, iso_week, 1)
    return start, start + timedelta(days=6)


def last_complete_iso_week(today: date) -> tuple[int, int]:
    year, week, weekday = today.isocalendar()
    if weekday == 7:
        return year, week
    prev = date.fromisocalendar(year, week, 1) - timedelta(days=1)
    y, w, _ = prev.isocalendar()
    return y, w


def parse_week(value: str) -> tuple[int, int]:
    text = value.strip().upper().replace("_", "-")
    if "W" not in text:
        raise argparse.ArgumentTypeError("expected YYYY-Www, e.g. 2026-W37")
    year_s, week_s = text.split("W", 1)
    year_s = year_s.rstrip("-")
    return int(year_s), int(week_s)


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() or (parent / "README.md").exists():
            return parent
    return Path.cwd()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", type=parse_week, help="ISO week, e.g. 2026-W37")
    parser.add_argument("--days", type=int, help="Rolling last N days ending today (UTC)")
    parser.add_argument("--current", action="store_true", help="Current ISO week through today")
    parser.add_argument("--today", help="Override UTC today as YYYY-MM-DD (for tests)")
    args = parser.parse_args()

    if args.today:
        today = date.fromisoformat(args.today)
    else:
        today = datetime.now(timezone.utc).date()

    rolling_days = None
    if args.days:
        rolling_days = args.days
        start = today - timedelta(days=args.days - 1)
        end = today
        iso_year, iso_week, _ = end.isocalendar()
    elif args.week:
        iso_year, iso_week = args.week
        start, end = iso_week_bounds(iso_year, iso_week)
    elif args.current:
        iso_year, iso_week, _ = today.isocalendar()
        start, week_end = iso_week_bounds(iso_year, iso_week)
        end = min(today, week_end)
    else:
        iso_year, iso_week = last_complete_iso_week(today)
        start, end = iso_week_bounds(iso_year, iso_week)

    week_label = f"{iso_year}-W{iso_week:02d}"
    root = repo_root()
    weekly_dir = root / "docs" / "agent-weekly"
    outfile = weekly_dir / f"{week_label}.md"
    existing = sorted(p.name for p in weekly_dir.glob("????-W??.md")) if weekly_dir.exists() else []

    seen_path = weekly_dir / "_seen.json"
    seen_count = 0
    if seen_path.exists():
        try:
            payload = json.loads(seen_path.read_text(encoding="utf-8"))
            seen_count = len(payload.get("items") or [])
        except json.JSONDecodeError:
            seen_count = -1

    result = {
        "today_utc": today.isoformat(),
        "iso_week": week_label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "mode": "rolling_days" if rolling_days else ("current_week" if args.current else "iso_week"),
        "outfile": str(outfile.relative_to(root)),
        "existing_weeks": existing,
        "seen_items": seen_count,
        "search_month_hints": sorted({start.strftime("%B %Y"), end.strftime("%B %Y")}),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
