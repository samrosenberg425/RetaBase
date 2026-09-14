#!/usr/bin/env python3
"""Fail fast if a config CSV is malformed, instead of letting every fetch silently
error out. This catches the exact class of bug where an unquoted comma in a notes
field shifts the columns (pandas: "Expected 6 fields ... saw 7"). Run at the top of
the fetch/backfill/update jobs so a bad config is caught before a run is wasted.

Exit code 0 = all good, 1 = a file failed to parse or has the wrong columns.
"""

import sys

import pandas as pd

# (path, expected exact column order or None to only check that it parses)
CHECKS = [
    ("config/MOLECULES.csv", None),
    ("config/SEARCH_RULES.csv",
     ["rule_id", "molecule_id", "match_strength", "query_string", "active", "notes"]),
]


def main() -> None:
    ok = True
    for path, expected_cols in CHECKS:
        try:
            df = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {path}: does not parse -> {exc}")
            ok = False
            continue
        if expected_cols is not None and list(df.columns) != expected_cols:
            print(f"FAIL  {path}: columns {list(df.columns)} != expected {expected_cols} "
                  "(likely an unquoted comma in a field)")
            ok = False
            continue
        print(f"OK    {path}: {len(df)} rows, {len(df.columns)} columns")
    if not ok:
        print("\nConfig validation FAILED — fix the CSV before fetching.")
        sys.exit(1)
    print("\nConfig OK.")


if __name__ == "__main__":
    main()
