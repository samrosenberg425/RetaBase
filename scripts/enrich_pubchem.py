#!/usr/bin/env python3
"""Resolve every molecule to a PubChem CID (for a 'learn more' link on the
Bioactives page) and harvest its PubChem synonyms as CANDIDATE search terms.

NON-destructive by design: it writes two NEW files and does NOT edit MOLECULES.csv
or SEARCH_RULES.csv. PubChem synonyms include a lot of noise (registry numbers,
vendor codes, ambiguous common words), and our search rules were deliberately
tightened -- so synonyms are emitted for REVIEW, not auto-merged.

Outputs (in --out-dir, default config/):
  pubchem_cids.csv               molecule_id, display_name, pubchem_cid, pubchem_name
  pubchem_synonyms_suggested.csv molecule_id, pubchem_cid, synonym   (filtered for usefulness)

    python3 scripts/enrich_pubchem.py

NETWORK REQUIRED (pubchem.ncbi.nlm.nih.gov). Polite (~3 requests/second).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.pubchem import pubchem_cid, pubchem_synonyms  # noqa: E402


def _load(path: str):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _name(row: dict) -> str:
    for k in ("display_name", "name", "molecule_name"):
        if row.get(k):
            return str(row[k]).strip()
    return str(row.get("molecule_id", "") or "").replace("_", " ").strip()


def _useful_synonym(s: str) -> bool:
    """Keep human-readable chemical/brand names; drop registry ids and codes."""
    s = s.strip()
    if len(s) < 3 or len(s) > 60:
        return False
    if s.replace("-", "").replace(",", "").replace(".", "").isdigit():
        return False  # CAS / registry numbers
    low = s.lower()
    if low.startswith(("cid ", "schembl", "chembl", "dtxsid", "unii", "ec ", "mfcd", "akos", "ncgc")):
        return False
    # Vendor/database codes like "AB-1234567" or "US1234567".
    if sum(ch.isdigit() for ch in s) >= 5 and any(ch.isalpha() for ch in s):
        return False
    return True


def _write(path: str, rows, cols) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    ap = argparse.ArgumentParser(description="PubChem CID + synonym enrichment (non-destructive).")
    ap.add_argument("--molecules", default="config/MOLECULES.csv")
    ap.add_argument("--out-dir", default="config")
    ap.add_argument("--max-synonyms", type=int, default=25)
    ap.add_argument("--sleep", type=float, default=0.34)
    args = ap.parse_args()

    rows = _load(args.molecules)
    cids_out, syn_out = [], []
    for row in rows:
        mid = str(row.get("molecule_id", "") or "").strip()
        if not mid:
            continue
        name = _name(row)
        cid = pubchem_cid(name)
        time.sleep(args.sleep)
        pname, syns = "", []
        if cid:
            syns = pubchem_synonyms(cid)
            time.sleep(args.sleep)
            pname = syns[0] if syns else ""
            for s in [x for x in syns if _useful_synonym(x)][: args.max_synonyms]:
                syn_out.append({"molecule_id": mid, "pubchem_cid": cid, "synonym": s})
        cids_out.append({"molecule_id": mid, "display_name": name,
                         "pubchem_cid": cid or "", "pubchem_name": pname})
        print(f"  {mid}: CID {cid or 'NONE'} ({len(syns)} synonyms)", flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    _write(os.path.join(args.out_dir, "pubchem_cids.csv"), cids_out,
           ["molecule_id", "display_name", "pubchem_cid", "pubchem_name"])
    _write(os.path.join(args.out_dir, "pubchem_synonyms_suggested.csv"), syn_out,
           ["molecule_id", "pubchem_cid", "synonym"])
    resolved = sum(1 for r in cids_out if r["pubchem_cid"])
    print(f"\nResolved {resolved}/{len(cids_out)} molecules to a CID; "
          f"{len(syn_out)} candidate synonyms written for review.")


if __name__ == "__main__":
    main()
