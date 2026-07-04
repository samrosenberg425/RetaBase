#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Run role/paper characterization, enrichment, lane postprocessing, review slices, queues, and CSV export.")
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--config-mode", default="inputs")
    parser.add_argument("--role-rules", default="config/ROLE_RULES.csv")
    parser.add_argument("--review-slices", default="config/REVIEW_SLICES.csv")
    parser.add_argument("--skip-roles", action="store_true")
    parser.add_argument("--skip-enrichment", action="store_true")
    parser.add_argument("--skip-lane-processors", action="store_true")
    parser.add_argument("--skip-review-slices", action="store_true")
    parser.add_argument("--skip-review-queue", action="store_true")
    parser.add_argument("--offline-enrichment", action="store_true", help="Run enrichment without external API calls.")
    parser.add_argument("--enrichment-mode", choices=["all", "human", "basic"], default="all")
    parser.add_argument("--enrichment-max-records", type=int, default=0)
    parser.add_argument("--enrichment-csv-only", action="store_true", help="Write audit CSVs but do not update enriched_* fields in SQLite.")
    parser.add_argument("--enable-pmc", action="store_true", help="Enable live PMC full-text fallback during basic-science enrichment.")
    parser.add_argument("--pmc-max-records", type=int, default=25)
    parser.add_argument("--contact-email", default="sr2007@rwjms.rutgers.edu")
    parser.add_argument("--ncbi-email", default="samrosenberg425@gmail.com")
    args = parser.parse_args()

    if not args.skip_roles:
        _run(["scripts/characterize_roles.py", "--db", args.db, "--role-rules", args.role_rules, "--config-mode", args.config_mode])

    _run(["scripts/characterize_papers.py", "--db", args.db, "--config-mode", args.config_mode])

    if not args.skip_enrichment:
        enrich_cmd = [
            "scripts/run_enrichment_pipeline.py",
            "--db", args.db,
            "--mode", args.enrichment_mode,
            "--contact-email", args.contact_email,
            "--ncbi-email", args.ncbi_email,
        ]
        if args.offline_enrichment:
            enrich_cmd.append("--offline")
        if args.enrichment_csv_only:
            enrich_cmd.append("--csv-only")
        if args.enrichment_max_records:
            enrich_cmd += ["--max-records", str(args.enrichment_max_records)]
        if args.enable_pmc:
            enrich_cmd.append("--enable-pmc")
            enrich_cmd += ["--pmc-max-records", str(args.pmc_max_records)]
        _run(enrich_cmd)

    if not args.skip_lane_processors:
        for script in [
            "scripts/postprocess_interventions.py",
            "scripts/postprocess_reviews.py",
            "scripts/postprocess_mechanisms.py",
            "scripts/postprocess_biomarkers.py",
            "scripts/postprocess_comparators.py",
            "scripts/postprocess_methods.py",
            "scripts/postprocess_unclear.py",
        ]:
            _run([script, "--db", args.db])

    if not args.skip_review_slices:
        _run(["scripts/build_review_slices.py", "--db", args.db, "--slices", args.review_slices])

    if not args.skip_review_queue:
        _run(["scripts/build_review_queue.py", "--db", args.db])

    _run(["scripts/export_sqlite_to_csv.py", "--db", args.db])


def _run(args: list[str]) -> None:
    cmd = [sys.executable] + args
    print("$", " ".join(cmd), flush=True)
    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
