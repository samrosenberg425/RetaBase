#!/usr/bin/env python3
"""Fetch ClinicalTrials.gov REGISTRY records for each active molecule.

This is the trial-registry corpus source, kept SEPARATE from the curated
peer-reviewed evidence: results land in their own SQLite DB
(``data/retarats_trials.sqlite``) in a ``trials`` payload table keyed by
``nct_id``. The build step (``build_trials_json.py``) turns that table into the
``exports/curated/trials_data.json`` feed.

Non-destructive / resumable: NCTs already stored are skipped unless ``--refresh``.

Modes:
    --offline   No network. Report how many molecules would be queried and exit.
    (default)   Live: query CT.gov v2 per molecule, parse + normalize, upsert.

Live network is only available on the user's machine / GitHub Actions.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import List, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.clients import ClinicalTrialsClient
from retarats_pipeline.enrichment.common import (
    APIConfig,
    CachedHTTPClient,
    load_payload_table,
    save_payload_rows,
    utc_now_iso,
)
from retarats_pipeline.enrichment.registry import (
    load_active_molecules,
    normalize_trial,
    trials_query,
)

DEFAULT_DB = "data/retarats_trials.sqlite"
TABLE = "trials"


def _existing_ncts(db_path: str) -> Set[str]:
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = load_payload_table(conn, TABLE)
    finally:
        conn.close()
    return {str(r.get("nct_id", "")).upper() for r in rows if r.get("nct_id")}


def run(
    db_path: str = DEFAULT_DB,
    page_size: int = 20,
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
        return {"molecules": len(molecules), "offline": True}

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    known = set() if refresh else _existing_ncts(db_path)

    config = APIConfig.from_env(api_enabled=True)
    http = CachedHTTPClient(config)
    client = ClinicalTrialsClient(http)

    conn = sqlite3.connect(db_path)
    stored = 0
    skipped = 0
    seen_this_run: Set[str] = set()
    try:
        for m in molecules:
            mol_id = m.get("molecule_id", "")
            mol_name = m.get("display_name", "")
            query = trials_query(m)
            studies, source = client.search(query, page_size=page_size)
            batch: List[dict] = []
            for study in studies:
                parsed = ClinicalTrialsClient.parse_study(study)
                nct = str(parsed.get("nct_id", "")).upper()
                if not nct or nct in seen_this_run:
                    continue
                if not refresh and nct in known:
                    skipped += 1
                    continue
                seen_this_run.add(nct)
                row = normalize_trial(parsed, molecule_id=mol_id, molecule_name=mol_name)
                row["enriched_at_utc"] = utc_now_iso()
                batch.append(row)
            if batch:
                save_payload_rows(conn, TABLE, "nct_id", batch)
                stored += len(batch)
            print(f"  {mol_id}: +{len(batch)} trials ({source})")
    finally:
        conn.close()

    print(f"Stored {stored} new trials, skipped {skipped} already-known -> {db_path}")
    return {"molecules": len(molecules), "stored": stored, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch ClinicalTrials.gov registry trials per molecule.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--page-size", type=int, default=20, help="Studies per molecule query.")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch and overwrite already-stored NCTs.")
    ap.add_argument("--offline", action="store_true", help="No network; just report molecule counts.")
    ap.add_argument("--molecules", default="config/MOLECULES.csv")
    args = ap.parse_args()
    run(
        db_path=args.db,
        page_size=args.page_size,
        refresh=args.refresh,
        offline=args.offline,
        molecules_csv=args.molecules,
    )


if __name__ == "__main__":
    main()
