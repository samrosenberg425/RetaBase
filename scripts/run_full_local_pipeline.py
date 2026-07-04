#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run PubMed fetch, local SQLite write, postprocessing, and CSV export with one paste-safe command."
    )
    parser.add_argument("--config-mode", default="inputs")
    parser.add_argument("--mode", choices=["daily", "backfill"], default="daily")
    parser.add_argument("--daily-days", type=int, default=7)
    parser.add_argument("--start-year", type=int, default=2026)
    parser.add_argument("--molecule", default="")
    parser.add_argument("--max-records-per-rule", type=int, default=0)
    parser.add_argument("--summary-mode", default="rule_based")
    parser.add_argument("--role-rules", default="config/ROLE_RULES.csv")
    parser.add_argument("--review-slices", default="config/REVIEW_SLICES.csv")
    parser.add_argument("--local-db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--state-db", default="data/retarats_state.sqlite")
    parser.add_argument("--sinks", default="local")
    parser.add_argument("--refresh-seen", action="store_true")
    parser.add_argument("--skip-fetch", action="store_true", help="Only run postprocessing/export on the existing local DB.")
    parser.add_argument("--skip-postprocessing", action="store_true", help="Only fetch/write PubMed data.")
    args = parser.parse_args()

    if not args.skip_fetch:
        fetch_cmd = [
            "retarats_v2.py",
            "--config-mode", args.config_mode,
            "--mode", args.mode,
            "--summary-mode", args.summary_mode,
            "--role-rules", args.role_rules,
            "--sinks", args.sinks,
            "--local-db", args.local_db,
            "--state-db", args.state_db,
        ]
        if args.mode == "daily":
            fetch_cmd += ["--daily-days", str(args.daily_days)]
        else:
            fetch_cmd += ["--start-year", str(args.start_year)]
        if args.molecule:
            fetch_cmd += ["--molecule", args.molecule]
        if args.max_records_per_rule:
            fetch_cmd += ["--max-records-per-rule", str(args.max_records_per_rule)]
        if args.refresh_seen:
            fetch_cmd.append("--refresh-seen")
        _run(fetch_cmd)

    if not args.skip_postprocessing:
        _run([
            "scripts/run_postprocessing_pipeline.py",
            "--db", args.local_db,
            "--config-mode", args.config_mode,
            "--role-rules", args.role_rules,
            "--review-slices", args.review_slices,
        ])

    print("\nDone. Start with exports/evidence_review.csv and exports/processing_routes_summary.csv", flush=True)


def _run(args: list[str]) -> None:
    cmd = [sys.executable] + args
    print("$", " ".join(cmd), flush=True)
    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
