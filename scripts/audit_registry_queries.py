#!/usr/bin/env python3
"""Audit the registry (CT.gov) and preprint (EuropePMC) search terms, per molecule and per term.

For every active molecule and every term in its query, run that term ALONE (quoted) against
the live source and measure how many of the first N returned records actually NAME the term
(whole-token, hyphen/space-insensitive). A term whose records mostly do not name it is
matching on tokenisation/fuzzy-expansion rather than on the molecule, e.g. "MT-II" read as
"MT" + "II" matches every "Phase II" trial.

Read-only: GET requests only, nothing is written to the corpus.

    python3 scripts/audit_registry_queries.py --source ctgov --out audit_ctgov.csv
    python3 scripts/audit_registry_queries.py --source preprints --out audit_preprints.csv

Output columns: molecule_id, source, term, in_current_query_bare, total_hits, sample_n,
sample_naming (records naming THIS term), naming_precision, sample_naming_any_name /
precision_any_name (records naming ANY known name of the molecule -- the on-topic measure),
total_hits_bare (CT.gov, bare single-token terms only).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.registry import load_active_molecules, molecule_query_terms  # noqa: E402
from retarats_pipeline.manual_exclusions import _term_regex  # noqa: E402

CTG = "https://clinicaltrials.gov/api/v2/studies"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CTG_FIELDS = ("NCTId|BriefTitle|OfficialTitle|BriefSummary|DetailedDescription|Condition|Keyword|"
              "InterventionName|InterventionOtherName|InterventionDescription|ArmGroupLabel|ArmGroupDescription")
SLEEP = 0.35


def _get(url: str, params: dict, tries: int = 4) -> Optional[dict]:
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=40,
                             headers={"User-Agent": "retarats-registry-audit (read-only)"})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** attempt * 2, 30))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(min(2 ** attempt * 2, 30))
    return None


def _naming(terms, texts: List[str]) -> int:
    """How many texts name ANY of ``terms`` (whole-token, hyphen/space-insensitive)."""
    rxs = [_term_regex(t) for t in ([terms] if isinstance(terms, str) else terms)]
    return sum(1 for t in texts if any(rx.search(t) for rx in rxs))


def all_names(molecule: dict) -> List[str]:
    """Every name we know for the molecule (display name + all synonyms, len>=3). A record
    naming any of them is on-topic even when it does not use the exact query term (CT.gov
    expands drug codes/brands server-side, so BMS-512148 legitimately returns dapagliflozin
    trials)."""
    names = [molecule.get("display_name", "")] + str(molecule.get("synonyms_csv", "") or "").split(",")
    out, seen = [], set()
    for n in names:
        n = n.strip()
        if len(n) >= 3 and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def ctgov_term(term: str, quoted: bool, sample: int, names=None) -> Dict[str, object]:
    q = f'"{term}"' if quoted else term
    data = _get(CTG, {"query.term": q, "pageSize": sample, "countTotal": "true", "format": "json",
                      "fields": CTG_FIELDS})
    if data is None:
        return {"total": None, "n": 0, "named": 0, "named_any": 0}
    studies = data.get("studies") or []
    texts = [json.dumps(s) for s in studies]
    return {"total": data.get("totalCount"), "n": len(studies), "named": _naming(term, texts),
            "named_any": _naming(names or [term], texts)}


def preprints_term(term: str, sample: int, names=None) -> Dict[str, object]:
    data = _get(EPMC, {"query": f'"{term}" AND SRC:PPR', "format": "json", "pageSize": sample, "resultType": "core"})
    if data is None:
        return {"total": None, "n": 0, "named": 0, "named_any": 0}
    res = (data.get("resultList") or {}).get("result") or []
    texts = [f"{r.get('title', '')} {r.get('abstractText', '')}" for r in res]
    return {"total": data.get("hitCount"), "n": len(res), "named": _naming(term, texts),
            "named_any": _naming(names or [term], texts)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["ctgov", "preprints"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=50, help="Records inspected per term.")
    ap.add_argument("--molecules", default="config/MOLECULES.csv")
    ap.add_argument("--only", default="", help="Comma-separated molecule_ids (default: all active).")
    args = ap.parse_args(argv)

    only = {m.strip() for m in args.only.split(",") if m.strip()}
    molecules = [m for m in load_active_molecules(args.molecules) if not only or m.get("molecule_id") in only]
    cols = ["molecule_id", "source", "term", "in_current_query_bare", "total_hits", "sample_n", "sample_naming",
            "naming_precision", "sample_naming_any_name", "precision_any_name", "total_hits_bare"]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for i, m in enumerate(molecules, 1):
            mid = m.get("molecule_id", "")
            for term in molecule_query_terms(m):
                bare = " " not in term  # production currently leaves single-token terms unquoted
                names = all_names(m)
                res = (ctgov_term(term, True, args.sample, names) if args.source == "ctgov"
                       else preprints_term(term, args.sample, names))
                time.sleep(SLEEP)
                bare_total = ""
                if args.source == "ctgov" and bare:
                    bare_total = (ctgov_term(term, False, 1) or {}).get("total", "")
                    time.sleep(SLEEP)
                n, named = res["n"], res["named"]
                w.writerow({"molecule_id": mid, "source": args.source, "term": term, "in_current_query_bare": bare,
                            "total_hits": res["total"], "sample_n": n, "sample_naming": named,
                            "naming_precision": round(named / n, 3) if n else "",
                            "sample_naming_any_name": res["named_any"],
                            "precision_any_name": round(res["named_any"] / n, 3) if n else "",
                            "total_hits_bare": bare_total})
                fh.flush()
            print(f"[{i}/{len(molecules)}] {mid}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
