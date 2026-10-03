"""Mark stored registry (trial) / preprint rows that a molecule's CURRENT search no longer returns.

When a search query is corrected (e.g. an ambiguous abbreviation is quoted or dropped), rows
fetched by the old query stay in the corpus forever, because the fetch only ever upserts what it
sees. This marks them ``stale_query`` instead of deleting them, so:

* the builds drop them from the published feeds and counts (see ``is_stale``);
* nothing is deleted -- a row that the search returns again is rewritten fresh (flag cleared);
* it is self-sustaining: any future query change cleans up after itself, no manual PMID/NCT list.

Fail-open: a molecule is only reconciled when ITS retrieval completed (ok and not partial),
and a retrieval that returned nothing for a molecule that already has many stored rows is
treated as an anomaly (outage / API glitch) and skipped -- never as "everything is stale".
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Set

from retarats_pipeline.enrichment.common import load_payload_table, save_payload_rows

STALE_FLAG = "stale_query"
STALE_SINCE = "stale_since_utc"
ANOMALY_MIN_STORED = 20  # empty result for a molecule with >= this many stored rows => skip, not stale


def is_stale(row: Mapping) -> bool:
    return bool(row.get(STALE_FLAG))


def drop_stale(rows: Iterable[dict]) -> List[dict]:
    return [r for r in rows if not is_stale(r)]


def mark_stale_rows(conn: sqlite3.Connection, table: str, key_field: str, seen_keys: Set[str],
                    complete_molecules: Set[str], returned_by_molecule: Mapping[str, int], now_iso: str,
                    key_norm=lambda k: str(k), expected_empty: Set[str] = frozenset()) -> Dict[str, object]:
    """Flag rows of ``complete_molecules`` whose key was not returned this run.

    ``seen_keys`` must be normalised with the same ``key_norm`` as used here. Returns
    {"marked": n, "by_molecule": {mol: n}, "skipped_anomalies": [mol, ...]}. ``expected_empty`` molecules
    (human-confirmed to have no real records) bypass the empty-result anomaly guard."""
    rows = load_payload_table(conn, table)
    stored_by_mol: Dict[str, int] = defaultdict(int)
    for r in rows:
        stored_by_mol[str(r.get("molecule_id", "") or "")] += 1

    skipped = sorted(m for m in complete_molecules
                     if returned_by_molecule.get(m, 0) == 0 and stored_by_mol.get(m, 0) >= ANOMALY_MIN_STORED
                     and m not in expected_empty)
    eligible = set(complete_molecules) - set(skipped)

    to_save: List[dict] = []
    by_mol: Dict[str, int] = defaultdict(int)
    for r in rows:
        mol = str(r.get("molecule_id", "") or "")
        if mol not in eligible or key_norm(r.get(key_field, "")) in seen_keys:
            continue
        if is_stale(r):
            continue  # already marked; keep its original stale_since_utc
        r = dict(r)
        r[STALE_FLAG] = True
        r[STALE_SINCE] = now_iso
        to_save.append(r)
        by_mol[mol] += 1
    if to_save:
        save_payload_rows(conn, table, key_field, to_save, updated_field=STALE_SINCE)
    return {"marked": len(to_save), "by_molecule": dict(by_mol), "skipped_anomalies": skipped}
