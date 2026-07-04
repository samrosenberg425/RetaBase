#!/usr/bin/env python3
"""Generate a polished, single-file interactive dashboard from the curated feed.

The curation build (``build_curated_database.py``) produces a compact, rank-sorted
``exports/curated/site_data.json`` feed plus flat CSVs. This script turns that feed
into a single ``exports/site/index.html`` dashboard that anyone can open locally: it
needs no server, no network, no CDN, and no ``localStorage``. By default all record
data is inlined as JSON so the page filters entirely client-side; a ``--mode fetch``
variant instead loads ``site_data.json`` at runtime (for GitHub Pages / hosted use).

Why a single self-contained file: the public evidence feed should be trivially
shareable and archivable. A lone HTML file can be emailed, checked into git, or
dropped on any static host and still work fully offline. That constraint is why we
inline the data by default instead of fetching a sibling ``.json``.

SECURITY: every value that originates from the feed/CSV is treated as hostile.
Titles, abstracts, and appraisal text can contain ``<``, ``>``, ``&``, and quotes.
Two layers protect the output:

* Text that lands in the visible HTML shell is passed through ``html.escape``.
* The bulk record data is emitted with ``json.dumps`` inside a
  ``<script type="application/json">`` block, with ``</`` neutralized so a value
  like ``</script><script>alert(1)`` cannot break out of the block. The browser
  parses that block as inert data, and the UI renders every field via
  ``textContent`` (never ``innerHTML``), so injected markup is shown as text.
* All outbound links are built with ``encodeURIComponent`` and no ``javascript:``
  scheme is ever emitted; downloads use a Blob URL (curator export), no server.

Pure stdlib (csv/json/html/argparse). Robust to missing columns/fields: any field
the UI wants but the feed lacks simply comes back empty. Prefers ``site_data.json``;
falls back to ``public_records.csv`` when the JSON feed is absent.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List


# Fields we pull into the inline JSON when falling back to public_records.csv.
# When site_data.json is present we take its fields verbatim (superset of these).
RECORD_FIELDS = [
    "molecule_id", "molecule_name", "pmid", "doi", "title", "journal", "pub_year",
    "website_section", "evidence_class_label", "publication_status",
    "authors_short", "first_author", "author_count",
    "journal_reputation", "journal_tier",
    "reliability_score", "reliability_tier", "evidence_directness", "directness_tier",
    "reliability_components", "rank_components",
    "rank_score", "rank_tier", "appraisal_summary", "appraisal_strengths", "appraisal_limitations",
    "refined_dose", "refined_route", "refined_duration", "refined_sample_size", "refined_outcome_direction",
    "facet_species", "facet_indication", "facet_endpoint", "facet_study_type",
    "facet_model_system", "facet_route",
    "facet_drug_class", "facet_population", "facet_sex", "facet_formulation",
    "facet_evidence_direction", "facet_all",
]

# Facet dropdown filters shown in the sidebar: (record field, human label).
# facet_* fields are semicolon-joined multi-values; the UI splits on "; ".
FILTER_FACETS = [
    ("molecule_name", "Molecule"),
    ("facet_species", "Species"),
    ("facet_indication", "Indication"),
    ("facet_endpoint", "Endpoint"),
    ("facet_study_type", "Study type"),
    ("facet_model_system", "Model system"),
    ("facet_route", "Route"),
    ("facet_drug_class", "Drug class"),
    ("facet_population", "Population"),
    ("facet_sex", "Sex"),
    ("facet_formulation", "Formulation"),
    ("facet_evidence_direction", "Evidence direction"),
    ("reliability_tier", "Reliability tier"),
    ("directness_tier", "Directness tier"),
    ("website_section", "Website section"),
]

# Which facet fields are multi-valued (semicolon-joined) vs single-valued.
MULTI_VALUE_FIELDS = {
    "facet_species", "facet_indication", "facet_endpoint", "facet_study_type",
    "facet_model_system", "facet_route",
    "facet_drug_class", "facet_population", "facet_sex", "facet_formulation",
    "facet_evidence_direction", "facet_all",
}

# Aspect-tag chips shown on each card: (record field, css class, short label).
# Order here is display order in the tag cloud.
ASPECT_TAGS = [
    ("facet_species", "sp", "species"),
    ("facet_indication", "ind", "indication"),
    ("facet_endpoint", "end", "endpoint"),
    ("facet_study_type", "st", "study"),
    ("facet_model_system", "ms", "model"),
    ("facet_route", "rt", "route"),
    ("facet_drug_class", "dc", "drug class"),
    ("facet_population", "pop", "population"),
    ("facet_sex", "sex", "sex"),
    ("facet_formulation", "frm", "formulation"),
    ("facet_evidence_direction", "ed", "evidence direction"),
]

MOLECULE_FIELDS = [
    "molecule_id", "molecule_name", "total_records", "auto_published",
    "human_evidence", "preclinical_evidence", "reviews", "max_reliability",
    "top_conditions", "sections_present",
]

# Candidate ("experimental") molecules proposed for future fetching. These carry
# NO evidence records; they are trusted config values but are still rendered via
# textContent to keep the safe pattern uniform.
EXPERIMENTAL_FIELDS = [
    "molecule_id", "display_name", "class", "rationale", "status",
    "example_search_terms",
]


@dataclass
class SiteData:
    """Everything the page needs, already normalized to the UI's field set."""

    records: List[Dict[str, str]] = field(default_factory=list)
    molecules: List[Dict[str, str]] = field(default_factory=list)
    experimental: List[Dict[str, str]] = field(default_factory=list)
    generated_utc: str = ""


def _read_csv(path: str, wanted: List[str]) -> List[Dict[str, str]]:
    """Read a CSV into dicts limited to ``wanted`` columns.

    Missing columns are tolerated: a requested field that the file lacks comes
    back as an empty string, so a schema change upstream degrades gracefully
    rather than raising.
    """
    if not os.path.exists(path):
        return []
    out: List[Dict[str, str]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = set(reader.fieldnames or [])
        for raw in reader:
            row = {k: str(raw.get(k, "") or "") if k in cols else "" for k in wanted}
            out.append(row)
    return out


def _as_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _norm_record(raw: Dict) -> Dict[str, str]:
    """Normalize a raw record dict to the UI field set (strings), tolerant of gaps."""
    out = {}
    for k in RECORD_FIELDS:
        v = raw.get(k, "")
        out[k] = "" if v is None else str(v)
    return out


def load_site_data(curated_dir: str) -> SiteData:
    """Load the curated feed, preferring site_data.json over public_records.csv.

    The JSON feed is compact + rank-sorted and carries the extra axes (rank,
    directness, evidence class, component breakdowns) the dashboard renders. If
    it is missing we degrade to the CSV, which lacks those extras but still
    populates the fields it has.
    """
    json_path = os.path.join(curated_dir, "site_data.json")
    generated = ""
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as fh:
            feed = json.load(fh)
        records = [_norm_record(r) for r in feed.get("records", [])]
        molecules = [
            {k: ("" if m.get(k) is None else str(m.get(k, ""))) for k in MOLECULE_FIELDS}
            for m in feed.get("molecules", [])
        ]
        # Candidate molecules proposed for future fetching (no evidence yet).
        # Absent/empty -> empty list, which the UI uses to hide the tab.
        experimental = [
            {k: ("" if e.get(k) is None else str(e.get(k, ""))) for k in EXPERIMENTAL_FIELDS}
            for e in feed.get("experimental", [])
            if (e.get("molecule_id") or e.get("display_name"))
        ]
        generated = str(feed.get("generated_utc", ""))
    else:
        records = _read_csv(os.path.join(curated_dir, "public_records.csv"), RECORD_FIELDS)
        molecules = _read_csv(os.path.join(curated_dir, "molecule_index.csv"), MOLECULE_FIELDS)
        experimental = []

    # The site browses PUBLIC records only, so the Molecules tab should list just
    # molecules that actually have at least one record in the feed. Showing
    # molecules with zero records made their cards resolve to an unfiltered "all
    # records" view when clicked. Cross-check against molecules truly present in
    # ``records`` in case the index and feed disagree.
    present = {r.get("molecule_name", "") for r in records} | {r.get("molecule_id", "") for r in records}
    molecules = [
        m for m in molecules
        if _as_int(m.get("auto_published")) > 0
        or m.get("molecule_name") in present
        or m.get("molecule_id") in present
    ]
    return SiteData(records=records, molecules=molecules,
                    experimental=experimental, generated_utc=generated)


def _safe_json_block(obj) -> str:
    """Serialize ``obj`` for embedding inside a <script type="application/json">.

    ``json.dumps`` already escapes quotes and control chars, but a JSON *string*
    may still contain the literal sequence ``</script>`` which would close the
    host block in an HTML parser. Escaping ``</`` -> ``<\\/`` keeps the payload
    inert while remaining valid JSON (``\\/`` is a legal JSON escape for ``/``).
    ``ensure_ascii=False`` keeps unicode readable; ``<`` and ``>`` on their own
    are harmless inside a data block, so we only special-case the closing tag.
    """
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/")


def _cross_filter_counts(records, filters, multi):
    """Reference implementation of the cross-filter facet counting the UI does.

    Kept in Python (and unit-tested) to pin the semantics the JS must match:
    for each facet FIELD, the count of a value V is the number of records that
    pass *every other* active filter (the search term aside) AND contain V for
    FIELD. This is faceted/"cross-filter" counting: selecting Species=human
    reshrinks every OTHER dropdown's counts, but a facet does not shrink its own
    option list (so you can still switch to a sibling value).

    ``records``  : list of record dicts.
    ``filters``  : {field: selected_value} of currently-active facet selections.
    ``multi``    : set of fields whose value is a "; "-joined multi-value.
    Returns {field: {value: count}}.
    """
    def split_vals(rec, fld):
        v = rec.get(fld, "") or ""
        if fld in multi:
            return [s.strip() for s in v.split(";") if s.strip()]
        return [v.strip()] if v.strip() else []

    def passes(rec, skip_field):
        for f, sel in filters.items():
            if f == skip_field or not sel:
                continue
            if sel not in split_vals(rec, f):
                return False
        return True

    out = {}
    fields = set(list(filters.keys()))
    return _count_fields(records, fields, filters, split_vals, passes)


def _count_fields(records, fields, filters, split_vals, passes):
    out = {}
    for fld in fields:
        counts = {}
        for rec in records:
            if not passes(rec, fld):
                continue
            for v in split_vals(rec, fld):
                counts[v] = counts.get(v, 0) + 1
        out[fld] = counts
    return out


def build_site(curated_dir: str, out_dir: str, mode: str = "inline",
               max_inline: int = 4000) -> Dict[str, int]:
    data = load_site_data(curated_dir)
    os.makedirs(out_dir, exist_ok=True)

    truncated = 0
    total_records = len(data.records)
    if mode == "inline" and max_inline and len(data.records) > max_inline:
        truncated = len(data.records) - max_inline
        print(
            f"warning: {len(data.records)} records exceeds --max-inline {max_inline}; "
            f"inlining the top {max_inline} by rank (use --mode fetch to serve all).",
            file=sys.stderr,
        )
        data.records = data.records[:max_inline]

    payload = {
        "generated_utc": data.generated_utc,
        "records": data.records,
        "molecules": data.molecules,
        # Experimental candidates are small trusted config (no record bodies), so
        # they are inlined in both inline and fetch modes: the tab works even when
        # the sibling feed hasn't loaded yet.
        "experimental": data.experimental,
        "filters": [{"field": f, "label": lbl} for f, lbl in FILTER_FACETS],
        "multi": sorted(MULTI_VALUE_FIELDS),
        "aspects": [{"field": f, "cls": c, "label": lbl} for f, c, lbl in ASPECT_TAGS],
        "total_records": total_records,
        "truncated": truncated,
    }

    molecule_count = len(data.molecules)

    if mode == "fetch":
        # In fetch mode the page loads site_data.json at runtime; only config
        # (filters/aspects/multi) is inlined, no record bodies.
        cfg = dict(payload)
        cfg["records"] = []
        cfg["molecules"] = []
        cfg["mode"] = "fetch"
        json_block = _safe_json_block(cfg)
        record_count = 0  # fetch mode inlines no record bodies
    else:
        payload["mode"] = "inline"
        json_block = _safe_json_block(payload)
        record_count = len(data.records)

    html_text = _render_html(json_block, record_count, molecule_count,
                             data.generated_utc, total_records, truncated, mode)

    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)

    return {
        "records": record_count,
        "molecules": molecule_count,
        "total": total_records,
        "truncated": truncated,
        "mode": mode,
        "bytes": os.path.getsize(out_path),
        "path": out_path,
    }


def _render_html(json_block: str, record_count: int, molecule_count: int,
                 generated_utc: str, total_records: int, truncated: int,
                 mode: str) -> str:
    """Assemble the single-file HTML.

    All dynamic-but-trusted numbers are ints; the only feed-derived content in
    the shell is inside the JSON data block (already neutralized). The JS renders
    every field with ``textContent``, so nothing from the feed is ever parsed as
    HTML at runtime.
    """
    title = html.escape("Retarats — Curated Evidence Dashboard")
    gen = html.escape(generated_utc or "unknown")
    note = ""
    if mode == "inline" and truncated:
        note = html.escape(
            f" (top {record_count} of {total_records} by rank inlined; "
            "rebuild with --mode fetch to browse all)"
        )
    subtitle = html.escape(
        f"{total_records} public records across {molecule_count} molecules "
        "— rule-based, offline, auditable"
    ) + note
    return _TEMPLATE.format(
        title=title,
        subtitle=subtitle,
        generated=gen,
        record_count=record_count,
        molecule_count=molecule_count,
        data_json=json_block,
    )


# The template uses {{ }} for literal CSS/JS braces and { } for .format fields.
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #0f1115; --panel: #171a21; --panel2: #1e222b; --border: #2a2f3a;
    --text: #e6e8ec; --muted: #9aa3b2; --accent: #5b9dff; --accent2: #7ee3a7;
    --tier-high: #7ee3a7; --tier-moderate: #ffd479; --tier-limited: #ff9e64;
    --tier-low: #ff6b6b; --tier-not_applicable: #9aa3b2;
    --ap-approve: #2f9e6b; --ap-reject: #d0455b;
    --t-sp: #5b9dff; --t-ind: #b98cff; --t-end: #7ee3a7; --t-st: #ffd479;
    --t-ms: #64d3ff; --t-rt: #ff9e64;
    --t-dc: #ff8fd1; --t-pop: #a0e88a; --t-sex: #ffc1a0; --t-frm: #8ad7ff;
    --t-ed: #d7b3ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  header {{ padding: 16px 24px; border-bottom: 1px solid var(--border); background: var(--panel); }}
  header h1 {{ margin: 0 0 4px; font-size: 20px; }}
  header p {{ margin: 0; color: var(--muted); font-size: 13px; }}
  header .gen {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .tabs {{ display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; align-items: center; }}
  .tabs button {{
    background: var(--panel2); color: var(--muted); border: 1px solid var(--border);
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
  }}
  .tabs button.active {{ color: var(--text); border-color: var(--accent); }}
  .tabs .spacer {{ flex: 1; }}
  .tabs .ap-summary {{ font-size: 12px; color: var(--muted); }}
  .tabs .ap-summary b {{ color: var(--text); }}
  .tabs .exp {{ background: var(--accent); color: #06101f; border: none; font-weight: 600; }}
  main {{ display: flex; gap: 0; align-items: flex-start; }}
  aside {{
    width: 288px; min-width: 288px; padding: 16px; border-right: 1px solid var(--border);
    background: var(--panel); height: calc(100vh - 118px); overflow-y: auto; position: sticky; top: 0;
  }}
  aside .fg {{ margin-bottom: 14px; }}
  aside label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: .04em; }}
  aside select, aside input {{
    width: 100%; padding: 7px 9px; background: var(--panel2); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; font-size: 13px;
  }}
  aside .reset {{ background: var(--panel2); color: var(--text); border: 1px solid var(--border); padding: 8px; border-radius: 6px; cursor: pointer; width: 100%; }}
  #q {{ font-size: 14px; }}
  section.content {{ flex: 1; padding: 16px 24px; height: calc(100vh - 118px); overflow-y: auto; }}
  .count {{ color: var(--muted); font-size: 13px; margin-bottom: 12px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }}
  .count select {{ background: var(--panel2); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; font-size: 12px; }}
  .card {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 16px; margin-bottom: 12px; cursor: pointer; transition: border-color .1s;
  }}
  .card:hover {{ border-color: var(--accent); }}
  .card.ap-approve {{ border-left: 4px solid var(--ap-approve); }}
  .card.ap-reject {{ border-left: 4px solid var(--ap-reject); opacity: .7; }}
  .card h3 {{ margin: 0 0 6px; font-size: 15px; line-height: 1.35; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; color: var(--muted); margin-bottom: 8px; align-items: center; }}
  .pill {{ background: var(--panel2); border: 1px solid var(--border); border-radius: 999px; padding: 2px 9px; }}
  /* reliability meter */
  .meter-wrap {{ display: flex; align-items: center; gap: 8px; }}
  .meter {{ width: 84px; height: 8px; background: var(--panel2); border-radius: 999px; overflow: hidden; border: 1px solid var(--border); }}
  .meter > i {{ display: block; height: 100%; border-radius: 999px; }}
  .meter-lbl {{ font-size: 11px; font-weight: 600; }}
  .badge {{ font-size: 11px; font-weight: 600; border-radius: 999px; padding: 2px 8px; border: 1px solid var(--border); }}
  .tier-high {{ color: var(--tier-high); }} .fill-high {{ background: var(--tier-high); }}
  .tier-moderate {{ color: var(--tier-moderate); }} .fill-moderate {{ background: var(--tier-moderate); }}
  .tier-limited {{ color: var(--tier-limited); }} .fill-limited {{ background: var(--tier-limited); }}
  .tier-low {{ color: var(--tier-low); }} .fill-low {{ background: var(--tier-low); }}
  .tier-not_applicable {{ color: var(--tier-not_applicable); }} .fill-not_applicable {{ background: var(--tier-not_applicable); }}
  .summary {{ font-size: 13px; margin: 8px 0; color: var(--text); }}
  .tags {{ display: flex; flex-wrap: wrap; gap: 5px; margin: 8px 0 2px; }}
  .tag {{ font-size: 11px; border-radius: 4px; padding: 1px 7px; cursor: pointer; border: 1px solid transparent; background: var(--panel2); }}
  .tag:hover {{ filter: brightness(1.25); }}
  .tag.sp {{ color: var(--t-sp); border-color: var(--t-sp); }}
  .tag.ind {{ color: var(--t-ind); border-color: var(--t-ind); }}
  .tag.end {{ color: var(--t-end); border-color: var(--t-end); }}
  .tag.st {{ color: var(--t-st); border-color: var(--t-st); }}
  .tag.ms {{ color: var(--t-ms); border-color: var(--t-ms); }}
  .tag.rt {{ color: var(--t-rt); border-color: var(--t-rt); }}
  .tag.dc {{ color: var(--t-dc); border-color: var(--t-dc); }}
  .tag.pop {{ color: var(--t-pop); border-color: var(--t-pop); }}
  .tag.sex {{ color: var(--t-sex); border-color: var(--t-sex); }}
  .tag.frm {{ color: var(--t-frm); border-color: var(--t-frm); }}
  .tag.ed {{ color: var(--t-ed); border-color: var(--t-ed); }}
  /* authors line */
  .authors {{ font-size: 12px; color: var(--muted); margin: 0 0 8px; }}
  .authors a {{ color: var(--accent); text-decoration: none; }}
  .authors a:hover {{ text-decoration: underline; }}
  /* journal-tier badge */
  .jtier {{ font-size: 10px; font-weight: 600; border-radius: 999px; padding: 1px 7px; margin-left: 5px;
           border: 1px solid var(--border); color: var(--muted); text-transform: uppercase; letter-spacing: .03em; }}
  /* "How to read this" explainer legend + note explainers */
  .explainer {{ margin-top: 10px; font-size: 12px; color: var(--muted); }}
  .explainer > summary {{ cursor: pointer; color: var(--accent); list-style: none; display: inline-block; }}
  .explainer > summary::-webkit-details-marker {{ display: none; }}
  .explainer > summary::before {{ content: "\\25B8 "; }}
  .explainer[open] > summary::before {{ content: "\\25BE "; }}
  .explainer ul {{ margin: 6px 0 0; padding-left: 18px; }}
  .explainer li {{ margin: 2px 0; }}
  .explainer b {{ color: var(--text); }}
  .note-hint {{ font-size: 11px; color: var(--muted); margin: 6px 0 0; font-style: italic; }}
  .links a {{ color: var(--accent); text-decoration: none; font-size: 12px; margin-right: 12px; }}
  .links a:hover {{ text-decoration: underline; }}
  .ap-row {{ display: flex; gap: 6px; margin-top: 10px; align-items: center; }}
  .ap-btn {{ font-size: 12px; padding: 4px 10px; border-radius: 6px; cursor: pointer; border: 1px solid var(--border); background: var(--panel2); color: var(--text); }}
  .ap-btn.on-approve {{ background: var(--ap-approve); border-color: var(--ap-approve); color: #fff; }}
  .ap-btn.on-reject {{ background: var(--ap-reject); border-color: var(--ap-reject); color: #fff; }}
  .ap-note {{ flex: 1; }}
  .mol-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }}
  .mol-card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; cursor: pointer; }}
  .mol-card:hover {{ border-color: var(--accent); }}
  .mol-card h3 {{ margin: 0 0 8px; font-size: 15px; }}
  .mol-stats {{ display: flex; flex-wrap: wrap; gap: 6px; font-size: 12px; color: var(--muted); }}
  .empty {{ color: var(--muted); padding: 30px; text-align: center; }}
  /* experimental (candidate) section */
  .exp-banner {{
    background: var(--panel2); border: 1px solid var(--tier-limited); border-left: 4px solid var(--tier-limited);
    border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; font-size: 13px; color: var(--text);
  }}
  .exp-card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; border-left: 4px solid var(--tier-limited); }}
  .exp-card h3 {{ margin: 0 0 6px; font-size: 15px; }}
  .exp-class {{ font-size: 12px; color: var(--muted); margin-bottom: 8px; }}
  .exp-rationale {{ font-size: 13px; margin: 6px 0; color: var(--text); }}
  .exp-terms-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-top: 8px; }}
  .exp-terms {{ display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }}
  .exp-term {{ font-size: 11px; border-radius: 4px; padding: 1px 7px; background: var(--panel2); border: 1px solid var(--border); color: var(--muted); }}
  .sl {{ font-size: 12px; margin: 4px 0; }}
  .sl b {{ color: var(--accent2); }} .sl.lim b {{ color: var(--tier-limited); }}
  /* modal */
  .modal-bg {{ position: fixed; inset: 0; background: rgba(0,0,0,.62); display: none; z-index: 50; }}
  .modal-bg.open {{ display: flex; align-items: flex-start; justify-content: center; overflow-y: auto; padding: 30px 16px; }}
  .modal {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; max-width: 780px; width: 100%; padding: 22px 26px; }}
  .modal h2 {{ margin: 0 0 4px; font-size: 18px; line-height: 1.35; }}
  .modal .close {{ float: right; background: var(--panel2); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 4px 10px; cursor: pointer; }}
  .modal .grid {{ display: grid; grid-template-columns: 130px 1fr; gap: 6px 14px; font-size: 13px; margin-top: 12px; }}
  .modal .grid .k {{ color: var(--muted); }}
  .modal .comp {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }}
  .modal .comp span {{ font-size: 11px; background: var(--panel2); border: 1px solid var(--border); border-radius: 6px; padding: 2px 7px; }}
  .modal h4 {{ margin: 16px 0 4px; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
  code {{ background: var(--panel2); padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  @media (max-width: 720px) {{
    main {{ flex-direction: column; }}
    aside {{ width: 100%; min-width: 0; height: auto; position: static; border-right: none; border-bottom: 1px solid var(--border); }}
    section.content {{ height: auto; }}
  }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <p>{subtitle}</p>
  <p class="gen">Generated {generated} &middot; {record_count} records inlined &middot; {molecule_count} molecules</p>
  <details class="explainer">
    <summary>How to read this</summary>
    <ul>
      <li><b>Reliability</b> = how well-conducted the study is <i>for its type</i> (within-class study quality, 0-100).</li>
      <li><b>Directness</b> = how directly the evidence applies to humans (human RCT high &rarr; in-vitro low).</li>
      <li><b>Rank</b> = the combined best-first ordering (reliability + directness + relevance + recency + citations + journal).</li>
      <li><b>Notes</b> are for curators: record why you approved/rejected a record (e.g. &ldquo;wrong molecule role&rdquo;, &ldquo;off-topic&rdquo;); exported with your decisions.</li>
    </ul>
  </details>
  <div class="tabs">
    <button id="tab-records" class="active" onclick="showTab('records')">Records</button>
    <button id="tab-molecules" onclick="showTab('molecules')">Molecules</button>
    <button id="tab-experimental" style="display:none" onclick="showTab('experimental')">Experimental</button>
    <span class="spacer"></span>
    <span class="ap-summary" id="ap-summary"></span>
    <button class="exp" onclick="exportDecisions('json')">Export decisions</button>
  </div>
</header>
<main>
  <aside id="sidebar">
    <div class="fg">
      <label for="q">Search</label>
      <input id="q" type="search" placeholder="title, molecule, facets, summary..." oninput="applyFilters()">
    </div>
    <div id="facet-filters"></div>
    <button class="reset" onclick="resetFilters()">Reset filters</button>
  </aside>
  <section class="content">
    <div id="records-view">
      <div class="count" id="records-count">
        <span id="showing"></span>
        <label style="text-transform:none;display:inline-flex;gap:6px;align-items:center;color:var(--muted)">Sort
          <select id="sort" onchange="applyFilters()">
            <option value="rank">Rank (best first)</option>
            <option value="reliability">Reliability</option>
            <option value="directness">Directness</option>
            <option value="year">Year (newest)</option>
          </select>
        </label>
      </div>
      <div id="records-list"></div>
      <div id="load-more-wrap" style="text-align:center;margin:8px 0 24px;display:none">
        <button id="load-more" class="reset" style="width:auto;padding:8px 20px" onclick="loadMore()">Load more</button>
      </div>
    </div>
    <div id="molecules-view" style="display:none">
      <div class="count" id="molecules-count"></div>
      <div class="mol-grid" id="molecules-list"></div>
    </div>
    <div id="experimental-view" style="display:none">
      <div class="exp-banner" id="exp-banner"></div>
      <div class="count" id="experimental-count"></div>
      <div class="mol-grid" id="experimental-list"></div>
    </div>
  </section>
</main>

<div class="modal-bg" id="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal"></div>
</div>

<script type="application/json" id="site-data">{data_json}</script>
<script>
(function() {{
  "use strict";
  // Parse the inert data block. Nothing here is executed as HTML; every value
  // is later written with textContent so feed content cannot inject markup.
  var DATA = JSON.parse(document.getElementById("site-data").textContent);
  var RECORDS = DATA.records || [];
  var MOLECULES = DATA.molecules || [];
  var EXPERIMENTAL = DATA.experimental || [];
  var FILTERS = DATA.filters || [];
  var MULTI = new Set(DATA.multi || []);
  var ASPECTS = DATA.aspects || [];
  var PUBMED = "https://pubmed.ncbi.nlm.nih.gov/";
  var DECISIONS = {{}};  // rid -> {{status, note}} (in-memory only, never persisted)
  // ---- render cap ------------------------------------------------------------
  // The full database can be tens of thousands of records; rendering one DOM
  // card per match would freeze the browser. We keep the FULL filtered array in
  // memory (so facet counts, computed separately, stay complete) but only mount
  // the first ``visibleCount`` cards. "Load more" grows visibleCount; any
  // filter/search/sort change resets it back to RENDER_LIMIT.
  var RENDER_LIMIT = 300;
  var visibleCount = RENDER_LIMIT;
  var lastVisible = [];  // full filtered+sorted array from the last applyFilters

  function rid(r) {{ return (r.pmid || "") + "|" + (r.molecule_id || "") + "|" + (r.title || "").slice(0,40); }}

  function splitVals(rec, field) {{
    var v = rec[field] || "";
    if (MULTI.has(field)) {{
      return v.split(";").map(function(s) {{ return s.trim(); }}).filter(Boolean);
    }}
    return v ? [v.trim()] : [];
  }}

  function num(v) {{ var n = parseFloat(v); return isNaN(n) ? 0 : n; }}

  function tierClass(t) {{ return (t || "").replace(/[^a-z_]/gi, "") || "not_applicable"; }}

  // ---- cross-filter facet counting -------------------------------------------
  // For each facet field, a value's count = number of records that pass EVERY
  // OTHER active facet filter (search term excluded) and contain that value.
  // A facet does not constrain its own option counts, so users can still pivot
  // to a sibling value. This mirrors _cross_filter_counts in the Python module.
  function passesExcept(rec, filters, skipField) {{
    for (var f in filters) {{
      if (f === skipField || !filters[f]) continue;
      if (splitVals(rec, f).indexOf(filters[f]) === -1) return false;
    }}
    return true;
  }}
  function crossFilterCounts(filters, q) {{
    var out = {{}};
    FILTERS.forEach(function(f) {{ out[f.field] = {{}}; }});
    RECORDS.forEach(function(rec) {{
      if (q && !matchesQuery(rec, q)) return;
      FILTERS.forEach(function(f) {{
        if (!passesExcept(rec, filters, f.field)) return;
        splitVals(rec, f.field).forEach(function(v) {{
          out[f.field][v] = (out[f.field][v] || 0) + 1;
        }});
      }});
    }});
    return out;
  }}

  function matchesQuery(rec, q) {{
    // multi-term AND, case-insensitive, over title + facet_all + molecule + summary.
    var hay = ((rec.title || "") + " " + (rec.facet_all || "") + " " +
               (rec.molecule_name || "") + " " + (rec.appraisal_summary || "")).toLowerCase();
    var terms = q.split(/\\s+/).filter(Boolean);
    for (var i = 0; i < terms.length; i++) {{ if (hay.indexOf(terms[i]) === -1) return false; }}
    return true;
  }}

  function matches(rec, filters, q) {{
    for (var field in filters) {{
      if (filters[field] && splitVals(rec, field).indexOf(filters[field]) === -1) return false;
    }}
    if (q && !matchesQuery(rec, q)) return false;
    return true;
  }}

  // ---- readable one-line summary composed client-side ------------------------
  var PRETTY = {{
    beneficial_or_desired_signal: "beneficial", beneficial: "beneficial",
    harmful_or_adverse_signal: "adverse", harmful: "adverse",
    no_effect_or_null: "no effect", mixed_or_conditional: "mixed", unclear: ""
  }};
  function firstFacet(rec, field) {{ var a = splitVals(rec, field); return a.length ? a[0] : ""; }}
  function humanize(s) {{ return (s || "").replace(/_/g, " "); }}
  // Friendly DISPLAY label only: underscores -> spaces, title-case. The raw
  // value (with underscores) is preserved everywhere it is used for
  // filtering/matching/counting; only the visible text is prettified.
  function pretty(s) {{
    return humanize(s).replace(/\\b\\w/g, function(c) {{ return c.toUpperCase(); }});
  }}
  function composeSummary(rec) {{
    var cls = rec.evidence_class_label || "";
    var mol = rec.molecule_name || "the molecule";
    var ind = humanize(firstFacet(rec, "facet_indication"));
    var end = humanize(firstFacet(rec, "facet_endpoint"));
    var dir = PRETTY[(rec.refined_outcome_direction || "").trim()] || "";
    var parts = [];
    if (cls) parts.push(cls);
    var mid = mol;
    if (ind) mid += " for " + ind;
    parts.push(mid);
    if (end || dir) {{
      var tail = end || "outcomes";
      if (dir) tail += " (" + dir + ")";
      parts.push(tail);
    }}
    var s = parts.join(" — ");
    if (s && s.replace(/[—\\s]/g, "").length > 3) return s + ".";
    return rec.appraisal_summary || "";
  }}

  function parseComp(raw) {{
    if (!raw) return null;
    try {{ var o = JSON.parse(raw); return (o && typeof o === "object") ? o : null; }}
    catch (e) {{ return null; }}
  }}

  function el(tag, cls, text) {{
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;  // textContent -> injection-safe
    return e;
  }}

  function reliabilityMeter(rec) {{
    var wrap = el("span", "meter-wrap");
    var score = Math.max(0, Math.min(100, num(rec.reliability_score)));
    var tier = tierClass(rec.reliability_tier);
    var meter = el("div", "meter");
    var fill = el("i", "fill-" + tier);
    fill.style.width = score + "%";
    meter.appendChild(fill);
    wrap.appendChild(meter);
    var lbl = el("span", "meter-lbl tier-" + tier, (rec.reliability_score || "?") + " " + (rec.reliability_tier || ""));
    wrap.appendChild(lbl);
    return wrap;
  }}

  function directnessBadge(rec) {{
    var tier = tierClass(rec.directness_tier);
    var b = el("span", "badge tier-" + tier,
              "directness " + (rec.evidence_directness || "?") + (rec.directness_tier ? " (" + rec.directness_tier + ")" : ""));
    return b;
  }}

  function aspectTags(rec, onClick) {{
    var tags = el("div", "tags");
    ASPECTS.forEach(function(a) {{
      splitVals(rec, a.field).forEach(function(v) {{
        // Display the prettified label but filter on the raw value v.
        var t = el("span", "tag " + a.cls, pretty(v));
        t.title = a.label;
        t.addEventListener("click", function(ev) {{ ev.stopPropagation(); onClick(a.field, v); }});
        tags.appendChild(t);
      }});
    }});
    return tags;
  }}

  // Author line: names shown via textContent (injection-safe), each linking to a
  // Google Scholar search built with encodeURIComponent (no javascript: scheme).
  // authors_short looks like "Giblin K; Kaplan LM; Somers VK et al." — we split
  // on ";", keep an "et al." suffix as plain text, and link each real name.
  var SCHOLAR = "https://scholar.google.com/scholar?q=";
  function authorsLine(rec) {{
    var raw = (rec.authors_short || "").trim();
    if (!raw) return null;  // empty -> render nothing
    var wrap = el("div", "authors");
    var parts = raw.split(";").map(function(s) {{ return s.trim(); }}).filter(Boolean);
    parts.forEach(function(name, i) {{
      if (i > 0) wrap.appendChild(document.createTextNode("; "));
      // "et al." (and similar trailing markers) is not a searchable author name.
      if (/^et al\\.?$/i.test(name)) {{
        wrap.appendChild(document.createTextNode(name));
        return;
      }}
      var a = el("a", null, name);  // textContent set via el() -> safe
      a.href = SCHOLAR + encodeURIComponent(name);
      a.target = "_blank"; a.rel = "noopener noreferrer";
      a.addEventListener("click", function(e) {{ e.stopPropagation(); }});
      wrap.appendChild(a);
    }});
    return wrap;
  }}

  function journalTierBadge(rec) {{
    if (!rec.journal_tier) return null;
    return el("span", "jtier", pretty(rec.journal_tier));
  }}

  function applyTagFilter(field, value) {{
    if (filterEls[field]) {{
      var sel = filterEls[field];
      var has = Array.prototype.some.call(sel.options, function(o) {{ return o.value === value; }});
      if (has) {{ sel.value = value; applyFilters(); return; }}
    }}
    // fall back to search if the value has no dedicated dropdown
    var q = document.getElementById("q");
    q.value = (q.value ? q.value + " " : "") + value;
    applyFilters();
  }}

  function renderRecord(r) {{
    var card = el("div", "card");
    var dec = DECISIONS[rid(r)];
    if (dec && dec.status) card.className = "card ap-" + dec.status;
    card.appendChild(el("h3", null, r.title || "(untitled)"));

    var meta = el("div", "meta");
    if (r.molecule_name) meta.appendChild(el("span", "pill", r.molecule_name));
    if (r.pub_year) meta.appendChild(el("span", "pill", r.pub_year));
    if (r.evidence_class_label) meta.appendChild(el("span", "pill", r.evidence_class_label));
    if (r.journal) {{
      var jp = el("span", "pill", r.journal);
      var jt = journalTierBadge(r);
      if (jt) jp.appendChild(jt);
      meta.appendChild(jp);
    }}
    meta.appendChild(reliabilityMeter(r));
    meta.appendChild(directnessBadge(r));
    card.appendChild(meta);

    var au = authorsLine(r);
    if (au) card.appendChild(au);

    card.appendChild(el("div", "summary", composeSummary(r)));
    card.appendChild(aspectTags(r, applyTagFilter));

    var links = el("div", "links");
    if (r.pmid) {{
      var a = el("a", null, "PubMed"); a.href = PUBMED + encodeURIComponent(r.pmid) + "/";
      a.target = "_blank"; a.rel = "noopener noreferrer";
      a.addEventListener("click", function(e) {{ e.stopPropagation(); }});
      links.appendChild(a);
    }}
    if (r.doi) {{
      var d = el("a", null, "DOI"); d.href = "https://doi.org/" + encodeURIComponent(r.doi);
      d.target = "_blank"; d.rel = "noopener noreferrer";
      d.addEventListener("click", function(e) {{ e.stopPropagation(); }});
      links.appendChild(d);
    }}
    if (links.childNodes.length) card.appendChild(links);

    card.appendChild(approvalRow(r, card));
    card.addEventListener("click", function() {{ openModal(r); }});
    return card;
  }}

  // ---- curator approval (in-memory, exportable) ------------------------------
  function approvalRow(r, card) {{
    var row = el("div", "ap-row");
    var id = rid(r);
    var dec = DECISIONS[id] || {{}};
    var approve = el("button", "ap-btn" + (dec.status === "approve" ? " on-approve" : ""), "Approve");
    var reject = el("button", "ap-btn" + (dec.status === "reject" ? " on-reject" : ""), "Reject");
    var note = el("input", "ap-note"); note.type = "text"; note.placeholder = "note (optional)";
    note.value = dec.note || "";
    function set(status) {{
      var d = DECISIONS[id] || {{}};
      d.status = (d.status === status) ? "" : status;  // toggle off if same
      d.note = note.value; d.record = {{pmid: r.pmid, molecule_id: r.molecule_id, title: r.title}};
      if (!d.status && !d.note) delete DECISIONS[id]; else DECISIONS[id] = d;
      updateApSummary();
      // repaint just this row's buttons + card class
      approve.className = "ap-btn" + (d.status === "approve" ? " on-approve" : "");
      reject.className = "ap-btn" + (d.status === "reject" ? " on-reject" : "");
      card.className = "card" + (d.status ? " ap-" + d.status : "");
    }}
    approve.addEventListener("click", function(e) {{ e.stopPropagation(); set("approve"); }});
    reject.addEventListener("click", function(e) {{ e.stopPropagation(); set("reject"); }});
    note.addEventListener("click", function(e) {{ e.stopPropagation(); }});
    note.addEventListener("input", function() {{
      var d = DECISIONS[id] || {{record: {{pmid: r.pmid, molecule_id: r.molecule_id, title: r.title}}}};
      d.note = note.value; d.status = d.status || "";
      if (!d.status && !d.note) delete DECISIONS[id]; else DECISIONS[id] = d;
      updateApSummary();
    }});
    row.appendChild(approve); row.appendChild(reject); row.appendChild(note);
    return row;
  }}

  function updateApSummary() {{
    var a = 0, r = 0;
    for (var k in DECISIONS) {{
      if (DECISIONS[k].status === "approve") a++;
      else if (DECISIONS[k].status === "reject") r++;
    }}
    document.getElementById("ap-summary").innerHTML = "";
    var s = document.getElementById("ap-summary");
    s.appendChild(document.createTextNode("Approved "));
    var ab = el("b", null, String(a)); s.appendChild(ab);
    s.appendChild(document.createTextNode(" · Rejected "));
    var rb = el("b", null, String(r)); s.appendChild(rb);
  }}

  function exportDecisions(fmt) {{
    var rows = [];
    for (var k in DECISIONS) {{
      var d = DECISIONS[k];
      if (!d.status && !d.note) continue;
      rows.push({{
        pmid: (d.record && d.record.pmid) || "", molecule_id: (d.record && d.record.molecule_id) || "",
        title: (d.record && d.record.title) || "", decision: d.status || "", note: d.note || ""
      }});
    }}
    var blob, name;
    if (fmt === "csv") {{
      var esc = function(v) {{ v = String(v == null ? "" : v); return '"' + v.replace(/"/g, '""') + '"'; }};
      var lines = ["pmid,molecule_id,title,decision,note"];
      rows.forEach(function(x) {{ lines.push([x.pmid, x.molecule_id, x.title, x.decision, x.note].map(esc).join(",")); }});
      blob = new Blob([lines.join("\\n")], {{type: "text/csv"}}); name = "curation_decisions.csv";
    }} else {{
      blob = new Blob([JSON.stringify({{generated: new Date().toISOString(), decisions: rows}}, null, 2)],
                      {{type: "application/json"}}); name = "curation_decisions.json";
    }}
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a"); a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function() {{ URL.revokeObjectURL(url); }}, 1000);
  }}
  window.exportDecisions = exportDecisions;

  // ---- per-paper detail modal ------------------------------------------------
  function kv(grid, k, v) {{
    if (!v) return;
    grid.appendChild(el("div", "k", k));
    grid.appendChild(el("div", "v", v));
  }}
  // kv variant whose value is a DOM node (used for journal + tier badge).
  function kvNode(grid, k, node) {{
    if (!node) return;
    grid.appendChild(el("div", "k", k));
    var cell = el("div", "v");
    cell.appendChild(node);
    grid.appendChild(cell);
  }}
  function openModal(r) {{
    var m = document.getElementById("modal");
    m.textContent = "";
    var close = el("button", "close", "Close");
    close.addEventListener("click", closeModal);
    m.appendChild(close);
    m.appendChild(el("h2", null, r.title || "(untitled)"));
    var mau = authorsLine(r);
    if (mau) m.appendChild(mau);
    m.appendChild(el("div", "summary", composeSummary(r)));

    // meters
    var mrow = el("div", "meta");
    mrow.appendChild(reliabilityMeter(r));
    mrow.appendChild(directnessBadge(r));
    if (r.rank_score) mrow.appendChild(el("span", "badge tier-" + tierClass(r.rank_tier),
        "rank " + r.rank_score + (r.rank_tier ? " (" + r.rank_tier + ")" : "")));
    m.appendChild(mrow);

    var grid = el("div", "grid");
    kv(grid, "Molecule", r.molecule_name);
    if (r.journal) {{
      var jcell = el("span", null, r.journal);
      var jtb = journalTierBadge(r);
      if (jtb) jcell.appendChild(jtb);
      kvNode(grid, "Journal", jcell);
    }}
    if (r.author_count && r.author_count !== "0") kv(grid, "Authors", r.author_count + " total");
    kv(grid, "Year", r.pub_year);
    kv(grid, "Evidence class", r.evidence_class_label);
    kv(grid, "Website section", r.website_section);
    kv(grid, "Publication", r.publication_status);
    kv(grid, "Dose", r.refined_dose);
    kv(grid, "Route", r.refined_route);
    kv(grid, "Duration", r.refined_duration);
    kv(grid, "Sample size", r.refined_sample_size);
    kv(grid, "Outcome", humanize(r.refined_outcome_direction));
    m.appendChild(grid);

    var links = el("div", "links"); links.style.marginTop = "12px";
    if (r.pmid) {{ var a = el("a", null, "PubMed"); a.href = PUBMED + encodeURIComponent(r.pmid) + "/"; a.target = "_blank"; a.rel = "noopener noreferrer"; links.appendChild(a); }}
    if (r.doi) {{ var d = el("a", null, "DOI"); d.href = "https://doi.org/" + encodeURIComponent(r.doi); d.target = "_blank"; d.rel = "noopener noreferrer"; links.appendChild(d); }}
    if (links.childNodes.length) m.appendChild(links);

    var rc = parseComp(r.reliability_components);
    if (rc) {{
      m.appendChild(el("h4", null, "Reliability breakdown"));
      var comp = el("div", "comp");
      Object.keys(rc).forEach(function(k) {{ comp.appendChild(el("span", null, humanize(k) + ": " + rc[k])); }});
      m.appendChild(comp);
    }}
    var kc = parseComp(r.rank_components);
    if (kc) {{
      m.appendChild(el("h4", null, "Rank breakdown"));
      var comp2 = el("div", "comp");
      Object.keys(kc).forEach(function(k) {{ comp2.appendChild(el("span", null, humanize(k) + ": " + kc[k])); }});
      m.appendChild(comp2);
    }}

    if (r.appraisal_strengths) {{
      m.appendChild(el("h4", null, "Strengths"));
      m.appendChild(el("div", "sl", r.appraisal_strengths));
    }}
    if (r.appraisal_limitations) {{
      m.appendChild(el("h4", null, "Limitations"));
      m.appendChild(el("div", "sl lim", r.appraisal_limitations));
    }}
    m.appendChild(el("h4", null, "Aspects"));
    m.appendChild(aspectTags(r, function(f, v) {{ closeModal(); applyTagFilter(f, v); }}));
    m.appendChild(el("div", "note-hint",
      "Notes are for curators: record why you approved/rejected this record " +
      "(e.g. \\u201cwrong molecule role\\u201d, \\u201coff-topic\\u201d); exported with your decisions."));
    m.appendChild(approvalRow(r, el("div")));  // detached card ref; buttons still work

    document.getElementById("modal-bg").className = "modal-bg open";
  }}
  function closeModal() {{ document.getElementById("modal-bg").className = "modal-bg"; }}
  window.closeModal = closeModal;
  document.addEventListener("keydown", function(e) {{ if (e.key === "Escape") closeModal(); }});

  // ---- filter dropdowns ------------------------------------------------------
  var filterEls = {{}};
  var fc = document.getElementById("facet-filters");
  function buildFilters() {{
    FILTERS.forEach(function(f) {{
      var wrap = el("div", "fg");
      var lab = el("label", null, f.label);
      var sel = document.createElement("select");
      sel.addEventListener("change", applyFilters);
      filterEls[f.field] = sel;
      wrap.appendChild(lab); wrap.appendChild(sel); fc.appendChild(wrap);
    }});
  }}

  function currentFilters() {{
    var out = {{}};
    FILTERS.forEach(function(f) {{ var v = filterEls[f.field].value; if (v) out[f.field] = v; }});
    return out;
  }}

  // Rebuild each dropdown's <option> list with cross-filtered counts. The
  // selected value is preserved even if its count drops to 0 (so the user can
  // still un-select it); zero-count sibling values are hidden.
  function refreshDropdowns(counts) {{
    FILTERS.forEach(function(f) {{
      var sel = filterEls[f.field];
      var cur = sel.value;
      var cmap = counts[f.field] || {{}};
      var vals = Object.keys(cmap).sort(function(a, b) {{
        if (cmap[b] !== cmap[a]) return cmap[b] - cmap[a];
        return a.localeCompare(b);
      }});
      sel.textContent = "";
      var total = 0; vals.forEach(function(v) {{ total += cmap[v]; }});
      var optAll = document.createElement("option");
      optAll.value = ""; optAll.textContent = "All (" + vals.length + ")";
      sel.appendChild(optAll);
      var seen = false;
      vals.forEach(function(v) {{
        var o = document.createElement("option");
        // value stays raw (used for matching); label is prettified for display.
        o.value = v; o.textContent = pretty(v) + " (" + cmap[v] + ")";
        if (v === cur) seen = true;
        sel.appendChild(o);
      }});
      if (cur && !seen) {{  // keep the active selection visible even at count 0
        var o2 = document.createElement("option");
        o2.value = cur; o2.textContent = pretty(cur) + " (0)";
        sel.appendChild(o2);
      }}
      sel.value = cur;
    }});
  }}

  function sortRecords(list, mode) {{
    var key = {{rank: "rank_score", reliability: "reliability_score", directness: "evidence_directness", year: "pub_year"}}[mode] || "rank_score";
    // stable sort descending by numeric key; RECORDS already rank-sorted so
    // "rank" preserves feed order via the index tiebreak.
    return list.map(function(r, i) {{ return [r, i]; }}).sort(function(a, b) {{
      var d = num(b[0][key]) - num(a[0][key]);
      return d !== 0 ? d : a[1] - b[1];
    }}).map(function(x) {{ return x[0]; }});
  }}

  function fmtInt(n) {{ return String(n).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, ","); }}

  // Mount only the first ``visibleCount`` of the full filtered+sorted array in
  // ``lastVisible``. The full array stays in memory so cross-filter counts (which
  // are recomputed over the complete filtered set, independent of the render cap)
  // are unaffected. Idempotent: rebuilds the list DOM from scratch each call.
  function renderVisible() {{
    var total = lastVisible.length;
    var shown = Math.min(visibleCount, total);
    var list = document.getElementById("records-list");
    list.textContent = "";
    var frag = document.createDocumentFragment();
    for (var i = 0; i < shown; i++) {{ frag.appendChild(renderRecord(lastVisible[i])); }}
    if (!total) frag.appendChild(el("div", "empty", "No records match these filters."));
    list.appendChild(frag);

    // Count line uses the FULL filtered total, not just the rendered count.
    var showing = document.getElementById("showing");
    if (total > shown) {{
      showing.textContent = "Showing " + fmtInt(shown) + " of " + fmtInt(total) +
        " matches (refine filters to narrow, or Load more)";
    }} else {{
      showing.textContent = "Showing " + fmtInt(total) + " of " + fmtInt(RECORDS.length) + " records";
    }}
    // Show "Load more" only while some filtered records remain hidden.
    document.getElementById("load-more-wrap").style.display = (total > shown) ? "" : "none";
  }}

  function loadMore() {{
    visibleCount += RENDER_LIMIT;
    renderVisible();
  }}
  window.loadMore = loadMore;

  function applyFilters() {{
    var filters = currentFilters();
    var q = (document.getElementById("q").value || "").trim().toLowerCase();
    // 1) cross-filter counts drive every dropdown (top-priority faceted search).
    //    Computed over the FULL filtered set, so the render cap never affects them.
    refreshDropdowns(crossFilterCounts(filters, q));
    // 2) the full filtered+sorted list applies ALL filters + search
    var visible = RECORDS.filter(function(r) {{ return matches(r, filters, q); }});
    visible = sortRecords(visible, document.getElementById("sort").value);
    lastVisible = visible;
    // Any filter/search/sort change resets the render window to the first page.
    visibleCount = RENDER_LIMIT;
    renderVisible();
  }}

  function resetFilters() {{
    FILTERS.forEach(function(f) {{ filterEls[f.field].value = ""; }});
    document.getElementById("q").value = "";
    applyFilters();
  }}
  window.resetFilters = resetFilters;
  window.applyFilters = applyFilters;

  function renderMolecules() {{
    var grid = document.getElementById("molecules-list");
    grid.textContent = "";
    MOLECULES.forEach(function(m) {{
      var card = el("div", "mol-card");
      card.appendChild(el("h3", null, m.molecule_name || m.molecule_id || "(unnamed)"));
      var stats = el("div", "mol-stats");
      function stat(label, val) {{ if (val && val !== "0") stats.appendChild(el("span", "pill", label + ": " + val)); }}
      stat("records", m.total_records);
      stat("featured", m.auto_published);
      stat("human", m.human_evidence);
      stat("preclinical", m.preclinical_evidence);
      stat("max reliability", m.max_reliability);
      card.appendChild(stats);
      if (m.top_conditions) card.appendChild(el("div", "sl", m.top_conditions));
      card.addEventListener("click", function() {{
        var sel = filterEls["molecule_name"];
        var name = m.molecule_name || "";
        showTab("records");
        resetFilters();
        // ensure the option exists after reset repaints counts
        var has = sel && Array.prototype.some.call(sel.options, function(o) {{ return o.value === name; }});
        if (has) {{ sel.value = name; applyFilters(); }}
      }});
      grid.appendChild(card);
    }});
    document.getElementById("molecules-count").textContent = MOLECULES.length + " molecules";
  }}

  // ---- experimental (candidate) molecules ------------------------------------
  // These are proposals with NO evidence records yet. Every field is trusted
  // config but rendered via textContent to keep the safe pattern uniform.
  var EXP_BANNER = "Experimental candidates \\u2014 proposed molecules not yet in " +
    "the database. They populate once added to the search config and fetched.";
  function renderExperimental() {{
    var grid = document.getElementById("experimental-list");
    grid.textContent = "";
    document.getElementById("exp-banner").textContent = EXP_BANNER;
    EXPERIMENTAL.forEach(function(e) {{
      var card = el("div", "exp-card");
      card.appendChild(el("h3", null, e.display_name || e.molecule_id || "(unnamed)"));
      if (e["class"]) card.appendChild(el("div", "exp-class", e["class"]));
      if (e.rationale) card.appendChild(el("div", "exp-rationale", e.rationale));
      var terms = (e.example_search_terms || "").split(";")
        .map(function(s) {{ return s.trim(); }}).filter(Boolean);
      if (terms.length) {{
        card.appendChild(el("div", "exp-terms-label", "Example search terms"));
        var wrap = el("div", "exp-terms");
        terms.forEach(function(t) {{ wrap.appendChild(el("span", "exp-term", t)); }});
        card.appendChild(wrap);
      }}
      grid.appendChild(card);
    }});
    document.getElementById("experimental-count").textContent =
      EXPERIMENTAL.length + " candidate molecule" + (EXPERIMENTAL.length === 1 ? "" : "s");
    // Reveal the tab only when there is at least one candidate.
    document.getElementById("tab-experimental").style.display =
      EXPERIMENTAL.length ? "" : "none";
  }}

  function showTab(name) {{
    var isRec = name === "records";
    var isExp = name === "experimental";
    var isMol = name === "molecules";
    document.getElementById("records-view").style.display = isRec ? "" : "none";
    document.getElementById("molecules-view").style.display = isMol ? "" : "none";
    document.getElementById("experimental-view").style.display = isExp ? "" : "none";
    document.getElementById("sidebar").style.display = isRec ? "" : "none";
    document.getElementById("tab-records").className = isRec ? "active" : "";
    document.getElementById("tab-molecules").className = isMol ? "active" : "";
    document.getElementById("tab-experimental").className = isExp ? "active" : "";
  }}
  window.showTab = showTab;

  function boot() {{
    buildFilters();
    updateApSummary();
    renderMolecules();
    renderExperimental();
    applyFilters();
  }}

  if (DATA.mode === "fetch") {{
    // Hosted mode: fetch the sibling feed, then boot with real records.
    fetch("site_data.json").then(function(r) {{ return r.json(); }}).then(function(feed) {{
      RECORDS = feed.records || [];
      MOLECULES = (feed.molecules || []).filter(function(m) {{ return true; }});
      // Prefer the feed's experimental list if present; else keep the inlined one.
      if (feed.experimental) EXPERIMENTAL = feed.experimental;
      boot();
    }}).catch(function() {{
      document.getElementById("records-list").appendChild(el("div", "empty",
        "Could not load site_data.json (fetch mode requires it be served alongside this page)."));
      buildFilters(); updateApSummary(); renderExperimental();
    }});
  }} else {{
    boot();
  }}
}})();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the self-contained public dashboard from the curated feed.")
    ap.add_argument("--curated-dir", default="exports/curated")
    ap.add_argument("--out-dir", default="exports/site")
    ap.add_argument("--mode", choices=["inline", "fetch"], default="inline",
                    help="inline: embed record data (opens by double-click). "
                         "fetch: load site_data.json at runtime (for hosted/GitHub Pages).")
    ap.add_argument("--max-inline", type=int, default=4000,
                    help="Cap inlined records by rank in inline mode (0 = no cap).")
    args = ap.parse_args()

    if not os.path.isdir(args.curated_dir):
        print(f"error: curated dir not found: {args.curated_dir}", file=sys.stderr)
        sys.exit(1)

    result = build_site(args.curated_dir, args.out_dir, mode=args.mode, max_inline=args.max_inline)
    kb = result["bytes"] / 1024.0
    print(f"Built site ({result['mode']}) -> {result['path']}")
    print(f"  records  : {result['records']} inlined / {result['total']} total"
          + (f" ({result['truncated']} truncated)" if result["truncated"] else ""))
    print(f"  molecules: {result['molecules']}")
    print(f"  size     : {kb:.1f} KiB ({result['bytes']} bytes)")


if __name__ == "__main__":
    main()
