#!/usr/bin/env python3
"""Backfill citation counts (OpenAlex + Semantic Scholar) for the ranking impact axis.

The combined ``rank_score`` reserves weight for an ``impact`` axis that stays 0
until citation data exists. This script fills ``citation_count`` (and
``citation_source``) onto each paper, non-destructively, from two free, keyless
sources tried in order:

  1. **OpenAlex** (preferred): query by DOI, then PMID. Just needs a contact
     email for the polite pool.
  2. **Semantic Scholar Graph API** (fallback): tried only when OpenAlex returns
     nothing. This covers the ~32 no-DOI papers (matched by PMID) and any DOI
     OpenAlex missed. When available it also fills ``s2_authors`` (JSON list of
     ``{name, authorId, url}``) and ``influential_citation_count`` onto the paper.

``citation_source`` records which source (and cache/api) produced the count, so
the fill is fully auditable. The next curated build feeds ``citation_count`` into
ranking automatically (it is merged onto every evidence row).

WS4 freshness policy: the missing-only fill below covers "new papers daily".
``--rolling-refresh N`` additionally re-queries OpenAlex ONLY (never re-triggers
the Semantic Scholar fallback, which stays fill-only) for the N
OpenAlex-sourced papers with the oldest ``citation_updated_utc``, so citation
counts keep climbing after the first fill instead of freezing forever. If
OpenAlex returns nothing for a refresh candidate, the paper's existing
citation_count/source/timestamp are left untouched (see the ``n is not None``
gate below) -- an upstream hiccup never erases a previously-filled value.

**Recency:** the historical fetch (``run_backfill.py``) walks newest→oldest, so
the papers table is already recency-ordered; ``--max-records`` therefore backfills
the most recent papers first. Pass ``--newest-first`` to be explicit (it sorts
missing papers by ``pub_year`` descending before capping).

Two modes:
  --offline  (default here): NO network. Report how many papers still lack a
             citation count (coverage plan). Safe in the sandbox.
  live       (drop --offline): query OpenAlex, then Semantic Scholar, cached +
             polite, and write citation data back into the papers payload table.

NETWORK REQUIRED for live mode -> run on your machine or the Actions runner.
Set API_CONTACT_EMAIL (or NCBI_EMAIL) in .env so OpenAlex can identify the caller.

    python3 scripts/run_impact_backfill.py --db data/retarats_pubmed.sqlite --max-records 500 --newest-first
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.common import (
    APIConfig,
    is_blankish,
    json_dumps,
    load_payload_table,
    utc_now_iso,
)


def _needs_impact(paper: dict) -> bool:
    return is_blankish(paper.get("citation_count"))


def _pub_year(paper: dict) -> int:
    try:
        return int(str(paper.get("pub_year", "") or "")[:4])
    except (TypeError, ValueError):
        return 0


def _citation_age_days(paper: dict, now=None) -> float:
    from datetime import datetime, timezone
    stamp = paper.get("citation_updated_utc")
    if not stamp:
        return float("inf")
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def openalex_refresh_candidates(papers, limit: int):
    """Oldest-``citation_updated_utc``-first among papers whose CURRENT value
    came from OpenAlex (never Semantic Scholar -- that source is fill-only, see
    module docstring) -- the "rolling refresh of oldest values" policy."""
    openalex_sourced = [p for p in papers if str(p.get("citation_source", "")).startswith("openalex")]
    openalex_sourced.sort(key=lambda p: _citation_age_days(p), reverse=True)
    return openalex_sourced[:limit]


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill citation counts (OpenAlex + Semantic Scholar) for the ranking impact axis.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--offline", action="store_true", help="Coverage report only; no network.")
    ap.add_argument("--max-records", type=int, default=500, help="Max papers to query per chunk (and per run unless --all).")
    ap.add_argument("--all", action="store_true",
                    help="Keep going in --max-records chunks until every paper has a citation count "
                         "(saves after each chunk, so it is resumable). Use for an unattended full fill.")
    ap.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between chunks (be gentle on APIs).")
    ap.add_argument("--newest-first", action="store_true",
                    help="Explicitly sort missing papers by pub_year descending before capping "
                         "(prioritize recent papers). The papers table is already newest-first from "
                         "run_backfill.py, so this mainly guarantees ordering.")
    ap.add_argument("--rolling-refresh", type=int, default=0,
                    help="Also re-query OpenAlex (only -- never Semantic Scholar) for this many "
                         "OpenAlex-sourced papers with the oldest citation_updated_utc. 0 = off.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    papers = load_payload_table(conn, "papers")
    missing = [p for p in papers if _needs_impact(p)]
    if args.newest_first:
        missing.sort(key=_pub_year, reverse=True)
    have = len(papers) - len(missing)
    print(f"Papers: {len(papers)}; with citation_count: {have}; missing: {len(missing)}")

    if args.offline:
        with_doi = sum(1 for p in missing if not is_blankish(p.get("doi")))
        print(f"Offline plan: {len(missing)} need citations; {with_doi} have a DOI (OpenAlex best match), "
              f"{len(missing) - with_doi} would fall back to PMID (OpenAlex, then Semantic Scholar).")
        print("Order: OpenAlex (DOI, then PMID) -> Semantic Scholar (DOI, then PMID) as fallback.")
        print("Run without --offline on a networked machine to fetch. --newest-first prioritizes recent papers.")
        conn.close()
        return

    # --- live ---
    from retarats_pipeline.enrichment.clients import (
        IdentifierMetadataClient,
        semanticscholar_authors,
        semanticscholar_citation_count,
        semanticscholar_influential_count,
    )
    from retarats_pipeline.enrichment.common import CachedHTTPClient, save_payload_rows

    import time

    config = APIConfig.from_env(api_enabled=True, cache_dir=os.path.join("data", "api_cache"))
    client = IdentifierMetadataClient(CachedHTTPClient(config), config)

    # In --all mode we work through every missing paper; otherwise just one chunk.
    work = missing if args.all else missing[: args.max_records]
    chunk = max(1, args.max_records)
    total = len(work)
    filled = filled_openalex = filled_s2 = queried = 0

    for start in range(0, total, chunk):
        batch = work[start:start + chunk]
        updated = []
        for p in batch:
            queried += 1
            doi = str(p.get("doi", ""))
            pmid = str(p.get("pmid", ""))
            # 1) OpenAlex first.
            n, source = client.openalex_cited_by(doi=doi, pmid=pmid)
            s2_data = None
            if n is None:
                # 2) Semantic Scholar fallback (covers no-DOI papers via PMID).
                s2_data, source = client.semanticscholar_paper(doi=doi, pmid=pmid)
                if s2_data is not None:
                    n = semanticscholar_citation_count(s2_data)
            if n is not None:
                p = dict(p)
                p["citation_count"] = n
                p["citation_source"] = source
                p["citation_updated_utc"] = utc_now_iso()
                if source.startswith("semanticscholar") and s2_data is not None:
                    filled_s2 += 1
                    authors = semanticscholar_authors(s2_data)
                    if authors:
                        p["s2_authors"] = json_dumps(authors)
                    infl = semanticscholar_influential_count(s2_data)
                    if infl is not None:
                        p["influential_citation_count"] = infl
                else:
                    filled_openalex += 1
                updated.append(p)
                filled += 1
        # Save after every chunk so a long/unattended run is resumable.
        if updated:
            save_payload_rows(conn, "papers", "pmid", updated, updated_field="citation_updated_utc")
        print(f"  chunk {start // chunk + 1}: queried {queried}/{total}, filled {filled} "
              f"(OpenAlex {filled_openalex}, S2 {filled_s2})", flush=True)
        if args.sleep and start + chunk < total:
            time.sleep(args.sleep)

    refreshed = 0
    if args.rolling_refresh > 0:
        candidates = openalex_refresh_candidates(papers, args.rolling_refresh)
        print(f"Rolling refresh: {len(candidates)} OpenAlex-sourced paper(s) "
              f"(oldest citation_updated_utc first)...", flush=True)
        updated = []
        for p in candidates:
            doi, pmid = str(p.get("doi", "")), str(p.get("pmid", ""))
            n, source = client.openalex_cited_by(doi=doi, pmid=pmid)
            if n is None:
                continue  # upstream had nothing this time; leave the old value untouched
            p = dict(p)
            p["citation_count"] = n
            p["citation_source"] = source
            p["citation_updated_utc"] = utc_now_iso()
            updated.append(p)
            refreshed += 1
        if updated:
            save_payload_rows(conn, "papers", "pmid", updated, updated_field="citation_updated_utc")
        print(f"Rolling refresh: updated {refreshed} of {len(candidates)} candidate(s).")

    conn.close()
    print(f"Live: queried {queried} papers, filled {filled} citation counts "
          f"(OpenAlex {filled_openalex}, Semantic Scholar fallback {filled_s2}).")
    print("Re-run the curated build so ranking picks up the new impact values.")


if __name__ == "__main__":
    main()
