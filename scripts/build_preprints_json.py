#!/usr/bin/env python3
"""Build exports/curated/preprints_data.json from the preprints SQLite table.

Reads the ``preprints`` payload table written by ``run_preprints_fetch.py`` and
emits a compact JSON feed sorted by date descending. Empty/absent DB -> empty
(valid) feed.
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

DEFAULT_DB = "data/retarats_preprints.sqlite"
DEFAULT_OUT = "exports/curated/preprints_data.json"
TABLE = "preprints"

COMPACT_FIELDS = [
    "id", "molecule_id", "molecule_name", "title", "authors_short",
    "server", "date", "doi", "url",
]


def _load_preprints(db_path: str) -> List[dict]:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        return load_payload_table(conn, TABLE)
    finally:
        conn.close()


def _compact(row: Dict) -> Dict:
    return {k: row.get(k, "") for k in COMPACT_FIELDS}


def build(db_path: str = DEFAULT_DB, out_path: str = DEFAULT_OUT) -> dict:
    preprints = _load_preprints(db_path)
    compact = [_compact(p) for p in preprints]
    # date descending; blank dates sink to the bottom.
    compact.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    payload = {
        "generated_utc": utc_now_iso(),
        "count": len(compact),
        "preprints": compact,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
    return {"count": len(compact), "out": out_path}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build preprints_data.json from the preprints SQLite table.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    result = build(args.db, args.out)
    print(f"Wrote {result['count']} preprints -> {result['out']}")


if __name__ == "__main__":
    main()
