#!/usr/bin/env python3
"""One-time (and self-healing) backfill of the identity-relevant fields of ALREADY-STORED CT.gov trials.

    python3 scripts/run_trials_identity_backfill.py --db work/retarats_trials.sqlite [--max-rows N] [--max-seconds S]

Trial rows stored before WS4.5 only carry brief title, conditions and interventions. The identity layer also judges
official title, keywords, arm groups, intervention other names, outcome measure titles, outcome text, summary and
eligibility text. This fetches those from ClinicalTrials.gov (``filter.ids``, up to 100 trials per request) and ADDS
the new fields to the stored row. It never changes an existing field (no source metadata is rewritten), never deletes
a row, and marks each completed row ``identity_fields_v`` so it is not fetched again.

Bounded and resumable: only non-stale rows without the marker are selected (rows that would be held on their old
fields first); ``--max-rows`` / ``--max-seconds`` cap one run; an interrupted or failed batch leaves its rows
unmarked, and the identity gate never HOLDS an unmarked ("legacy") row on its incomplete evidence (fail-open).
Rows CT.gov no longer returns (withdrawn ids etc.) simply stay legacy.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline import identity  # noqa: E402
from retarats_pipeline.enrichment.clients import ClinicalTrialsClient  # noqa: E402
from retarats_pipeline.enrichment.common import (  # noqa: E402
    APIConfig, CachedHTTPClient, load_payload_table, save_payload_rows)
from retarats_pipeline.enrichment.registry import normalize_trial  # noqa: E402
from retarats_pipeline.enrichment.registry_stale import is_stale  # noqa: E402

DEFAULT_DB = "data/retarats_trials.sqlite"
NEW_FIELDS = ("official_title", "keywords", "arms", "other_names", "outcome_measures", "outcome_text",
              "summary_text", "eligibility_text", "identity_fields_v")


def pending_rows(rows: List[dict], include_stale: bool = False) -> List[dict]:
    """Rows still lacking the identity fields, would-be-held-on-old-fields first (they matter most)."""
    cfg = identity.get_config()
    todo = [r for r in rows if not r.get(identity.TRIAL_FIELDS_MARKER) and (include_stale or not is_stale(r))]

    def prio(r: dict) -> int:
        v = identity.evaluate(cfg, "ctgov", str(r.get("molecule_id", "") or ""), identity.zones_for_trial(r),
                              str(r.get("nct_id", "")))
        return 0 if v.match_type == identity.M_LEGACY else 1

    return sorted(todo, key=lambda r: (prio(r), str(r.get("nct_id", ""))))


def run(db_path: str = DEFAULT_DB, batch: int = 100, max_rows: int = 0, max_seconds: float = 0.0,
        include_stale: bool = False, dry_run: bool = False, client=None) -> dict:
    if not os.path.exists(db_path):
        print(f"{db_path} missing -- nothing to backfill")
        return {"pending": 0, "filled": 0, "not_returned": 0, "failed_batches": 0, "ok": True}
    conn = sqlite3.connect(db_path)
    try:
        rows = load_payload_table(conn, "trials")
        todo = pending_rows(rows, include_stale)
        stats = {"stored": len(rows), "pending": len(todo), "filled": 0, "not_returned": 0, "failed_batches": 0,
                 "stopped": "", "ok": True}
        print(f"{len(todo)} of {len(rows)} stored trial(s) lack the identity fields")
        if dry_run or not todo:
            return stats
        if client is None:
            client = ClinicalTrialsClient(CachedHTTPClient(APIConfig.from_env(api_enabled=True)))
        if max_rows:
            todo = todo[:max_rows]
        by_id: Dict[str, dict] = {str(r["nct_id"]).upper(): r for r in todo}
        started = time.monotonic()
        ids = list(by_id)
        consecutive_fail = 0
        for i in range(0, len(ids), batch):
            if max_seconds and time.monotonic() - started > max_seconds:
                stats["stopped"] = f"time budget {max_seconds:g}s reached"
                break
            chunk = ids[i:i + batch]
            studies, source, err = client.studies_by_ids(chunk)
            if err:
                stats["failed_batches"] += 1
                consecutive_fail += 1
                print(f"  batch {i // batch + 1}: FAILED ({err})")
                if consecutive_fail >= 3:
                    stats["stopped"] = "3 consecutive failed batches (CT.gov unavailable?)"
                    break
                continue
            consecutive_fail = 0
            out = []
            seen = set()
            for study in studies:
                parsed = ClinicalTrialsClient.parse_study(study)
                nct = str(parsed.get("nct_id", "")).upper()
                row = by_id.get(nct)
                if row is None:
                    continue
                seen.add(nct)
                fresh = normalize_trial(parsed)
                merged = dict(row)
                for k in NEW_FIELDS:          # ADD only; every existing field is left exactly as stored
                    merged[k] = fresh[k]
                out.append(merged)
            stats["not_returned"] += len(set(chunk) - seen)
            if out:
                save_payload_rows(conn, "trials", "nct_id", out)
                stats["filled"] += len(out)
            print(f"  batch {i // batch + 1}: {len(out)} filled, {len(set(chunk) - seen)} not returned ({source})")
        stats["ok"] = stats["filled"] > 0 or not todo or stats["failed_batches"] == 0
    finally:
        conn.close()
    print(f"filled {stats['filled']}, not returned {stats['not_returned']}, failed batches {stats['failed_batches']}"
          + (f"; stopped: {stats['stopped']}" if stats["stopped"] else ""))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--max-rows", type=int, default=0, help="cap one run (0 = every pending row)")
    ap.add_argument("--max-seconds", type=float, default=0.0, help="wall-clock cap for one run (0 = none)")
    ap.add_argument("--include-stale", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="", help="write the run summary as JSON here")
    args = ap.parse_args()
    stats = run(args.db, args.batch, args.max_rows, args.max_seconds, args.include_stale, args.dry_run)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, indent=2, sort_keys=True)
    return 0 if stats.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
