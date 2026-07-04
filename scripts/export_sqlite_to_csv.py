#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description="Export local v2 SQLite payload tables to CSV files.")
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--out-dir", default="exports")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    conn = sqlite3.connect(args.db)
    exported = {}
    for table in ["molecules", "papers", "evidence", "molecule_profiles"]:
        rows = []
        try:
            payloads = conn.execute(f"select payload_json from {table}")
        except sqlite3.OperationalError:
            continue
        for (payload_json,) in payloads:
            rows.append(json.loads(payload_json))
        if rows:
            exported[table] = pd.DataFrame(rows)
            exported[table].to_csv(os.path.join(args.out_dir, f"{table}.csv"), index=False)
            print(f"wrote {args.out_dir}/{table}.csv ({len(rows)} rows)")

    if "evidence" in exported and "papers" in exported:
        review = build_evidence_review(exported["evidence"], exported["papers"])
        review.to_csv(os.path.join(args.out_dir, "evidence_review.csv"), index=False)
        print(f"wrote {args.out_dir}/evidence_review.csv ({len(review)} rows)")


def build_evidence_review(evidence: pd.DataFrame, papers: pd.DataFrame) -> pd.DataFrame:
    paper_cols = [
        col for col in [
            "pmid", "title", "abstract", "journal", "pub_date_iso", "pub_year",
            "pubtypes", "mesh_terms", "keywords", "chemicals", "doi", "pubmed_url",
        ]
        if col in papers.columns
    ]
    review = evidence.merge(papers[paper_cols], on="pmid", how="left", suffixes=("", "_paper"))
    preferred = [
        "processing_lane", "next_postprocessing_script", "database_section", "processing_priority",
        "molecule_id", "molecule_name", "paper_purpose", "what_it_is", "evidence_question",
        "role_review_bucket", "public_candidate", "role_category", "evidence_strength_label",
        "primary_study_type", "model_type", "molecule_relevance", "role_confidence",
        "condition_tags", "endpoint_tags", "mechanistic_focus", "intervention_or_exposure",
        "comparator_or_control", "dose_route", "duration", "sample_size", "outcome_direction",
        "efficacy_signal", "safety_signal", "review_status", "title",
        "evidence_summary", "key_result_sentence", "role_evidence_text",
        "abstract", "pubmed_url", "pmid", "journal",
        "pub_date_iso", "pub_year", "pubtypes", "mesh_terms", "keywords",
        "chemicals", "doi", "evidence_id", "rule_id", "match_strength",
    ]
    ordered = [col for col in preferred if col in review.columns]
    remaining = [col for col in review.columns if col not in ordered]
    return review[ordered + remaining]


if __name__ == "__main__":
    main()
