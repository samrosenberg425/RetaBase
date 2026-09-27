#!/usr/bin/env python3
"""Weekly refresh: find PubMed records now flagged retracted/erratum'd and
refresh them in place. Never deletes historical records.

For each active molecule, queries PubMed for:

    (<molecule terms>) AND ("retracted publication"[pt] OR "retraction of
    publication"[pt] OR "published erratum"[pt])

with NO date restriction: this asks "of all papers matching our rules, which
ones currently carry one of these pubtypes" -- so it finds a status change (a
paper retracted long after it was first fetched) regardless of when the
retraction was issued, without needing to guess which upstream date field
would have moved.

The hit set is intersected against PMIDs ALREADY in the corpus (the ``papers``
table) -- this never adds a paper we have not already fetched through the
normal discovery path, it only refreshes ones we already have. Matching PMIDs
are re-fetched via efetch and MERGED onto the existing payload (same pmid):
the refreshed PubMed-backed fields (title/abstract/pubtypes/doi/...) overwrite
their old values, and ``retraction_status``/``retraction_checked_utc`` are
stamped, but every enrichment field this script doesn't know about --
``icite_*``, ``citation_count``/``citation_source``/``citation_updated_utc``,
``influential_citation_count``, ``s2_authors``, ``first_seen_utc``, etc. --
is carried over untouched from the existing row. Nothing is ever deleted or
replaced wholesale.

    python3 scripts/run_retraction_refresh.py --db data/retarats_pubmed.sqlite

NETWORK REQUIRED -> run on your machine or the Actions runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.common import load_payload_table, utc_now_iso  # noqa: E402
from retarats_pipeline.enrichment.registry import load_active_molecules, molecule_query_terms  # noqa: E402
from retarats_pipeline.pubmed import PubMedClient, parse_pubmed_xml  # noqa: E402

DEFAULT_DB = "data/retarats_pubmed.sqlite"

RETRACTION_PUBTYPE_FILTER = (
    '("retracted publication"[pt] OR "retraction of publication"[pt] OR "published erratum"[pt])'
)
RETRACTION_PUBTYPE_MARKERS = {"retracted publication", "retraction of publication", "published erratum"}
REFETCH_BATCH_SIZE = 100


def is_retraction_flagged(pubtypes) -> bool:
    if not pubtypes:
        return False
    lowered = {str(p).strip().lower() for p in pubtypes}
    return bool(lowered & RETRACTION_PUBTYPE_MARKERS)


def retraction_query(molecule: dict) -> str:
    terms = molecule_query_terms(molecule)
    quoted = [f'"{t}"[tiab]' if " " in t else f"{t}[tiab]" for t in terms]
    inner = " OR ".join(quoted)
    return f"({inner}) AND {RETRACTION_PUBTYPE_FILTER}" if inner else ""


def _existing_papers(db_path: str) -> Dict[str, dict]:
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = load_payload_table(conn, "papers")
    finally:
        conn.close()
    return {str(r.get("pmid", "")).strip(): r for r in rows if r.get("pmid")}


def _batched(items: List[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def run(db_path: str = DEFAULT_DB, molecules_csv: str = "config/MOLECULES.csv") -> dict:
    molecules = load_active_molecules(molecules_csv)
    existing_papers = _existing_papers(db_path)
    known_pmids = set(existing_papers)

    client = PubMedClient(email=os.getenv("NCBI_EMAIL", "").strip() or "wsr4-retraction-refresh@example.org",
                          api_key=os.getenv("NCBI_API_KEY", "").strip())

    molecules_failed: List[str] = []
    flagged_pmids: Set[str] = set()
    for m in molecules:
        mol_id = m.get("molecule_id", "")
        query = retraction_query(m)
        if not query:
            continue
        try:
            search = client.esearch(term=query, usehistory=False, retmax=500)
        except Exception as exc:  # noqa: BLE001 -- one molecule's transient failure must not abort the run
            molecules_failed.append(mol_id)
            print(f"  {mol_id}: FAILED ({type(exc).__name__}: {exc})")
            continue
        in_corpus = [pmid for pmid in search.ids if pmid in known_pmids]
        flagged_pmids.update(in_corpus)
        if search.count:
            print(f"  {mol_id}: {search.count} pubtype-flagged upstream, {len(in_corpus)} already in corpus")

    refreshed: List[str] = []
    newly_flagged: List[str] = []
    if flagged_pmids:
        from retarats_pipeline.sinks import LocalSQLiteSink
        sink = LocalSQLiteSink(db_path)
        for batch in _batched(sorted(flagged_pmids), REFETCH_BATCH_SIZE):
            try:
                xml_text = client.efetch_xml(ids=batch)
                records = parse_pubmed_xml(xml_text)
            except Exception as exc:  # noqa: BLE001
                print(f"  refetch batch of {len(batch)} FAILED: {type(exc).__name__}: {exc}")
                continue
            for record in records:
                pmid = record.pmid
                # MERGE the refreshed PubMed-backed fields onto the existing payload --
                # never replace it outright. record.to_dict() only carries PubMed's own
                # fields (title/abstract/pubtypes/doi/...); starting from a fresh dict
                # here would silently erase every enrichment field this script doesn't
                # know about (icite_*, citation_*, s2_authors, first_seen_utc, ...).
                paper = dict(existing_papers.get(pmid) or {})
                paper.update(record.to_dict())
                paper["updated_at_utc"] = utc_now_iso()
                was_flagged = is_retraction_flagged((existing_papers.get(pmid) or {}).get("pubtypes"))
                is_flagged = is_retraction_flagged(paper.get("pubtypes"))
                paper["retraction_status"] = "retracted_or_corrected" if is_flagged else ""
                paper["retraction_checked_utc"] = utc_now_iso()
                sink.upsert_papers([paper])
                refreshed.append(pmid)
                if is_flagged and not was_flagged:
                    newly_flagged.append(pmid)
                    print(f"  {pmid}: NEWLY flagged {paper.get('pubtypes')}")

    ok = len(molecules) == 0 or len(molecules_failed) < len(molecules)
    outcome = "failed" if not ok else ("partial" if molecules_failed else "live")
    result = {
        "molecules_queried": len(molecules),
        "molecules_failed": len(molecules_failed),
        "failed_molecule_ids": molecules_failed,
        "flagged_pmids_in_corpus": sorted(flagged_pmids),
        "refreshed": refreshed,
        "newly_flagged": newly_flagged,
        "ok": ok,
        "outcome": outcome,
    }
    print(f"Retraction/erratum refresh: {len(refreshed)} record(s) refreshed "
          f"({len(newly_flagged)} newly flagged, {len(molecules_failed)} molecule "
          f"quer{'y' if len(molecules_failed) == 1 else 'ies'} failed).")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--molecules", default="config/MOLECULES.csv")
    ap.add_argument("--retrieval-out", default="", help="Write the run summary as JSON here.")
    args = ap.parse_args()
    result = run(db_path=args.db, molecules_csv=args.molecules)
    if args.retrieval_out:
        os.makedirs(os.path.dirname(args.retrieval_out) or ".", exist_ok=True)
        with open(args.retrieval_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
