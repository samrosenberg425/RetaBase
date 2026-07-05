#!/usr/bin/env python3
"""Fetch PREPRINTS (EuropePMC SRC:PPR) for each active molecule.

Preprints are non-peer-reviewed and stored SEPARATELY from the curated evidence,
in their own SQLite DB (``data/retarats_preprints.sqlite``), ``preprints`` table
keyed by a stable id (DOI when present, else the EuropePMC preprint id). The
builder (``build_preprints_json.py``) emits ``exports/curated/preprints_data.json``.

Non-destructive / resumable: existing ids are skipped unless ``--refresh``.

Modes:
    --offline   No network; report molecule coverage and exit.
    (default)   Live: query EuropePMC per molecule, normalize, upsert.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import List, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.clients import IdentifierMetadataClient
from retarats_pipeline.enrichment.common import (
    APIConfig,
    CachedHTTPClient,
    load_payload_table,
    save_payload_rows,
    utc_now_iso,
)
from retarats_pipeline.enrichment.registry import (
    europepmc_results,
    load_active_molecules,
    normalize_preprint,
    preprints_query,
)

DEFAULT_DB = "data/retarats_preprints.sqlite"
TABLE = "preprints"


def _existing_ids(db_path: str) -> Set[str]:
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = load_payload_table(conn, TABLE)
    finally:
        conn.close()
    return {str(r.get("id", "")) for r in rows if r.get("id")}


def run(
    db_path: str = DEFAULT_DB,
    page_size: int = 20,
    refresh: bool = False,
    offline: bool = False,
    molecules_csv: str = "config/MOLECULES.csv",
) -> dict:
    molecules = load_active_molecules(molecules_csv)

    if offline:
        print(f"[offline] {len(molecules)} active molecules would be queried on EuropePMC (SRC:PPR).")
        for m in molecules[:10]:
            print(f"  - {m.get('molecule_id')}: {preprints_query(m)}")
        if len(molecules) > 10:
            print(f"  ... and {len(molecules) - 10} more")
        return {"molecules": len(molecules), "offline": True}

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    known = set() if refresh else _existing_ids(db_path)

    config = APIConfig.from_env(api_enabled=True)
    http = CachedHTTPClient(config)
    client = IdentifierMetadataClient(http, config)

    conn = sqlite3.connect(db_path)
    stored = 0
    skipped = 0
    seen_this_run: Set[str] = set()
    try:
        for m in molecules:
            mol_id = m.get("molecule_id", "")
            mol_name = m.get("display_name", "")
            query = preprints_query(m)
            payload, source = client.europepmc_search(query, page_size=page_size)
            batch: List[dict] = []
            for result in europepmc_results(payload):
                row = normalize_preprint(result, molecule_id=mol_id, molecule_name=mol_name)
                pid = row.get("id", "")
                if not pid or pid in seen_this_run:
                    continue
                if not refresh and pid in known:
                    skipped += 1
                    continue
                seen_this_run.add(pid)
                row["enriched_at_utc"] = utc_now_iso()
                batch.append(row)
            if batch:
                save_payload_rows(conn, TABLE, "id", batch)
                stored += len(batch)
            print(f"  {mol_id}: +{len(batch)} preprints ({source})")
    finally:
        conn.close()

    print(f"Stored {stored} new preprints, skipped {skipped} already-known -> {db_path}")
    return {"molecules": len(molecules), "stored": stored, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch EuropePMC preprints (SRC:PPR) per molecule.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--page-size", type=int, default=20, help="Results per molecule query.")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch and overwrite already-stored ids.")
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
