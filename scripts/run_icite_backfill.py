#!/usr/bin/env python3
"""Enrich the corpus with NIH iCite metrics (RCR, APT, human/animal/molecular,
triangle coords, clinical flags, field-normalized citation stats).

Additive and non-destructive: writes ``icite_*`` fields onto each paper's JSON
payload. Nothing downstream changes until a later curated build is taught to read
these fields (staged separately, so this enrichment is safe to run on its own).

Resumable: skips papers that already have iCite data; saves after fetching.

    python3 scripts/run_icite_backfill.py --db data/retarats_pubmed.sqlite --newest-first --max-records 20000

NETWORK REQUIRED (icite.od.nih.gov) -> run on your machine or the Actions runner.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.common import (  # noqa: E402
    is_blankish,
    load_payload_table,
    save_payload_rows,
    utc_now_iso,
)
from retarats_pipeline.enrichment.icite import fetch_icite  # noqa: E402

# iCite key -> stored paper field. Prefixed ``icite_`` so it never clobbers the
# existing OpenAlex/S2 ``citation_count`` (we keep both; curation can prefer iCite).
FIELD_MAP = {
    "relative_citation_ratio": "icite_rcr",
    "nih_percentile": "icite_nih_percentile",
    "citation_count": "icite_citation_count",
    "field_citation_rate": "icite_field_citation_rate",
    "expected_citations_per_year": "icite_expected_cpy",
    "citations_per_year": "icite_citations_per_year",
    "apt": "icite_apt",
    "human": "icite_human",
    "animal": "icite_animal",
    "molecular_cellular": "icite_molecular",
    "x_coord": "icite_x_coord",
    "y_coord": "icite_y_coord",
    "is_clinical": "icite_is_clinical",
    # cited_by_clin -> derived into an integer count (icite_clinical_influence) in
    # the loop below, rather than stored raw, to avoid corpus bloat.
}


def _needs(p: dict) -> bool:
    # Consider a paper un-enriched until it has RCR or APT (either implies a hit).
    return is_blankish(p.get("icite_rcr")) and is_blankish(p.get("icite_apt"))


def _year(p: dict) -> int:
    try:
        return int(str(p.get("pub_year", "") or "")[:4])
    except (TypeError, ValueError):
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Enrich corpus with NIH iCite metrics.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--max-records", type=int, default=20000, help="Max papers to enrich this run.")
    ap.add_argument("--all", action="store_true", help="Enrich every missing paper (resumable).")
    ap.add_argument("--newest-first", action="store_true", help="Prioritize recent papers.")
    ap.add_argument("--batch-size", type=int, default=200, help="PMIDs per iCite request.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    papers = load_payload_table(conn, "papers")
    missing = [p for p in papers if _needs(p) and str(p.get("pmid", "")).strip().isdigit()]
    if args.newest_first:
        missing.sort(key=_year, reverse=True)
    print(f"Papers: {len(papers)}; with iCite: {len(papers) - len(missing)}; missing: {len(missing)}")

    work = missing if args.all else missing[: args.max_records]
    if not work:
        print("Nothing to enrich.")
        conn.close()
        return

    pmids = [str(p["pmid"]) for p in work]
    print(f"Fetching iCite for {len(pmids)} papers (batch {args.batch_size})...", flush=True)
    icite = fetch_icite(pmids, batch_size=args.batch_size)

    updated = []
    for p in work:
        rec = icite.get(str(p.get("pmid", "")).strip())
        if not rec:
            continue
        p = dict(p)
        for ik, field in FIELD_MAP.items():
            v = rec.get(ik)
            if v is not None and v != "":
                p[field] = v
        # Clinical influence: how many clinical articles cite this paper (count of
        # PMIDs in iCite's space-separated cited_by_clin), a strong translational signal.
        cbc = rec.get("cited_by_clin")
        if cbc not in (None, ""):
            p["icite_clinical_influence"] = len(str(cbc).split())
        p["icite_updated_utc"] = utc_now_iso()
        updated.append(p)

    if updated:
        save_payload_rows(conn, "papers", "pmid", updated, updated_field="icite_updated_utc")
    conn.close()
    print(f"Enriched {len(updated)} of {len(work)} papers with iCite metrics "
          f"({len(work) - len(updated)} had no iCite record).")
    print("Re-run the curated build so downstream categorization/ranking can use them.")


if __name__ == "__main__":
    main()
