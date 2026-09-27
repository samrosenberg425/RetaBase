#!/usr/bin/env python3
"""Enrich the corpus with NIH iCite metrics (RCR, APT, human/animal/molecular,
triangle coords, clinical flags, field-normalized citation stats).

Additive and non-destructive: writes ``icite_*`` fields onto each paper's JSON
payload. Nothing downstream changes until a later curated build is taught to read
these fields (staged separately, so this enrichment is safe to run on its own).

Resumable: skips papers that already have iCite data; saves after fetching.

WS4 freshness policy: daily runs (the default -- missing-only) top up NEW papers;
``--refresh-older-than-days N`` additionally re-enriches papers whose iCite data
is older than N days, oldest-first, so the FULL corpus rotates through a refresh
on roughly a weekly cadence (citation counts/RCR are not static -- a paper's
metrics keep changing after it's first enriched). A refresh attempt that gets no
usable iCite record back leaves the paper's existing values untouched (see
``got_any`` below) -- an upstream hiccup never erases previously-fetched data.

    python3 scripts/run_icite_backfill.py --db data/retarats_pubmed.sqlite --newest-first --max-records 20000
    python3 scripts/run_icite_backfill.py --db data/retarats_pubmed.sqlite --refresh-older-than-days 6 --all

NETWORK REQUIRED (icite.od.nih.gov) -> run on your machine or the Actions runner.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import List, Optional

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
    "is_research_article": "icite_is_research_article",
    # cited_by_clin -> derived into an integer count (icite_clinical_influence) in
    # the loop below, rather than stored raw, to avoid corpus bloat.
}


# Bump this whenever FIELD_MAP / derived fields change, so a later run tops up
# papers that were enriched under an older field set (audit-and-add, no full reset).
ICITE_SCHEMA = 2


def _needs(p: dict) -> bool:
    """A paper needs (re-)enrichment if it has never been enriched, OR it was
    enriched under an older field schema (so it's missing newer fields). Papers
    iCite responded to but had NO record for are marked attempted and skipped, so
    we don't re-query them every run."""
    has_core = not (is_blankish(p.get("icite_rcr")) and is_blankish(p.get("icite_apt")))
    if has_core:
        try:
            return int(p.get("icite_schema", 0) or 0) < ICITE_SCHEMA
        except (TypeError, ValueError):
            return True
    # No iCite metrics: needs enrichment unless we already tried and iCite had none.
    return is_blankish(p.get("icite_attempted_utc"))


def _year(p: dict) -> int:
    try:
        return int(str(p.get("pub_year", "") or "")[:4])
    except (TypeError, ValueError):
        return 0


def _icite_age_days(p: dict, now: Optional[datetime] = None) -> Optional[float]:
    """Days since this paper's iCite data was last refreshed, or None if it has
    never been enriched (handled separately by ``_needs``)."""
    stamp = p.get("icite_updated_utc")
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def _stale(p: dict, refresh_older_than_days: float, now: Optional[datetime] = None) -> bool:
    """A paper that already has iCite data is due for a refresh once it's older
    than the cutoff -- this is what makes the weekly job re-check the corpus
    that the daily (missing-only) job never touches again."""
    age = _icite_age_days(p, now)
    return age is not None and age >= refresh_older_than_days


def main() -> None:
    ap = argparse.ArgumentParser(description="Enrich corpus with NIH iCite metrics.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--max-records", type=int, default=20000, help="Max papers to enrich this run.")
    ap.add_argument("--all", action="store_true", help="Enrich every missing paper (resumable).")
    ap.add_argument("--newest-first", action="store_true", help="Prioritize recent papers.")
    ap.add_argument("--refresh-older-than-days", type=float, default=0,
                    help="Also re-enrich papers whose iCite data is older than this many days "
                         "(oldest-first), not just genuinely missing ones. 0 = off (daily default).")
    ap.add_argument("--batch-size", type=int, default=200, help="PMIDs per iCite request.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    papers = load_payload_table(conn, "papers")
    now = datetime.now(timezone.utc)
    missing = [p for p in papers if _needs(p) and str(p.get("pmid", "")).strip().isdigit()]
    if args.newest_first:
        missing.sort(key=_year, reverse=True)
    print(f"Papers: {len(papers)}; with iCite: {len(papers) - len(missing)}; missing: {len(missing)}")

    stale: List[dict] = []
    if args.refresh_older_than_days > 0:
        missing_pmids = {p.get("pmid") for p in missing}
        stale = [p for p in papers if p.get("pmid") not in missing_pmids and str(p.get("pmid", "")).strip().isdigit()
                and _stale(p, args.refresh_older_than_days, now)]
        # Oldest-refreshed first, so a bounded --max-records rotates through the
        # WHOLE corpus over successive weekly runs rather than always hitting the
        # same subset.
        stale.sort(key=lambda p: _icite_age_days(p, now) or 0, reverse=True)
        if stale:
            print(f"Also refreshing {len(stale)} paper(s) with iCite data older than "
                  f"{args.refresh_older_than_days:g} day(s).")

    work = (missing + stale) if args.all else (missing + stale)[: args.max_records]
    if not work:
        print("Nothing to enrich.")
        conn.close()
        return

    pmids = [str(p["pmid"]) for p in work]
    print(f"Fetching iCite for {len(pmids)} papers (batch {args.batch_size})...", flush=True)
    icite = fetch_icite(pmids, batch_size=args.batch_size)

    got_any = bool(icite)  # did the API actually respond? (guards against outages)
    updated = []
    for p in work:
        rec = icite.get(str(p.get("pmid", "")).strip())
        if not rec:
            # Responded but no iCite record for this PMID -> stamp attempted so we
            # don't re-query it forever. Total-failure batches (got_any False) are
            # left untouched to retry next run.
            if got_any:
                p = dict(p)
                p["icite_attempted_utc"] = utc_now_iso()
                updated.append(p)
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
        p["icite_schema"] = ICITE_SCHEMA
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
