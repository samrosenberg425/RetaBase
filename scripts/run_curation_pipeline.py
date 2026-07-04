#!/usr/bin/env python3
"""One-shot: build the curated database, then the public browsing site.

This is the convenience entrypoint for the curation + publication layer. It runs
``build_curated_database.py`` (SQLite -> curated CSVs) and then
``build_public_site.py`` (curated CSVs -> single self-contained ``index.html``)
with sensible defaults, so a curator can go from the internal DB to a shareable
site in one command.

Why a separate script from ``run_postprocessing_pipeline.py``: postprocessing is
the heavy, characterization stage; curation is a thin, purely rule-based
transform over its output. Keeping a dedicated one-shot lets you rebuild the
public artifacts without re-running (or touching) postprocessing. The
postprocessing runner still exposes ``--curate``/``--build-site`` for the
end-to-end path.

Pure stdlib, offline, non-destructive. No LLM, no network.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def main() -> None:
    ap = argparse.ArgumentParser(description="Build curated CSVs and the public site in one step.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--curated-dir", default="exports/curated")
    ap.add_argument("--site-dir", default="exports/site")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the first N evidence rows (0 = all). For quick smoke tests.")
    ap.add_argument("--skip-site", action="store_true",
                    help="Build only the curated CSVs, not the site.")
    ap.add_argument("--site-mode", choices=["inline", "fetch"], default="inline",
                    help="'inline' = portable single file (top records by rank, double-clickable); "
                         "'fetch' = loads site_data.json at runtime so ALL records are browsable "
                         "(for hosting / a local server).")
    ap.add_argument("--max-inline", type=int, default=4000,
                    help="Max records inlined in 'inline' mode (kept modest so the HTML stays small).")
    args = ap.parse_args()

    print("=== Curation pipeline ===")
    print(f"  db          : {args.db}")
    print(f"  curated-dir : {args.curated_dir}")
    if not args.skip_site:
        print(f"  site-dir    : {args.site_dir}")
    print()

    print("[1/2] Building curated database (SQLite -> curated CSVs)...", flush=True)
    _run([
        "scripts/build_curated_database.py",
        "--db", args.db,
        "--out-dir", args.curated_dir,
        "--limit", str(args.limit),
    ])

    if args.skip_site:
        print("\nDone (site skipped). Curated CSVs in", args.curated_dir)
        return

    print("\n[2/2] Building public site (curated CSVs -> index.html)...", flush=True)
    _run([
        "scripts/build_public_site.py",
        "--curated-dir", args.curated_dir,
        "--out-dir", args.site_dir,
        "--mode", args.site_mode,
        "--max-inline", str(args.max_inline),
    ])

    site_index = os.path.join(args.site_dir, "index.html")
    print("\nDone.")
    print(f"  Curated CSVs : {args.curated_dir}/")
    print(f"  Public site  : {site_index}")
    print("  Open the index.html directly in a browser (no server needed).")


def _run(args: list[str]) -> None:
    cmd = [sys.executable] + args
    print("$", " ".join(cmd), flush=True)
    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
