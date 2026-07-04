#!/usr/bin/env python3
"""Validate + backfill missing paper metadata from multiple APIs.

Two modes:

  --offline  (default here): NO network. Assess every paper and report which
             identity/metadata fields are missing and which source would fill
             them. Writes exports/curated/metadata_backfill_plan.csv and prints
             a coverage summary. Safe to run anywhere, including this sandbox.

  live       (drop --offline): actually query EuropePMC / Crossref / PMC id
             converter / Unpaywall for incomplete records (cached, polite) and
             write exports/curated/metadata_backfill_audit.csv with proposed
             ``backfilled_*`` values and provenance. Non-destructive: nothing in
             the SQLite is overwritten.

NOTE: NCBI/EuropePMC/Crossref must be reachable, so run *live* mode on a machine
with outbound network (e.g. your Mac's research env), not in the offline sandbox.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.backfill import assess_paper
from retarats_pipeline.enrichment.common import APIConfig, load_payload_table


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate and backfill missing paper metadata.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--out-dir", default="exports/curated")
    ap.add_argument("--offline", action="store_true", help="Coverage/plan only; no network calls.")
    ap.add_argument("--max-records", type=int, default=100, help="Max incomplete papers to backfill live.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    papers = load_payload_table(conn, "papers")
    conn.close()
    os.makedirs(args.out_dir, exist_ok=True)

    reports = [assess_paper(p) for p in papers]
    incomplete = [r for r in reports if r.has_gaps]

    field_gaps: Counter = Counter()
    for r in incomplete:
        for f in r.missing:
            field_gaps[f] += 1

    plan_path = os.path.join(args.out_dir, "metadata_backfill_plan.csv")
    with open(plan_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pmid", "missing_fields", "backfill_sources"])
        for r in incomplete:
            sources = "; ".join(f"{f}<-{'>'.join(r.backfillable.get(f, []))}" for f in r.missing)
            w.writerow([r.pmid, "; ".join(r.missing), sources])

    print(f"Assessed {len(papers)} papers; {len(incomplete)} have metadata gaps.")
    print("Missing-field counts:", dict(field_gaps.most_common()))
    print(f"Wrote plan -> {plan_path}")

    if args.offline:
        print("\nOffline mode: no network calls made. Run without --offline on a networked")
        print("machine to fetch and write metadata_backfill_audit.csv.")
        return

    # --- live backfill ---
    from retarats_pipeline.enrichment.backfill import MetadataBackfiller

    config = APIConfig.from_env(api_enabled=True, cache_dir=os.path.join("data", "api_cache"))
    backfiller = MetadataBackfiller(config)
    audit_path = os.path.join(args.out_dir, "metadata_backfill_audit.csv")

    incomplete_papers = [p for p in papers if assess_paper(p).has_gaps][: args.max_records]
    rows = []
    filled_total = 0
    for p in incomplete_papers:
        res = backfiller.backfill_paper(p)
        filled_total += len(res.proposals)
        rows.append(
            {
                "pmid": res.pmid,
                "attempted_sources": "; ".join(res.attempted),
                "proposals": "; ".join(f"{k}={v[:80]}" for k, v in res.proposals.items()),
                "provenance": "; ".join(f"{k}={v}" for k, v in res.provenance.items()),
                "notes": res.notes,
            }
        )
    with open(audit_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pmid", "attempted_sources", "proposals", "provenance", "notes"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nLive backfill: processed {len(incomplete_papers)} papers, proposed {filled_total} field fills.")
    print(f"Wrote audit -> {audit_path}")


if __name__ == "__main__":
    main()
