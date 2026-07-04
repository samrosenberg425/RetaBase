#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retarats_pipeline.config import load_config, molecule_lookup
from retarats_pipeline.paper_characterizer import characterize_many
from retarats_pipeline.processing_router import route_many


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Characterize papers into richer extraction fields and route them to lane-specific postprocessors."
    )
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--config-mode", choices=["local", "inputs", "excel", "xlsx"], default="inputs")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--input-workbook", default="inputs/Moleculessearch.xlsx")
    parser.add_argument("--summary-workbook", default="inputs/Summary Sheet.xlsx")
    parser.add_argument("--exports-dir", default="exports")
    parser.add_argument("--lanes-dir", default="exports/lanes")
    parser.add_argument("--csv-only", action="store_true", help="Write exports only; do not update SQLite payloads.")
    args = parser.parse_args()

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

    characterized = characterize_many(evidence_rows, paper_by_pmid, molecules)
    routed = route_many(characterized)

    if not args.csv_only:
        _save_payload_table(conn, "evidence", "evidence_id", routed, "fetched_at_utc")

    os.makedirs(args.exports_dir, exist_ok=True)
    os.makedirs(args.lanes_dir, exist_ok=True)
    evidence_df = pd.DataFrame(routed)
    evidence_df.to_csv(os.path.join(args.exports_dir, "evidence_characterized.csv"), index=False)
    route_summary = _route_summary(evidence_df)
    route_summary.to_csv(os.path.join(args.exports_dir, "processing_routes_summary.csv"), index=False)
    _write_lane_files(evidence_df, paper_by_pmid, args.lanes_dir)

    print(f"evidence characterized: {len(routed)}")
    print(f"lanes: {evidence_df['processing_lane'].nunique() if 'processing_lane' in evidence_df else 0}")
    print(f"wrote {args.exports_dir}/evidence_characterized.csv")
    print(f"wrote {args.exports_dir}/processing_routes_summary.csv")
    print(f"wrote lane CSVs under {args.lanes_dir}/")
    print(route_summary.to_string(index=False))


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


def _route_summary(evidence_df: pd.DataFrame) -> pd.DataFrame:
    if evidence_df.empty:
        return pd.DataFrame(columns=["processing_lane", "n", "next_postprocessing_script", "database_section"])
    summary = (
        evidence_df
        .groupby(["processing_lane", "next_postprocessing_script", "database_section"], dropna=False)
        .size()
        .reset_index(name="n")
        .sort_values(["n", "processing_lane"], ascending=[False, True])
    )
    return summary


def _write_lane_files(evidence_df: pd.DataFrame, paper_by_pmid: dict, lanes_dir: str) -> None:
    if evidence_df.empty or "processing_lane" not in evidence_df:
        return
    papers = pd.DataFrame(paper_by_pmid.values())
    paper_cols = [
        col for col in [
            "pmid", "title", "abstract", "journal", "pub_date_iso", "pub_year",
            "pubtypes", "mesh_terms", "keywords", "chemicals", "doi", "pubmed_url",
        ]
        if col in papers.columns
    ]
    review = evidence_df.merge(papers[paper_cols], on="pmid", how="left", suffixes=("", "_paper"))
    preferred = [
        "processing_lane", "next_postprocessing_script", "database_section", "processing_priority",
        "molecule_id", "molecule_name", "paper_purpose", "what_it_is", "evidence_question",
        "role_category", "role_review_bucket", "primary_study_type", "model_type",
        "condition_tags", "endpoint_tags", "mechanistic_focus", "intervention_or_exposure",
        "comparator_or_control", "dose_route", "duration", "sample_size", "outcome_direction",
        "efficacy_signal", "safety_signal", "title", "abstract", "pubmed_url", "pmid",
        "evidence_id", "route_reason", "key_paper_parts",
    ]
    ordered = [col for col in preferred if col in review.columns]
    remaining = [col for col in review.columns if col not in ordered]
    review = review[ordered + remaining]
    for lane, lane_df in review.groupby("processing_lane", dropna=False):
        filename = _safe_filename(str(lane or "unrouted")) + ".csv"
        lane_df.sort_values(["processing_priority", "molecule_id"], ascending=[False, True]).to_csv(
            os.path.join(lanes_dir, filename),
            index=False,
        )


def _safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value.strip().lower())


if __name__ == "__main__":
    main()
