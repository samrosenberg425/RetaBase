#!/usr/bin/env python3
"""Gold-standard retrieval regression check (OFFLINE, against the built corpus).

For each molecule listed in config/gold_standard_pmids.csv, verify the corpus
CONTAINS the PMIDs it MUST retrieve (known-relevant) and does NOT contain the
ones it MUST NOT (known off-topic / wrong-entity). This catches search-rule
regressions -- e.g. a tightened query that silently drops a known landmark paper,
or a broadened one that pulls in a wrong-molecule collision.

    python3 scripts/check_gold_standard.py --db data/retarats_pubmed.sqlite

Exit 0 if every gold set passes (or the file has no filled rows); exit 1 on any
regression, so it can gate CI. No network required -- it reads the corpus that the
search rules already produced (the evidence table maps pmid -> molecule).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from typing import Dict, List, Set


def _pmids(s) -> Set[str]:
    return set(re.findall(r"\d+", str(s or "")))


def _load_gold(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    ap = argparse.ArgumentParser(description="Gold-standard retrieval regression check (offline).")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--gold", default="config/gold_standard_pmids.csv")
    args = ap.parse_args()

    gold = _load_gold(args.gold)
    active = [r for r in gold if _pmids(r.get("must_retrieve_pmids")) or _pmids(r.get("must_not_retrieve_pmids"))]
    if not active:
        print("No gold-standard rows filled in yet; nothing to check.")
        return

    if not os.path.exists(args.db):
        print(f"No corpus DB at {args.db}; run a backfill first.")
        sys.exit(1)

    # molecule_id -> set of PMIDs the searches actually stored for it.
    mol_pmids: Dict[str, Set[str]] = {}
    conn = sqlite3.connect(args.db)
    try:
        cur = conn.execute("select payload_json from evidence")
    except sqlite3.OperationalError:
        print("No 'evidence' table in the corpus.")
        conn.close()
        sys.exit(1)
    for (payload,) in cur:
        try:
            e = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        m, p = str(e.get("molecule_id", "")), str(e.get("pmid", ""))
        if m and p:
            mol_pmids.setdefault(m, set()).add(p)
    conn.close()

    ok = True
    for row in active:
        mid = (row.get("molecule_id", "") or "").strip()
        must = _pmids(row.get("must_retrieve_pmids"))
        must_not = _pmids(row.get("must_not_retrieve_pmids"))
        have = mol_pmids.get(mid, set())
        missing = must - have
        wrong = must_not & have
        if missing or wrong:
            ok = False
            print(f"FAIL  {mid}: missing must-retrieve {sorted(missing) or '-'}; "
                  f"contains must-not {sorted(wrong) or '-'}")
        else:
            print(f"OK    {mid}: {len(must)} must-retrieve present, {len(must_not)} must-not absent")

    if not ok:
        print("\nGold-standard retrieval regression detected.")
        sys.exit(1)
    print("\nAll gold-standard sets pass.")


if __name__ == "__main__":
    main()
