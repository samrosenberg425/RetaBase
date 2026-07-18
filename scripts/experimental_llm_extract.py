#!/usr/bin/env python3
"""EXPERIMENTAL, OPT-IN: compare rules-based extraction vs a local LLM (e.g. Llama).

This tool is deliberately OUTSIDE the build pipeline. It NEVER runs during a site
build, NEVER writes to the corpus, and NEVER changes what the site publishes. Its
only job is to let you *see how a free/local LLM performs* at the same structured
extraction the rules-based curator does -- so you can decide whether an LLM layer
is worth adding later, with eyes open about its uncertainty/bias.

For each sampled paper it asks the LLM to return the same fields the rules-based
extractor produces (dose / route / duration / sample size / outcome direction)
plus a one-sentence plain-language summary, then writes a SIDE-BY-SIDE HTML report
(rules vs LLM, disagreements flagged) you can skim.

Backends
--------
* Ollama (default): a local model server. Start one first, e.g. `ollama run llama3.1`,
  then run this. No API key, no data leaves your machine.
      python3 scripts/experimental_llm_extract.py --db data/retarats_pubmed.sqlite \
          --limit 25 --out exports/llm_compare.html
* OpenAI-compatible endpoint:
      python3 scripts/experimental_llm_extract.py --api openai \
          --base-url https://api.example.com/v1 --model some-model --limit 25
  (API key read from env LLM_API_KEY.)

Offline plumbing check: `--mock` returns a canned response so the sampling,
comparison, and report generation can be exercised without any model or network.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from retarats_pipeline.curation.extractors import refine_extraction  # noqa: E402
from retarats_pipeline.enrichment import context as ctxmod  # noqa: E402

FIELDS = ["dose", "route", "duration", "sample_size", "outcome_direction"]


# ----------------------------- corpus sampling ------------------------------
def _load_payloads(conn: sqlite3.Connection, table: str) -> List[dict]:
    try:
        cur = conn.execute(f"select payload_json from {table}")
    except sqlite3.OperationalError:
        return []
    out = []
    for (p,) in cur:
        try:
            out.append(json.loads(p))
        except (TypeError, json.JSONDecodeError):
            continue
    return out


def sample_papers(db: str, limit: int, seed: int = 0, prefer_human: bool = True) -> List[dict]:
    """Return up to ``limit`` papers with title+abstract+molecule for comparison.

    By default biases toward human/clinical papers with substantive abstracts, so
    the comparison lands on records that actually have dose/route/duration/N to
    extract (random sampling otherwise hits off-topic, low-content papers where
    both columns are empty and the comparison is uninformative).
    """
    conn = sqlite3.connect(db)
    papers = {str(p.get("pmid", "")): p for p in _load_payloads(conn, "papers")}
    evidence = _load_payloads(conn, "evidence")
    conn.close()
    rows: List[dict] = []
    seen = set()
    for e in evidence:
        pmid = str(e.get("pmid", ""))
        if not pmid or pmid in seen:
            continue
        pap = papers.get(pmid, {})
        abstract = str(e.get("abstract", "") or pap.get("abstract", "") or "")
        if len(abstract) < 200:
            continue  # too little to extract from
        seen.add(pmid)
        rows.append({
            "pmid": pmid,
            "molecule_name": str(e.get("molecule_name", "") or ""),
            "title": str(e.get("title", "") or pap.get("title", "") or ""),
            "abstract": abstract,
            "_evidence": e,
            "_paper": pap,
        })
    rng = random.Random(seed)
    rng.shuffle(rows)
    if prefer_human:
        def _score(r):
            e = r["_evidence"]
            s = 0.0
            if str(e.get("website_section", "")) == "Human evidence":
                s += 3
            if str(e.get("model_type", "")) == "human":
                s += 2
            if any(u in r["abstract"].lower() for u in (" mg", "µg", " mcg", "weeks", "n=", "randomi")):
                s += 1  # has dose/duration/N-like content to compare on
            if len(r["abstract"]) > 700:
                s += 0.5
            return s
        rows.sort(key=_score, reverse=True)  # stable after shuffle -> random within a tier
    return rows[:limit]


# ------------------------------- LLM backend --------------------------------
_PROMPT = (
    "You are extracting structured facts from a biomedical abstract about the drug/"
    "compound \"{mol}\". Return ONLY a JSON object with these keys: dose, route, "
    "duration, sample_size, outcome_direction (one of beneficial/harmful/neutral/"
    "unclear), one_sentence_summary. Use \"\" for anything not stated. Attribute "
    "dose/route to \"{mol}\" specifically, not to any comparator drug.\n\n"
    "TITLE: {title}\nABSTRACT: {abstract}\n\nJSON:"
)


# A review / meta-analysis reports a DIFFERENT kind of data than a primary study:
# there is no single administered dose or cohort, there is a set of included studies.
# Asking for "the dose" of a meta-analysis invites a misleading answer, so synthesis
# papers get their own prompt and their own adoption rules.
_PROMPT_SYNTHESIS = (
    "You are extracting structured facts from a SYSTEMATIC REVIEW or META-ANALYSIS "
    "about \"{mol}\". This is NOT a single study: do not report one dose or one cohort. "
    "Return ONLY a JSON object with these keys: included_studies (number of studies/"
    "trials included, digits only), pooled_sample_size (total participants across "
    "included studies, digits only), dose (the RANGE of {mol} doses across included "
    "studies, e.g. \"5-15 mg\", or \"\" if not stated), route, duration (range across "
    "studies), outcome_direction (one of beneficial/harmful/neutral/unclear), "
    "one_sentence_summary. Use \"\" for anything not stated. Copy values verbatim from "
    "the text; do not infer.\n\n"
    "TITLE: {title}\nABSTRACT: {abstract}\n\nJSON:"
)

_SYNTH_RE = re.compile(r"meta-?analys|systematic review|pooled analysis|scoping review|"
                       r"narrative review|umbrella review", re.I)


def is_synthesis(paper: dict) -> bool:
    """True when the record is a review / meta-analysis rather than a primary study."""
    ev = paper.get("_evidence", {}) or {}
    if str(ev.get("evidence_class", "")).strip() == "evidence_synthesis":
        return True
    if str(ev.get("model_type", "")).strip() == "review":
        return True
    if str(ev.get("model_primary", "")).strip() == "review":
        return True
    blob = " ".join([str(ev.get("primary_study_type", "") or ""), str(paper.get("title", "") or "")])
    return bool(_SYNTH_RE.search(blob))


def build_prompt(paper: dict) -> str:
    tmpl = _PROMPT_SYNTHESIS if is_synthesis(paper) else _PROMPT
    return tmpl.format(mol=paper["molecule_name"] or "the compound",
                       title=paper["title"][:400], abstract=paper["abstract"][:3500])


# --- batching -------------------------------------------------------------
# Free tiers often cap REQUESTS PER DAY hard (Gemini: 20/day) while allowing a huge
# token throughput (250K/min). One paper per request wastes almost all of that
# budget, so we pack many papers into a single request and ask for one JSON object
# per paper. 20 requests x 25 papers = 500 papers/day instead of 20.
_BATCH_HEADER = (
    "You are extracting structured facts from {n} biomedical {kind}. For EACH item "
    "return one JSON object. Respond with ONLY a JSON object of the form "
    "{{\"results\": [ ... ]}} where each element has keys: pmid, {fields}, "
    "one_sentence_summary. Use \"\" for anything not stated. Copy values verbatim "
    "from the text; do not infer. Attribute dose/route to that item's OWN molecule, "
    "never to a comparator drug.\n\n"
)
_BATCH_FIELDS_PRIMARY = "dose, route, duration, sample_size, outcome_direction"
_BATCH_FIELDS_SYNTH = ("included_studies, pooled_sample_size, dose (RANGE across included "
                       "studies), route, duration, outcome_direction")


def build_batch_prompt(papers: List[dict], synthesis: bool, per_paper_chars: int = 4000) -> str:
    head = _BATCH_HEADER.format(
        n=len(papers),
        kind="systematic reviews / meta-analyses (NOT single studies: report the "
             "number of included studies and pooled totals, and dose as a RANGE)"
             if synthesis else "studies",
        fields=_BATCH_FIELDS_SYNTH if synthesis else _BATCH_FIELDS_PRIMARY)
    blocks = []
    for p in papers:
        blocks.append(
            f"---\nPMID: {p['pmid']}\nMOLECULE: {p.get('molecule_name') or 'the compound'}\n"
            f"TITLE: {str(p.get('title',''))[:300]}\n"
            f"TEXT: {str(p.get('abstract',''))[:per_paper_chars]}\n")
    return head + "\n".join(blocks) + "\nJSON:"


def parse_batch(raw: str, papers: List[dict]) -> Dict[str, Dict[str, str]]:
    """Map pmid -> field dict from a batched response (falls back to order)."""
    out: Dict[str, Dict[str, str]] = {}
    obj = parse_llm_json(raw) or {}
    items = obj.get("results")
    if not isinstance(items, list):
        # Some models return a bare array or a pmid-keyed object.
        m = re.search(r"\[.*\]", raw or "", re.DOTALL)
        if m:
            try:
                items = json.loads(m.group(0))
            except json.JSONDecodeError:
                items = None
        if not isinstance(items, list):
            items = [obj.get(str(p["pmid"])) for p in papers] if obj else []
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        pmid = str(item.get("pmid") or (papers[i]["pmid"] if i < len(papers) else "")).strip()
        if pmid:
            out[pmid] = {k: ("" if v is None else str(v)) for k, v in item.items()}
    return out


def call_llm(prompt: str, api: str, base_url: str, model: str,
             api_key: Optional[str], mock: bool, timeout: int = 120) -> str:
    if mock:
        return ('{"dose":"5 mg","route":"subcutaneous","duration":"24 weeks",'
                '"sample_size":"n=101","outcome_direction":"beneficial",'
                '"one_sentence_summary":"(mock) beneficial weight-loss result."}')
    import requests  # local import so --mock needs no dependency
    if api == "ollama":
        # format:"json" makes Ollama constrain output to valid JSON -- this is the
        # single biggest quality lift for small models (llama3.2:3b), which
        # otherwise leave the structured fields blank. num_ctx raised so long
        # abstracts aren't truncated by the default 2k window.
        r = requests.post(base_url.rstrip("/") + "/api/generate",
                          json={"model": model, "prompt": prompt, "stream": False,
                                "format": "json",
                                "options": {"temperature": 0, "num_ctx": 8192}},
                          timeout=timeout)
        r.raise_for_status()
        return r.json().get("response", "")
    # OpenAI-compatible endpoint. Works for OpenAI, Google Gemini (via its
    # /v1beta/openai compat layer), Groq, Mistral, DeepSeek, Together, vLLM, etc.
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}]}
    # Two things to survive on free tiers: (1) not every compatible endpoint accepts
    # response_format, so fall back to a plain request; (2) free tiers rate-limit
    # aggressively (Gemini ~10-15 RPM), so back off and retry on 429/5xx instead of
    # dropping the paper.
    retry_codes = {429, 500, 502, 503, 504}
    # Providers disagree on the model id form: the Gemini /models listing returns
    # "models/gemini-2.5-flash" while its chat endpoint usually wants the bare
    # "gemini-2.5-flash". A wrong one returns 404, so try both.
    model_ids = [model]
    if model.startswith("models/"):
        model_ids.append(model.split("/", 1)[1])
    else:
        model_ids.append("models/" + model)
    r = None
    for mid in model_ids:
        for variant in ({**body, "model": mid, "response_format": {"type": "json_object"}},
                        {**body, "model": mid}):
            for attempt in range(1, 6):
                r = requests.post(url, headers=headers, json=variant, timeout=timeout)
                if r.status_code in retry_codes:
                    wait = 0.0
                    try:
                        wait = float(r.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        wait = 0.0
                    time.sleep(wait or min(60.0, 5.0 * attempt))
                    continue
                break
            if r is not None and r.status_code < 400:
                return r.json()["choices"][0]["message"]["content"]
            # A 404 means this model id is wrong -> try the other form, not the
            # other body variant.
            if r is not None and r.status_code == 404:
                break
    if r is not None:
        detail = (r.text or "")[:400].replace("\n", " ")
        raise RuntimeError(f"LLM endpoint {r.status_code}: {detail}")
    raise RuntimeError("no response from LLM endpoint")


def preflight(api: str, base_url: str, model: str) -> Optional[str]:
    """Return an error string if the backend/model isn't ready, else None."""
    if api != "ollama":
        return None
    try:
        import requests
        r = requests.get(base_url.rstrip("/") + "/api/tags", timeout=10)
        r.raise_for_status()
        tags = [m.get("name", "") for m in (r.json().get("models") or [])]
    except Exception as e:  # noqa: BLE001
        return (f"Could not reach Ollama at {base_url}. Is it running? Start it with "
                f"`ollama serve` in another terminal.\n  ({e})")
    # Ollama tags look like "llama3.2:3b"; match exact or bare name.
    if model not in tags and not any(t.split(":")[0] == model for t in tags):
        have = ", ".join(tags) if tags else "(none pulled yet)"
        return (f"Model '{model}' is not pulled. Run:\n    ollama pull {model}\n"
                f"Currently available: {have}")
    return None


def parse_llm_json(text: str) -> Dict[str, str]:
    """Pull the first JSON object out of an LLM response, defensively."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {k: ("" if v is None else str(v)) for k, v in obj.items()} if isinstance(obj, dict) else {}


# ------------------------------- comparison ---------------------------------
def _norm(v) -> str:
    """Normalize a field value so cosmetic differences aren't counted as conflicts.

    "n=101" / "101" / "101 participants" all collapse to the same thing; without
    this, sample_size showed 0% agreement purely from formatting.
    """
    s = str(v or "").strip().lower()
    s = s.replace("n =", " ").replace("n=", " ")
    s = re.sub(r"\b(participants?|patients?|subjects?|volunteers?|individuals?|adults?)\b", " ", s)
    s = re.sub(r"[^\w.%/·-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Fold synonyms/abbreviations so "SC" vs "subcutaneous" or "12 wks" vs "12 weeks"
    # count as agreement (a difference in SYNTAX, not content).
    return " ".join(_SYNONYMS.get(t, t) for t in s.split(" "))


_SYNONYMS = {
    # route
    "sc": "subcutaneous", "s.c": "subcutaneous", "s.c.": "subcutaneous", "subq": "subcutaneous",
    "subcutaneously": "subcutaneous", "subcut": "subcutaneous",
    "po": "oral", "p.o": "oral", "orally": "oral", "gavage": "oral",
    "iv": "intravenous", "i.v": "intravenous", "intravenously": "intravenous",
    "ip": "intraperitoneal", "i.p": "intraperitoneal", "intraperitoneally": "intraperitoneal",
    "im": "intramuscular", "i.m": "intramuscular", "intramuscularly": "intramuscular",
    "inhaled": "inhalation", "topically": "topical",
    # duration
    "wk": "week", "wks": "week", "weeks": "week", "weekly": "week",
    "d": "day", "days": "day", "daily": "day",
    "mo": "month", "mos": "month", "months": "month",
    "yr": "year", "yrs": "year", "years": "year", "hr": "hour", "hrs": "hour", "hours": "hour",
    # dose units
    "mgs": "mg", "milligram": "mg", "milligrams": "mg",
    "mcg": "µg", "ug": "µg", "microgram": "µg", "micrograms": "µg",
    "grams": "g", "gram": "g", "kilogram": "kg", "kilograms": "kg",
}


# Fields an LLM value could ever be ADOPTED for. outcome_direction is deliberately
# excluded and stays rules-based: authors systematically frame their own findings
# positively, so a model reading that framing over-calls "beneficial". The rules
# engine reads negation/effect words, not the authors' enthusiasm.
ADOPTABLE_FIELDS = ["dose", "route", "duration", "sample_size"]
RULES_ONLY_FIELDS = ["outcome_direction"]


def _split_sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", str(text or "")) if s.strip()]


def ground_status(value: str, abstract: str, molecule) -> Optional[str]:
    """attributed | unattributed | ungrounded (None when there's no value).

    Groundedness alone is NOT enough: a comparator's dose, or a number belonging to a
    different endpoint, also appears in the abstract. So we locate the sentence that
    supports the value and require it to ALSO name this record's molecule. Only then
    is the value both real and about the right drug.
    """
    v = _norm(value)
    if not v:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", v)
    # molecule may be a single name or a list of aliases (PubTator synonyms/brands).
    names = molecule if isinstance(molecule, (list, tuple)) else [molecule]
    mols = [m for m in (_norm(x) for x in names) if m]
    hit = False
    for s in _split_sentences(abstract):
        ns = _norm(s)
        if v in ns or (nums and all(n in ns for n in nums)):
            hit = True
            if any(m in ns for m in mols):
                return "attributed"
    return "unattributed" if hit else "ungrounded"


def classify_field(rules_v: str, llm_v: str, abstract: str, molecule: str = "") -> str:
    """agree | differ | llm_only_attributed | llm_only_unattributed |
    llm_only_ungrounded | rules_only | both_empty."""
    r, l = _norm(rules_v), _norm(llm_v)
    if not r and not l:
        return "both_empty"
    if r and not l:
        return "rules_only"
    if l and not r:
        g = ground_status(llm_v, abstract, molecule)
        return {"attributed": "llm_only_attributed",
                "unattributed": "llm_only_unattributed"}.get(g, "llm_only_ungrounded")
    if r == l or r in l or l in r:
        return "agree"
    return "differ"



def compare_paper(paper: dict, api: str, base_url: str, model: str,
                  api_key: Optional[str], mock: bool, ctx: Optional[dict] = None,
                  llm_override: Optional[dict] = None) -> dict:
    rules = refine_extraction(paper["_evidence"], paper["_paper"])
    # When richer context is available (OA Methods text), the model reads THAT rather
    # than the abstract alone -- most "both missed it" cases are simply information
    # that never appears in the abstract.
    prompt_paper = dict(paper)
    if ctx:
        prompt_paper["abstract"] = ctxmod.context_text(ctx)
    if llm_override is not None:
        raw, llm = "(batched)", llm_override
    else:
        raw = call_llm(build_prompt(prompt_paper), api, base_url, model, api_key, mock)
        llm = parse_llm_json(raw)
    rules_map = {
        "dose": rules.get("refined_dose", ""),
        "route": rules.get("refined_route", ""),
        "duration": rules.get("refined_duration", ""),
        "sample_size": rules.get("refined_sample_size", ""),
        "outcome_direction": rules.get("refined_outcome_direction", ""),
    }
    # Ground against the RICHEST available text and the fullest alias set.
    src_text = ctxmod.context_text(ctx) if ctx else paper.get("abstract", "")
    mol_names = ctxmod.molecule_aliases(ctx) if ctx else [paper.get("molecule_name", "")]
    status = {f: classify_field(rules_map[f], llm.get(f, ""), src_text, mol_names) for f in FIELDS}
    disagree = [f for f in FIELDS
                if status[f] in ("differ", "llm_only_ungrounded", "llm_only_unattributed")]
    return {
        "pmid": paper["pmid"],
        "molecule_name": paper["molecule_name"],
        "title": paper["title"],
        "abstract": paper.get("abstract", "")[:280],
        "rules": rules_map,
        "llm": {f: llm.get(f, "") for f in FIELDS},
        "llm_summary": llm.get("one_sentence_summary", ""),
        "rules_scope": rules.get("refined_extraction_scope", ""),
        "status": status,
        "disagree": disagree,
        "raw": raw,
        # Authoritative cross-checks (no model involved): structured CT.gov enrollment
        # for trial-linked papers, and whether OA full text was actually used.
        "trial": (ctx or {}).get("trial", {}),
        "pmcid": (ctx or {}).get("pmcid", ""),
        "chemicals": (ctx or {}).get("chemicals", [])[:8],
        # Reviews/meta-analyses are judged on different fields (k studies + pooled N,
        # dose as a RANGE) and are tallied separately in the trust breakdown.
        "kind": "synthesis" if is_synthesis(paper) else "primary",
        "included_studies": llm.get("included_studies", ""),
        "pooled_sample_size": llm.get("pooled_sample_size", ""),
    }


def write_report(rows: List[dict], out: str, mock: bool = False, model: str = "") -> None:
    def esc(s):
        return html.escape(str(s or ""))
    n = len(rows)
    n_dis = sum(1 for r in rows if r["disagree"])
    STATUSES = ["agree", "differ", "llm_only_attributed", "llm_only_unattributed",
                "llm_only_ungrounded", "rules_only", "both_empty"]
    tally = {f: {s: sum(1 for r in rows if r.get("status", {}).get(f) == s) for s in STATUSES} for f in FIELDS}
    agree = {f: tally[f]["agree"] for f in FIELDS}
    parts = [
        "<!doctype html><meta charset=utf-8><title>LLM vs rules extraction</title>",
        "<style>body{font:14px system-ui;margin:24px;background:#0f1115;color:#e6e8ec}"
        "table{border-collapse:collapse;width:100%;margin:8px 0 28px}"
        "th,td{border:1px solid #2a2f3a;padding:6px 9px;text-align:left;vertical-align:top}"
        "th{color:#9aa3b2}.d{background:#3a1d1d}.h{color:#5b9dff}small{color:#9aa3b2}"
        ".sum{background:#171a21;border:1px solid #2a2f3a;border-radius:8px;padding:12px 16px;margin:12px 0}"
        ".warn{background:#3a2f10;border:1px solid #8a6d1a;border-radius:8px;padding:10px 14px;margin:12px 0}"
        ".ab{color:#9aa3b2;font-size:12px;margin:4px 0 8px}</style>",
        "<h1>Experimental: LLM vs rules-based extraction</h1>",
    ]
    if mock:
        parts.append("<div class=warn><b>MOCK MODE</b> &mdash; the LLM column is a canned "
                     "placeholder (no model was called). Run without <code>--mock</code> against "
                     "a real Ollama model to populate it.</div>")
    parts.append(f"<div class=sum><b>{n}</b> papers"
                 + (f" &middot; model <code>{esc(model)}</code>" if model and not mock else "")
                 + f" &middot; <b>{n_dis}</b> with at least one field disagreement."
                 "<br>Per-field agreement (rules == LLM): "
                 + " &middot; ".join(f"{esc(f)} <b>{(100*agree[f]//n) if n else 0}%</b>" for f in FIELDS)
                 + "<br><small>Research comparison only &mdash; the LLM output is NOT used "
                 "anywhere on the live site.</small></div>")
    # Trust breakdown: the number that actually matters is llm_only_GROUNDED (safe
    # gap-fill) vs llm_only_UNGROUNDED (likely hallucination) vs differ (conflict).
    prim = [r for r in rows if r.get("kind") != "synthesis"]
    synth = [r for r in rows if r.get("kind") == "synthesis"]
    parts.append(f"<p><small>{len(prim)} primary studies &middot; {len(synth)} reviews/"
                 "meta-analyses (tallied separately &mdash; a synthesis has no single dose "
                 "or cohort, so its dose/route/duration must not be adopted as study "
                 "facts).</small></p>")
    for label, subset in (("Primary studies", prim), ("Reviews / meta-analyses", synth)):
        if not subset:
            continue
        sub = {f: {s: sum(1 for r in subset if r.get("status", {}).get(f) == s) for s in STATUSES}
               for f in FIELDS}
        parts.append(f"<h2>Trust breakdown &mdash; {esc(label)} (n={len(subset)})</h2>"
                     "<table><tr><th>field</th>"
                     + "".join(f"<th>{esc(s)}</th>" for s in STATUSES) + "</tr>")
        for f in FIELDS:
            parts.append(f"<tr><td>{esc(f)}</td>"
                         + "".join(f"<td>{sub[f][s]}</td>" for s in STATUSES) + "</tr>")
        parts.append("</table>")
    parts.append("<h2>Trust breakdown &mdash; all papers</h2><table><tr><th>field</th>"
                 + "".join(f"<th>{esc(s)}</th>" for s in STATUSES) + "</tr>")
    for f in FIELDS:
        parts.append(f"<tr><td>{esc(f)}</td>"
                     + "".join(f"<td>{tally[f][s]}</td>" for s in STATUSES) + "</tr>")
    parts.append("</table>"
                 "<div class=sum><b>How to read this:</b> "
                 "<b>agree</b> = both extracted the same value (highest confidence). "
                 "<b>llm_only_attributed</b> = the LLM filled a gap AND the supporting "
                 "sentence both contains the value and names THIS molecule &mdash; the only "
                 "LLM-only case safe to adopt. "
                 "<b>llm_only_unattributed</b> = the value is in the abstract but in a "
                 "sentence that does not name this molecule (likely a comparator's dose or a "
                 "different endpoint) &mdash; do not adopt. "
                 "<b>llm_only_ungrounded</b> = not supported by the abstract at all &mdash; "
                 "hallucination, never publish. "
                 "<b>differ</b> = both produced conflicting values &mdash; publish neither as "
                 "fact; keep the rules value (auditable) and flag. "
                 "<b>rules_only</b> = the LLM missed something the rules found."
                 "<br><br><b>outcome_direction is rules-only by policy</b> and is shown for "
                 "comparison only: authors frame their own findings positively, so a model "
                 "reading that framing over-calls &ldquo;beneficial&rdquo;. It is never adopted "
                 "from the LLM regardless of its status above."
                 "</div>")
    for r in rows:
        kind = r.get("kind", "primary")
        badge = " &middot; <b>review/meta-analysis</b>" if kind == "synthesis" else ""
        src = []
        if r.get("pmcid"):
            src.append("OA full text " + esc(r["pmcid"]))
        if r.get("trial", {}).get("nct_id"):
            t = r["trial"]
            src.append("CT.gov " + esc(t.get("nct_id", "")) + " (enrolled "
                       + esc(t.get("enrollment_count", "?")) + ")")
        parts.append(f"<h3 class=h>{esc(r['molecule_name'])} &middot; PMID {esc(r['pmid'])}{badge}</h3>")
        parts.append(f"<p>{esc(r['title'])}</p>")
        if src:
            parts.append("<div class=ab>sources: " + " &middot; ".join(src) + "</div>")
        if kind == "synthesis" and (r.get("included_studies") or r.get("pooled_sample_size")):
            parts.append(f"<div class=ab>synthesis scale: {esc(r.get('included_studies'))} studies "
                         f"&middot; pooled N {esc(r.get('pooled_sample_size'))}</div>")
        if r.get("abstract"):
            parts.append(f"<div class=ab>{esc(r['abstract'])}&hellip;</div>")
        if r["llm_summary"]:
            parts.append(f"<p><small>LLM summary:</small> {esc(r['llm_summary'])}</p>")
        parts.append("<table><tr><th>field</th><th>rules-based</th><th>LLM</th><th>verdict</th></tr>")
        for f in FIELDS:
            st = r.get("status", {}).get(f, "")
            cls = " class=d" if st in ("differ", "llm_only_ungrounded") else ""
            parts.append(f"<tr{cls}><td>{esc(f)}</td><td>{esc(r['rules'][f])}</td>"
                         f"<td>{esc(r['llm'][f])}</td><td><small>{esc(st)}</small></td></tr>")
        parts.append("</table>")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--api", choices=["ollama", "openai"], default="ollama")
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--model", default="llama3.1")
    ap.add_argument("--out", default="exports/llm_compare.html")
    ap.add_argument("--mock", action="store_true", help="canned response; no model/network needed")
    ap.add_argument("--any-papers", action="store_true",
                    help="sample papers at random instead of biasing toward human/clinical ones")
    # Richer inputs. Most "both extractors missed it" cases are information that is
    # simply not in the abstract, so these matter more than the model choice.
    ap.add_argument("--fulltext", action="store_true",
                    help="fetch open-access Methods/Results from Europe PMC (network)")
    ap.add_argument("--pubtator", action="store_true",
                    help="fetch curated chemical entity spans from NCBI PubTator3 (network)")
    ap.add_argument("--trials-db", default="data/retarats_trials.sqlite",
                    help="local CT.gov mirror; gives structured enrollment for trial-linked papers")
    ap.add_argument("--cache-dir", default=".cache/context",
                    help="on-disk cache for fetched context so re-runs don't re-fetch")
    ap.add_argument("--rpm", type=float, default=0,
                    help="throttle to at most N requests/minute (free tiers rate-limit; "
                         "e.g. --rpm 4 for the Gemini free tier). 0 = no throttle.")
    ap.add_argument("--batch", type=int, default=1,
                    help="papers per LLM request. Free tiers cap requests/DAY (Gemini: 20) "
                         "while allowing huge tokens/minute, so batching is how you get "
                         "real volume: --batch 25 turns 20 requests into 500 papers.")
    args = ap.parse_args()

    if not args.mock and not os.path.exists(args.db):
        print(f"No corpus DB at {args.db}", file=sys.stderr)
        sys.exit(1)

    papers = (sample_papers(args.db, args.limit, args.seed, prefer_human=not args.any_papers)
              if os.path.exists(args.db) else [])
    if not papers and args.mock:
        # allow a no-corpus smoke test of the plumbing
        papers = [{"pmid": "0", "molecule_name": "Demo", "title": "Demo paper",
                   "abstract": "Demo received 5 mg subcutaneously for 24 weeks; n=101; weight was reduced.",
                   "_evidence": {"molecule_name": "Demo", "efficacy_signal": "weight was reduced"},
                   "_paper": {"title": "Demo", "abstract": "Demo received 5 mg subcutaneously for 24 weeks; n=101."}}]
    if not args.mock:
        err = preflight(args.api, args.base_url, args.model)
        if err:
            print("LLM backend not ready:\n" + err, file=sys.stderr)
            sys.exit(2)
    api_key = os.environ.get("LLM_API_KEY")
    trials_index = ctxmod.load_trial_index(args.trials_db)
    if trials_index:
        print(f"Trial links loaded for {len(trials_index)} PMIDs (structured CT.gov facts).")
    rows = []
    min_gap = 60.0 / args.rpm if args.rpm and args.rpm > 0 else 0.0
    last_call = [0.0]

    def _throttle():
        if min_gap:
            gap = min_gap - (time.time() - last_call[0])
            if gap > 0:
                time.sleep(gap)
            last_call[0] = time.time()

    # Build context once per paper (cached; independent of batching).
    ctxs = {}
    for p in papers:
        ctxs[p["pmid"]] = ctxmod.build_context(
            p["pmid"], p.get("molecule_name", ""), p.get("abstract", ""),
            trials_index=trials_index, use_fulltext=args.fulltext,
            use_pubtator=args.pubtator, cache_dir=args.cache_dir)

    if args.batch > 1:
        # Group by kind so reviews get the synthesis prompt, then chunk. One request
        # carries many papers -- this is what makes a 20-requests/day tier usable.
        groups = {True: [p for p in papers if is_synthesis(p)],
                  False: [p for p in papers if not is_synthesis(p)]}
        for synth, group in groups.items():
            for start in range(0, len(group), args.batch):
                chunk = group[start:start + args.batch]
                prompt_chunk = []
                for p in chunk:
                    q = dict(p)
                    q["abstract"] = ctxmod.context_text(ctxs[p["pmid"]])
                    prompt_chunk.append(q)
                _throttle()
                try:
                    raw = call_llm(build_batch_prompt(prompt_chunk, synth),
                                   args.api, args.base_url, args.model, api_key, args.mock)
                    parsed = parse_batch(raw, chunk)
                except Exception as e:  # noqa: BLE001
                    print(f"  batch of {len(chunk)} failed ({e}); skipping", file=sys.stderr)
                    continue
                for p in chunk:
                    rows.append(compare_paper(p, args.api, args.base_url, args.model, api_key,
                                              args.mock, ctxs[p["pmid"]],
                                              llm_override=parsed.get(str(p["pmid"]), {})))
                print(f"  batch done: {len(chunk)} papers ({len(rows)}/{len(papers)})",
                      file=sys.stderr)
    else:
        for i, p in enumerate(papers):
            _throttle()
            if i and i % 10 == 0:
                print(f"  ...{i}/{len(papers)} papers", file=sys.stderr)
            try:
                rows.append(compare_paper(p, args.api, args.base_url, args.model, api_key,
                                          args.mock, ctxs[p["pmid"]]))
            except Exception as e:  # noqa: BLE001 -- one bad call shouldn't lose the run
                print(f"  paper {p.get('pmid','?')}: LLM call failed ({e}); skipping",
                      file=sys.stderr)
    if not rows:
        print("No results produced (all LLM calls failed).", file=sys.stderr)
        sys.exit(1)
    write_report(rows, args.out, mock=args.mock, model=args.model)
    n_dis = sum(1 for r in rows if r["disagree"])
    print(f"Compared {len(rows)} papers ({n_dis} with disagreements) -> {args.out}")
    print("NOTE: experimental only; LLM output is not used on the live site.")


if __name__ == "__main__":
    main()
