#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retarats_pipeline.enrichment.common import clean_text, ensure_dir, is_blankish, load_payload_table, write_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Build targeted review queues from enriched_* audit fields in SQLite.")
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--out-dir", default="exports/review_queue")
    args = parser.parse_args()

    ensure_dir(args.out_dir)
    conn = sqlite3.connect(args.db)
    rows = load_payload_table(conn, "evidence")
    conn.close()

    pico = []
    trial_needed = []
    role_ambiguous = []
    basic = []
    for row in rows:
        lane = clean_text(row.get("processing_lane"))
        if lane == "human_intervention":
            if str(row.get("enriched_needs_human_review", "")).lower() == "true":
                pico.append(_review_row(row))
            if clean_text(row.get("primary_study_type")).lower() in {"rct", "human interventional non-rct"} and is_blankish(row.get("enriched_nct_id")):
                trial_needed.append(_review_row(row, extra_reason="possible_trial_registry_lookup_needed"))
        if clean_text(row.get("role_confidence")).lower() in {"low", "uncertain"}:
            role_ambiguous.append(_review_row(row, extra_reason="low_role_confidence"))
        if row.get("enriched_basic_review_reason") and lane != "human_intervention":
            basic.append(_review_row(row, extra_reason=row.get("enriched_basic_review_reason")))

    write_csv(Path(args.out_dir) / "pico_incomplete.csv", pico)
    write_csv(Path(args.out_dir) / "trial_registry_needed.csv", trial_needed)
    write_csv(Path(args.out_dir) / "role_ambiguous.csv", role_ambiguous)
    write_csv(Path(args.out_dir) / "basic_science_incomplete.csv", basic)
    write_csv(Path(args.out_dir) / "human_review_queue.csv", pico + trial_needed + role_ambiguous + basic)
    print(f"wrote review queues under {args.out_dir}")


def _review_row(row: dict, extra_reason: str = "") -> dict:
    return {
        "processing_lane": row.get("processing_lane"),
        "molecule_id": row.get("molecule_id"),
        "molecule_name": row.get("molecule_name"),
        "evidence_id": row.get("evidence_id"),
        "pmid": row.get("pmid"),
        "public_candidate": row.get("public_candidate"),
        "primary_study_type": row.get("primary_study_type"),
        "review_reason": extra_reason or row.get("enriched_human_review_reason") or row.get("enriched_basic_review_reason") or "needs_review",
        "original_missing": row.get("enriched_human_original_missing_fields") or row.get("enriched_basic_original_missing_fields"),
        "proposed_missing": row.get("enriched_human_proposed_missing_fields") or row.get("enriched_basic_proposed_missing_fields"),
        "matched_nct_id": row.get("enriched_nct_id"),
        "title": row.get("title"),
    }


if __name__ == "__main__":
    main()
