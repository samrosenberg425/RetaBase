from __future__ import annotations

import json
import os
import sqlite3
from typing import Iterable, List

import pandas as pd


def export_lane(
    *,
    db: str,
    lanes: Iterable[str],
    out_path: str,
    preferred_columns: List[str],
) -> pd.DataFrame:
    conn = sqlite3.connect(db)
    evidence = _payload_table(conn, "evidence")
    papers = _payload_table(conn, "papers")
    conn.close()

    lane_set = {str(lane) for lane in lanes}
    if lane_set and "processing_lane" in evidence.columns:
        evidence = evidence[evidence["processing_lane"].astype(str).isin(lane_set)].copy()

    paper_cols = [
        col for col in [
            "pmid", "title", "abstract", "journal", "pub_date_iso", "pub_year",
            "pubtypes", "mesh_terms", "keywords", "chemicals", "doi", "pubmed_url",
        ]
        if col in papers.columns
    ]
    if not evidence.empty and not papers.empty:
        evidence = evidence.merge(papers[paper_cols], on="pmid", how="left", suffixes=("", "_paper"))

    ordered = [col for col in preferred_columns if col in evidence.columns]
    remaining = [col for col in evidence.columns if col not in ordered]
    out = evidence[ordered + remaining] if ordered else evidence
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out.to_csv(out_path, index=False)
    return out


def _payload_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    rows = []
    for (payload_json,) in conn.execute(f"select payload_json from {table}"):
        rows.append(json.loads(payload_json))
    return pd.DataFrame(rows)
