#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retarats_pipeline.config import load_config, molecule_lookup
from retarats_pipeline.profiles import build_molecule_profiles
from retarats_pipeline.pubmed import utc_now_iso
from retarats_pipeline.role_classifier import classify_many, load_role_rules


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-process existing PubMed evidence with configurable molecule-role rules."
    )
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--role-rules", default="config/ROLE_RULES.csv")
    parser.add_argument("--config-mode", choices=["local", "inputs", "excel", "xlsx"], default="inputs")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--input-workbook", default="inputs/Moleculessearch.xlsx")
    parser.add_argument("--summary-workbook", default="inputs/Summary Sheet.xlsx")
    parser.add_argument("--exports-dir", default="exports")
    parser.add_argument("--csv-only", action="store_true", help="Write exports only; do not update SQLite payloads.")
    args = parser.parse_args()

    rules = load_role_rules(args.role_rules)
    loaded = load_config(
        mode=args.config_mode,
        local_config_dir=args.config_dir,
        input_workbook=args.input_workbook,
        summary_workbook=args.summary_workbook,
    )
    molecules = molecule_lookup(loaded.molecules)

    conn = sqlite3.connect(args.db)
    evidence_rows = _load_payload_table(conn, "evidence")
    paper_rows = _load_payload_table(conn, "papers")
    paper_by_pmid = {str(row.get("pmid", "")): row for row in paper_rows}

    characterized = classify_many(evidence_rows, paper_by_pmid, molecules, rules)
    profiles = build_molecule_profiles(molecules, characterized, updated_at=utc_now_iso())

    if not args.csv_only:
        _save_payload_table(conn, "evidence", "evidence_id", characterized, "fetched_at_utc")
        _save_payload_table(conn, "molecule_profiles", "molecule_id", profiles, "profile_updated_at_utc")

    os.makedirs(args.exports_dir, exist_ok=True)
    pd.DataFrame(characterized).to_csv(os.path.join(args.exports_dir, "evidence_roles.csv"), index=False)
    pd.DataFrame(profiles).to_csv(os.path.join(args.exports_dir, "molecule_profiles_roles.csv"), index=False)
    print(f"role rules: {len(rules)}")
    print(f"evidence characterized: {len(characterized)}")
    print(f"profiles characterized: {len(profiles)}")
    print(f"wrote {args.exports_dir}/evidence_roles.csv")
    print(f"wrote {args.exports_dir}/molecule_profiles_roles.csv")


def _load_payload_table(conn: sqlite3.Connection, table: str) -> List[dict]:
    rows = []
    for (payload_json,) in conn.execute(f"select payload_json from {table}"):
        rows.append(json.loads(payload_json))
    return rows


def _save_payload_table(
    conn: sqlite3.Connection,
    table: str,
    key_field: str,
    rows: List[dict],
    updated_field: str,
) -> None:
    conn.execute(
        f"create table if not exists {table} ({key_field} text primary key, payload_json text, updated_at_utc text)"
    )
    payloads = [
        (
            str(row.get(key_field, "")),
            json.dumps(row, ensure_ascii=False, sort_keys=True),
            str(row.get(updated_field, "")),
        )
        for row in rows
    ]
    conn.executemany(
        f"insert into {table} ({key_field}, payload_json, updated_at_utc) values (?, ?, ?) "
        f"on conflict({key_field}) do update set payload_json = excluded.payload_json, "
        "updated_at_utc = excluded.updated_at_utc",
        payloads,
    )
    conn.commit()


if __name__ == "__main__":
    main()
