#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local multi-stage postprocessing pipeline.")
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--config-mode", default="inputs")
    parser.add_argument("--role-rules", default="config/ROLE_RULES.csv")
    parser.add_argument("--review-slices", default="config/REVIEW_SLICES.csv")
    parser.add_argument("--skip-roles", action="store_true")
    parser.add_argument("--skip-lane-processors", action="store_true")
    parser.add_argument("--skip-review-slices", action="store_true")
    # Curation is opt-in and runs *after* the existing postprocessing so it can
    # read the fully characterized evidence. Default off to preserve behavior.
    parser.add_argument("--curate", action="store_true",
                        help="After postprocessing, build the curated CSVs (build_curated_database.py).")
    parser.add_argument("--build-site", action="store_true",
                        help="With --curate, also generate the self-contained public site.")
    parser.add_argument("--curated-dir", default="exports/curated",
                        help="Output dir for curated CSVs (used with --curate).")
    parser.add_argument("--site-dir", default="exports/site",
                        help="Output dir for the public site (used with --build-site).")
    args = parser.parse_args()

    if not args.skip_roles:
        _run([
            "scripts/characterize_roles.py",
            "--db", args.db,
            "--role-rules", args.role_rules,
            "--config-mode", args.config_mode,
        ])

    _run([
        "scripts/characterize_papers.py",
        "--db", args.db,
        "--config-mode", args.config_mode,
    ])

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
        _run([
            "scripts/build_review_slices.py",
            "--db", args.db,
            "--slices", args.review_slices,
        ])

    _run(["scripts/export_sqlite_to_csv.py", "--db", args.db])

    # Optional curation layer + public site. Kept last and behind flags so the
    # default pipeline is byte-for-byte unchanged.
    if args.curate:
        _run([
            "scripts/build_curated_database.py",
            "--db", args.db,
            "--out-dir", args.curated_dir,
        ])
        if args.build_site:
            _run([
                "scripts/build_public_site.py",
                "--curated-dir", args.curated_dir,
                "--out-dir", args.site_dir,
            ])


def _run(args: list[str]) -> None:
    cmd = [sys.executable] + args
    print("$", " ".join(cmd), flush=True)
    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
