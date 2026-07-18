#!/usr/bin/env python3
"""Audit which structured fields are MISSING across the corpus, and why.

Answers three questions the rules-vs-AI decision depends on:

1. How many records lack dose / route / duration / sample size?
2. Is the field missing because the ABSTRACT never states it (in which case only
   full text can help), or because the extractor failed on text that does state it
   (in which case it's a rule bug we can fix)?
3. How many of the gaps sit on open-access papers, i.e. are recoverable from full
   text at all?

Offline: reads the local corpus. `--check-oa` additionally asks the PMC ID Converter
which of the gap papers have open-access full text (one request per 200 PMIDs).

    python3 scripts/audit_missing_fields.py --db data/retarats_pubmed.sqlite
    python3 scripts/audit_missing_fields.py --check-oa --limit-oa 2000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from retarats_pipeline.curation.extractors import refine_extraction, _DOSE_RE  # noqa: E402

FIELDS = ["refined_dose", "refined_route", "refined_duration", "refined_sample_size"]
LABEL = {"refined_dose": "dose", "refined_route": "route",
         "refined_duration": "duration", "refined_sample_size": "sample size"}

# Does the abstract even contain something that LOOKS like the field? If not, the
# information isn't there and no extractor could find it -- only full text can.
_HAS = {
    "refined_dose": lambda t: bool(_DOSE_RE.search(t)),
    "refined_route": lambda t: bool(re.search(
        r"\b(oral|subcutaneous|intravenous|intraperitoneal|intramuscular|inhal|topical|gavage)", t, re.I)),
    "refined_duration": lambda t: bool(re.search(r"\b\d+\s*(?:week|day|month|year|hour)", t, re.I)),
    "refined_sample_size": lambda t: bool(re.search(
        r"\bn\s*=\s*\d|\b\d+\s+(?:patients?|participants?|subjects?|mice|rats?|animals?)", t, re.I)),
}


def _rows(db: str) -> List[dict]:
    conn = sqlite3.connect(db)
    papers: Dict[str, dict] = {}
    try:
        for (p,) in conn.execute("select payload_json from papers"):
            try:
                r = json.loads(p)
            except (TypeError, json.JSONDecodeError):
                continue
            if r.get("pmid"):
                papers[str(r["pmid"])] = r
    except sqlite3.OperationalError:
        pass
    out = []
    try:
        cur = conn.execute("select payload_json from evidence")
    except sqlite3.OperationalError:
        conn.close()
        return out
    for (p,) in cur:
        try:
            e = json.loads(p)
        except (TypeError, json.JSONDecodeError):
            continue
        pmid = str(e.get("pmid", ""))
        pap = papers.get(pmid, {})
        abstract = str(e.get("abstract", "") or pap.get("abstract", "") or "")
        if not abstract:
            continue
        out.append({"pmid": pmid, "molecule": str(e.get("molecule_name", "") or ""),
                    "evidence": e, "paper": pap, "abstract": abstract})
    conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--sample", type=int, default=0, help="audit only the first N records (0 = all)")
    ap.add_argument("--check-oa", action="store_true", help="ask PMC which gap papers are open access")
    ap.add_argument("--limit-oa", type=int, default=2000)
    ap.add_argument("--cache-dir", default=".cache/context")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No corpus at {args.db}", file=sys.stderr)
        sys.exit(1)

    rows = _rows(args.db)
    if args.sample:
        rows = rows[:args.sample]
    print(f"Auditing {len(rows)} evidence records with abstracts.\n")

    missing = Counter()          # field -> count missing
    absent_in_abstract = Counter()   # missing AND the abstract shows no such value
    rule_gap = Counter()         # missing BUT the abstract does look like it has one
    by_molecule = Counter()      # molecule -> missing dose (the field that matters most)
    gap_pmids: List[str] = []

    for r in rows:
        ref = refine_extraction(r["evidence"], r["paper"])
        text = (str(r["paper"].get("title", "") or "") + " " + r["abstract"])
        any_gap = False
        for f in FIELDS:
            if str(ref.get(f, "")).strip():
                continue
            missing[f] += 1
            if _HAS[f](text):
                rule_gap[f] += 1      # stated in the abstract but not extracted
                any_gap = True
            else:
                absent_in_abstract[f] += 1  # not in the abstract at all -> needs full text
            if f == "refined_dose":
                by_molecule[r["molecule"]] += 1
        if any_gap or not str(ref.get("refined_dose", "")).strip():
            gap_pmids.append(r["pmid"])

    n = max(len(rows), 1)
    print(f"{'field':<14}{'missing':>10}{'%':>7}   {'not in abstract':>17}{'looks extractable':>20}")
    for f in FIELDS:
        print(f"{LABEL[f]:<14}{missing[f]:>10}{100*missing[f]//n:>6}%   "
              f"{absent_in_abstract[f]:>17}{rule_gap[f]:>20}")
    print("\n'not in abstract'   = the value is not stated -> ONLY full text can recover it.")
    print("'looks extractable' = the abstract appears to contain it -> a RULE GAP worth fixing.")

    print("\nTop 15 molecules by missing dose:")
    for mol, c in by_molecule.most_common(15):
        print(f"  {c:>6}  {mol}")

    if args.check_oa and gap_pmids:
        from retarats_pipeline.enrichment import context as ctxmod
        sample = gap_pmids[:args.limit_oa]
        print(f"\nChecking open-access availability for {len(sample)} gap papers...")
        oa = ctxmod.pmc_ids_bulk(sample, args.cache_dir)
        print(f"  {len(oa)}/{len(sample)} ({100*len(oa)//max(len(sample),1)}%) have OA full text "
              f"-> recoverable by feeding full text to the rules.")


if __name__ == "__main__":
    main()
