#!/usr/bin/env python3
"""Show how many papers the CORPUS actually holds per publication year, and cross-
check it against the backfill checkpoint's "completed" years. Use this to spot
years that were marked done but came back nearly empty (a fetch that died partway
and still recorded the year), or years never attempted at all.

Read-only: opens the SQLite corpus + checkpoint, prints a table, writes nothing.
Papers are stored as JSON blobs (papers.payload_json), so pub_year is read from
inside the JSON, not a column.

    python3 scripts/audit_corpus_years.py --db data/retarats_pubmed.sqlite

Runs anywhere the corpus file is present. On GitHub it's inside the Actions cache,
so run it via the "Audit corpus coverage" workflow, which restores the cache first.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime

# Years inside the target window that hold fewer than this are almost certainly
# incomplete (a real year of retatrutide/metformin/etc. literature is far bigger).
SUSPICIOUS_BELOW = 50


def _year_counts(conn: sqlite3.Connection):
    """Return (Counter{year: n}, total_papers). Tries JSON1, falls back to Python."""
    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    try:
        rows = conn.execute(
            "SELECT substr(json_extract(payload_json,'$.pub_year'),1,4) AS yr, COUNT(*) AS n "
            "FROM papers WHERE json_extract(payload_json,'$.pub_year') IS NOT NULL "
            "AND json_extract(payload_json,'$.pub_year') <> '' GROUP BY yr"
        ).fetchall()
        return Counter({str(y): int(n) for y, n in rows if y}), total
    except sqlite3.OperationalError:
        counts: Counter = Counter()
        for (payload,) in conn.execute("SELECT payload_json FROM papers"):
            try:
                y = str(json.loads(payload).get("pub_year", ""))[:4]
            except (ValueError, TypeError):
                y = ""
            if y:
                counts[y] += 1
        return counts, total


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-year corpus coverage vs backfill checkpoint.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--checkpoint", default="data/backfill_checkpoint.json")
    ap.add_argument("--min-year", type=int, default=2000, help="Low end of the window to judge completeness.")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No corpus DB at {args.db} (nothing fetched yet, or wrong path).")
        return

    conn = sqlite3.connect(args.db)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "papers" not in tables:
        print(f"No 'papers' table in the DB (found: {tables}).")
        return

    counts, total = _year_counts(conn)

    completed = set()
    if os.path.exists(args.checkpoint):
        try:
            completed = {int(y) for y in json.loads(open(args.checkpoint).read()).get("completed_years", [])}
        except (ValueError, OSError, json.JSONDecodeError):
            pass

    print(f"Corpus: {total} distinct papers across {len(counts)} years\n")
    print(f"{'YEAR':>6}  {'PAPERS':>8}  {'CHECKPOINT':<11}  FLAG")
    print("-" * 48)
    suspicious = []
    for yr in sorted(counts, reverse=True):
        n = counts[yr]
        try:
            y = int(yr)
        except (TypeError, ValueError):
            continue
        done = "done" if y in completed else "-"
        flag = ""
        if args.min_year <= y and n < SUSPICIOUS_BELOW:
            flag = "LOW -> re-fetch"
            suspicious.append(y)
        print(f"{yr:>6}  {n:>8}  {done:<11}  {flag}")

    present_years = {int(y) for y in counts if str(y).isdigit()}
    missing = [y for y in range(args.min_year, datetime.utcnow().year + 1) if y not in present_years]
    if missing:
        print(f"\nYears with ZERO papers in [{args.min_year}-present]: {sorted(missing, reverse=True)}")
    if suspicious:
        print(f"Under-populated years (<{SUSPICIOUS_BELOW}): {sorted(suspicious, reverse=True)}")
    if missing or suspicious:
        bad = sorted(set(suspicious) | set(missing))
        print(f"\nRe-fetch these with a forced backfill covering {min(bad)}-{max(bad)} "
              "(Historical backfill -> force = true, start_year = highest, min_year = lowest).")
    else:
        print(f"\nAll years >= {args.min_year} look adequately populated.")


if __name__ == "__main__":
    main()
