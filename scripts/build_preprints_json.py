#!/usr/bin/env python3
"""Build exports/curated/preprints_data.json from the preprints SQLite table.

Reads the ``preprints`` payload table written by ``run_preprints_fetch.py`` and
emits a compact JSON feed sorted by date descending. Empty/absent DB -> empty
(valid) feed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.common import load_payload_table, utc_now_iso
from retarats_pipeline import identity
from retarats_pipeline.enrichment.registry_stale import drop_stale
from retarats_pipeline.curation.facets import derive_facets, load_facet_defs

DEFAULT_DB = "data/retarats_preprints.sqlite"
DEFAULT_OUT = "exports/curated/preprints_data.json"
TABLE = "preprints"

# Facet tags to carry so preprints show the same aspect tags as papers (no rank/rigor).
_FACET_FIELDS = [
    "facet_species", "facet_indication", "facet_endpoint", "facet_study_type",
    "facet_model_system", "facet_route", "facet_drug_class", "facet_population",
    "facet_sex", "facet_formulation", "facet_evidence_direction",
]
COMPACT_FIELDS = [
    "id", "molecule_id", "molecule_name", "title", "authors_short",
    "server", "date", "doi", "url",
    "abstract", "published_pmid", "published_doi",
] + _FACET_FIELDS

_FACET_DEFS = None


def _facets_for(row: Dict) -> Dict[str, str]:
    """Derive the same facet tags papers get, from the preprint's title + abstract.
    Best-effort: any error or missing config yields no tags rather than failing."""
    global _FACET_DEFS
    try:
        if _FACET_DEFS is None:
            _FACET_DEFS = load_facet_defs()
        fr = derive_facets(dict(row), {}, _FACET_DEFS)
        return {k: fr.wide.get(k, "") for k in _FACET_FIELDS}
    except Exception:  # noqa: BLE001 -- facets are additive; never block the feed
        return {}


def _load_preprints(db_path: str, report_path: str = "") -> List[dict]:
    """Published preprints: stored rows minus stale-query rows minus rows whose identity verdict is
    hold/exclude (the molecule is not established by the title/abstract). Nothing is deleted."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = drop_stale(load_payload_table(conn, TABLE))
    finally:
        conn.close()
    return identity.apply_identity_gate("preprints", rows, report_path)


def _compact(row: Dict) -> Dict:
    out = {k: row.get(k, "") for k in COMPACT_FIELDS}
    facets = _facets_for(row)
    for k, v in facets.items():
        if v:
            out[k] = v
    return out


def build(db_path: str = DEFAULT_DB, out_path: str = DEFAULT_OUT, release_id: str = "",
          identity_report: str = "") -> dict:
    preprints = _load_preprints(db_path, identity_report)
    compact = [_compact(p) for p in preprints]
    # date descending; blank dates sink to the bottom.
    compact.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    payload = {
        "generated_utc": utc_now_iso(),
        "count": len(compact),
        "preprints": compact,
    }
    if release_id:
        payload["release_id"] = release_id
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
    return {"count": len(compact), "out": out_path}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build preprints_data.json from the preprints SQLite table.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--release-id", default=os.environ.get("RELEASE_ID", ""),
                    help="Stamp this release_id into the feed (default: $RELEASE_ID; blank = unstamped).")
    ap.add_argument("--identity-report", default="",
                    help="Write the CSV of preprints held by the identity gate (and why) here (keep it OUT of the site dir).")
    args = ap.parse_args()
    result = build(args.db, args.out, release_id=args.release_id, identity_report=args.identity_report)
    print(f"Wrote {result['count']} preprints -> {result['out']}")


if __name__ == "__main__":
    main()
