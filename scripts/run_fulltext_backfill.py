#!/usr/bin/env python3
"""Backfill open-access FULL TEXT (Methods/Results) onto corpus papers.

Why this matters more than any extractor change: an audit of the corpus
(`scripts/audit_missing_fields.py`) found ~90% of records have no dose, and ~96% of
those gaps exist because the ABSTRACT never states it. No rule and no model can
recover a value that isn't in its input. On a verified 12-paper sample the rules
found a dose on 2/12 from abstracts alone and 12/12 once full text was supplied.

Stores ``fulltext_methods`` / ``fulltext_results`` / ``pmcid`` on the paper record;
``refine_extraction`` picks them up automatically for dose/route/duration/sample
size (deliberately NOT for model or outcome classification).

Resumable and polite: PMIDs are resolved to PMCIDs in bulk (200 per request via the
PMC ID Converter), each paper is attempted once (``fulltext_attempted_utc``), and
non-open-access papers are recorded as attempted so they are never retried.

    python3 scripts/run_fulltext_backfill.py --db data/retarats_pubmed.sqlite --max-records 5000
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from retarats_pipeline.enrichment import context as ctxmod  # noqa: E402

FULLTEXT_SCHEMA = 1


def _needs(paper: dict) -> bool:
    """Attempt a paper once; re-attempt only if the schema version moved on."""
    if paper.get("fulltext_methods") or paper.get("fulltext_results"):
        return False
    if not paper.get("fulltext_attempted_utc"):
        return True
    try:
        return int(paper.get("fulltext_schema", 0)) < FULLTEXT_SCHEMA
    except (TypeError, ValueError):
        return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--max-records", type=int, default=5000)
    ap.add_argument("--cache-dir", default=".cache/context")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No corpus at {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    try:
        rows = conn.execute("select rowid, payload_json from papers").fetchall()
    except sqlite3.OperationalError:
        print("No 'papers' table in the corpus.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    todo = []
    for rowid, payload in rows:
        try:
            p = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if p.get("pmid") and _needs(p):
            todo.append((rowid, p))
    todo = todo[:args.max_records]
    print(f"{len(rows)} papers in corpus; {len(todo)} need full-text enrichment.")
    if not todo or args.dry_run:
        conn.close()
        return

    pmids = [str(p["pmid"]) for _, p in todo]
    pmcid_map = ctxmod.pmc_ids_bulk(pmids, args.cache_dir)
    print(f"Open access: {len(pmcid_map)}/{len(pmids)} "
          f"({100 * len(pmcid_map) // max(len(pmids), 1)}%) have full text available.")

    now = dt.datetime.utcnow().isoformat() + "Z"
    stored = empty = 0
    for i, (rowid, p) in enumerate(todo, 1):
        pmid = str(p["pmid"])
        pmcid = pmcid_map.get(pmid, "")
        methods = results = ""
        if pmcid:
            bio = ctxmod.bioc_fulltext(pmcid, args.cache_dir)
            methods, results = bio.get("methods", ""), bio.get("results", "")
            if not (methods or results):  # BioC empty -> Europe PMC fallback
                ft = ctxmod.europepmc_fulltext(pmid, args.cache_dir)
                methods, results = ft.get("methods", ""), ft.get("results", "")
        p["pmcid"] = pmcid
        p["fulltext_methods"] = methods
        p["fulltext_results"] = results
        p["fulltext_attempted_utc"] = now
        p["fulltext_schema"] = FULLTEXT_SCHEMA
        conn.execute("update papers set payload_json = ? where rowid = ?",
                     (json.dumps(p, ensure_ascii=False), rowid))
        if methods or results:
            stored += 1
        else:
            empty += 1
        if i % 200 == 0:
            conn.commit()
            print(f"  ...{i}/{len(todo)} ({stored} with full text)")
    conn.commit()
    conn.close()
    print(f"Done. {stored} papers gained full text; {empty} had none available.")


if __name__ == "__main__":
    main()
