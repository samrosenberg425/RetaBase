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
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from retarats_pipeline.curation.extractors import refine_extraction  # noqa: E402

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


def sample_papers(db: str, limit: int, seed: int = 0) -> List[dict]:
    """Return up to ``limit`` papers with title+abstract+molecule for comparison."""
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
        if not abstract:
            continue  # nothing to extract from
        seen.add(pmid)
        rows.append({
            "pmid": pmid,
            "molecule_name": str(e.get("molecule_name", "") or ""),
            "title": str(e.get("title", "") or pap.get("title", "") or ""),
            "abstract": abstract,
            "_evidence": e,
            "_paper": pap,
        })
    random.Random(seed).shuffle(rows)
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


def build_prompt(paper: dict) -> str:
    return _PROMPT.format(mol=paper["molecule_name"] or "the compound",
                          title=paper["title"][:400], abstract=paper["abstract"][:3500])


def call_llm(prompt: str, api: str, base_url: str, model: str,
             api_key: Optional[str], mock: bool, timeout: int = 120) -> str:
    if mock:
        return ('{"dose":"5 mg","route":"subcutaneous","duration":"24 weeks",'
                '"sample_size":"n=101","outcome_direction":"beneficial",'
                '"one_sentence_summary":"(mock) beneficial weight-loss result."}')
    import requests  # local import so --mock needs no dependency
    if api == "ollama":
        r = requests.post(base_url.rstrip("/") + "/api/generate",
                          json={"model": model, "prompt": prompt, "stream": False,
                                "options": {"temperature": 0}}, timeout=timeout)
        r.raise_for_status()
        return r.json().get("response", "")
    # OpenAI-compatible
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    r = requests.post(base_url.rstrip("/") + "/chat/completions",
                      headers=headers,
                      json={"model": model, "temperature": 0,
                            "messages": [{"role": "user", "content": prompt}]},
                      timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


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
def compare_paper(paper: dict, api: str, base_url: str, model: str,
                  api_key: Optional[str], mock: bool) -> dict:
    rules = refine_extraction(paper["_evidence"], paper["_paper"])
    raw = call_llm(build_prompt(paper), api, base_url, model, api_key, mock)
    llm = parse_llm_json(raw)
    rules_map = {
        "dose": rules.get("refined_dose", ""),
        "route": rules.get("refined_route", ""),
        "duration": rules.get("refined_duration", ""),
        "sample_size": rules.get("refined_sample_size", ""),
        "outcome_direction": rules.get("refined_outcome_direction", ""),
    }
    disagree = [f for f in FIELDS
                if (rules_map[f] or "").strip().lower() != (llm.get(f, "") or "").strip().lower()]
    return {
        "pmid": paper["pmid"],
        "molecule_name": paper["molecule_name"],
        "title": paper["title"],
        "rules": rules_map,
        "llm": {f: llm.get(f, "") for f in FIELDS},
        "llm_summary": llm.get("one_sentence_summary", ""),
        "rules_scope": rules.get("refined_extraction_scope", ""),
        "disagree": disagree,
        "raw": raw,
    }


def write_report(rows: List[dict], out: str) -> None:
    def esc(s):
        return html.escape(str(s or ""))
    n_dis = sum(1 for r in rows if r["disagree"])
    parts = [
        "<!doctype html><meta charset=utf-8><title>LLM vs rules extraction</title>",
        "<style>body{font:14px system-ui;margin:24px;background:#0f1115;color:#e6e8ec}"
        "table{border-collapse:collapse;width:100%;margin:8px 0 28px}"
        "th,td{border:1px solid #2a2f3a;padding:6px 9px;text-align:left;vertical-align:top}"
        "th{color:#9aa3b2}.d{background:#3a1d1d}.h{color:#5b9dff}small{color:#9aa3b2}</style>",
        "<h1>Experimental: LLM vs rules-based extraction</h1>",
        f"<p><b>{len(rows)}</b> papers &middot; <b>{n_dis}</b> with at least one "
        "field disagreement. This is a research comparison only &mdash; the LLM output "
        "is <b>not</b> used anywhere on the live site.</p>",
    ]
    for r in rows:
        parts.append(f"<h3 class=h>{esc(r['molecule_name'])} &middot; PMID {esc(r['pmid'])}</h3>")
        parts.append(f"<p>{esc(r['title'])}</p>")
        if r["llm_summary"]:
            parts.append(f"<p><small>LLM summary:</small> {esc(r['llm_summary'])}</p>")
        parts.append("<table><tr><th>field</th><th>rules-based</th><th>LLM</th></tr>")
        for f in FIELDS:
            cls = " class=d" if f in r["disagree"] else ""
            parts.append(f"<tr{cls}><td>{esc(f)}</td><td>{esc(r['rules'][f])}</td>"
                         f"<td>{esc(r['llm'][f])}</td></tr>")
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
    args = ap.parse_args()

    if not args.mock and not os.path.exists(args.db):
        print(f"No corpus DB at {args.db}", file=sys.stderr)
        sys.exit(1)

    papers = sample_papers(args.db, args.limit, args.seed) if os.path.exists(args.db) else []
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
    rows = []
    for i, p in enumerate(papers):
        try:
            rows.append(compare_paper(p, args.api, args.base_url, args.model, api_key, args.mock))
        except Exception as e:  # noqa: BLE001 -- one bad call shouldn't lose the whole run
            print(f"  paper {p.get('pmid','?')}: LLM call failed ({e}); skipping", file=sys.stderr)
    if not rows:
        print("No results produced (all LLM calls failed).", file=sys.stderr)
        sys.exit(1)
    write_report(rows, args.out)
    n_dis = sum(1 for r in rows if r["disagree"])
    print(f"Compared {len(rows)} papers ({n_dis} with disagreements) -> {args.out}")
    print("NOTE: experimental only; LLM output is not used on the live site.")


if __name__ == "__main__":
    main()
