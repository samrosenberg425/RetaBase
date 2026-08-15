#!/usr/bin/env python3
"""Propose regulatory / development-stage rows from free, open registries.

SAFE BY DEFAULT: this NEVER overwrites the human-curated config/regulatory.csv. It
writes proposals to config/regulatory_suggested.csv for review. Only `--emit` merges
them in, and even then only for molecule_ids that DON'T already have a curated row
(unless `--force`). Regulatory status is high-stakes and changes over time, so a
human confirms before anything goes live.

Sources (all free, no key required):
* ChEMBL   ebi.ac.uk/chembl        -> max_phase (0-4): GLOBAL highest development stage
* openFDA  api.fda.gov/drug/drugsfda + /drug/label -> US approval + indications
* DailyMed dailymed.nlm.nih.gov    -> is there a currently marketed US label (us_marketed)

Every proposed row carries reg_source + reg_retrieved_utc. Heuristic fields (status,
access pathways) are best-effort and flagged for review; grey-market / compounding
are NEVER inferred here (those must be curated with a citable source).

    python3 scripts/run_regulatory_enrich.py                 # dry-run -> _suggested.csv
    python3 scripts/run_regulatory_enrich.py --emit          # merge NEW molecules only
    python3 scripts/run_regulatory_enrich.py --mock --limit 3  # offline plumbing test
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_curated_database import REGULATORY_FIELDS  # noqa: E402

MOLECULES_CSV = os.path.join("config", "molecules.csv")
REG_CSV = os.path.join("config", "regulatory.csv")
SUGGEST_CSV = os.path.join("config", "regulatory_suggested.csv")
_PHASE_LABEL = {4: "approved", 3: "Phase 3", 2: "Phase 2", 1: "Phase 1", 0: ""}


def _get(url: str, mock: Optional[dict] = None) -> Optional[dict]:
    if mock is not None:
        return mock
    try:
        import requests
    except ImportError:
        return None
    try:
        time.sleep(0.2)
        r = requests.get(url, timeout=30, headers={"User-Agent": "RetaBase/1.0 (research)"})
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 -- enrichment is optional; never crash a run
        return None


def _load_molecules(path: str) -> List[dict]:
    if not os.path.exists(path):
        # tolerate case-variant filename
        alt = os.path.join("config", "MOLECULES.csv")
        path = alt if os.path.exists(alt) else path
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            mid = str(row.get("molecule_id", "") or "").strip()
            if mid and str(row.get("active", "True")).strip().lower() != "false":
                out.append({"molecule_id": mid,
                            "name": str(row.get("display_name", "") or mid).strip()})
    return out


def _existing_ids(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        return {str(r.get("molecule_id", "")).strip() for r in csv.DictReader(fh)}


def _chembl_max_phase(name: str, mock=None) -> Optional[int]:
    data = _get(f"https://www.ebi.ac.uk/chembl/api/data/molecule/search?q={name}&format=json",
                (mock or {}).get("chembl") if mock else None)
    try:
        mols = (data or {}).get("molecules") or []
        if mols:
            mp = mols[0].get("max_phase")
            return int(float(mp)) if mp not in (None, "") else None
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _openfda(name: str, mock=None) -> Dict[str, str]:
    out = {"approved": False, "application": "", "indications": ""}
    daf = _get(f'https://api.fda.gov/drug/drugsfda.json?search=openfda.generic_name:"{name}"&limit=1',
               (mock or {}).get("drugsfda") if mock else None)
    try:
        res = (daf or {}).get("results") or []
        if res:
            out["approved"] = True
            out["application"] = str(res[0].get("application_number", "") or "")
    except (AttributeError, TypeError):
        pass
    lab = _get(f'https://api.fda.gov/drug/label.json?search=openfda.generic_name:"{name}"&limit=1',
               (mock or {}).get("label") if mock else None)
    try:
        res = (lab or {}).get("results") or []
        if res:
            ind = res[0].get("indications_and_usage") or []
            txt = " ".join(ind) if isinstance(ind, list) else str(ind)
            out["indications"] = txt.strip().replace("\n", " ")[:300]
    except (AttributeError, TypeError):
        pass
    return out


def _dailymed_marketed(name: str, mock=None) -> bool:
    data = _get(f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name={name}",
                (mock or {}).get("dailymed") if mock else None)
    try:
        return int((data or {}).get("metadata", {}).get("total_elements", 0)) > 0
    except (ValueError, TypeError, AttributeError):
        return False


def propose(mol: dict, mock=None) -> Dict[str, str]:
    name = mol["name"]
    max_phase = _chembl_max_phase(name, mock)
    fda = _openfda(name, mock)
    marketed = _dailymed_marketed(name, mock)

    approved = fda["approved"] or max_phase == 4 or marketed
    if approved:
        status = "approved"
    elif max_phase and max_phase >= 1:
        status = "investigational"
    else:
        status = ""  # unknown -> leave for curation

    # Access pathway: only the two we can defensibly infer. Compounding/grey-market/
    # research-only are NEVER inferred -- they must be curated with a citable source.
    pathways = []
    if approved and marketed:
        pathways.append("physician-prescribed")
    elif status == "investigational":
        pathways.append("clinical-trial-only")

    row = {k: "" for k in REGULATORY_FIELDS}
    row.update({
        "regulatory_status": status,
        "fda_approved_indications": fda["indications"] if approved else "",
        "us_marketed": "True" if marketed else ("False" if status else ""),
        "ex_us_status": "developed/approved outside the US" if (max_phase == 4 and not fda["approved"]) else "",
        "access_pathways": "; ".join(pathways),
        "reg_source": "auto: ChEMBL + openFDA + DailyMed (REVIEW BEFORE PUBLISHING)",
        "reg_source_url": "https://api.fda.gov/ ; https://www.ebi.ac.uk/chembl/",
        "reg_retrieved_utc": dt.datetime.utcnow().strftime("%Y-%m-%d"),
    })
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="cap molecules processed (0 = all)")
    ap.add_argument("--only-missing", action="store_true", default=True,
                    help="skip molecules already in regulatory.csv (default)")
    ap.add_argument("--all", dest="only_missing", action="store_false",
                    help="propose for every molecule, even curated ones")
    ap.add_argument("--emit", action="store_true", help="merge NEW molecule rows into regulatory.csv")
    ap.add_argument("--force", action="store_true", help="with --emit, overwrite curated rows too")
    ap.add_argument("--mock", action="store_true", help="canned responses; no network")
    args = ap.parse_args()

    mols = _load_molecules(MOLECULES_CSV)
    if not mols:
        print("No molecules found in config/molecules.csv", file=sys.stderr)
        sys.exit(1)
    curated = _existing_ids(REG_CSV)
    todo = [m for m in mols if not (args.only_missing and m["molecule_id"] in curated)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(mols)} molecules; proposing regulatory rows for {len(todo)} "
          f"({len(curated)} already curated).")

    _mock = {"chembl": {"molecules": [{"max_phase": 4}]},
             "drugsfda": {"results": [{"application_number": "NDA000000"}]},
             "label": {"results": [{"indications_and_usage": ["Indicated for the demo condition."]}]},
             "dailymed": {"metadata": {"total_elements": 3}}} if args.mock else None

    rows = []
    for i, m in enumerate(todo, 1):
        row = propose(m, _mock)
        row = {"molecule_id": m["molecule_id"], **row}
        rows.append(row)
        if i % 25 == 0:
            print(f"  ...{i}/{len(todo)}")

    header = ["molecule_id"] + REGULATORY_FIELDS
    os.makedirs("config", exist_ok=True)
    with open(SUGGEST_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    filled = sum(1 for r in rows if r["regulatory_status"])
    print(f"Wrote {len(rows)} proposals ({filled} with a status) -> {SUGGEST_CSV}")
    print("REVIEW these before publishing; grey-market/compounding are NOT auto-inferred.")

    if args.emit:
        existing = {}
        if os.path.exists(REG_CSV):
            with open(REG_CSV, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    existing[str(r.get("molecule_id", "")).strip()] = r
        added = 0
        for r in rows:
            mid = r["molecule_id"]
            if mid in existing and not args.force:
                continue  # never clobber curated rows without --force
            if not r["regulatory_status"]:
                continue  # nothing worth adding
            existing[mid] = r
            added += 1
        with open(REG_CSV, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=header)
            w.writeheader()
            for mid in sorted(existing):
                w.writerow({k: existing[mid].get(k, "") for k in header})
        print(f"--emit: merged {added} new rows into {REG_CSV} (curated rows preserved).")


if __name__ == "__main__":
    main()
