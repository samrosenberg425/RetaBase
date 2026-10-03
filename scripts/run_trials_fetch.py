#!/usr/bin/env python3
"""Fetch ClinicalTrials.gov REGISTRY records for each active molecule.

This is the trial-registry corpus source, kept SEPARATE from the curated
peer-reviewed evidence: results land in their own SQLite DB
(``data/retarats_trials.sqlite``) in a ``trials`` payload table keyed by
``nct_id``. The build step (``build_trials_json.py``) turns that table into the
``exports/curated/trials_data.json`` feed.

Resumable, and safely re-runnable: a molecule's full result set is retrieved via
CT.gov's ``nextPageToken`` pagination (not just the first page), and every trial
returned is upserted -- including ones already known -- so a status change
(RECRUITING -> COMPLETED, a newly-posted result, ...) is captured on the next run
instead of being skipped forever. ``first_seen_utc`` is preserved across upserts;
``last_seen_utc``/``fetched_at_utc`` advance every time a trial is seen again.

Exit status reflects whether the fetch actually produced usable data: a molecule
query that errors out after retries is not silently treated as "zero trials" --
if every molecule query fails, this exits non-zero so the failure is visible
upstream instead of looking like a clean, empty success.

Modes:
    --offline   No network. Report how many molecules would be queried and exit.
    (default)   Live: query CT.gov v2 per molecule (all pages), parse + upsert.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.clients import ClinicalTrialsClient
from retarats_pipeline.enrichment.common import (
    APIConfig,
    CachedHTTPClient,
    load_payload_table,
    save_payload_rows,
    utc_now_iso,
)
from retarats_pipeline.enrichment.registry_stale import mark_stale_rows
from retarats_pipeline.enrichment.registry import (
    load_active_molecules,
    load_registry_expected_empty,
    load_registry_keep,
    molecule_all_names,
    names_molecule,
    normalize_trial,
    trials_query,
)

DEFAULT_DB = "data/retarats_trials.sqlite"
TABLE = "trials"


def _existing_rows(db_path: str) -> Dict[str, dict]:
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = load_payload_table(conn, TABLE)
    finally:
        conn.close()
    return {str(r.get("nct_id", "")).upper(): r for r in rows if r.get("nct_id")}


def run(
    db_path: str = DEFAULT_DB,
    page_size: int = 100,
    max_pages: int = 20,
    refresh: bool = False,
    offline: bool = False,
    molecules_csv: str = "config/MOLECULES.csv",
) -> dict:
    molecules = load_active_molecules(molecules_csv)

    if offline:
        print(f"[offline] {len(molecules)} active molecules would be queried on CT.gov v2.")
        for m in molecules[:10]:
            print(f"  - {m.get('molecule_id')}: {trials_query(m)}")
        if len(molecules) > 10:
            print(f"  ... and {len(molecules) - 10} more")
        return {"molecules": len(molecules), "offline": True, "ok": True}

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    known = {} if refresh else _existing_rows(db_path)

    config = APIConfig.from_env(api_enabled=True)
    http = CachedHTTPClient(config)
    client = ClinicalTrialsClient(http)

    conn = sqlite3.connect(db_path)
    stored_new = 0
    stored_updated = 0
    seen_this_run: set = set()
    molecules_failed: List[str] = []
    molecules_partial: List[str] = []
    complete_molecules: set = set()
    returned_by_mol: Dict[str, int] = {}
    stale_summary: dict = {"marked": 0, "by_molecule": {}, "skipped_anomalies": []}
    filtered_by_mol: Dict[str, List[str]] = {}
    keep = load_registry_keep()
    total_pages = 0
    total_rows_retrieved = 0
    total_reported_sum = 0
    saw_live = False
    saw_cache = False
    try:
        for m in molecules:
            mol_id = m.get("molecule_id", "")
            mol_name = m.get("display_name", "")
            query = trials_query(m)
            page_result = client.search_all(query, page_size=page_size, max_pages=max_pages)
            total_pages += page_result["pages"]
            total_rows_retrieved += page_result["rows_retrieved"]
            if isinstance(page_result.get("total_reported"), int):
                total_reported_sum += page_result["total_reported"]
            if page_result["source"] == "api":
                saw_live = True
            elif page_result["source"] in ("cache", "stale_cache"):
                saw_cache = True
            if not page_result["ok"]:
                molecules_failed.append(mol_id)
                print(f"  {mol_id}: FAILED ({page_result['error']})")
                continue
            if page_result["partial"]:
                molecules_partial.append(mol_id)
            else:
                complete_molecules.add(mol_id)
            returned_by_mol[mol_id] = len(page_result["items"])

            batch: List[dict] = []
            new_count = 0
            updated_count = 0
            now = utc_now_iso()
            names = molecule_all_names(m)
            for study in page_result["items"]:
                parsed = ClinicalTrialsClient.parse_study(study)
                nct = str(parsed.get("nct_id", "")).upper()
                if not nct or nct in seen_this_run:
                    continue
                # Precision guard: CT.gov matches on tokens and server-side synonym expansion
                # ("MT-II" -> every Phase II trial). Keep a trial only if its own record names
                # the molecule; the rest are never stored (and are marked stale if stored before).
                if (mol_id, nct) not in keep and not names_molecule(json.dumps(study), names):
                    filtered_by_mol.setdefault(mol_id, []).append(nct)
                    continue
                seen_this_run.add(nct)
                existing = known.get(nct)
                row = normalize_trial(parsed, molecule_id=mol_id, molecule_name=mol_name)
                row["first_seen_utc"] = (existing or {}).get("first_seen_utc") or now
                row["last_seen_utc"] = now
                row["fetched_at_utc"] = now
                row["enriched_at_utc"] = now
                if existing is None:
                    new_count += 1
                else:
                    updated_count += 1
                batch.append(row)
            if batch:
                save_payload_rows(conn, TABLE, "nct_id", batch)
                stored_new += new_count
                stored_updated += updated_count
            flag = " [partial]" if page_result["partial"] else ""
            print(f"  {mol_id}: +{new_count} new, {updated_count} updated "
                  f"({page_result['pages']} page(s), {page_result['source']}){flag}")
        # Rows stored by an OLDER/different query that this run's complete retrieval no
        # longer returns are marked stale (held out of the feeds, not deleted).
        stale_summary = mark_stale_rows(conn, TABLE, "nct_id", seen_this_run, complete_molecules,
                                        returned_by_mol, utc_now_iso(), key_norm=lambda k: str(k).upper(),
                                        expected_empty=load_registry_expected_empty())
    finally:
        conn.close()
    if filtered_by_mol:
        topf = sorted(filtered_by_mol.items(), key=lambda kv: -len(kv[1]))[:8]
        print(f"Skipped {sum(len(v) for v in filtered_by_mol.values())} CT.gov hit(s) whose record does not name the "
              f"molecule: {', '.join(f'{m}={len(n)}' for m, n in topf)}")
        # Full audit trail (log only): exactly which records were skipped, per molecule, so a wrongly
        # skipped one can be added to config/registry_keep.csv.
        for m, ids in sorted(filtered_by_mol.items()):
            print(f"  skipped[{m}] ({len(ids)}): {' '.join(ids[:40])}{' ...' if len(ids) > 40 else ''}")
    if stale_summary["marked"]:
        top = sorted(stale_summary["by_molecule"].items(), key=lambda kv: -kv[1])[:8]
        print(f"Marked {stale_summary['marked']} stored trial(s) stale (no longer returned by their molecule's "
              f"query): {', '.join(f'{m}={n}' for m, n in top)}")
    if stale_summary["skipped_anomalies"]:
        print(f"  NOT reconciled (empty result for a molecule with many stored trials; treated as an API "
              f"anomaly): {', '.join(stale_summary['skipped_anomalies'])}")

    ok = len(molecules) == 0 or len(molecules_failed) < len(molecules)
    outcome = (
        "failed" if not ok else
        "partial" if (molecules_failed or molecules_partial) else
        "live" if saw_live else
        "cached" if saw_cache else "live"
    )
    retrieval = {
        "molecules_queried": len(molecules),
        "molecules_failed": len(molecules_failed),
        "molecules_partial": len(molecules_partial),
        "pages": total_pages,
        "rows_retrieved": total_rows_retrieved,
        "total_reported": total_reported_sum,
        "exhausted": not molecules_failed and not molecules_partial,
        "partial": bool(molecules_failed or molecules_partial),
        "failed_molecule_ids": molecules_failed,
        "partial_molecule_ids": molecules_partial,
    }
    print(f"Stored {stored_new} new trials, updated {stored_updated} known -> {db_path}")
    if molecules_failed:
        print(f"  {len(molecules_failed)} molecule quer{'y' if len(molecules_failed) == 1 else 'ies'} FAILED: "
              f"{', '.join(molecules_failed)}")
    if molecules_partial:
        print(f"  {len(molecules_partial)} molecule quer{'y' if len(molecules_partial) == 1 else 'ies'} PARTIAL: "
              f"{', '.join(molecules_partial)}")
    return {
        "molecules": len(molecules),
        "stored_new": stored_new,
        "stored_updated": stored_updated,
        "ok": ok,
        "outcome": outcome,
        "retrieval": retrieval,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch ClinicalTrials.gov registry trials per molecule.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--page-size", type=int, default=100, help="Studies per CT.gov page.")
    ap.add_argument("--max-pages", type=int, default=20, help="Safety cap on pages fetched per molecule query.")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch and overwrite already-stored NCTs from scratch "
                                                            "(drops first_seen_utc instead of preserving it).")
    ap.add_argument("--offline", action="store_true", help="No network; just report molecule counts.")
    ap.add_argument("--molecules", default="config/MOLECULES.csv")
    ap.add_argument("--retrieval-out", default="", help="Write the retrieval-completeness summary as JSON here.")
    args = ap.parse_args()
    result = run(
        db_path=args.db,
        page_size=args.page_size,
        max_pages=args.max_pages,
        refresh=args.refresh,
        offline=args.offline,
        molecules_csv=args.molecules,
    )
    if args.retrieval_out and "retrieval" in result:
        os.makedirs(os.path.dirname(args.retrieval_out) or ".", exist_ok=True)
        with open(args.retrieval_out, "w", encoding="utf-8") as fh:
            json.dump({"outcome": result["outcome"], **result["retrieval"]}, fh, indent=2, sort_keys=True)
    if not result.get("ok", True):
        sys.exit(1)


if __name__ == "__main__":
    main()
