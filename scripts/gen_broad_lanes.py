#!/usr/bin/env python3
"""Generate broad-recall PubMed "lanes" for SPARSE molecules.

Some molecules are badly under-represented in the local corpus (few or zero
matched records, or almost no human evidence). Their existing SEARCH_RULES.csv
rows tend to be *strict* — anchored to ``[tiab]`` on the display name plus a
handful of dev-codes — which is great for precision but leaves recall on the
table for molecules where we simply do not have enough papers yet.

This script builds an extra BROAD-RECALL lane per sparse molecule:

    display_name + every known synonym (config/pubchem_synonyms_suggested.csv)
    + MeSH terms (config/pubchem_cids.csv mesh_terms) + MOLECULES.csv synonyms_csv

all OR'd together in ``[tiab]`` (or ``[All Fields]`` with --field allfields),
NOT restricted to ``[majr]``/``[ti]`` and with NO citation floor — maximising
recall for the molecules that need it most.

SAFE BY DEFAULT
---------------
* Default run is a DRY-RUN: it prints the proposed rows + a summary and writes
  them to a SEPARATE file (config/broad_lanes_suggested.csv). It NEVER touches
  SEARCH_RULES.csv.
* Pass --emit to APPEND the validated, de-duplicated rows to SEARCH_RULES.csv.
  Rows are only appended if they parse cleanly (exactly 6 columns, commas inside
  fields are quoted) and do not duplicate an existing molecule's broad-recall
  lane. A timestamped .bak of SEARCH_RULES.csv is written first.

NO NETWORK REQUIRED. This only builds query strings; the user runs the actual
PubMed fetch later.

Examples
--------
    # dry-run against the repo (writes config/broad_lanes_suggested.csv):
    python3 scripts/gen_broad_lanes.py

    # tighten/loosen the sparseness cutoffs:
    python3 scripts/gen_broad_lanes.py --threshold 50 --human-threshold 5

    # actually append the suggestions to SEARCH_RULES.csv:
    python3 scripts/gen_broad_lanes.py --emit
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact SEARCH_RULES.csv schema (see scripts/validate_config.py). Any output
# must match this column order exactly or validate_config / the pipeline loader
# will reject it.
RULE_COLUMNS = ["rule_id", "molecule_id", "match_strength", "query_string", "active", "notes"]

TRUTHY = {"true", "1", "yes"}

FIELD_TAGS = {
    "tiab": ("[tiab]", "tiab"),
    "allfields": ("[All Fields]", "allfields"),
}


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _truthy(value) -> bool:
    return str(value).strip().lower() in TRUTHY


def load_molecules(path: Path) -> List[dict]:
    """Return active molecule rows from MOLECULES.csv (in file order)."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if _truthy(r.get("active", ""))]


def load_pubchem_synonyms(path: Path) -> Dict[str, List[str]]:
    """molecule_id -> ordered list of synonym `term` strings."""
    out: Dict[str, List[str]] = defaultdict(list)
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            mid = (row.get("molecule_id") or "").strip()
            term = (row.get("term") or "").strip()
            if mid and term:
                out[mid].append(term)
    return out


def load_mesh_terms(path: Path) -> Dict[str, List[str]]:
    """molecule_id -> list of MeSH terms (from the ';'-separated mesh_terms col).

    Also captures pubchem_name so the canonical PubChem label is included.
    """
    out: Dict[str, List[str]] = defaultdict(list)
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            mid = (row.get("molecule_id") or "").strip()
            if not mid:
                continue
            for term in (row.get("mesh_terms") or "").split(";"):
                term = term.strip()
                if term:
                    out[mid].append(term)
            pcn = (row.get("pubchem_name") or "").strip()
            if pcn:
                out[mid].append(pcn)
    return out


# --------------------------------------------------------------------------- #
# Corpus record counting
# --------------------------------------------------------------------------- #
def count_records(db_path: Path) -> Tuple[Optional[Counter], Optional[Counter], str]:
    """Count evidence records per molecule from the corpus DB.

    Returns (total_counter, human_counter, note). If the DB is missing or has no
    usable ``evidence`` table, both counters are None and the note explains the
    fallback (every molecule is then treated as a sparse candidate).
    """
    if not db_path.exists():
        return None, None, f"corpus DB not found at {db_path} — treating ALL molecules as sparse candidates"
    try:
        con = sqlite3.connect(str(db_path))
    except Exception as exc:  # noqa: BLE001
        return None, None, f"could not open corpus DB ({exc}) — treating ALL molecules as sparse candidates"
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        if "evidence" not in tables:
            return None, None, ("corpus DB has no 'evidence' table (not fetched yet) — "
                                "treating ALL molecules as sparse candidates")
        total: Counter = Counter()
        human: Counter = Counter()
        for (payload_json,) in con.execute("select payload_json from evidence"):
            try:
                d = json.loads(payload_json)
            except Exception:  # noqa: BLE001
                continue
            mid = str(d.get("molecule_id", "")).strip()
            if not mid:
                continue
            total[mid] += 1
            if d.get("model_type") == "human":
                human[mid] += 1
        return total, human, f"counted {sum(total.values())} evidence records across {len(total)} molecules"
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Query building
# --------------------------------------------------------------------------- #
def gather_terms(mol: dict, syn_map: Dict[str, List[str]], mesh_map: Dict[str, List[str]],
                 min_term_len: int) -> List[str]:
    """Ordered, case-insensitively de-duplicated list of query terms for a molecule."""
    mid = (mol.get("molecule_id") or "").strip()
    ordered: List[str] = []
    seen: Set[str] = set()

    def add(term: str) -> None:
        term = (term or "").strip().strip('"').strip()
        if len(term) < min_term_len:
            return
        key = term.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(term)

    # display name first, then curator synonyms, then PubChem synonyms, then MeSH.
    add(mol.get("display_name", ""))
    for term in (mol.get("synonyms_csv") or "").split(","):
        add(term)
    for term in syn_map.get(mid, []):
        add(term)
    for term in mesh_map.get(mid, []):
        add(term)
    return ordered


def build_query(terms: List[str], field_tag: str) -> str:
    parts = [f'"{t}"{field_tag}' for t in terms]
    return "(" + " OR ".join(parts) + ")"


def build_row(mol: dict, terms: List[str], field_tag: str, field_slug: str,
              total: Optional[int], human: Optional[int]) -> dict:
    mid = (mol.get("molecule_id") or "").strip()
    query = build_query(terms, field_tag)
    n_syn = max(len(terms) - 1, 0)  # excludes the display name itself
    low_conf = n_syn == 0
    count_note = ("counts unavailable (fallback)" if total is None
                  else f"corpus total={total} human={human}")
    note = f"auto broad-recall lane; {n_syn} synonym(s); {count_note}"
    if low_conf:
        note = "LOW CONFIDENCE name-only lane; " + note
    return {
        "rule_id": f"{mid}_broadrecall_{field_slug}_v1",
        "molecule_id": mid,
        "match_strength": "broad",
        "query_string": query,
        "active": "True",
        "notes": note,
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_rows(path: Path, rows: List[dict]) -> None:
    """Write rows to `path` with correct quoting (commas inside fields are quoted)."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RULE_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in RULE_COLUMNS})


def existing_rule_ids_and_lanes(rules_path: Path) -> Tuple[Set[str], Set[str]]:
    """Return (set of existing rule_ids, set of molecule_ids that already have a
    broad-recall lane) so --emit never duplicates a molecule+lane."""
    rule_ids: Set[str] = set()
    broadrecall_mols: Set[str] = set()
    if not rules_path.exists():
        return rule_ids, broadrecall_mols
    with open(rules_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rid = (row.get("rule_id") or "").strip()
            mid = (row.get("molecule_id") or "").strip()
            if rid:
                rule_ids.add(rid)
            if "broadrecall" in rid and mid:
                broadrecall_mols.add(mid)
    return rule_ids, broadrecall_mols


def validate_appended(rules_path: Path) -> None:
    """Raise if the rules file no longer parses as a clean 6-column CSV."""
    with open(rules_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header != RULE_COLUMNS:
            raise ValueError(f"header changed unexpectedly: {header}")
        for i, row in enumerate(reader, start=2):
            if len(row) != len(RULE_COLUMNS):
                raise ValueError(f"line {i} has {len(row)} fields, expected {len(RULE_COLUMNS)} "
                                 "(unquoted comma?)")


def emit_to_rules(rules_path: Path, new_rows: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Append de-duplicated new_rows to SEARCH_RULES.csv atomically.

    Returns (appended_rows, skipped_rows).
    """
    existing_ids, existing_broadrecall = existing_rule_ids_and_lanes(rules_path)
    to_add: List[dict] = []
    skipped: List[dict] = []
    for row in new_rows:
        if row["rule_id"] in existing_ids or row["molecule_id"] in existing_broadrecall:
            skipped.append(row)
        else:
            to_add.append(row)
    if not to_add:
        return [], skipped

    # Read existing content, append, write to a temp file, validate, then swap.
    with open(rules_path, newline="", encoding="utf-8") as fh:
        existing_text = fh.read()

    tmp_path = rules_path.with_suffix(".csv.tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8") as fh:
        fh.write(existing_text)
        if not existing_text.endswith("\n"):
            fh.write("\n")
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        for row in to_add:
            writer.writerow([row.get(c, "") for c in RULE_COLUMNS])

    validate_appended(tmp_path)  # raises on any malformed row before we commit

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = rules_path.with_suffix(f".csv.bak_{stamp}")
    backup.write_text(existing_text, encoding="utf-8")
    os.replace(tmp_path, rules_path)
    return to_add, skipped


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--molecules", default="config/MOLECULES.csv", help="molecule registry CSV")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite", help="corpus DB used to count records")
    ap.add_argument("--rules", default="config/SEARCH_RULES.csv", help="search-rule file (only touched with --emit)")
    ap.add_argument("--synonyms", default="config/pubchem_synonyms_suggested.csv")
    ap.add_argument("--cids", default="config/pubchem_cids.csv")
    ap.add_argument("--out", default="config/broad_lanes_suggested.csv",
                    help="separate file the dry-run always writes")
    ap.add_argument("--threshold", type=int, default=100,
                    help="a molecule is sparse if it has FEWER than this many total corpus records")
    ap.add_argument("--human-threshold", type=int, default=10,
                    help="a molecule is also sparse if it has FEWER than this many human records")
    ap.add_argument("--field", choices=sorted(FIELD_TAGS), default="tiab",
                    help="PubMed field to search the OR'd terms in (default: tiab)")
    ap.add_argument("--min-term-len", type=int, default=3,
                    help="drop synonyms shorter than this many characters")
    ap.add_argument("--emit", action="store_true",
                    help="APPEND validated, de-duplicated rows to SEARCH_RULES.csv (off by default)")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    field_tag, field_slug = FIELD_TAGS[args.field]

    mol_path = _resolve(args.molecules)
    db_path = _resolve(args.db)
    rules_path = _resolve(args.rules)
    out_path = _resolve(args.out)

    molecules = load_molecules(mol_path)
    syn_map = load_pubchem_synonyms(_resolve(args.synonyms))
    mesh_map = load_mesh_terms(_resolve(args.cids))
    total_counts, human_counts, count_note = count_records(db_path)
    fallback = total_counts is None

    print("=" * 72)
    print("gen_broad_lanes.py — broad-recall lanes for SPARSE molecules")
    print("=" * 72)
    print(f"molecules registry : {mol_path}  ({len(molecules)} active)")
    print(f"corpus DB          : {db_path}")
    print(f"  -> {count_note}")
    print(f"sparseness rule    : total < {args.threshold}  OR  human < {args.human_threshold}")
    print(f"query field        : {field_tag}")
    print(f"mode               : {'EMIT (will append to SEARCH_RULES.csv)' if args.emit else 'DRY-RUN (safe)'}")
    print()

    rows: List[dict] = []
    qualified: List[Tuple[str, int, int, int, bool]] = []  # mid, total, human, n_syn, low_conf
    for mol in molecules:
        mid = (mol.get("molecule_id") or "").strip()
        if not mid:
            continue
        total = None if fallback else total_counts.get(mid, 0)
        human = None if fallback else human_counts.get(mid, 0)
        if not fallback and not (total < args.threshold or human < args.human_threshold):
            continue  # not sparse
        terms = gather_terms(mol, syn_map, mesh_map, args.min_term_len)
        if not terms:  # extreme edge: no usable name at all — skip
            continue
        row = build_row(mol, terms, field_tag, field_slug, total, human)
        rows.append(row)
        n_syn = max(len(terms) - 1, 0)
        qualified.append((mid, total if total is not None else -1,
                          human if human is not None else -1, n_syn, n_syn == 0))

    # Always write the separate suggestions file (never SEARCH_RULES.csv).
    write_rows(out_path, rows)

    # ---- summary -------------------------------------------------------------
    print(f"SPARSE molecules qualifying: {len(rows)} / {len(molecules)}")
    if fallback:
        print("  (fallback active: corpus counts unavailable, so every molecule qualifies)")
    low_conf_n = sum(1 for q in qualified if q[4])
    print(f"  name-only / LOW CONFIDENCE lanes: {low_conf_n}")
    print()
    print(f"{'MOLECULE':<26}{'TOTAL':>7}{'HUMAN':>7}{'SYN':>6}  FLAG")
    print("-" * 60)
    for mid, total, human, n_syn, low_conf in sorted(qualified, key=lambda x: (x[1], x[0]))[:40]:
        t = "n/a" if total < 0 else str(total)
        h = "n/a" if human < 0 else str(human)
        flag = "LOW-CONF" if low_conf else ""
        print(f"{mid:<26}{t:>7}{h:>7}{n_syn:>6}  {flag}")
    if len(qualified) > 40:
        print(f"... and {len(qualified) - 40} more (see {out_path})")
    print()

    # ---- a few example generated queries ------------------------------------
    print("Sample generated broad-recall lanes:")
    for row in rows[:3]:
        print(f"\n  rule_id : {row['rule_id']}")
        print(f"  query   : {row['query_string']}")
        print(f"  notes   : {row['notes']}")
    print()
    print(f"Wrote {len(rows)} proposed rows to: {out_path}")

    # ---- emit / would-emit ---------------------------------------------------
    existing_ids, existing_broadrecall = existing_rule_ids_and_lanes(rules_path)
    would_add = [r for r in rows
                 if r["rule_id"] not in existing_ids and r["molecule_id"] not in existing_broadrecall]
    would_skip = len(rows) - len(would_add)

    if not args.emit:
        print()
        print(f"[DRY-RUN] --emit would APPEND {len(would_add)} new row(s) to {rules_path} "
              f"({would_skip} skipped as duplicates). SEARCH_RULES.csv is UNCHANGED.")
        for r in would_add[:5]:
            print(f"    + {r['rule_id']}")
        if len(would_add) > 5:
            print(f"    ... and {len(would_add) - 5} more")
    else:
        appended, skipped = emit_to_rules(rules_path, rows)
        print()
        backup_note = " A timestamped .bak was written alongside." if appended else ""
        print(f"[EMIT] appended {len(appended)} row(s) to {rules_path}; "
              f"skipped {len(skipped)} duplicate(s).{backup_note}")
        for r in appended:
            print(f"    + {r['rule_id']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
