#!/usr/bin/env python3
"""Resolve each molecule to the RIGHT PubChem CID and harvest high-quality search
terms (PubChem synonyms + NCBI MeSH entry terms), keeping only terms that actually
retrieve papers in PubMed.

Why this is smarter than a bare name->CID lookup:
  * pulls SEVERAL candidate CIDs and picks the one whose synonyms actually contain
    the molecule name (avoids grabbing a wrong record for biologics/ambiguous names
    -- e.g. a sotatercept CID that has no usable PubMed-facing synonyms);
  * adds authoritative NIH MeSH entry terms for the molecule;
  * VALIDATES every candidate term against PubMed (`"term"[tiab]` count) and keeps
    only those with hits, recording the count -- so the suggestions are guaranteed
    to be real, usable search terms, not dead ends;
  * records a confidence flag so you don't wire up a link/terms for a molecule that
    couldn't be confidently matched.

NON-destructive: writes two NEW files, never edits MOLECULES.csv/SEARCH_RULES.csv.
  config/pubchem_cids.csv               molecule_id, display_name, pubchem_cid, pubchem_name, mesh_terms, confidence
  config/pubchem_synonyms_suggested.csv molecule_id, pubchem_cid, term, source, pubmed_count   (validated, hit-count desc)

NETWORK REQUIRED (pubchem.ncbi.nlm.nih.gov + eutils.ncbi.nlm.nih.gov). Set
NCBI_API_KEY / NCBI_EMAIL in .env for the higher PubMed rate limit.

    source .venv/bin/activate
    python3 scripts/enrich_pubchem.py
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from typing import List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.pubchem import pubchem_cids, pubchem_synonyms  # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _load(path: str):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _name(row: dict) -> str:
    for k in ("display_name", "name", "molecule_name"):
        if row.get(k):
            return str(row[k]).strip()
    return str(row.get("molecule_id", "") or "").replace("_", " ").strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _useful_synonym(s: str) -> bool:
    """Keep human-readable chemical/brand names; drop registry ids and codes."""
    s = s.strip()
    if len(s) < 3 or len(s) > 60:
        return False
    if s.replace("-", "").replace(",", "").replace(".", "").isdigit():
        return False
    low = s.lower()
    if low.startswith(("cid ", "schembl", "chembl", "dtxsid", "unii", "ec ", "mfcd", "akos", "ncgc")):
        return False
    if sum(ch.isdigit() for ch in s) >= 5 and any(ch.isalpha() for ch in s):
        return False
    return True


class _Eutils:
    """Tiny polite eutils client for MeSH lookup + PubMed term validation."""

    def __init__(self, sleep: float):
        self.api_key = os.getenv("NCBI_API_KEY", "").strip()
        self.email = os.getenv("NCBI_EMAIL", os.getenv("API_CONTACT_EMAIL", "")).strip()
        self.sleep = sleep
        self.s = requests.Session()

    def _get(self, endpoint: str, params: dict) -> Optional[dict]:
        params = dict(params)
        params.update({"retmode": "json", "tool": "retarats_pubchem_enrich"})
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        for attempt in range(1, 5):
            try:
                r = self.s.get(f"{EUTILS}/{endpoint}", params=params, timeout=30)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(str(r.status_code))
                r.raise_for_status()
                time.sleep(self.sleep)
                return r.json()
            except (requests.RequestException, ValueError):
                if attempt == 4:
                    return None
                time.sleep(min(20, 2 ** attempt))
        return None

    def mesh_terms(self, name: str) -> List[str]:
        d = self._get("esearch.fcgi", {"db": "mesh", "term": name, "retmax": 3})
        ids = (((d or {}).get("esearchresult") or {}).get("idlist")) or []
        if not ids:
            return []
        d2 = self._get("esummary.fcgi", {"db": "mesh", "id": ",".join(ids)})
        res = ((d2 or {}).get("result")) or {}
        terms: List[str] = []
        for uid in ids:
            for t in (res.get(uid, {}) or {}).get("ds_meshterms", []) or []:
                if t and t not in terms:
                    terms.append(t)
        return terms

    def pubmed_count(self, term: str) -> int:
        d = self._get("esearch.fcgi", {"db": "pubmed", "term": f'"{term}"[tiab]', "retmax": 0})
        try:
            return int((((d or {}).get("esearchresult") or {}).get("count")) or 0)
        except (TypeError, ValueError):
            return 0


def _best_cid(name: str, cid_sleep: float):
    """Pick the candidate CID whose synonyms actually contain the molecule name;
    fall back to the first candidate. Returns (cid, synonyms) or (None, [])."""
    target = _norm(name)
    candidates = pubchem_cids(name, limit=6)
    time.sleep(cid_sleep)
    first = None
    for cid in candidates:
        syns = pubchem_synonyms(cid)
        time.sleep(cid_sleep)
        if first is None:
            first = (cid, syns)
        if any(_norm(s) == target for s in syns):     # exact name match -> the right record
            return cid, syns
    return first if first else (None, [])


def _write(path: str, rows, cols) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    ap = argparse.ArgumentParser(description="PubChem CID + MeSH + PubMed-validated synonym enrichment.")
    ap.add_argument("--molecules", default="config/MOLECULES.csv")
    ap.add_argument("--out-dir", default="config")
    ap.add_argument("--max-terms", type=int, default=30, help="Max candidate terms to validate per molecule.")
    ap.add_argument("--min-count", type=int, default=1, help="Keep terms with at least this many PubMed hits.")
    ap.add_argument("--pubchem-sleep", type=float, default=0.25)
    args = ap.parse_args()

    eu = _Eutils(sleep=(0.11 if os.getenv("NCBI_API_KEY") else 0.34))
    rows = _load(args.molecules)
    cids_out, syn_out = [], []

    for row in rows:
        mid = str(row.get("molecule_id", "") or "").strip()
        if not mid:
            continue
        name = _name(row)
        cid, syns = _best_cid(name, args.pubchem_sleep)
        mesh = eu.mesh_terms(name)

        # Candidate terms: the name itself, PubChem synonyms, and MeSH entry terms.
        seen, candidates = set(), []
        for term, source in ([(name, "name")]
                             + [(s, "synonym") for s in syns if _useful_synonym(s)]
                             + [(m, "mesh") for m in mesh if _useful_synonym(m)]):
            key = _norm(term)
            if key and key not in seen:
                seen.add(key)
                candidates.append((term, source))
        candidates = candidates[: args.max_terms]

        # Validate each against PubMed; keep only terms that actually retrieve papers.
        validated = []
        name_hits = 0
        for term, source in candidates:
            n = eu.pubmed_count(term)
            if term.lower() == name.lower():
                name_hits = n
            if n >= args.min_count:
                validated.append({"molecule_id": mid, "pubchem_cid": cid or "",
                                  "term": term, "source": source, "pubmed_count": n})
        validated.sort(key=lambda r: r["pubmed_count"], reverse=True)
        syn_out.extend(validated)

        # Confidence: did the molecule name itself retrieve PubMed papers, and did we
        # land a CID whose synonyms include the name?
        cid_named = bool(cid and any(_norm(s) == _norm(name) for s in syns))
        confidence = "high" if (name_hits >= args.min_count and cid_named) else (
            "medium" if validated else "none")
        cids_out.append({
            "molecule_id": mid, "display_name": name,
            "pubchem_cid": cid or "", "pubchem_name": (syns[0] if syns else ""),
            "mesh_terms": "; ".join(mesh[:6]), "confidence": confidence,
        })
        print(f"  {mid}: CID {cid or 'NONE'}  conf={confidence}  "
              f"{len(validated)} validated terms  {len(mesh)} MeSH", flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    _write(os.path.join(args.out_dir, "pubchem_cids.csv"), cids_out,
           ["molecule_id", "display_name", "pubchem_cid", "pubchem_name", "mesh_terms", "confidence"])
    _write(os.path.join(args.out_dir, "pubchem_synonyms_suggested.csv"), syn_out,
           ["molecule_id", "pubchem_cid", "term", "source", "pubmed_count"])
    hi = sum(1 for r in cids_out if r["confidence"] == "high")
    print(f"\n{len(cids_out)} molecules processed ({hi} high-confidence); "
          f"{len(syn_out)} PubMed-validated terms written for review.")


if __name__ == "__main__":
    main()
