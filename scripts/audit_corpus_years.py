#!/usr/bin/env python3
"""Show how many papers the CORPUS actually holds per publication year, and cross-
check it against the backfill checkpoint's "completed" years. Use this to spot
years that were marked done but came back nearly empty (a fetch that died partway
and still recorded the year), or years never attempted at all.

Read-only: opens the SQLite corpus + checkpoint, prints a table, writes nothing.

    python3 scripts/audit_corpus_years.py --db data/retarats_pubmed.sqlite

Runs anywhere the corpus file is present. On GitHub it's inside the Actions cache,
so run it via the "Audit corpus coverage" workflow, which restores the cache first.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3

# Years inside the target window that hold fewer than this are almost certainly
# incomplete (a real year of retatrutide/metformin/etc. literature is far bigger).
SUSPICIOUS_BELOW = 50


def _table_and_year_col(conn: sqlite3.Connection):
    """Find the papers table and its year column defensively across schema tweaks."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    table = "papers" if "papers" in tables else (tables[0] if tables else "")
    if not table:
        return "", ""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
    year_col = "pub_year" if "pub_year" in cols else next((c for c in cols if "year" in c.lower()), "")
    return table, year_col


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
    table, year_col = _table_and_year_col(conn)
    if not table or not year_col:
        print("Could not find a papers table / year column in the DB.")
        return

    rows = conn.execute(
        f"SELECT substr({year_col},1,4) AS yr, COUNT(DISTINCT pmid) AS n "
        f"FROM {table} WHERE {year_col} <> '' GROUP BY yr ORDER BY yr DESC"
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(DISTINCT pmid) FROM {table}").fetchone()[0]

    completed = set()
    if os.path.exists(args.checkpoint):
        try:
            completed = {int(y) for y in json.loads(open(args.checkpoint).read()).get("completed_years", [])}
        except (ValueError, OSError, json.JSONDecodeError):
            pass

    print(f"Corpus: {total} distinct papers across {len(rows)} years\n")
    print(f"{'YEAR':>6}  {'PAPERS':>8}  {'CHECKPOINT':<11}  FLAG")
    print("-" * 48)
    suspicious = []
    for yr, n in rows:
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

    # Years in the window that produced no rows at all won't appear above.
    present_years = {int(r[0]) for r in rows if str(r[0]).isdigit()}
    from datetime import datetime
    missing = [y for y in range(args.min_year, datetime.utcnow().year + 1) if y not in present_years]
    if missing:
        print(f"\nYears with ZERO papers in [{args.min_year}-present]: {sorted(missing, reverse=True)}")
    if suspicious:
        print(f"Under-populated years (<{SUSPICIOUS_BELOW}): {sorted(suspicious, reverse=True)}")
    if missing or suspicious:
        bad = sorted(set(suspicious) | set(missing))
        print(f"\nRe-fetch these with a forced backfill covering {min(bad)}-{max(bad)} "
              "(Historical backfill -> force = true, min_year/start_year to span them).")
    else:
        print(f"\nAll years >= {args.min_year} look adequately populated.")


if __name__ == "__main__":
    main()
