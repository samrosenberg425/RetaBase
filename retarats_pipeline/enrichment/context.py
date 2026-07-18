"""Richer extraction CONTEXT for a paper: full text, entity annotations, trial facts.

Abstracts routinely omit the exact dose, per-arm N, and duration -- those live in the
Methods. So the biggest quality lever for any extractor (rules OR model) is feeding it
better input. This module assembles three additional sources for a PMID:

* ``europepmc_fulltext``  -- open-access full text (Methods/Results) from Europe PMC.
* ``pubtator_chemicals``  -- curated chemical/disease/species entity spans from NCBI
  PubTator3, used to decide whether a sentence really is about THIS molecule
  (handles synonyms, abbreviations and brand names that string matching misses).
* ``trial_context``       -- authoritative structured enrollment / interventions /
  phase from the LOCAL ClinicalTrials.gov mirror, for papers already linked to an
  NCT via ``result_pmids``. No network, highest precision.

Everything degrades gracefully: any network/parse failure returns empty rather than
raising, so callers can always run. Responses are cached on disk so repeat runs and
interrupted jobs don't re-fetch.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional

EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PUBTATOR_URL = ("https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/"
                "export/biocjson?pmids={pmid}")
_METHODS_RE = re.compile(r"method|material|procedure|design|participant|intervention", re.I)
_RESULTS_RE = re.compile(r"result|finding|outcome", re.I)


# ------------------------------- disk cache ---------------------------------
def _cache_path(cache_dir: str, kind: str, pmid: str) -> str:
    d = os.path.join(cache_dir, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{pmid}.json")


def _cache_get(cache_dir: Optional[str], kind: str, pmid: str):
    if not cache_dir:
        return None
    p = _cache_path(cache_dir, kind, pmid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_put(cache_dir: Optional[str], kind: str, pmid: str, value) -> None:
    if not cache_dir:
        return
    try:
        with open(_cache_path(cache_dir, kind, pmid), "w", encoding="utf-8") as fh:
            json.dump(value, fh)
    except OSError:
        pass


def _get(url: str, timeout: int = 30, accept: str = "application/json"):
    """GET with a polite delay. Returns the requests response or None on failure."""
    try:
        import requests
    except ImportError:
        return None
    try:
        time.sleep(0.34)  # be polite to EBI/NCBI (~3 req/s)
        r = requests.get(url, timeout=timeout, headers={"Accept": accept,
                                                        "User-Agent": "RetaBase/1.0 (research)"})
        return r if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 -- context is optional; never break the caller
        return None


# --------------------------- Europe PMC full text ---------------------------
def _pmcid_for(pmid: str) -> Optional[str]:
    r = _get(f"{EPMC_BASE}/search?query=EXT_ID:{pmid}%20AND%20SRC:MED&resultType=core&format=json")
    if r is None:
        return None
    try:
        results = (r.json().get("resultList") or {}).get("result") or []
    except ValueError:
        return None
    for item in results:
        pmcid = item.get("pmcid")
        # Only OA full text is fetchable.
        if pmcid and str(item.get("isOpenAccess", "")).upper() in {"Y", "YES", "TRUE"}:
            return pmcid
    return None


def _sections_from_jats(xml_text: str) -> Dict[str, str]:
    """Return {"methods": ..., "results": ...} plain text from a JATS full-text XML."""
    import xml.etree.ElementTree as ET
    out = {"methods": [], "results": []}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {"methods": "", "results": ""}
    for sec in root.iter("sec"):
        title_el = sec.find("title")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        bucket = None
        if _METHODS_RE.search(title):
            bucket = "methods"
        elif _RESULTS_RE.search(title):
            bucket = "results"
        if not bucket:
            continue
        text = " ".join(" ".join(p.itertext()).strip() for p in sec.iter("p"))
        if text:
            out[bucket].append(text)
    return {k: re.sub(r"\s+", " ", " ".join(v)).strip() for k, v in out.items()}


# ------------------- PMC ID Converter (bulk PMID -> PMCID) -------------------
IDCONV_URL = ("https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
              "?ids={ids}&format=json")


def pmc_ids_bulk(pmids: List[str], cache_dir: Optional[str] = None) -> Dict[str, str]:
    """Map PMID -> PMCID for up to 200 PMIDs per request.

    Far cheaper than resolving one at a time via a per-paper search: 100 papers
    becomes ONE request instead of 100.
    """
    out: Dict[str, str] = {}
    todo: List[str] = []
    for p in pmids:
        cached = _cache_get(cache_dir, "idconv", str(p))
        if cached is not None:
            if cached:
                out[str(p)] = cached
        else:
            todo.append(str(p))
    for i in range(0, len(todo), 200):
        chunk = todo[i:i + 200]
        r = _get(IDCONV_URL.format(ids=",".join(chunk)))
        found = {}
        if r is not None:
            try:
                for rec in (r.json().get("records") or []):
                    pmid, pmcid = str(rec.get("pmid", "")), str(rec.get("pmcid", "") or "")
                    if pmid and pmcid:
                        found[pmid] = pmcid
            except ValueError:
                pass
        for p in chunk:  # cache misses too, so we don't retry them every run
            _cache_put(cache_dir, "idconv", p, found.get(p, ""))
            if found.get(p):
                out[p] = found[p]
    return out


# ---------------------- PMC BioC (structured full text) ----------------------
BIOC_URL = ("https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/"
            "BioC_json/{pmcid}/unicode")


def bioc_fulltext(pmcid: str, cache_dir: Optional[str] = None) -> Dict[str, str]:
    """Methods/Results text for an OA article via PMC's BioC API.

    BioC returns passages already LABELLED with their section type, so we can pick
    the Methods without guessing from section titles the way JATS parsing requires.
    """
    cached = _cache_get(cache_dir, "bioc", pmcid)
    if cached is not None:
        return cached
    out = {"methods": "", "results": ""}
    r = _get(BIOC_URL.format(pmcid=pmcid))
    if r is not None:
        try:
            payload = r.json()
        except ValueError:
            payload = None
        docs = []
        if isinstance(payload, list):
            for entry in payload:
                docs.extend((entry or {}).get("documents", []) or [])
        elif isinstance(payload, dict):
            docs = payload.get("documents", []) or []
        buckets = {"methods": [], "results": []}
        for doc in docs:
            for passage in (doc or {}).get("passages", []) or []:
                infons = passage.get("infons") or {}
                sec = str(infons.get("section_type") or infons.get("section") or "").lower()
                typ = str(infons.get("type") or "").lower()
                if typ in {"title", "ref", "front"}:
                    continue
                text = str(passage.get("text", "") or "").strip()
                if not text:
                    continue
                if "method" in sec:
                    buckets["methods"].append(text)
                elif "result" in sec:
                    buckets["results"].append(text)
        out = {k: re.sub(r"\s+", " ", " ".join(v)).strip() for k, v in buckets.items()}
    _cache_put(cache_dir, "bioc", pmcid, out)
    return out


def europepmc_fulltext(pmid: str, cache_dir: Optional[str] = None) -> Dict[str, str]:
    """{"methods": str, "results": str, "pmcid": str} -- empty strings when not OA."""
    cached = _cache_get(cache_dir, "epmc", pmid)
    if cached is not None:
        return cached
    empty = {"methods": "", "results": "", "pmcid": ""}
    pmcid = _pmcid_for(pmid)
    if not pmcid:
        _cache_put(cache_dir, "epmc", pmid, empty)
        return empty
    r = _get(f"{EPMC_BASE}/{pmcid}/fullTextXML", accept="application/xml")
    if r is None:
        _cache_put(cache_dir, "epmc", pmid, empty)
        return empty
    sec = _sections_from_jats(r.text)
    out = {"methods": sec.get("methods", ""), "results": sec.get("results", ""), "pmcid": pmcid}
    _cache_put(cache_dir, "epmc", pmid, out)
    return out


# ------------------------------ PubTator3 -----------------------------------
def pubtator_chemicals(pmid: str, cache_dir: Optional[str] = None) -> List[dict]:
    """Chemical/drug mentions PubTator annotated, as ``{"text", "id"}`` pairs.

    The normalized ``id`` (e.g. ``MESH:D000068877``) is what makes this worth
    fetching: every surface form of the SAME drug shares an id, so a brand name
    ("Mounjaro") or development code ("LY3298176") can be recognised as the same
    molecule, while a different drug in the same paper keeps a different id.
    """
    cached = _cache_get(cache_dir, "pubtator", pmid)
    if cached is not None:
        return cached
    out: List[dict] = []
    r = _get(PUBTATOR_URL.format(pmid=pmid))
    if r is not None:
        try:
            payload = r.json()
        except ValueError:
            # PubTator can return newline-delimited JSON documents.
            payload = None
            for line in (r.text or "").splitlines():
                line = line.strip()
                if line:
                    try:
                        payload = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
        docs = []
        if isinstance(payload, dict):
            docs = payload.get("documents") or [payload]
        elif isinstance(payload, list):
            docs = payload
        seen = set()
        for doc in docs or []:
            for passage in (doc or {}).get("passages", []) or []:
                for ann in passage.get("annotations", []) or []:
                    infons = ann.get("infons") or {}
                    if str(infons.get("type", "")).lower() in {"chemical", "drug"}:
                        t = str(ann.get("text", "")).strip()
                        ident = str(infons.get("identifier") or infons.get("Identifier") or "").strip()
                        k = (t.lower(), ident)
                        if t and k not in seen:
                            seen.add(k)
                            out.append({"text": t, "id": ident})
    _cache_put(cache_dir, "pubtator", pmid, out)
    return out


# --------------------- ClinicalTrials.gov (local, no network) ----------------
def _index_trial_rows(rows, index: Dict[str, dict]) -> Dict[str, dict]:
    for t in rows:
        if not isinstance(t, dict):
            continue
        facts = {
            "nct_id": t.get("nct_id", ""),
            "enrollment_count": t.get("enrollment_count", ""),
            "interventions": t.get("interventions", ""),
            "phases": t.get("phases", ""),
            "study_type": t.get("study_type", ""),
        }
        for key in ("result_pmids", "reference_pmids"):
            for pmid in re.findall(r"\d+", str(t.get(key, "") or "")):
                index.setdefault(pmid, facts)
    return index


def load_trial_index(trials_db: str) -> Dict[str, dict]:
    """Map PMID -> structured trial facts from the local trials mirror.

    Accepts EITHER the trials SQLite mirror or a ``trials_data.json`` feed (the
    published site already serves that file, so you don't need the Actions cache to
    get authoritative CT.gov enrollment locally).

    Uses the ``result_pmids`` / ``reference_pmids`` links already captured from
    CT.gov, so a paper reporting a registered trial inherits authoritative
    enrollment / interventions / phase with no parsing and no model.
    """
    index: Dict[str, dict] = {}
    if not trials_db or not os.path.exists(trials_db):
        return index
    if trials_db.lower().endswith(".json"):
        try:
            with open(trials_db, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return index
        rows = payload.get("trials", payload) if isinstance(payload, dict) else payload
        return _index_trial_rows(rows or [], index)
    conn = sqlite3.connect(trials_db)
    try:
        cur = conn.execute("select payload_json from trials")
    except sqlite3.OperationalError:
        conn.close()
        return index
    for (payload,) in cur:
        try:
            t = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        facts = {
            "nct_id": t.get("nct_id", ""),
            "enrollment_count": t.get("enrollment_count", ""),
            "interventions": t.get("interventions", ""),
            "phases": t.get("phases", ""),
            "study_type": t.get("study_type", ""),
        }
        for key in ("result_pmids", "reference_pmids"):
            for pmid in re.findall(r"\d+", str(t.get(key, "") or "")):
                index.setdefault(pmid, facts)
    conn.close()
    return index


def build_context(pmid: str, molecule: str, abstract: str, *, trials_index=None,
                  use_fulltext: bool = False, use_pubtator: bool = False,
                  cache_dir: Optional[str] = None, pmcid_map: Optional[Dict[str, str]] = None) -> dict:
    """Assemble everything available for one paper. Missing pieces are just empty.

    ``pmcid_map`` comes from :func:`pmc_ids_bulk` (one request for up to 200 PMIDs);
    when supplied we go straight to PMC BioC, whose passages are already labelled by
    section. Europe PMC remains the fallback when the paper isn't in that map.
    """
    ctx = {"pmid": pmid, "molecule": molecule, "abstract": abstract,
           "methods": "", "results": "", "pmcid": "", "chemicals": [], "trial": {}}
    if use_fulltext:
        pmcid = (pmcid_map or {}).get(str(pmid), "")
        ft = {}
        if pmcid:
            bio = bioc_fulltext(pmcid, cache_dir)
            if bio.get("methods") or bio.get("results"):
                ft = {"methods": bio.get("methods", ""), "results": bio.get("results", ""),
                      "pmcid": pmcid}
        if not ft:  # not in the OA map, or BioC had nothing -> Europe PMC
            ft = europepmc_fulltext(pmid, cache_dir)
        ctx.update({"methods": ft.get("methods", ""), "results": ft.get("results", ""),
                    "pmcid": ft.get("pmcid", "") or pmcid})
    if use_pubtator:
        ctx["chemicals"] = pubtator_chemicals(pmid, cache_dir)
    if trials_index:
        ctx["trial"] = trials_index.get(str(pmid), {}) or {}
    return ctx


def context_text(ctx: dict, max_chars: int = 6000) -> str:
    """The best available source text: Methods+Results when OA, else the abstract."""
    parts = []
    if ctx.get("methods"):
        parts.append("METHODS: " + ctx["methods"])
    if ctx.get("results"):
        parts.append("RESULTS: " + ctx["results"])
    if not parts:
        return str(ctx.get("abstract", ""))[:max_chars]
    return (str(ctx.get("abstract", "")) + " " + " ".join(parts))[:max_chars]


def molecule_aliases(ctx: dict) -> List[str]:
    """Molecule name + every PubTator surface form of the SAME entity id.

    Two-step so brand names and codes are captured without pulling in other drugs:
    1. find the entity id(s) whose mention text matches this molecule by name;
    2. return every mention sharing those ids (e.g. tirzepatide -> Mounjaro,
       LY3298176) while a different drug (semaglutide) keeps a different id.
    """
    mol = str(ctx.get("molecule", "") or "").strip()
    out = [mol] if mol else []
    low = mol.lower()
    chems = ctx.get("chemicals", []) or []
    # Tolerate the older plain-string cache format.
    norm = [c if isinstance(c, dict) else {"text": str(c), "id": ""} for c in chems]
    ids = {c["id"] for c in norm
           if c.get("id") and (c["text"].lower() in low or low in c["text"].lower())}
    for c in norm:
        t = c.get("text", "")
        same_entity = c.get("id") and c["id"] in ids
        name_match = t.lower() in low or low in t.lower()
        if t and (same_entity or name_match) and t not in out:
            out.append(t)
    return out
