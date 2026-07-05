#!/usr/bin/env python3
"""Audit how many PubMed records each search rule returns — WITHOUT downloading
them. Use this to spot rules that are too broad (endogenous/dietary molecules
whose name floods the results) before committing to a big backfill.

It only calls esearch (counts), so it's fast (~one request per rule) and cheap.

    # totals across all years (best signal for over-broad rules):
    python3 scripts/audit_rule_counts.py

    # a single busy year, to spot the >10,000/year rules that crash the fetch:
    python3 scripts/audit_rule_counts.py --year 2025

    # only flag rules above a threshold:
    python3 scripts/audit_rule_counts.py --min-count 5000

NETWORK REQUIRED (hits NCBI E-utilities). Set NCBI_EMAIL / NCBI_API_KEY in the
environment (or .env) for the higher rate limit. Runs anywhere with network —
your machine or a GitHub Actions runner; writes nothing to disk.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retarats_pipeline.pubmed import PubMedClient  # noqa: E402

# NCBI's history-server efetch cannot page past this many records per query, so
# any rule/year above it crashes a normal fetch (that's the glutathione case).
CRASH_CEILING = 10000


def _load_active_rules(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if str(r.get("active", "")).strip().lower() in {"true", "1", "yes"}]


def main() -> None:
    ap = argparse.ArgumentParser(description="Count PubMed hits per search rule (no downloads).")
    ap.add_argument("--rules", default="config/SEARCH_RULES.csv")
    ap.add_argument("--year", type=int, default=0,
                    help="Restrict the count to this publication year (spot >10k/year crash risks). "
                         "Default 0 = all years.")
    ap.add_argument("--min-count", type=int, default=0,
                    help="Only print rules with at least this many hits.")
    ap.add_argument("--datetype", default=os.getenv("NCBI_DATETYPE", "pdat"))
    args = ap.parse_args()

    client = PubMedClient(
        email=os.getenv("NCBI_EMAIL", os.getenv("API_CONTACT_EMAIL", "")).strip() or "anonymous@example.com",
        api_key=os.getenv("NCBI_API_KEY", "").strip(),
        max_requests_per_second=float(os.getenv("NCBI_MAX_RPS", "9.0" if os.getenv("NCBI_API_KEY") else "2.5")),
    )

    rules = _load_active_rules(args.rules)
    scope = f"year {args.year}" if args.year else "all years"
    print(f"Auditing {len(rules)} active rules ({scope})...\n", flush=True)

    results = []
    for rule in rules:
        kwargs = {"term": rule["query_string"], "usehistory": False, "retmax": 0, "datetype": args.datetype}
        if args.year:
            kwargs["mindate"] = f"{args.year}/01/01"
            kwargs["maxdate"] = f"{args.year}/12/31"
        try:
            count = client.esearch(**kwargs).count
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {rule['rule_id']}: query failed ({exc})", flush=True)
            continue
        results.append((count, rule["rule_id"], rule["molecule_id"]))

    results.sort(reverse=True)
    print(f"{'HITS':>9}  {'FLAG':<12}  RULE (molecule)")
    print("-" * 72)
    for count, rule_id, mol in results:
        if count < args.min_count:
            continue
        flag = ""
        if args.year and count > CRASH_CEILING:
            flag = "CRASHES/yr"
        elif count > CRASH_CEILING:
            flag = "very broad"
        elif count > 3000:
            flag = "broad"
        print(f"{count:>9}  {flag:<12}  {rule_id} ({mol})")

    if args.year:
        n_crash = sum(1 for c, _, _ in results if c > CRASH_CEILING)
        print(f"\n{n_crash} rule(s) exceed the {CRASH_CEILING}/year fetch ceiling in {args.year}.")


if __name__ == "__main__":
    main()
