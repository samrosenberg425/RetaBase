#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retarats_pipeline.lane_exporter import export_lane


def main() -> None:
    parser = argparse.ArgumentParser(description="Export unclear/general records for manual or enhanced-rule review.")
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--out", default="exports/postprocessed/unclear_refined.csv")
    args = parser.parse_args()
    rows = export_lane(
        db=args.db,
        lanes=["unclear_manual_triage", "general_context"],
        out_path=args.out,
        preferred_columns=[
            "molecule_id", "molecule_name", "processing_lane", "paper_purpose", "what_it_is",
            "paper_characterization_notes", "role_category", "molecule_relevance",
            "primary_study_type", "model_type", "title", "abstract", "pubmed_url", "pmid",
        ],
    )
    print(f"wrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
