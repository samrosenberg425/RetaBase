#!/usr/bin/env python3
"""Build exports/curated/trials_data.json from the trials SQLite table.

Reads the ``trials`` payload table written by ``run_trials_fetch.py`` and emits a
compact, ongoing-first JSON feed for the UI. An empty or absent DB yields an
empty (valid) feed rather than crashing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.common import load_payload_table, utc_now_iso

DEFAULT_DB = "data/retarats_trials.sqlite"
DEFAULT_OUT = "exports/curated/trials_data.json"
TABLE = "trials"

COMPACT_FIELDS = [
    "nct_id", "molecule_id", "molecule_name", "brief_title", "overall_status",
    "phases", "study_type", "conditions", "interventions", "enrollment_count",
    "start_date", "primary_completion_date", "completion_date", "lead_sponsor",
    "has_results", "result_pmids", "reference_pmids", "url", "ongoing",
]


def _load_trials(db_path: str) -> List[dict]:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        return load_payload_table(conn, TABLE)
    finally:
        conn.close()


def _compact(row: Dict) -> Dict:
    return {k: row.get(k, "") for k in COMPACT_FIELDS}


def _sort_key(row: Dict):
    # ongoing-first, then start_date descending (string ISO-ish dates sort well).
    return (0 if row.get("ongoing") else 1, _neg_date(row.get("start_date", "")))


def _neg_date(date: str):
    # Reverse-sort dates by returning a tuple that inverts lexical order.
    return tuple(-ord(c) for c in str(date or ""))


def build(db_path: str = DEFAULT_DB, out_path: str = DEFAULT_OUT) -> dict:
    trials = _load_trials(db_path)
    compact = [_compact(t) for t in trials]
    compact.sort(key=_sort_key)
    ongoing_count = sum(1 for t in compact if t.get("ongoing"))
    payload = {
        "generated_utc": utc_now_iso(),
        "count": len(compact),
        "ongoing_count": ongoing_count,
        "trials": compact,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
    return {"count": len(compact), "ongoing_count": ongoing_count, "out": out_path}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build trials_data.json from the trials SQLite table.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    result = build(args.db, args.out)
    print(f"Wrote {result['count']} trials ({result['ongoing_count']} ongoing) -> {result['out']}")


if __name__ == "__main__":
    main()
