#!/usr/bin/env python3
"""Resumable, size-capped historical backfill of PubMed records.

Fetches one year at a time, walking **backwards** from a start year, so the
database fills with progressively older literature. It is:

* **Resumable** — completed years are recorded in a checkpoint JSON; re-running
  skips them. Safe to Ctrl-C and restart (the underlying fetch also dedupes via
  the state DB, so partial years don't duplicate).
* **Size-capped** — after each year it checks the SQLite size and stops once the
  target (default 10 GB) is reached, or when it runs out of years.
* **Polite** — relies on the existing cached, rate-limited fetch layer. Set an
  NCBI API key (NCBI_API_KEY) + email (NCBI_EMAIL) in .env to raise rate limits.

NETWORK REQUIRED: this hits NCBI E-utilities, so run it on a machine with
outbound network (your computer or the GitHub Actions runner) — NOT in the
offline sandbox. Example:

    python3 scripts/run_backfill.py --start-year 2025 --min-year 1975 --target-gb 10

After it finishes (or hits the cap), rebuild the curated layer + site:

    python3 scripts/run_curation_pipeline.py --db data/retarats_pubmed.sqlite
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

CHECKPOINT = "data/backfill_checkpoint.json"


def _db_size_gb(path: str) -> float:
    try:
        return os.path.getsize(path) / (1024 ** 3)
    except OSError:
        return 0.0


def _load_ckpt(path: str) -> dict:
    if os.path.exists(path):
        try:
            return json.loads(open(path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError):
            pass
    return {"completed_years": [], "history": []}


def _save_ckpt(path: str, ckpt: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ckpt, fh, indent=2)


def _fetch_year(year: int, args) -> int:
    """Fetch one year via retarats_v2 backfill window. Returns process rc."""
    cmd = [
        sys.executable, "retarats_v2.py",
        "--config-mode", args.config_mode,
        "--mode", "backfill",
        "--start-year", str(year),
        "--end-year", str(year),
        "--summary-mode", args.summary_mode,
        "--role-rules", args.role_rules,
        "--sinks", "local",
        "--local-db", args.db,
        "--state-db", args.state_db,
    ]
    if args.molecule:
        cmd += ["--molecule", args.molecule]
    if args.max_records_per_rule:
        cmd += ["--max-records-per-rule", str(args.max_records_per_rule)]
    print("  $", " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def main() -> None:
    ap = argparse.ArgumentParser(description="Resumable, size-capped PubMed historical backfill.")
    ap.add_argument("--start-year", type=int, default=dt.datetime.now().year,
                    help="Newest year to fetch (walk backwards from here).")
    ap.add_argument("--min-year", type=int, default=1975, help="Oldest year to attempt.")
    ap.add_argument("--target-gb", type=float, default=10.0, help="Stop once the DB reaches this size.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--state-db", default="data/retarats_state.sqlite")
    ap.add_argument("--checkpoint", default=CHECKPOINT)
    ap.add_argument("--config-mode", default="local",
                    help="Config source: 'local' reads config/MOLECULES.csv + SEARCH_RULES.csv "
                         "(88 active rules); 'inputs' reads inputs/*.xlsx (empty in this repo).")
    ap.add_argument("--summary-mode", default="rule_based")
    ap.add_argument("--role-rules", default="config/ROLE_RULES.csv")
    ap.add_argument("--molecule", default="")
    ap.add_argument("--max-records-per-rule", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true", help="Run the curation pipeline after backfill.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore the checkpoint and re-fetch every year in range (use after a run "
                         "that recorded years as done but fetched nothing).")
    args = ap.parse_args()

    ckpt = _load_ckpt(args.checkpoint)
    done = set() if args.force else set(ckpt.get("completed_years", []))
    if args.force:
        ckpt = {"completed_years": [], "history": ckpt.get("history", [])}

    print(f"Backfill: {args.start_year} -> {args.min_year}, target {args.target_gb} GB, db={args.db}")
    print(f"Already completed: {sorted(done, reverse=True) or 'none'}")

    for year in range(args.start_year, args.min_year - 1, -1):
        size = _db_size_gb(args.db)
        if size >= args.target_gb:
            print(f"Reached target size {size:.2f} GB >= {args.target_gb} GB. Stopping.")
            break
        if year in done:
            print(f"[{year}] already done, skipping.")
            continue
        print(f"[{year}] fetching (db {size:.2f} GB)...", flush=True)
        rc = _fetch_year(year, args)
        if rc != 0:
            print(f"[{year}] fetch returned rc={rc}; stopping so the run can be inspected/resumed.")
            break
        done.add(year)
        ckpt["completed_years"] = sorted(done, reverse=True)
        ckpt["history"].append({"year": year, "db_gb_after": round(_db_size_gb(args.db), 3),
                                "at": dt.datetime.utcnow().isoformat() + "Z"})
        _save_ckpt(args.checkpoint, ckpt)

    final = _db_size_gb(args.db)
    print(f"Done. DB size {final:.2f} GB. Completed years: {sorted(done, reverse=True)}")

    if args.rebuild:
        print("Rebuilding curated layer + site...")
        subprocess.call([sys.executable, "scripts/run_curation_pipeline.py", "--db", args.db])


if __name__ == "__main__":
    main()
