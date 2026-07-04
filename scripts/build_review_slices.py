#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retarats_pipeline.review_slices import (
    apply_review_slice,
    build_exclusion_summary,
    load_review_slices,
    methods_summary,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PICO/PECO-style review-slice exports from characterized evidence.")
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--slices", default="config/REVIEW_SLICES.csv")
    parser.add_argument("--out-dir", default="exports/review_slices")
    parser.add_argument("--prisma-dir", default="exports/prisma")
    args = parser.parse_args()

    review_slices = load_review_slices(args.slices)
    evidence = _load_review_dataframe(args.db)
    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.prisma_dir, exist_ok=True)

    all_manifest = []
    flow_frames = []
    for review_slice in review_slices:
        sliced, flow = apply_review_slice(evidence, review_slice)
        out_path = os.path.join(args.out_dir, f"{review_slice.slice_id}.csv")
        sliced.to_csv(out_path, index=False)
        flow_frames.append(flow)
        manifest_row = review_slice.to_dict()
        manifest_row["records_included"] = len(sliced)
        manifest_row["output_csv"] = out_path
        all_manifest.append(manifest_row)
        print(f"wrote {out_path} ({len(sliced)} rows)")

    manifest = pd.DataFrame(all_manifest)
    flow_all = pd.concat(flow_frames, ignore_index=True) if flow_frames else pd.DataFrame()
    exclusions = build_exclusion_summary(flow_all) if not flow_all.empty else pd.DataFrame()

    manifest.to_csv(os.path.join(args.prisma_dir, "review_slice_manifest.csv"), index=False)
    flow_all.to_csv(os.path.join(args.prisma_dir, "flow_counts_by_slice.csv"), index=False)
    exclusions.to_csv(os.path.join(args.prisma_dir, "exclusion_reasons_by_slice.csv"), index=False)
    with open(os.path.join(args.prisma_dir, "methods_summary.md"), "w", encoding="utf-8") as handle:
        handle.write(methods_summary(review_slices, flow_all))

    print(f"wrote {args.prisma_dir}/review_slice_manifest.csv")
    print(f"wrote {args.prisma_dir}/flow_counts_by_slice.csv")
    print(f"wrote {args.prisma_dir}/exclusion_reasons_by_slice.csv")
    print(f"wrote {args.prisma_dir}/methods_summary.md")


def _load_review_dataframe(db: str) -> pd.DataFrame:
    conn = sqlite3.connect(db)
    evidence = _payload_table(conn, "evidence")
    papers = _payload_table(conn, "papers")
    conn.close()
    paper_cols = [
        col for col in [
            "pmid", "title", "abstract", "journal", "pub_date_iso", "pub_year",
            "pubtypes", "mesh_terms", "keywords", "chemicals", "doi", "pubmed_url",
        ]
        if col in papers.columns
    ]
    if not evidence.empty and not papers.empty and "pmid" in evidence and "pmid" in papers:
        return evidence.merge(papers[paper_cols], on="pmid", how="left", suffixes=("", "_paper"))
    return evidence


def _payload_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    rows = []
    for (payload_json,) in conn.execute(f"select payload_json from {table}"):
        rows.append(json.loads(payload_json))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    main()
