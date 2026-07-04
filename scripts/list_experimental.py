#!/usr/bin/env python3
"""List / validate / export the experimental (candidate) molecule table.

``config/EXPERIMENTAL_MOLECULES.csv`` proposes candidate peptides/drugs that fit
the metabolic / longevity / peptide-therapeutic vibe of ``config/MOLECULES.csv``
but do **not** have data in the pipeline yet (no fetch has been run for them).

This script is the small, offline "awareness" layer for those candidates:

  * validates the CSV shape (required columns, non-empty ids, unique ids),
  * prints a readable table (or ``--json`` / ``--csv`` export),
  * with ``--include-experimental`` prints the note describing how a candidate
    actually enters the pipeline.

**How an experimental molecule enters the pipeline (documented here so it is not
lost):** these rows are *proposals only* — nothing is fetched for them. To make a
candidate a real, fetched molecule the user must:

  1. Add a row to ``config/MOLECULES.csv`` (molecule_id, display_name, type,
     mechanism_class, status, synonyms_csv, exclusions_csv, active, notes) reusing
     the ``example_search_terms`` here as the synonyms/query seeds.
  2. Add matching ``config/SEARCH_RULES.csv`` entries so the PubMed fetch has a
     query for the molecule (respecting exclusion terms to avoid ambiguity).
  3. Run a fetch/backfill on a networked machine
     (``python3 scripts/run_backfill.py`` or the normal fetch mode) so
     ``papers``/``evidence`` rows are populated.
  4. Re-run ``scripts/build_curated_database.py`` so the candidate appears in the
     curated dataset with facets/reliability/ranking like any other molecule.

Pure stdlib; no network; non-destructive (read-only over the CSV).

    python3 scripts/list_experimental.py
    python3 scripts/list_experimental.py --json
    python3 scripts/list_experimental.py --csv out.csv
    python3 scripts/list_experimental.py --include-experimental
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

DEFAULT_CSV = os.path.join("config", "EXPERIMENTAL_MOLECULES.csv")

REQUIRED_COLUMNS = [
    "molecule_id",
    "display_name",
    "class",
    "rationale",
    "status",
    "example_search_terms",
]


def load_experimental(path: str = DEFAULT_CSV) -> list:
    """Load experimental molecule rows from CSV. Returns a list of dicts."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def validate(rows: list, header: list) -> list:
    """Return a list of validation error strings (empty == valid)."""
    errors: list = []
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing_cols:
        errors.append(f"missing required columns: {', '.join(missing_cols)}")
        return errors  # can't do row-level checks without the columns
    seen: dict = {}
    for i, r in enumerate(rows, start=2):  # line 1 is the header
        mid = (r.get("molecule_id") or "").strip()
        if not mid:
            errors.append(f"line {i}: empty molecule_id")
            continue
        if mid in seen:
            errors.append(f"line {i}: duplicate molecule_id '{mid}' (first seen line {seen[mid]})")
        else:
            seen[mid] = i
        if not (r.get("display_name") or "").strip():
            errors.append(f"line {i}: '{mid}' missing display_name")
        status = (r.get("status") or "").strip()
        if status != "experimental":
            errors.append(f"line {i}: '{mid}' status is '{status}', expected 'experimental'")
        if not (r.get("example_search_terms") or "").strip():
            errors.append(f"line {i}: '{mid}' missing example_search_terms")
    return errors


NOTE = """\
[--include-experimental] Experimental molecules are PROPOSALS ONLY; they have no
fetched data. To bring one into the pipeline:
  1) add it to config/MOLECULES.csv (reuse example_search_terms as synonyms),
  2) add matching config/SEARCH_RULES.csv query rows,
  3) run a fetch/backfill on a networked machine,
  4) re-run scripts/build_curated_database.py.
Until then they do not appear in the curated dataset."""


def _print_table(rows: list) -> None:
    if not rows:
        print("(no experimental molecules found)")
        return
    for r in rows:
        print(f"- {r.get('display_name','')}  [{r.get('molecule_id','')}]  ({r.get('class','')})")
        rationale = (r.get("rationale") or "").strip()
        if rationale:
            print(f"    rationale: {rationale}")
        terms = (r.get("example_search_terms") or "").strip()
        if terms:
            print(f"    search terms: {terms}")


def main() -> None:
    ap = argparse.ArgumentParser(description="List / validate / export experimental candidate molecules.")
    ap.add_argument("--file", default=DEFAULT_CSV, help="Path to EXPERIMENTAL_MOLECULES.csv")
    ap.add_argument("--json", action="store_true", help="Emit the rows as JSON to stdout.")
    ap.add_argument("--csv", default="", help="Copy the validated rows to this CSV path.")
    ap.add_argument("--include-experimental", action="store_true",
                    help="Print the note describing how candidates enter the pipeline.")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"ERROR: {args.file} not found", file=sys.stderr)
        sys.exit(2)

    with open(args.file, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        rows = list(reader)

    errors = validate(rows, header)
    if errors:
        print("VALIDATION FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} experimental molecules -> {args.csv}")
    else:
        print(f"Experimental candidate molecules ({len(rows)}) — VALIDATED OK\n")
        _print_table(rows)

    if args.include_experimental:
        print()
        print(NOTE)


if __name__ == "__main__":
    main()
