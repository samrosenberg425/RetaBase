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
    "website_section", "evidence_class", "evidence_class_label", "publication_status",
    "authors_short", "first_author", "author_count", "citation_count",
    "journal_reputation", "journal_tier",
    "reliability_score", "reliability_tier", "evidence_directness", "directness_tier",
    "reliability_components", "rank_components",
    "rank_score", "rank_tier", "appraisal_summary", "appraisal_strengths", "appraisal_limitations",
    "refined_dose", "refined_route", "refined_duration", "refined_sample_size", "refined_outcome_direction",
    "facet_species", "facet_indication", "facet_endpoint", "facet_study_type",
    "facet_model_system", "facet_route",
    "facet_drug_class", "facet_population", "facet_sex", "facet_formulation",
    "facet_evidence_direction",
    # NIH iCite-derived facets: impact tier (from nih_percentile) + clinical-article
    # flag. Carried through so they can be offered as sidebar filters. "" when absent.
    "facet_evidence_impact", "facet_clinical_article",
    "facet_research_article", "facet_translational_compartment",
    # Retraction / correction flags (from PubMed pubtypes) drive a caution badge;
    # facet_publication_flag makes them filterable. Blank/False on ordinary papers.
    "is_retracted", "is_corrected", "facet_publication_flag",
    "facet_all",
    # NIH iCite metrics (merged per-record upstream by build_curated_database.py).
    # Carried through so the UI can sort by impact percentile / translational
    # potential (APT) and offer a clinical-only toggle. Absent -> empty string.
    "icite_nih_percentile", "icite_apt", "icite_is_clinical",
    # More NIH iCite signals: clinical_influence = how many CLINICAL articles cite
    # this paper (sortable + shown as a badge); x/y_coord = the paper's position on
    # iCite's "triangle of biomedicine" (Human / Animal / Molecular-Cellular corners),
    # used by the translational-triangle view. Absent -> empty string.
    "icite_clinical_influence", "icite_x_coord", "icite_y_coord",
    # Remaining iCite values surfaced in the paper detail view. Absent -> "".
    "icite_rcr", "icite_human", "icite_animal", "icite_molecular",
    "icite_field_citation_rate", "icite_citation_count",
]

# Facet dropdown filters shown in the sidebar: (record field, human label).
# facet_* fields are semicolon-joined multi-values; the UI splits on "; ".
FILTER_FACETS = [
    ("molecule_name", "Bioactive"),
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
    ("facet_evidence_impact", "Evidence impact"),
    ("facet_clinical_article", "Clinical article"),
    ("facet_research_article", "Research article"),
    ("facet_translational_compartment", "Translational compartment"),
    ("facet_publication_flag", "Publication flag"),
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
    # Evidence-density tier (literature VOLUME, not quality) + the raw counts it
    # derives from. Drives the honest density badge on the molecule card.
    "record_count", "human_count", "density_tier",
    # Optional PubChem CID (from scripts/enrich_pubchem.py via molecule_index).
    # Drives the "View on PubChem" link on the Bioactives card; "" when unknown.
    "pubchem_cid",
]

# Candidate ("experimental") molecules proposed for future fetching. These carry
# NO evidence records; they are trusted config values but are still rendered via
# textContent to keep the safe pattern uniform.
EXPERIMENTAL_FIELDS = [
    "molecule_id", "display_name", "class", "rationale", "status",
    "example_search_terms",
]

# Clinical-trials registry rows (ClinicalTrials.gov via ``trials_data.json``).
# These are study REGISTRATIONS, not published results, and are rendered on their
# own tab. Every field is treated as hostile and rendered via textContent.
TRIAL_FIELDS = [
    "nct_id", "molecule_id", "molecule_name", "brief_title", "overall_status",
    "phases", "study_type", "conditions", "interventions", "enrollment_count",
    "start_date", "primary_completion_date", "completion_date", "lead_sponsor",
    "has_results", "result_pmids", "reference_pmids", "url", "ongoing",
]

# Preprint rows (bioRxiv/medRxiv via EuropePMC, ``preprints_data.json``). NOT
# peer-reviewed; rendered on their own tab with a prominent caution.
PREPRINT_FIELDS = [
    "id", "molecule_id", "molecule_name", "title", "authors_short",
    "server", "date", "doi", "url",
]

# Corpus-wide summary numbers (``corpus_stats`` inside site_data.json) shown as a
# compact strip near the header. All numeric; formatted with thousands separators.
CORPUS_STATS_FIELDS = [
    "generated_utc", "total_papers", "total_evidence", "molecules_with_data",
    "year_min", "year_max", "pct_citations_filled", "featured", "listed",
    # Data-health coverage percentages (share of curated records with each signal
    # filled). Surfaced as a compact "Data health" line in the corpus strip.
    "pct_with_abstract", "pct_with_doi", "pct_with_icite",
    # Per-molecule feed-cap disclosure ({focus_cap, other_cap, total_public_records,
    # published_records, capped_molecule_count, capped_molecules}); nested dict is
    # preserved verbatim and JSON-serialized for the "top N of M" UI note.
    "feed",
]


@dataclass
class SiteData:
    """Everything the page needs, already normalized to the UI's field set."""

    records: List[Dict[str, str]] = field(default_factory=list)
    molecules: List[Dict[str, str]] = field(default_factory=list)
    experimental: List[Dict[str, str]] = field(default_factory=list)
    trials: List[Dict[str, object]] = field(default_factory=list)
    preprints: List[Dict[str, str]] = field(default_factory=list)
    corpus_stats: Dict[str, object] = field(default_factory=dict)
    trials_generated_utc: str = ""
    preprints_generated_utc: str = ""
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


def _norm_trial(raw: Dict) -> Dict[str, object]:
    """Normalize one trial row to the UI field set.

    All fields are stringified EXCEPT ``ongoing`` which is kept as a real bool so
    the UI's "Ongoing only" toggle can filter on it without string coercion.
    """
    out: Dict[str, object] = {}
    for k in TRIAL_FIELDS:
        v = raw.get(k, "")
        if k == "ongoing":
            out[k] = bool(v)
        else:
            out[k] = "" if v is None else str(v)
    return out


def _norm_preprint(raw: Dict) -> Dict[str, str]:
    """Normalize one preprint row to the UI field set (all strings)."""
    out: Dict[str, str] = {}
    for k in PREPRINT_FIELDS:
        v = raw.get(k, "")
        out[k] = "" if v is None else str(v)
    return out


def _load_feed(curated_dir: str, name: str, list_key: str, normalizer):
    """Load a sibling JSON feed (trials/preprints), tolerant of absence/emptiness.

    Returns ``(rows, generated_utc)``. A missing file, unreadable JSON, or a feed
    with no rows yields ``([], "")`` so the UI simply shows its "no data yet"
    placeholder rather than crashing. The feeds are produced by a separate fetch
    step and may not exist on an initial build.
    """
    path = os.path.join(curated_dir, name)
    if not os.path.exists(path):
        return [], ""
    try:
        with open(path, encoding="utf-8") as fh:
            feed = json.load(fh)
    except (ValueError, OSError):
        return [], ""
    if not isinstance(feed, dict):
        return [], ""
    rows = [normalizer(r) for r in (feed.get(list_key) or []) if isinstance(r, dict)]
    return rows, str(feed.get("generated_utc", "") or "")


def _norm_corpus_stats(raw) -> Dict[str, object]:
    """Normalize the corpus_stats object; non-dict/absent -> empty dict.

    Numeric fields are preserved as-is (ints/floats) for thousands-separator
    formatting in JS; generated_utc stays a string.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, object] = {}
    for k in CORPUS_STATS_FIELDS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
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
    corpus_stats: Dict[str, object] = {}
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as fh:
            feed = json.load(fh)
        records = [_norm_record(r) for r in feed.get("records", [])]
        corpus_stats = _norm_corpus_stats(feed.get("corpus_stats"))
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

    # New sibling feeds: registry trials + preprints. Both are optional (the fetch
    # step may not have run yet) and degrade to empty lists -> UI placeholders.
    trials, trials_gen = _load_feed(
        curated_dir, "trials_data.json", "trials", _norm_trial)
    preprints, preprints_gen = _load_feed(
        curated_dir, "preprints_data.json", "preprints", _norm_preprint)

    return SiteData(records=records, molecules=molecules,
                    experimental=experimental, trials=trials,
                    preprints=preprints, corpus_stats=corpus_stats,
                    trials_generated_utc=trials_gen,
                    preprints_generated_utc=preprints_gen,
                    generated_utc=generated)


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


def _split_vals(rec, fld, multi):
    """Split a record field into a list of discrete values.

    Multi-valued facet fields are ";"-joined; single-valued fields yield a
    one-element list (or empty when blank).
    """
    v = rec.get(fld, "") or ""
    if fld in multi:
        return [s.strip() for s in v.split(";") if s.strip()]
    return [v.strip()] if str(v).strip() else []


def _domain_passes(rec, field, sel, multi):
    """Does ``rec`` pass ONE facet domain's include+exclude selection?

    ``sel`` is a dict ``{"inc": [...], "exc": [...]}`` for the domain:
      * INCLUDE (``inc``): if any include values are chosen, the record must
        carry AT LEAST ONE of them (OR within the domain). Empty include list
        means "no include constraint" (all pass the include test).
      * EXCLUDE (``exc``): if the record carries ANY excluded value it is
        dropped, regardless of includes. Exclude always wins.
    """
    vals = _split_vals(rec, field, multi)
    exc = sel.get("exc") or []
    if exc:
        for v in vals:
            if v in exc:
                return False
    inc = sel.get("inc") or []
    if inc:
        for v in vals:
            if v in inc:
                return True
        return False
    return True


def _year_passes(rec, year_filter):
    """Does ``rec`` pass the pub_year filter?

    ``year_filter`` = {"mode": "before"|"after"|"range"|"exact", "a": int|None, "b": int|None}.
    Records with a blank/unparseable pub_year pass only when no bound applies.
    An unset bound (None) is treated as no constraint.
    """
    if not year_filter or not year_filter.get("mode"):
        return True
    mode = year_filter.get("mode")
    a = year_filter.get("a")
    b = year_filter.get("b")
    try:
        y = int(str(rec.get("pub_year", "")).strip()[:4])
    except (TypeError, ValueError):
        return not (a is not None or b is not None)
    if mode == "before":
        return a is None or y <= a
    if mode == "after":
        return a is None or y >= a
    if mode == "exact":
        return a is None or y == a
    if mode == "range":
        if a is not None and y < a:
            return False
        if b is not None and y > b:
            return False
        return True
    return True


def _record_passes(rec, filters, multi, year_filter=None, journal_sub="",
                   min_citations=0, skip_field=None):
    """Master predicate: does ``rec`` satisfy every active NON-search filter?

    ``filters``      : {field: {"inc": [...], "exc": [...]}} include/exclude per domain.
    ``multi``        : set of ";"-joined multi-value fields.
    ``year_filter``  : see :func:`_year_passes`.
    ``journal_sub``  : case-insensitive substring the journal must contain.
    ``min_citations``: minimum citation_count (0 = no minimum).
    ``skip_field``   : a facet domain to ignore (used for cross-filter counts so
                       a domain never constrains its own option list).

    This is the single source of truth the JS mirrors; it is unit-tested so the
    include/exclude + year/journal/citation semantics stay pinned.
    """
    for field, sel in filters.items():
        if field == skip_field:
            continue
        if not sel:
            continue
        if not _domain_passes(rec, field, sel, multi):
            return False
    if not _year_passes(rec, year_filter):
        return False
    if journal_sub:
        if journal_sub.lower() not in (rec.get("journal", "") or "").lower():
            return False
    if min_citations:
        try:
            if int(str(rec.get("citation_count", "")).strip() or "0") < min_citations:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _cross_filter_counts(records, filters, multi, year_filter=None,
                         journal_sub="", min_citations=0, fields=None):
    """Reference implementation of the include/exclude cross-filter counting.

    For each facet FIELD, the count of a value V is the number of records that
    pass *every OTHER* active domain's include/exclude selection PLUS the
    year/journal/citation filters, AND carry V for FIELD. A facet never
    constrains its own option list, so a user can still add sibling include
    values or lift an exclude. This mirrors the JS ``crossFilterCounts``.

    ``filters`` maps field -> {"inc": [...], "exc": [...]}. Returns
    {field: {value: paper_count}}.
    """
    if fields is None:
        fields = list(filters.keys())
    out = {}
    for fld in fields:
        counts = {}
        for rec in records:
            if not _record_passes(rec, filters, multi, year_filter,
                                  journal_sub, min_citations, skip_field=fld):
                continue
            for v in _split_vals(rec, fld, multi):
                counts[v] = counts.get(v, 0) + 1
        out[fld] = counts
    return out


def build_site(curated_dir: str, out_dir: str, mode: str = "inline",
               max_inline: int = 4000, internal: bool = False) -> Dict[str, int]:
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
        # Registry trials + preprints: small feeds, inlined in inline mode and
        # fetched at runtime in fetch mode (blanked here, filled by _mode below).
        "trials": data.trials,
        "preprints": data.preprints,
        "trials_generated_utc": data.trials_generated_utc,
        "preprints_generated_utc": data.preprints_generated_utc,
        # Corpus-wide summary strip; small + trusted, so inlined in BOTH modes so
        # the header stat line renders without waiting on a sibling fetch.
        "corpus_stats": data.corpus_stats,
        "filters": [{"field": f, "label": lbl} for f, lbl in FILTER_FACETS],
        "multi": sorted(MULTI_VALUE_FIELDS),
        "aspects": [{"field": f, "cls": c, "label": lbl} for f, c, lbl in ASPECT_TAGS],
        "total_records": total_records,
        "truncated": truncated,
        # Public build (default) omits curator approve/reject/notes UI entirely.
        "internal": bool(internal),
    }

    molecule_count = len(data.molecules)

    if mode == "fetch":
        # In fetch mode the page loads site_data.json at runtime; only config
        # (filters/aspects/multi) is inlined, no record bodies.
        cfg = dict(payload)
        cfg["records"] = []
        cfg["molecules"] = []
        # trials/preprints are fetched at runtime like site_data.json (tolerate
        # 404 -> empty). corpus_stats stays inlined (it is tiny, trusted config).
        cfg["trials"] = []
        cfg["preprints"] = []
        cfg["mode"] = "fetch"
        json_block = _safe_json_block(cfg)
        record_count = 0  # fetch mode inlines no record bodies
    else:
        payload["mode"] = "inline"
        json_block = _safe_json_block(payload)
        record_count = len(data.records)

    html_text = _render_html(json_block, record_count, molecule_count,
                             data.generated_utc, total_records, truncated, mode,
                             internal)

    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)

    return {
        "records": record_count,
        "molecules": molecule_count,
        "total": total_records,
        "truncated": truncated,
        "mode": mode,
        "internal": bool(internal),
        "bytes": os.path.getsize(out_path),
        "path": out_path,
    }


def _render_html(json_block: str, record_count: int, molecule_count: int,
                 generated_utc: str, total_records: int, truncated: int,
                 mode: str, internal: bool = False) -> str:
    """Assemble the single-file HTML.

    All dynamic-but-trusted numbers are ints; the only feed-derived content in
    the shell is inside the JSON data block (already neutralized). The JS renders
    every field with ``textContent``, so nothing from the feed is ever parsed as
    HTML at runtime.

    ``internal`` gates the curator approve/reject/notes UI: the default public
    build emits none of that markup; the internal-review build keeps it.
    """
    title = html.escape("RetaBase — Curated Evidence Dashboard")
    gen = html.escape(generated_utc or "unknown")
    note = ""
    if mode == "inline" and truncated:
        note = html.escape(
            f" (top {record_count} of {total_records} by rank inlined; "
            "rebuild with --mode fetch to browse all)"
        )
    subtitle = html.escape(
        f"Transparent, rule-based evidence on retatrutide & related bioactives "
        f"— {total_records} papers across {molecule_count} bioactives, offline & auditable"
    ) + note
    # Curator approval controls (public build omits them entirely).
    export_btn = (
        '\n    <button class="exp" onclick="exportDecisions(\'json\')">Export decisions</button>'
        if internal else ""
    )
    return _TEMPLATE.format(
        title=title,
        subtitle=subtitle,
        generated=gen,
        record_count=record_count,
        molecule_count=molecule_count,
        data_json=json_block,
        export_btn=export_btn,
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
    width: 340px; min-width: 340px; padding: 16px; border-right: 1px solid var(--border);
    background: var(--panel); height: calc(100vh - 118px); overflow-y: auto; position: sticky; top: 0;
    resize: horizontal;
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
  /* Visible keyboard-focus ring for interactive elements (a11y). */
  .card:focus, .card:focus-visible, button:focus, button:focus-visible,
  a:focus, a:focus-visible, .tri-dot:focus, .tri-dot:focus-visible {{
    outline: 2px solid var(--accent); outline-offset: 2px;
  }}
  /* Translational-triangle dots: clickable, with a clear hover state. */
  .tri-dot {{ cursor: pointer; }}
  .tri-dot:hover {{ fill-opacity: 1; stroke: var(--accent); stroke-width: 1.5; }}
  /* Custom hover tooltip for triangle dots (a real text box; the native SVG
     <title> was unreliable). Follows the cursor via inline left/top. */
  .tri-tip {{ position: fixed; z-index: 1000; display: none; max-width: 260px;
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 9px; font-size: 12px; line-height: 1.35;
    text-align: left; pointer-events: none; box-shadow: 0 4px 14px rgba(0,0,0,.4); }}
  .card.ap-approve {{ border-left: 4px solid var(--ap-approve); }}
  .card.ap-reject {{ border-left: 4px solid var(--ap-reject); opacity: .7; }}
  .card h3 {{ margin: 0 0 6px; font-size: 15px; line-height: 1.35; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; color: var(--muted); margin-bottom: 8px; align-items: center; }}
  .pill {{ background: var(--panel2); border: 1px solid var(--border); border-radius: 999px; padding: 2px 9px; }}
  .pill.retracted {{ background: #7f1d1d; border-color: #ef4444; color: #fff; font-weight: 700; letter-spacing: .02em; }}
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
  .mol-density {{ margin-top: 8px; display: inline-block; font-size: 11px; padding: 2px 8px;
    border-radius: 999px; border: 1px solid var(--border); color: var(--muted); cursor: help; }}
  .mol-density.tier-sparse {{ border-color: var(--tier-limited); color: var(--tier-limited); }}
  .mol-density.tier-moderate {{ border-color: var(--border); }}
  .mol-density.tier-saturated {{ border-color: var(--accent); color: var(--accent); }}
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
  /* one-line descriptor under each tab/section */
  .tab-desc {{ font-size: 12px; color: var(--muted); margin: 2px 0 12px; }}
  /* single-molecule "Evidence map": use case x evidence-class count matrix */
  .evmap {{
    margin: 8px 0; padding: 10px 12px; background: var(--panel2);
    border: 1px solid var(--border); border-radius: 8px;
  }}
  .evmap h4 {{ margin: 0 0 3px; font-size: 13px; color: var(--text); }}
  .evmap .evcap {{ font-size: 11px; color: var(--muted); margin: 0 0 8px; font-style: italic; }}
  .evmap-table {{ border-collapse: collapse; font-size: 12px; max-width: 100%; }}
  .evmap-table th, .evmap-table td {{
    border: 1px solid var(--border); padding: 3px 9px; text-align: right; color: var(--text);
  }}
  .evmap-table th {{ color: var(--muted); font-weight: 600; }}
  .evmap-table .use {{ text-align: left; color: var(--text); overflow-wrap: anywhere; }}
  .evmap-table td.zero {{ color: var(--muted); }}
  /* single-molecule "Safety & evidence status": caution panel (NOT a safety
     verdict). Amber border sets it apart from the neutral evidence-map. */
  .safety {{
    margin: 8px 0; padding: 10px 12px; background: var(--panel2);
    border: 1px solid #f59e0b; border-left: 4px solid #f59e0b; border-radius: 8px;
  }}
  .safety h4 {{ margin: 0 0 6px; font-size: 13px; color: var(--text); }}
  .safety .srow {{ font-size: 12px; color: var(--text); margin: 2px 0; }}
  .safety .slabel {{ color: var(--muted); }}
  .safety .sretract {{ font-size: 12px; color: #ef4444; font-weight: 600; margin: 4px 0 2px; }}
  .safety .scaution {{
    margin: 8px 0 0; padding: 6px 0 0; border-top: 1px solid var(--border);
    font-size: 11px; color: var(--muted); font-style: italic;
  }}
  .safety .scaution li {{ margin: 2px 0; }}
  /* corpus-stats summary strip near the header */
  .corpus-strip {{
    display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center;
    font-size: 12px; color: var(--muted); margin-top: 10px;
    padding: 8px 12px; background: var(--panel2); border: 1px solid var(--border);
    border-radius: 8px;
  }}
  .corpus-strip .cs-label {{ text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
  .corpus-strip b {{ color: var(--text); }}
  .corpus-strip .cs-sep {{ color: var(--border); }}
  /* trials + preprints tables/lists */
  .caution-banner {{
    background: var(--panel2); border: 1px solid var(--tier-limited); border-left: 4px solid var(--tier-limited);
    border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; font-size: 13px; color: var(--text);
  }}
  .caution-banner.hard {{ border-color: var(--tier-low); border-left-color: var(--tier-low); }}
  .feed-toolbar {{ display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; margin-bottom: 12px; }}
  .feed-toolbar input, .feed-toolbar select {{
    padding: 6px 9px; background: var(--panel2); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; font-size: 13px;
  }}
  .feed-toolbar input[type=search] {{ min-width: 200px; }}
  .feed-toolbar label {{ display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); cursor: pointer; }}
  .feed-toolbar label input {{ width: auto; }}
  .trial-card, .pp-card {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 16px; margin-bottom: 12px;
  }}
  .trial-card h3, .pp-card h3 {{ margin: 0 0 6px; font-size: 15px; line-height: 1.35; }}
  .trial-card.ongoing {{ border-left: 4px solid var(--tier-high); }}
  .status-badge {{ font-size: 11px; font-weight: 600; border-radius: 999px; padding: 2px 9px; border: 1px solid var(--border); color: var(--muted); }}
  .status-badge.on {{ color: var(--tier-high); border-color: var(--tier-high); }}
  .server-badge {{ font-size: 11px; font-weight: 600; border-radius: 999px; padding: 2px 9px; border: 1px solid var(--t-ind); color: var(--t-ind); text-transform: uppercase; letter-spacing: .03em; }}
  .trial-grid {{ display: grid; grid-template-columns: 130px 1fr; gap: 4px 14px; font-size: 12px; margin: 8px 0; }}
  .trial-grid .k {{ color: var(--muted); }}
  .trial-pubs {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 12px; margin: 6px 0 2px; }}
  .trial-pubs .k {{ color: var(--muted); }}
  /* include/exclude multi-select filter groups */
  .fgroup {{ margin-bottom: 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel2); }}
  .fgroup > summary {{
    cursor: pointer; padding: 7px 10px; font-size: 12px; color: var(--text);
    display: flex; align-items: center; gap: 6px; list-style: none; text-transform: none; letter-spacing: 0;
  }}
  .fgroup > summary::-webkit-details-marker {{ display: none; }}
  .fgroup > summary::before {{ content: "\\25B8"; color: var(--muted); font-size: 10px; }}
  .fgroup[open] > summary::before {{ content: "\\25BE"; }}
  .fgroup .fcount {{ margin-left: auto; font-size: 11px; color: var(--accent); font-weight: 600; }}
  .fgroup .fcount.zero {{ color: var(--muted); font-weight: 400; }}
  .fbody {{ padding: 4px 10px 10px; }}
  .fbody .ftools {{ display: flex; gap: 8px; margin-bottom: 6px; }}
  .fbody .ftools button {{
    font-size: 11px; padding: 2px 8px; border-radius: 4px; cursor: pointer;
    background: var(--panel); color: var(--muted); border: 1px solid var(--border); width: auto;
  }}
  .fbody .ftools button:hover {{ color: var(--text); }}
  .foptions {{ max-height: 190px; overflow-y: auto; }}
  .fopt {{ display: flex; align-items: flex-start; gap: 6px; font-size: 12px; padding: 3px 0; text-transform: none; letter-spacing: 0; }}
  .fopt .fname {{ flex: 1; color: var(--text); white-space: normal; overflow-wrap: anywhere; line-height: 1.3; }}
  .fopt .fchk {{ flex: 0 0 auto; }}
  .fopt .fpapers {{ font-size: 11px; color: var(--muted); }}
  .fopt .fchk {{ display: flex; gap: 3px; }}
  .fopt .fchk label {{ display: inline-flex; align-items: center; gap: 2px; margin: 0; font-size: 10px; text-transform: uppercase; color: var(--muted); cursor: pointer; letter-spacing: .02em; }}
  .fopt .fchk input {{ width: auto; }}
  .fopt .fchk .inc.on {{ color: var(--tier-high); }}
  .fopt .fchk .exc.on {{ color: var(--tier-low); }}
  .yearrow {{ display: flex; gap: 6px; }}
  .yearrow select {{ flex: 0 0 88px; }}
  .yearrow input {{ flex: 1; }}
  /* About / Methods page */
  .about {{ max-width: 820px; font-size: 14px; line-height: 1.6; }}
  .about h2 {{ font-size: 20px; margin: 4px 0 6px; }}
  .about h3 {{ font-size: 15px; margin: 20px 0 6px; color: var(--accent2); }}
  .about p {{ color: var(--text); margin: 8px 0; }}
  .about ul {{ margin: 6px 0; padding-left: 20px; }}
  .about li {{ margin: 4px 0; }}
  .about code {{ font-size: 12px; }}
  .about .formula {{ background: var(--panel2); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; font-size: 13px; margin: 10px 0; }}
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
  <p class="gen">Generated {generated} &middot; {record_count} papers inlined &middot; {molecule_count} bioactives</p>
  <details class="explainer">
    <summary>How to read this</summary>
    <ul>
      <li><b>Automated rigor</b> = rule-based signals of how well-conducted the study is <i>for its type</i> (within-class study quality, 0-100). Not a formal risk-of-bias assessment.</li>
      <li><b>Directness</b> = how directly the evidence applies to humans (human RCT high &rarr; in-vitro low).</li>
      <li><b>Rank</b> = the combined best-first ordering (directness + quality + relevance + recency + impact + venue).</li>
      <li>Open <b>About / Methods</b> for the exact formulas. Every metric is rule-based and auditable.</li>
    </ul>
  </details>
  <div class="corpus-strip" id="corpus-strip" style="display:none"></div>
  <div class="tabs">
    <button id="tab-evidence" class="active" onclick="showTab('evidence')">Evidence</button>
    <button id="tab-clinical" onclick="showTab('clinical')">Clinical evidence</button>
    <button id="tab-trials" onclick="showTab('trials')">Trials registry</button>
    <button id="tab-preprints" onclick="showTab('preprints')">Preprints</button>
    <button id="tab-molecules" onclick="showTab('molecules')">Bioactives</button>
    <button id="tab-experimental" style="display:none" onclick="showTab('experimental')">Experimental</button>
    <button id="tab-about" onclick="showTab('about')">About / Methods</button>
    <span class="spacer"></span>
    <span class="ap-summary" id="ap-summary"></span>{export_btn}
  </div>
</header>
<main>
  <aside id="sidebar">
    <div class="fg">
      <label for="q">Search</label>
      <input id="q" type="search" placeholder="title, bioactive, facets, summary..." oninput="qDebounced()">
    </div>
    <div class="fg">
      <label>Year (publication)</label>
      <div class="yearrow">
        <select id="year-mode" onchange="applyFilters()">
          <option value="">Any</option>
          <option value="after">After</option>
          <option value="before">Before</option>
          <option value="exact">Exact</option>
          <option value="range">Range</option>
        </select>
        <input id="year-a" type="number" placeholder="year" oninput="applyFilters()">
        <input id="year-b" type="number" placeholder="to" oninput="applyFilters()" style="display:none">
      </div>
    </div>
    <div class="fg">
      <label for="journal-sub">Journal name includes</label>
      <input id="journal-sub" type="search" placeholder="type part of a journal, e.g. Lancet" oninput="applyFilters()">
      <div class="note-hint">Text match on the journal name &mdash; a partial word works (e.g. &ldquo;diabetes&rdquo;).</div>
    </div>
    <div class="fg">
      <label for="min-cit">Min times cited</label>
      <input id="min-cit" type="number" placeholder="0" min="0" oninput="applyFilters()">
      <div class="note-hint">How often the paper has been cited by others (via OpenAlex).</div>
    </div>
    <div id="facet-filters"></div>
    <button class="reset" onclick="resetFilters()">Reset filters</button>
  </aside>
  <section class="content">
    <div id="browser-view">
      <div class="tab-desc" id="browser-desc"></div>
      <div class="count" id="records-count">
        <span id="showing" aria-live="polite"></span>
        <label style="text-transform:none;display:inline-flex;gap:6px;align-items:center;color:var(--muted)">Sort
          <select id="sort" onchange="applyFilters()">
            <option value="rank">Rank (best first)</option>
            <option value="reliability">Automated rigor</option>
            <option value="directness">Directness</option>
            <option value="citations">Times cited (most)</option>
            <option value="year">Year (newest)</option>
            <option value="percentile">Impact percentile</option>
            <option value="apt">Translational potential (APT)</option>
            <option value="clinical_influence">Clinical influence</option>
          </select>
        </label>
        <label style="text-transform:none;display:inline-flex;gap:6px;align-items:center;color:var(--muted)">View
          <select id="rank-preset" onchange="applyFilters()">
            <option value="default">Default (blended rank)</option>
            <option value="clinical">Clinical answer</option>
            <option value="synthesis">Best synthesis</option>
            <option value="landmark">Landmark</option>
            <option value="latest">Latest</option>
            <option value="mechanism">Mechanism</option>
          </select>
        </label>
        <label style="text-transform:none;display:inline-flex;gap:6px;align-items:center;color:var(--muted)">
          <input id="clinical-only" type="checkbox" onchange="applyFilters()" style="width:auto"> Clinical articles only
        </label>
        <button id="triangle-toggle" class="reset" style="width:auto;padding:4px 10px" onclick="toggleTriangle()">Triangle view</button>
      </div>
      <div class="tab-desc" id="cap-note" style="display:none"></div>
      <div class="safety" id="safety-panel" style="display:none"></div>
      <div class="evmap" id="evidence-map" style="display:none"></div>
      <div id="triangle-wrap" style="display:none;margin:8px 0;text-align:center">
        <svg id="triangle-svg" viewBox="0 0 300 260" width="300" height="260" role="img" aria-label="Translational triangle"></svg>
      </div>
      <div id="records-list"></div>
      <div id="load-more-wrap" style="text-align:center;margin:8px 0 24px;display:none">
        <button id="load-more" class="reset" style="width:auto;padding:8px 20px" onclick="loadMore()">Load more</button>
      </div>
    </div>
    <div id="molecules-view" style="display:none">
      <div class="tab-desc">Bioactives &mdash; peptides, small molecules &amp; related compounds. Click one to see its papers.</div>
      <div class="count" id="molecules-count"></div>
      <div class="mol-grid" id="molecules-list"></div>
    </div>
    <div id="experimental-view" style="display:none">
      <div class="tab-desc">Experimental &mdash; candidate compounds proposed for future indexing (no papers yet).</div>
      <div class="exp-banner" id="exp-banner"></div>
      <div class="count" id="experimental-count"></div>
      <div class="mol-grid" id="experimental-list"></div>
    </div>
    <div id="trials-view" style="display:none">
      <div class="tab-desc">Trials registry &mdash; ongoing &amp; completed studies from ClinicalTrials.gov &mdash; study registrations, not published results.</div>
      <div class="caution-banner" id="trials-note"></div>
      <div class="feed-toolbar" id="trials-toolbar" style="display:none">
        <input id="trials-q" type="search" placeholder="search title / conditions..." oninput="renderTrials()">
        <select id="trials-mol" onchange="renderTrials()"><option value="">All bioactives</option></select>
        <select id="trials-sort" onchange="renderTrials()">
          <option value="ongoing">Ongoing first</option>
          <option value="start">Start date (newest)</option>
          <option value="start-asc">Start date (oldest)</option>
        </select>
        <input id="trials-year" type="number" placeholder="year" min="1990" max="2035" style="width:5.5em" oninput="renderTrials()">
        <label><input id="trials-ongoing" type="checkbox" onchange="renderTrials()"> Ongoing only</label>
      </div>
      <div class="count" id="trials-count"></div>
      <div id="trials-list"></div>
    </div>
    <div id="preprints-view" style="display:none">
      <div class="tab-desc">Preprints (bioRxiv/medRxiv) &mdash; NOT peer-reviewed; interpret with caution.</div>
      <div class="caution-banner hard" id="preprints-note"></div>
      <div class="feed-toolbar" id="preprints-toolbar" style="display:none">
        <input id="pp-q" type="search" placeholder="search title / authors..." oninput="renderPreprints()">
        <select id="pp-mol" onchange="renderPreprints()"><option value="">All bioactives</option></select>
        <select id="pp-sort" onchange="renderPreprints()">
          <option value="date">Date (newest)</option>
          <option value="date-asc">Date (oldest)</option>
        </select>
        <input id="pp-year" type="number" placeholder="year" min="1990" max="2035" style="width:5.5em" oninput="renderPreprints()">
      </div>
      <div class="count" id="preprints-count"></div>
      <div id="preprints-list"></div>
    </div>
    <div id="about-view" style="display:none">
      <div class="about" id="about-body"></div>
    </div>
  </section>
</main>

<div class="modal-bg" id="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal" role="dialog" aria-modal="true"></div>
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
  var TRIALS = DATA.trials || [];
  var PREPRINTS = DATA.preprints || [];
  var CORPUS = DATA.corpus_stats || {{}};
  var FILTERS = DATA.filters || [];
  var MULTI = new Set(DATA.multi || []);
  var ASPECTS = DATA.aspects || [];
  var PUBMED = "https://pubmed.ncbi.nlm.nih.gov/";
  var PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/compound/";
  var INTERNAL = !!DATA.internal;  // curator approval UI only in the internal build
  var DECISIONS = {{}};  // rid -> {{status, note}} (in-memory only, never persisted)
  // Which top-level view is active: "evidence" (all) or "clinical" (human only).
  // Both reuse the SAME browser render + filter code; only the base set differs.
  var currentView = "evidence";
  // Human-only definition for the Clinical evidence tab. A record is "human"
  // evidence if its evidence_class is one of these OR its website_section is a
  // human/review section. Kept in one place so the definition is auditable.
  var HUMAN_CLASSES = new Set([
    "human_clinical_controlled", "human_clinical", "human_observational", "evidence_synthesis"
  ]);
  var HUMAN_SECTIONS = new Set(["Human evidence", "Reviews and overviews"]);
  function isHuman(rec) {{
    return HUMAN_CLASSES.has(rec.evidence_class || "") ||
           HUMAN_SECTIONS.has(rec.website_section || "");
  }}
  // The base set the browser filters over, driven by the active tab.
  function baseRecords() {{
    return currentView === "clinical" ? RECORDS.filter(isHuman) : RECORDS;
  }}
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

  // Citation display: blank or 0 (unknown) renders as an em dash.
  function citationText(rec) {{
    var c = parseInt(String(rec.citation_count || "").trim() || "0", 10);
    return (isNaN(c) || c <= 0) ? "\\u2014" : String(c);
  }}

  // NIH iCite clinical influence: how many CLINICAL articles cite this paper.
  // Blank / unparseable / <= 0 -> 0 (nothing rendered).
  function clinicalInfluence(rec) {{
    var c = parseInt(String(rec.icite_clinical_influence || "").trim() || "0", 10);
    return (isNaN(c) || c <= 0) ? 0 : c;
  }}

  function tierClass(t) {{ return (t || "").replace(/[^a-z_]/gi, "") || "not_applicable"; }}

  // ---- include/exclude filter predicate --------------------------------------
  // Each facet domain carries {{inc:[...], exc:[...]}}. INCLUDE is OR-within-domain
  // (record must carry at least one chosen include value, if any are chosen);
  // EXCLUDE drops a record that carries any excluded value. Across domains the
  // domains AND together. This mirrors _record_passes / _domain_passes in Python.
  function domainPasses(rec, field, sel) {{
    var vals = splitVals(rec, field);
    var exc = sel.exc || [], inc = sel.inc || [];
    if (exc.length) {{ for (var i = 0; i < vals.length; i++) if (exc.indexOf(vals[i]) !== -1) return false; }}
    if (inc.length) {{
      for (var j = 0; j < vals.length; j++) if (inc.indexOf(vals[j]) !== -1) return true;
      return false;
    }}
    return true;
  }}
  function yearPasses(rec, yf) {{
    if (!yf || !yf.mode) return true;
    var y = parseInt(String(rec.pub_year || "").trim().slice(0, 4), 10);
    if (isNaN(y)) return !(yf.a != null || yf.b != null);
    if (yf.mode === "before") return yf.a == null || y <= yf.a;
    if (yf.mode === "after") return yf.a == null || y >= yf.a;
    if (yf.mode === "exact") return yf.a == null || y === yf.a;
    if (yf.mode === "range") {{
      if (yf.a != null && y < yf.a) return false;
      if (yf.b != null && y > yf.b) return false;
      return true;
    }}
    return true;
  }}
  // iCite "clinical article" flag can arrive as the string "Yes", the number 1,
  // or a boolean true (depending on how the feed serialized it) -> normalize all.
  function isClinical(rec) {{
    var v = rec.icite_is_clinical;
    if (v === true || v === 1) return true;
    var s = String(v == null ? "" : v).trim().toLowerCase();
    return s === "yes" || s === "y" || s === "true" || s === "1";
  }}
  // Passes every active NON-search filter; skipField lets a facet ignore itself
  // during cross-filter counting.
  function recordPasses(rec, filters, extra, skipField) {{
    for (var field in filters) {{
      if (field === skipField) continue;
      var sel = filters[field];
      if (!sel || (!(sel.inc && sel.inc.length) && !(sel.exc && sel.exc.length))) continue;
      if (!domainPasses(rec, field, sel)) return false;
    }}
    if (!yearPasses(rec, extra.year)) return false;
    if (extra.journalSub && (rec.journal || "").toLowerCase().indexOf(extra.journalSub) === -1) return false;
    if (extra.minCit) {{
      var c = parseInt(String(rec.citation_count || "").trim() || "0", 10);
      if (isNaN(c) || c < extra.minCit) return false;
    }}
    if (extra.clinicalOnly && !isClinical(rec)) return false;
    return true;
  }}

  // ---- cross-filter facet counting -------------------------------------------
  // For each facet field, a value's count (a PAPER count) = number of records in
  // the active base set that pass EVERY OTHER domain's include/exclude PLUS the
  // year/journal/citation filters and the search term, and carry that value. A
  // facet never constrains its own option list, so users can still add sibling
  // include values. Mirrors _cross_filter_counts in the Python module.
  function crossFilterCounts(base, filters, extra, q) {{
    var out = {{}};
    FILTERS.forEach(function(f) {{ out[f.field] = {{}}; }});
    base.forEach(function(rec) {{
      if (q && !matchesQuery(rec, q)) return;
      FILTERS.forEach(function(f) {{
        if (!recordPasses(rec, filters, extra, f.field)) return;
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

  function matches(rec, filters, extra, q) {{
    if (!recordPasses(rec, filters, extra, null)) return false;
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

  // Retraction flag, tolerant of the value's serialized shape (Python bool ->
  // "True"/"False" string, JSON bool, or "1"/"yes"). Blank/False -> not retracted.
  function isRetracted(r) {{
    var v = r && r.is_retracted;
    if (v === true) return true;
    var s = String(v == null ? "" : v).trim().toLowerCase();
    return s === "true" || s === "1" || s === "yes";
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

  // Clicking an aspect tag adds that value to its domain's INCLUDE set (if the
  // domain has a filter group); otherwise it falls back to the search box.
  function applyTagFilter(field, value) {{
    if (SELECT[field]) {{
      var inc = SELECT[field].inc;
      if (inc.indexOf(value) === -1) inc.push(value);
      // clear any conflicting exclude of the same value
      var ei = SELECT[field].exc.indexOf(value);
      if (ei !== -1) SELECT[field].exc.splice(ei, 1);
      applyFilters();
      return;
    }}
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
    // Prominent caution badge first so a retracted paper is unmistakable.
    if (isRetracted(r)) meta.appendChild(el("span", "pill retracted", "\\u26a0 RETRACTED"));
    if (r.molecule_name) meta.appendChild(el("span", "pill", r.molecule_name));
    if (r.pub_year) meta.appendChild(el("span", "pill", r.pub_year));
    if (r.evidence_class_label) meta.appendChild(el("span", "pill", r.evidence_class_label));
    // iCite preview pills (present once the corpus is iCite-enriched).
    if (r.icite_rcr !== undefined && String(r.icite_rcr).trim() !== "" && !isNaN(parseFloat(r.icite_rcr)))
      meta.appendChild(el("span", "pill", "RCR " + parseFloat(r.icite_rcr).toFixed(1)));
    if (r.icite_nih_percentile !== undefined && String(r.icite_nih_percentile).trim() !== "" && !isNaN(parseFloat(r.icite_nih_percentile)))
      meta.appendChild(el("span", "pill", Math.round(parseFloat(r.icite_nih_percentile)) + "th pct"));
    var iclp = String(r.icite_is_clinical || "").trim().toLowerCase();
    if (iclp === "yes" || iclp === "y" || iclp === "1" || iclp === "true")
      meta.appendChild(el("span", "pill", "clinical"));
    if (r.journal) {{
      var jp = el("span", "pill", r.journal);
      var jt = journalTierBadge(r);
      if (jt) jp.appendChild(jt);
      meta.appendChild(jp);
    }}
    meta.appendChild(reliabilityMeter(r));
    meta.appendChild(directnessBadge(r));
    meta.appendChild(el("span", "pill", "Cited by " + citationText(r)));
    var ci = clinicalInfluence(r);
    if (ci > 0) {{
      meta.appendChild(el("span", "pill", "Cited by " + ci + " clinical article" + (ci === 1 ? "" : "s")));
    }}
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

    if (INTERNAL) card.appendChild(approvalRow(r, card));
    // Keyboard-operable card: behaves like a button that opens the detail modal.
    card.setAttribute("tabindex", "0");
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", r.title || "(untitled)");
    card.addEventListener("click", function() {{ openModal(r); }});
    card.addEventListener("keydown", function(e) {{
      if (e.key === "Enter" || e.key === " ") {{ e.preventDefault(); openModal(r); }}
    }});
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
  var modalOpener = null;  // element to restore focus to when the modal closes
  function openModal(r) {{
    modalOpener = document.activeElement;  // remember the opener for focus restore
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
    // Prominent caution badge first so a retracted paper is unmistakable.
    if (isRetracted(r)) mrow.appendChild(el("span", "pill retracted", "\\u26a0 RETRACTED"));
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
    kv(grid, "Cited by", citationText(r));
    var mci = clinicalInfluence(r);
    if (mci > 0) kv(grid, "Clinical influence", mci + " clinical article" + (mci === 1 ? "" : "s") + " citing");
    kv(grid, "Year", r.pub_year);
    kv(grid, "Evidence class", r.evidence_class_label);
    kv(grid, "Website section", r.website_section);
    kv(grid, "Publication", r.publication_status);
    kv(grid, "Dose", r.refined_dose);
    kv(grid, "Route", r.refined_route);
    kv(grid, "Duration", r.refined_duration);
    kv(grid, "Sample size", r.refined_sample_size);
    kv(grid, "Outcome", humanize(r.refined_outcome_direction));
    kv(grid, "Formal risk of bias", "not assessed (automated rigor signals only)");
    // NIH iCite metrics (fill in once the corpus is iCite-enriched; each row is
    // shown only when its value is present).
    if (r.icite_rcr !== undefined && String(r.icite_rcr).trim() !== "" && !isNaN(parseFloat(r.icite_rcr)))
      kv(grid, "Relative Citation Ratio (RCR)", parseFloat(r.icite_rcr).toFixed(2) + " (1.0 = field median)");
    if (r.icite_nih_percentile !== undefined && String(r.icite_nih_percentile).trim() !== "" && !isNaN(parseFloat(r.icite_nih_percentile)))
      kv(grid, "NIH percentile", Math.round(parseFloat(r.icite_nih_percentile)) + "");
    if (r.icite_apt !== undefined && String(r.icite_apt).trim() !== "" && !isNaN(parseFloat(r.icite_apt)))
      kv(grid, "Translational potential (APT)", parseFloat(r.icite_apt).toFixed(2) + " / 1.0");
    if (r.icite_field_citation_rate !== undefined && String(r.icite_field_citation_rate).trim() !== "" && !isNaN(parseFloat(r.icite_field_citation_rate)))
      kv(grid, "Field citation rate", parseFloat(r.icite_field_citation_rate).toFixed(2));
    if (r.icite_citation_count !== undefined && String(r.icite_citation_count).trim() !== "")
      kv(grid, "iCite citations", String(r.icite_citation_count));
    var icl = String(r.icite_is_clinical || "").trim().toLowerCase();
    if (icl !== "")
      kv(grid, "Clinical article", (icl === "yes" || icl === "y" || icl === "1" || icl === "true") ? "yes" : "no");
    var th = parseFloat(r.icite_human), ta = parseFloat(r.icite_animal), tm = parseFloat(r.icite_molecular);
    if (!isNaN(th) || !isNaN(ta) || !isNaN(tm))
      kv(grid, "Biomedicine triangle",
         "Human " + Math.round((th || 0) * 100) + "% / Animal " + Math.round((ta || 0) * 100)
         + "% / Molecular " + Math.round((tm || 0) * 100) + "%");
    m.appendChild(grid);

    var links = el("div", "links"); links.style.marginTop = "12px";
    if (r.pmid) {{ var a = el("a", null, "PubMed"); a.href = PUBMED + encodeURIComponent(r.pmid) + "/"; a.target = "_blank"; a.rel = "noopener noreferrer"; links.appendChild(a); }}
    if (r.doi) {{ var d = el("a", null, "DOI"); d.href = "https://doi.org/" + encodeURIComponent(r.doi); d.target = "_blank"; d.rel = "noopener noreferrer"; links.appendChild(d); }}
    if (links.childNodes.length) m.appendChild(links);

    var rc = parseComp(r.reliability_components);
    if (rc) {{
      m.appendChild(el("h4", null, "Automated rigor breakdown"));
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
    if (INTERNAL) {{
      m.appendChild(el("div", "note-hint",
        "Notes are for curators: record why you approved/rejected this record " +
        "(e.g. \\u201cwrong molecule role\\u201d, \\u201coff-topic\\u201d); exported with your decisions."));
      m.appendChild(approvalRow(r, el("div")));  // detached card ref; buttons still work
    }}

    document.getElementById("modal-bg").className = "modal-bg open";
    // Move focus into the dialog so keyboard users land inside it.
    close.focus();
  }}
  function closeModal() {{
    document.getElementById("modal-bg").className = "modal-bg";
    // Restore focus to whatever opened the modal (card, dot, etc.).
    if (modalOpener && typeof modalOpener.focus === "function") modalOpener.focus();
    modalOpener = null;
  }}
  window.closeModal = closeModal;
  document.addEventListener("keydown", function(e) {{ if (e.key === "Escape") closeModal(); }});

  // ---- include/exclude multi-select filter groups ----------------------------
  // SELECT[field] = {{inc:[...values], exc:[...values]}} holds the live selection.
  // Each domain is a collapsible <details> group. Options are checkbox rows with
  // an INCLUDE and an EXCLUDE box; each option shows a PAPER count. A "Select all
  // / Clear" tool row toggles all currently-listed includes.
  var SELECT = {{}};
  var groupEls = {{}};  // field -> {{body, count, options}}
  var fc = document.getElementById("facet-filters");
  function buildFilters() {{
    FILTERS.forEach(function(f) {{
      SELECT[f.field] = {{inc: [], exc: []}};
      var grp = document.createElement("details");
      grp.className = "fgroup";
      var sum = document.createElement("summary");
      sum.appendChild(document.createTextNode(f.label));
      var cnt = el("span", "fcount zero", "");
      sum.appendChild(cnt);
      grp.appendChild(sum);
      var body = el("div", "fbody");
      var tools = el("div", "ftools");
      var allBtn = el("button", null, "Select all");
      var clrBtn = el("button", null, "Clear");
      allBtn.addEventListener("click", function(e) {{
        e.preventDefault();
        // include every value currently listed for this domain (the cross-filtered set)
        (groupEls[f.field].values || []).forEach(function(v) {{
          if (SELECT[f.field].inc.indexOf(v) === -1) SELECT[f.field].inc.push(v);
          var ei = SELECT[f.field].exc.indexOf(v); if (ei !== -1) SELECT[f.field].exc.splice(ei, 1);
        }});
        applyFilters();
      }});
      clrBtn.addEventListener("click", function(e) {{
        e.preventDefault();
        SELECT[f.field].inc = []; SELECT[f.field].exc = [];
        applyFilters();
      }});
      tools.appendChild(allBtn); tools.appendChild(clrBtn);
      body.appendChild(tools);
      var opts = el("div", "foptions");
      body.appendChild(opts);
      grp.appendChild(body);
      fc.appendChild(grp);
      groupEls[f.field] = {{group: grp, count: cnt, options: opts, values: []}};
    }});
  }}

  function currentFilters() {{ return SELECT; }}

  function currentExtra() {{
    var ym = document.getElementById("year-mode").value;
    var ya = parseInt(document.getElementById("year-a").value, 10);
    var yb = parseInt(document.getElementById("year-b").value, 10);
    var mc = parseInt(document.getElementById("min-cit").value, 10);
    return {{
      year: ym ? {{mode: ym, a: isNaN(ya) ? null : ya, b: isNaN(yb) ? null : yb}} : null,
      journalSub: (document.getElementById("journal-sub").value || "").trim().toLowerCase(),
      minCit: isNaN(mc) ? 0 : mc,
      clinicalOnly: !!(document.getElementById("clinical-only") && document.getElementById("clinical-only").checked)
    }};
  }}

  // Rebuild each group's option rows with cross-filtered PAPER counts. A domain
  // shows every value present under all OTHER filters; already-selected values
  // are always shown (even at count 0) so a user can lift them. The group's
  // summary carries a badge with the number of active include+exclude picks.
  function refreshGroups(counts) {{
    FILTERS.forEach(function(f) {{
      var g = groupEls[f.field];
      var sel = SELECT[f.field];
      var cmap = counts[f.field] || {{}};
      var valSet = {{}};
      Object.keys(cmap).forEach(function(v) {{ valSet[v] = true; }});
      sel.inc.forEach(function(v) {{ valSet[v] = true; }});
      sel.exc.forEach(function(v) {{ valSet[v] = true; }});
      var vals = Object.keys(valSet).sort(function(a, b) {{
        var ca = cmap[a] || 0, cb = cmap[b] || 0;
        if (cb !== ca) return cb - ca;
        return a.localeCompare(b);
      }});
      g.values = vals;
      g.options.textContent = "";
      vals.forEach(function(v) {{
        var row = el("div", "fopt");
        var name = el("span", "fname", pretty(v)); name.title = pretty(v);
        var papers = el("span", "fpapers", (cmap[v] || 0) + " papers");
        var chk = el("span", "fchk");
        var incWrap = el("label", "inc" + (sel.inc.indexOf(v) !== -1 ? " on" : ""));
        var incBox = el("input"); incBox.type = "checkbox"; incBox.checked = sel.inc.indexOf(v) !== -1;
        incWrap.appendChild(incBox); incWrap.appendChild(document.createTextNode("inc"));
        var excWrap = el("label", "exc" + (sel.exc.indexOf(v) !== -1 ? " on" : ""));
        var excBox = el("input"); excBox.type = "checkbox"; excBox.checked = sel.exc.indexOf(v) !== -1;
        excWrap.appendChild(excBox); excWrap.appendChild(document.createTextNode("exc"));
        incBox.addEventListener("change", function() {{
          toggle(sel.inc, v, incBox.checked);
          if (incBox.checked) toggle(sel.exc, v, false);  // inc + exc mutually exclusive
          applyFilters();
        }});
        excBox.addEventListener("change", function() {{
          toggle(sel.exc, v, excBox.checked);
          if (excBox.checked) toggle(sel.inc, v, false);
          applyFilters();
        }});
        chk.appendChild(incWrap); chk.appendChild(excWrap);
        row.appendChild(name); row.appendChild(papers); row.appendChild(chk);
        g.options.appendChild(row);
      }});
      var active = sel.inc.length + sel.exc.length;
      g.count.textContent = active ? (active + " active") : (vals.length + " options");
      g.count.className = "fcount" + (active ? "" : " zero");
    }});
  }}

  function toggle(arr, v, on) {{
    var i = arr.indexOf(v);
    if (on && i === -1) arr.push(v);
    else if (!on && i !== -1) arr.splice(i, 1);
  }}

  function sortRecords(list, mode) {{
    var key = {{rank: "rank_score", reliability: "reliability_score", directness: "evidence_directness",
                citations: "citation_count", year: "pub_year",
                percentile: "icite_nih_percentile", apt: "icite_apt",
                clinical_influence: "icite_clinical_influence"}}[mode] || "rank_score";
    // iCite sorts treat a missing/blank value as -1 so unscored papers sink below
    // scored ones; the other sorts keep the existing num() (missing -> 0) behavior.
    var missNeg = (mode === "percentile" || mode === "apt" || mode === "clinical_influence");
    function sortVal(r) {{
      var raw = r[key];
      if (raw == null || String(raw).trim() === "") return missNeg ? -1 : 0;
      var n = parseFloat(raw);
      return isNaN(n) ? (missNeg ? -1 : 0) : n;
    }}
    // stable sort descending by numeric key; RECORDS already rank-sorted so
    // "rank" preserves feed order via the index tiebreak.
    return list.map(function(r, i) {{ return [r, i]; }}).sort(function(a, b) {{
      var d = sortVal(b[0]) - sortVal(a[0]);
      return d !== 0 ? d : a[1] - b[1];
    }}).map(function(x) {{ return x[0]; }});
  }}

  // ---- ranking presets -------------------------------------------------------
  // A non-default "View" preset OVERRIDES the Sort dropdown and re-orders the
  // currently-filtered records by an explicit, auditable comparator built from
  // existing record fields. Every comparator treats a missing field as sorting
  // last (numeric fields fall back to -1), and the original feed index is used
  // as a stable tiebreak so ties keep their best-first rank_score order.
  var MECHANISM_CLASSES = new Set(["in_vitro", "preclinical_invivo", "methods_tool"]);
  function pnum(v) {{
    if (v == null || String(v).trim() === "") return -1;
    var n = parseFloat(v);
    return isNaN(n) ? -1 : n;
  }}
  // "Clinical answer" front-loads human evidence: any human evidence_class OR a
  // record whose directness_tier is already "high".
  function isClinicalAnswer(r) {{
    return HUMAN_CLASSES.has(r.evidence_class || "") || r.directness_tier === "high";
  }}
  function presetSort(list, preset) {{
    function byRel(a, b) {{ return pnum(b.reliability_score) - pnum(a.reliability_score); }}
    function byYear(a, b) {{ return pnum(b.pub_year) - pnum(a.pub_year); }}
    var cmp;
    if (preset === "clinical") {{
      cmp = function(a, b) {{
        var d = (isClinicalAnswer(b) ? 1 : 0) - (isClinicalAnswer(a) ? 1 : 0);
        if (d) return d;
        d = byRel(a, b); if (d) return d;
        return byYear(a, b);
      }};
    }} else if (preset === "synthesis") {{
      cmp = function(a, b) {{
        var d = ((b.evidence_class === "evidence_synthesis") ? 1 : 0)
              - ((a.evidence_class === "evidence_synthesis") ? 1 : 0);
        if (d) return d;
        d = byRel(a, b); if (d) return d;
        return byYear(a, b);
      }};
    }} else if (preset === "landmark") {{
      // Impact-driven, NOT recency: NIH percentile, then RCR, then raw citations.
      cmp = function(a, b) {{
        var d = pnum(b.icite_nih_percentile) - pnum(a.icite_nih_percentile);
        if (d) return d;
        d = pnum(b.icite_rcr) - pnum(a.icite_rcr);
        if (d) return d;
        return pnum(b.citation_count) - pnum(a.citation_count);
      }};
    }} else if (preset === "latest") {{
      cmp = function(a, b) {{
        var d = byYear(a, b);
        if (d) return d;
        return ((b.directness_tier === "high") ? 1 : 0) - ((a.directness_tier === "high") ? 1 : 0);
      }};
    }} else if (preset === "mechanism") {{
      cmp = function(a, b) {{
        var d = (MECHANISM_CLASSES.has(b.evidence_class || "") ? 1 : 0)
              - (MECHANISM_CLASSES.has(a.evidence_class || "") ? 1 : 0);
        if (d) return d;
        d = byRel(a, b); if (d) return d;
        return byYear(a, b);
      }};
    }} else {{
      return list;  // "default" (or unknown) -> caller uses the Sort dropdown
    }}
    return list.map(function(r, i) {{ return [r, i]; }}).sort(function(a, b) {{
      var d = cmp(a[0], b[0]);
      return d !== 0 ? d : a[1] - b[1];
    }}).map(function(x) {{ return x[0]; }});
  }}

  function fmtInt(n) {{ return String(n).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, ","); }}

  // Mount only the first ``visibleCount`` of the full filtered+sorted array in
  // ``lastVisible``. The full array stays in memory so cross-filter counts (which
  // are recomputed over the complete filtered set, independent of the render cap)
  // are unaffected. Idempotent: rebuilds the list DOM from scratch each call.
  var lastBaseTotal = 0;  // records in the active view (Evidence or Clinical) before filters
  // When the browser is filtered down to a single molecule that was capped in the
  // published feed, return its {{total, published, ...}} stats so the UI can show a
  // "top N of M papers" note. Detection: every filtered record shares one
  // molecule_id AND that id is listed in CORPUS.feed.capped_molecules. Else null.
  function singleCappedMolecule() {{
    var feed = CORPUS && CORPUS.feed;
    if (!feed || !feed.capped_molecules) return null;
    var id = null;
    for (var i = 0; i < lastVisible.length; i++) {{
      var m = lastVisible[i].molecule_id || "";
      if (id === null) id = m;
      else if (m !== id) return null;  // more than one molecule in view
    }}
    if (!id) return null;
    var cm = feed.capped_molecules[id];
    return (cm && cm.total && cm.published) ? cm : null;
  }}
  // Evidence-map: when the Evidence browser is filtered to exactly ONE molecule,
  // show a use-case x evidence-class COUNT matrix (a map, NOT an efficacy verdict).
  // Detection reuses the same "all filtered records share one molecule_id" logic
  // the cap-note / triangle rely on. Returns the shared molecule_id, or null when
  // the view is empty or spans more than one molecule.
  function singleMoleculeId() {{
    var id = null;
    for (var i = 0; i < lastVisible.length; i++) {{
      var m = lastVisible[i].molecule_id || "";
      if (!m) return null;  // a record without a molecule id can't anchor a single-molecule view
      if (id === null) id = m;
      else if (m !== id) return null;  // more than one molecule in view
    }}
    return id;  // null when lastVisible is empty
  }}
  // Column groups: (display label, list of raw evidence_class values). Grouped for
  // readability; a record contributes to exactly one column via its evidence_class.
  var EVMAP_GROUPS = [
    ["Human controlled", ["human_clinical_controlled"]],
    ["Human other", ["human_clinical", "human_observational"]],
    ["Animal", ["preclinical_invivo"]],
    ["In vitro", ["in_vitro", "methods_tool"]],
    ["Reviews", ["evidence_synthesis", "narrative_review"]]
  ];
  function evmapGroupIndex(cls) {{
    cls = (cls || "").trim();
    if (!cls) return -1;
    for (var g = 0; g < EVMAP_GROUPS.length; g++) {{
      var members = EVMAP_GROUPS[g][1];
      for (var k = 0; k < members.length; k++) {{
        if (members[k] === cls) return g;
      }}
    }}
    return -1;  // class outside the shown groups -> not counted
  }}
  // Split facet_indication ("obesity; NAFLD") into discrete use cases, dropping
  // blanks and the "unspecified" placeholder. A record may count in several rows.
  function evmapIndications(v) {{
    var parts = String(v == null ? "" : v).split(";");
    var out = [];
    for (var i = 0; i < parts.length; i++) {{
      var s = parts[i].trim();
      if (!s) continue;
      if (s.toLowerCase() === "unspecified") continue;
      out.push(s);
    }}
    return out;
  }}
  var EVMAP_CAP = 12;  // keep the matrix small: top-N indications, rest folded into "other"
  function renderEvidenceMap() {{
    var host = document.getElementById("evidence-map");
    if (!host) return;
    host.textContent = "";  // rebuild from scratch each call (injection-safe)
    var molId = singleMoleculeId();
    if (!molId || !lastVisible.length) {{ host.style.display = "none"; return; }}
    // Tally counts[indication][groupIndex] over the visible records for this molecule.
    var counts = {{}};   // indication -> array(EVMAP_GROUPS.length) of ints
    var totals = {{}};   // indication -> total grouped count (for ranking + cap)
    var seenAny = false;
    for (var i = 0; i < lastVisible.length; i++) {{
      var r = lastVisible[i];
      var gi = evmapGroupIndex(r.evidence_class || "");
      if (gi < 0) continue;  // evidence_class has no column -> skip
      var inds = evmapIndications(r.facet_indication);
      for (var j = 0; j < inds.length; j++) {{
        var ind = inds[j];
        if (!counts[ind]) {{
          var row = [];
          for (var z = 0; z < EVMAP_GROUPS.length; z++) row.push(0);
          counts[ind] = row;
          totals[ind] = 0;
        }}
        counts[ind][gi] += 1;
        totals[ind] += 1;
        seenAny = true;
      }}
    }}
    if (!seenAny) {{ host.style.display = "none"; return; }}
    // Rank indications by frequency (desc), then alphabetically for stable ties.
    var ranked = Object.keys(counts).sort(function(a, b) {{
      var d = totals[b] - totals[a];
      return d !== 0 ? d : (a < b ? -1 : a > b ? 1 : 0);
    }});
    var top = ranked.slice(0, EVMAP_CAP);
    var rest = ranked.slice(EVMAP_CAP);
    var otherRow = null;
    if (rest.length) {{
      otherRow = [];
      for (var z2 = 0; z2 < EVMAP_GROUPS.length; z2++) otherRow.push(0);
      for (var q = 0; q < rest.length; q++) {{
        var rc = counts[rest[q]];
        for (var g2 = 0; g2 < EVMAP_GROUPS.length; g2++) otherRow[g2] += rc[g2];
      }}
    }}
    host.style.display = "";
    host.appendChild(el("h4", null, "Evidence map"));
    host.appendChild(el("p", "evcap",
      "Counts of retrieved papers by use case and evidence class (not an efficacy assessment)."));
    var table = document.createElement("table");
    table.className = "evmap-table";
    // Header row.
    var trh = document.createElement("tr");
    var thUse = document.createElement("th");
    thUse.className = "use";
    thUse.textContent = "Use case";
    trh.appendChild(thUse);
    for (var h = 0; h < EVMAP_GROUPS.length; h++) {{
      var th = document.createElement("th");
      th.textContent = EVMAP_GROUPS[h][0];
      trh.appendChild(th);
    }}
    table.appendChild(trh);
    // Body rows: one per (capped) indication, plus optional trailing "other".
    function addRow(label, arr) {{
      var tr = document.createElement("tr");
      var tdUse = document.createElement("td");
      tdUse.className = "use";
      tdUse.textContent = label;
      tr.appendChild(tdUse);
      for (var c = 0; c < arr.length; c++) {{
        var td = document.createElement("td");
        if (!arr[c]) td.className = "zero";
        td.textContent = String(arr[c]);  // count via textContent only
        tr.appendChild(td);
      }}
      table.appendChild(tr);
    }}
    for (var t = 0; t < top.length; t++) addRow(top[t], counts[top[t]]);
    if (otherRow) addRow("other", otherRow);
    host.appendChild(table);
  }}
  // Safety & evidence status panel: when the browser is filtered to exactly ONE
  // molecule, summarize what the VISIBLE records for that molecule do and do not
  // contain, plus a persistent static caution block. This is a literature map,
  // NOT an assertion that any molecule is safe or effective. Detection reuses
  // singleMoleculeId(); built entirely via createElement + textContent.
  // Human-efficacy is scoped per the task spec (tighter than isHuman(): the
  // "Human evidence" section OR the three human evidence_class values, NOT
  // reviews/synthesis) so the yes/none/count reflect primary human data only.
  var SAFETY_HUMAN_CLASSES = new Set([
    "human_clinical_controlled", "human_clinical", "human_observational"
  ]);
  function safetyIsHuman(r) {{
    return (r.website_section || "") === "Human evidence" ||
           SAFETY_HUMAN_CLASSES.has(r.evidence_class || "");
  }}
  function renderSafetyPanel() {{
    var host = document.getElementById("safety-panel");
    if (!host) return;
    host.textContent = "";  // rebuild from scratch each call (injection-safe)
    var molId = singleMoleculeId();
    if (!molId || !lastVisible.length) {{ host.style.display = "none"; return; }}
    // Tally over the visible records for this single molecule.
    var humanCount = 0, controlledCount = 0, anyRetracted = false;
    var routeSet = {{}}, routeList = [];
    for (var i = 0; i < lastVisible.length; i++) {{
      var r = lastVisible[i];
      if (safetyIsHuman(r)) humanCount += 1;
      if ((r.evidence_class || "") === "human_clinical_controlled") controlledCount += 1;
      if (isRetracted(r)) anyRetracted = true;
      var routes = splitVals(r, "facet_route");
      for (var j = 0; j < routes.length; j++) {{
        var rt = routes[j];
        if (!rt) continue;  // skip blanks
        if (!routeSet[rt]) {{ routeSet[rt] = true; routeList.push(rt); }}
      }}
    }}
    routeList.sort();
    host.style.display = "";
    host.appendChild(el("h4", null, "Safety & evidence status"));
    // Helper: one "Label: value" row (label muted, value plain), textContent only.
    function srow(label, value) {{
      var p = el("div", "srow");
      p.appendChild(el("span", "slabel", label + ": "));
      p.appendChild(document.createTextNode(value));
      host.appendChild(p);
    }}
    srow("Human efficacy data",
      humanCount > 0 ? ("yes (" + humanCount + " record" + (humanCount === 1 ? "" : "s") + ")")
                     : "none found");
    srow("Controlled human trials", String(controlledCount));
    srow("Routes studied", routeList.length ? routeList.join(", ") : "not clearly reported");
    if (anyRetracted) {{
      host.appendChild(el("div", "sretract",
        "Includes retracted literature \\u2014 see flagged records."));
    }}
    // Persistent static caution block: always shown, textContent only. These are
    // framing statements, NOT claims about this specific molecule.
    var ul = document.createElement("ul");
    ul.className = "scaution";
    var cautions = [
      "Many of these compounds are experimental, investigational, or not approved for the uses discussed.",
      "Absence of reported harms is not evidence of safety.",
      "Research compounds can differ from commercially sold preparations in purity, dose, and formulation.",
      "This is a literature map, not medical advice \\u2014 consult a qualified clinician."
    ];
    for (var c = 0; c < cautions.length; c++) {{
      ul.appendChild(el("li", null, cautions[c]));
    }}
    host.appendChild(ul);
  }}
  function renderVisible() {{
    var total = lastVisible.length;
    var shown = Math.min(visibleCount, total);
    var list = document.getElementById("records-list");
    list.textContent = "";
    var frag = document.createDocumentFragment();
    for (var i = 0; i < shown; i++) {{ frag.appendChild(renderRecord(lastVisible[i])); }}
    if (!total) frag.appendChild(el("div", "empty", "No evidence records match these filters."));
    list.appendChild(frag);

    // Count clarity: X = evidence records passing filters, Y = evidence records in
    // the current view, Z = filtered out. These are evidence-record counts (paper x
    // molecule x rule), distinct from the corpus strip's distinct-PAPER count.
    var y = lastBaseTotal, x = total, z = y - x;
    var showing = document.getElementById("showing");
    var msg = "Showing " + fmtInt(x) + " of " + fmtInt(y) + " evidence records";
    if (z > 0) msg += " \\u00b7 " + fmtInt(z) + " filtered out";
    if (total > shown) msg += " \\u00b7 " + fmtInt(shown) + " rendered (Load more for the rest)";
    showing.textContent = msg;
    // Show "Load more" only while some filtered records remain hidden.
    document.getElementById("load-more-wrap").style.display = (total > shown) ? "" : "none";

    // Feed-cap disclosure: when viewing a single capped molecule, show how many of
    // its papers are published here vs. exist in the full dataset. textContent only.
    var capNote = document.getElementById("cap-note");
    var cm = singleCappedMolecule();
    if (cm && cm.published < cm.total) {{
      capNote.textContent = "Showing top " + fmtInt(cm.published) + " of " + fmtInt(cm.total) +
        " papers for this molecule (older low-impact papers are in the full dataset).";
      capNote.style.display = "";
    }} else {{
      capNote.textContent = "";
      capNote.style.display = "none";
    }}

    // Single-molecule safety & evidence status panel: shown only when the view is
    // exactly one molecule; hidden otherwise. Never asserts safety/efficacy.
    renderSafetyPanel();

    // Single-molecule evidence map: counts by use case x evidence class (a map,
    // not an efficacy verdict). Shown only when the view is one molecule.
    renderEvidenceMap();

    // Keep the translational triangle in sync with the filtered set when shown.
    if (triangleOn) renderTriangle();
  }}

  // Translational triangle view. Toggling shows/hides an inline SVG that plots
  // the currently filtered records (lastVisible) on the iCite "triangle of
  // translation": Human (top), Animal (bottom-left), Molecular/Cellular
  // (bottom-right). Each dot comes from a record's icite_x_coord / icite_y_coord,
  // linearly scaled into the plotting area by the shown points' own min/max.
  // Records missing either coord are skipped. Built with createElementNS +
  // textContent only (no innerHTML) to preserve the injection-safe invariant.
  var triangleOn = false;
  var SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {{
    var e = document.createElementNS(SVGNS, tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }}
  function toggleTriangle() {{
    triangleOn = !triangleOn;
    document.getElementById("triangle-wrap").style.display = triangleOn ? "" : "none";
    var btn = document.getElementById("triangle-toggle");
    if (btn) btn.textContent = triangleOn ? "Hide triangle" : "Triangle view";
    if (triangleOn) renderTriangle();
  }}
  window.toggleTriangle = toggleTriangle;
  function renderTriangle() {{
    var svg = document.getElementById("triangle-svg");
    if (!svg) return;
    svg.textContent = "";  // clear previous frame
    var W = 300, H = 260, pad = 34;
    var top = [W / 2, pad], bl = [pad, H - pad], br = [W - pad, H - pad];
    svg.appendChild(svgEl("polygon", {{
      points: top[0] + "," + top[1] + " " + bl[0] + "," + bl[1] + " " + br[0] + "," + br[1],
      fill: "none", stroke: "var(--border)", "stroke-width": "1.5"
    }}));
    var corners = [
      [top[0], top[1] - 8, "middle", "Human"],
      [bl[0] - 6, bl[1] + 16, "start", "Animal"],
      [br[0] + 6, br[1] + 16, "end", "Molecular/Cellular"]
    ];
    for (var i = 0; i < corners.length; i++) {{
      var t = svgEl("text", {{
        x: corners[i][0], y: corners[i][1], "text-anchor": corners[i][2],
        fill: "var(--muted)", "font-size": "11"
      }});
      t.textContent = corners[i][3];
      svg.appendChild(t);
    }}
    // One shared, cursor-following tooltip box (reused across dots). The native
    // SVG <title> was unreliable, and re-parenting a dot on hover to raise it
    // cancelled the hover -- so no tooltip ever showed. This replaces both.
    var tip = document.getElementById("tri-tip");
    if (!tip) {{
      tip = document.createElement("div");
      tip.id = "tri-tip";
      tip.className = "tri-tip";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }}
    function hideTip() {{ tip.style.display = "none"; }}
    hideTip();

    // Place each dot by its Human / Animal / Molecular composition using the
    // barycentric coordinates of the triangle's three corners, so every dot lands
    // INSIDE the triangle at its true translational position. (The old code
    // min-max-normalized iCite x/y into a rectangle, which floated dots above the
    // triangle's narrowing top edge -- the "elevated" look.)
    var pts = [];
    for (var j = 0; j < lastVisible.length; j++) {{
      var r = lastVisible[j];
      var h = parseFloat(r.icite_human), a = parseFloat(r.icite_animal), c = parseFloat(r.icite_molecular);
      if (isNaN(h)) h = 0;
      if (isNaN(a)) a = 0;
      if (isNaN(c)) c = 0;
      var s = h + a + c;
      if (s <= 0) continue;  // no translational composition -> skip
      var px = (h * top[0] + a * bl[0] + c * br[0]) / s;
      var py = (h * top[1] + a * bl[1] + c * br[1]) / s;
      pts.push([px, py, r]);
    }}
    var note = svgEl("text", {{
      x: W / 2, y: H - 6, "text-anchor": "middle", fill: "var(--muted)", "font-size": "10"
    }});
    note.textContent = pts.length + " of " + lastVisible.length + " evidence records have translational coordinates";
    svg.appendChild(note);
    if (!pts.length) return;
    for (var m = 0; m < pts.length; m++) {{
      var recRef = pts[m][2];
      var dot = svgEl("circle", {{
        cx: pts[m][0], cy: pts[m][1], r: "3.5",
        fill: "var(--accent)", "fill-opacity": "0.75",
        "class": "tri-dot", tabindex: "0", role: "button"
      }});
      // Tooltip label: truncated title + journal/year (textContent only -> safe).
      var ttl = String((recRef && recRef.title) || "(untitled)");
      if (ttl.length > 90) ttl = ttl.slice(0, 89) + "\\u2026";
      var jy = [];
      if (recRef && recRef.journal) jy.push(String(recRef.journal));
      if (recRef && recRef.pub_year) jy.push(String(recRef.pub_year));
      var label = ttl + (jy.length ? " \\u2014 " + jy.join(" ") : "");
      dot.setAttribute("aria-label", label);
      // Click / Enter / Space opens the same detail modal as a card; hover/focus
      // shows the tooltip box.
      (function(rec, d, text) {{
        function moveTip(ev) {{
          d.setAttribute("fill-opacity", "1");
          tip.textContent = text;
          tip.style.display = "block";
          var x = (ev.clientX || 0) + 14, y = (ev.clientY || 0) + 14;
          var vw = window.innerWidth || 0;
          if (x + 268 > vw) {{ x = (ev.clientX || 0) - 268; }}
          tip.style.left = x + "px";
          tip.style.top = y + "px";
        }}
        function leaveTip() {{ d.setAttribute("fill-opacity", "0.75"); hideTip(); }}
        d.addEventListener("mouseenter", moveTip);
        d.addEventListener("mousemove", moveTip);
        d.addEventListener("mouseleave", leaveTip);
        d.addEventListener("focus", function() {{
          d.setAttribute("fill-opacity", "1");
          tip.textContent = text;
          tip.style.display = "block";
          var bb = d.getBoundingClientRect ? d.getBoundingClientRect() : null;
          if (bb) {{ tip.style.left = (bb.left + 12) + "px"; tip.style.top = (bb.bottom + 6) + "px"; }}
        }});
        d.addEventListener("blur", leaveTip);
        d.addEventListener("click", function() {{ hideTip(); openModal(rec); }});
        d.addEventListener("keydown", function(ev) {{
          if (ev.key === "Enter" || ev.key === " ") {{ ev.preventDefault(); hideTip(); openModal(rec); }}
        }});
      }})(recRef, dot, label);
      svg.appendChild(dot);
    }}
  }}
  window.renderTriangle = renderTriangle;

  function loadMore() {{
    visibleCount += RENDER_LIMIT;
    renderVisible();
  }}
  window.loadMore = loadMore;

  // Debounce so the search box does not re-filter on every keystroke (~150ms).
  // Facet clicks / selects stay immediate (they call applyFilters directly).
  function debounce(fn, wait) {{
    var timer = null;
    return function() {{
      var ctx = this, args = arguments;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function() {{ timer = null; fn.apply(ctx, args); }}, wait);
    }};
  }}
  var qDebounced = debounce(function() {{ applyFilters(); }}, 150);
  window.qDebounced = qDebounced;

  function applyFilters() {{
    var base = baseRecords();
    lastBaseTotal = base.length;
    var filters = currentFilters();
    var extra = currentExtra();
    // Year "Range" mode reveals the second year input; hide it otherwise.
    document.getElementById("year-b").style.display =
      (document.getElementById("year-mode").value === "range") ? "" : "none";
    var q = (document.getElementById("q").value || "").trim().toLowerCase();
    // 1) cross-filter counts drive every filter group's PAPER counts, computed
    //    over the active BASE set (all vs human) and the FULL filtered set, so
    //    the render cap never affects them.
    refreshGroups(crossFilterCounts(base, filters, extra, q));
    // 2) the full filtered+sorted list applies ALL filters + search over the base.
    var visible = base.filter(function(r) {{ return matches(r, filters, extra, q); }});
    // A non-default "View" preset overrides the Sort dropdown; otherwise sort as
    // selected. This keeps ordering predictable: exactly one control is in effect.
    var preset = (document.getElementById("rank-preset") || {{}}).value || "default";
    if (preset !== "default") {{
      visible = presetSort(visible, preset);
    }} else {{
      visible = sortRecords(visible, document.getElementById("sort").value);
    }}
    lastVisible = visible;
    // Any filter/search/sort change resets the render window to the first page.
    visibleCount = RENDER_LIMIT;
    renderVisible();
  }}

  function resetFilters() {{
    FILTERS.forEach(function(f) {{ SELECT[f.field] = {{inc: [], exc: []}}; }});
    document.getElementById("q").value = "";
    document.getElementById("year-mode").value = "";
    document.getElementById("year-a").value = "";
    document.getElementById("year-b").value = "";
    document.getElementById("journal-sub").value = "";
    document.getElementById("min-cit").value = "";
    var co = document.getElementById("clinical-only"); if (co) co.checked = false;
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
      stat("max rigor", m.max_reliability);
      card.appendChild(stats);
      // Evidence-density badge: honest literature-VOLUME signal (not quality).
      // Sparse molecules publish all their records; the badge sets expectations.
      if (m.density_tier) {{
        var rc = m.record_count || m.total_records || "0";
        var dtxt = "Evidence density: " + m.density_tier + " (" + rc + " records"
                   + (m.human_count && m.human_count !== "0" ? ", " + m.human_count + " human" : "") + ")";
        var dbadge = el("div", "mol-density tier-" + m.density_tier, dtxt);
        dbadge.title = "Amount of literature only \\u2014 not a rating of study quality.";
        card.appendChild(dbadge);
      }}
      if (m.top_conditions) card.appendChild(el("div", "sl", m.top_conditions));
      // Optional "learn more" link to PubChem. Only rendered when the molecule
      // resolved to a CID (from scripts/enrich_pubchem.py); href built with the
      // same safe pattern as other external links (textContent label via el(),
      // encodeURIComponent on the value, no innerHTML / no javascript: scheme).
      if (m.pubchem_cid) {{
        var pc = el("a", "mol-pubchem", "View on PubChem \\u2197");
        pc.href = PUBCHEM + encodeURIComponent(m.pubchem_cid);
        pc.target = "_blank"; pc.rel = "noopener noreferrer";
        pc.addEventListener("click", function(e) {{ e.stopPropagation(); }});
        card.appendChild(pc);
      }}
      card.addEventListener("click", function() {{
        var name = m.molecule_name || "";
        showTab("evidence");
        resetFilters();
        // Select this bioactive as an include in the Evidence browser.
        if (SELECT["molecule_name"] && name) {{
          SELECT["molecule_name"].inc = [name];
          applyFilters();
        }}
      }});
      grid.appendChild(card);
    }});
    document.getElementById("molecules-count").textContent =
      MOLECULES.length + " bioactives (with indexed papers)";
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

  // ---- shared feed helpers (trials + preprints) ------------------------------
  // A safe external link: textContent label, href built from a value we accept
  // ONLY if it is an http(s) URL; otherwise no link is emitted. This blocks
  // javascript:/data: schemes even though the values are trusted feeds.
  function safeLink(label, url) {{
    var u = String(url || "").trim();
    if (!/^https?:\\/\\//i.test(u)) return null;
    var a = el("a", null, label);
    a.href = encodeURI(u);  // encode any stray unsafe chars; scheme already vetted
    a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  }}
  // Populate a molecule <select> from the distinct molecule_name values in a feed.
  function fillMolSelect(sel, rows) {{
    var seen = {{}}, names = [];
    rows.forEach(function(r) {{
      var n = (r.molecule_name || "").trim();
      if (n && !seen[n]) {{ seen[n] = true; names.push(n); }}
    }});
    names.sort();
    names.forEach(function(n) {{
      var o = document.createElement("option"); o.value = n;
      o.textContent = n;  // textContent -> injection-safe
      sel.appendChild(o);
    }});
  }}

  // ---- Trials registry -------------------------------------------------------
  var TRIALS_NOTE = "These are study REGISTRATIONS from ClinicalTrials.gov \\u2014 " +
    "trial designs and status, NOT published results. A registration does not mean " +
    "the intervention works. \\u201cOngoing\\u201d means recruiting or active.";
  var TRIALS_EMPTY = "No registry studies indexed yet \\u2014 populates after the " +
    "trials fetch runs.";
  function trialsFeedInit() {{
    var note = document.getElementById("trials-note");
    var toolbar = document.getElementById("trials-toolbar");
    if (!TRIALS.length) {{
      note.textContent = TRIALS_EMPTY;
      toolbar.style.display = "none";
      document.getElementById("trials-count").textContent = "";
      document.getElementById("trials-list").textContent = "";
      return;
    }}
    note.textContent = TRIALS_NOTE;
    toolbar.style.display = "";
    fillMolSelect(document.getElementById("trials-mol"), TRIALS);
    renderTrials();
  }}
  function trialDates(t) {{
    var s = (t.start_date || "").trim();
    var e = (t.completion_date || t.primary_completion_date || "").trim();
    if (s && e) return s + " \\u2192 " + e;
    return s || e || "";
  }}
  function renderTrials() {{
    if (!TRIALS.length) return;
    var q = (document.getElementById("trials-q").value || "").trim().toLowerCase();
    var mol = document.getElementById("trials-mol").value;
    var ongoingOnly = document.getElementById("trials-ongoing").checked;
    var sortBy = document.getElementById("trials-sort").value;
    var yr = (document.getElementById("trials-year").value || "").trim();
    var rows = TRIALS.filter(function(t) {{
      if (ongoingOnly && !t.ongoing) return false;
      if (mol && (t.molecule_name || "") !== mol) return false;
      if (yr && String(t.start_date || "").slice(0, 4) !== yr) return false;
      if (q) {{
        var hay = ((t.brief_title || "") + " " + (t.conditions || "") + " " +
                   (t.interventions || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }}
      return true;
    }});
    rows = rows.map(function(t, i) {{ return [t, i]; }}).sort(function(a, b) {{
      if (sortBy === "start") {{
        var d = String(b[0].start_date || "").localeCompare(String(a[0].start_date || ""));
        return d !== 0 ? d : a[1] - b[1];
      }}
      if (sortBy === "start-asc") {{
        var da = String(a[0].start_date || "").localeCompare(String(b[0].start_date || ""));
        return da !== 0 ? da : a[1] - b[1];
      }}
      // ongoing-first (default), then feed order (already ongoing-first by start).
      var oa = a[0].ongoing ? 0 : 1, ob = b[0].ongoing ? 0 : 1;
      return oa !== ob ? oa - ob : a[1] - b[1];
    }}).map(function(x) {{ return x[0]; }});
    var ongoing = TRIALS.filter(function(t) {{ return t.ongoing; }}).length;
    document.getElementById("trials-count").textContent =
      "Showing " + fmtInt(rows.length) + " of " + fmtInt(TRIALS.length) +
      " registered studies \\u00b7 " + fmtInt(ongoing) + " ongoing";
    var list = document.getElementById("trials-list");
    list.textContent = "";
    if (!rows.length) {{ list.appendChild(el("div", "empty", "No studies match these filters.")); return; }}
    var frag = document.createDocumentFragment();
    rows.forEach(function(t) {{ frag.appendChild(renderTrialCard(t)); }});
    list.appendChild(frag);
  }}
  window.renderTrials = renderTrials;
  function renderTrialCard(t) {{
    var card = el("div", "trial-card" + (t.ongoing ? " ongoing" : ""));
    card.appendChild(el("h3", null, t.brief_title || t.nct_id || "(untitled study)"));
    var meta = el("div", "meta");
    if (t.molecule_name) meta.appendChild(el("span", "pill", t.molecule_name));
    if (t.overall_status) {{
      meta.appendChild(el("span", "status-badge" + (t.ongoing ? " on" : ""),
        (t.ongoing ? "\\u25CF " : "") + t.overall_status));
    }}
    if (t.phases) meta.appendChild(el("span", "pill", t.phases));
    if (t.study_type) meta.appendChild(el("span", "pill", t.study_type));
    if (t.has_results === "true" || t.has_results === true || t.has_results === "1")
      meta.appendChild(el("span", "pill", "has results"));
    card.appendChild(meta);
    var grid = el("div", "trial-grid");
    function row(k, v) {{ if (v) {{ grid.appendChild(el("div", "k", k)); grid.appendChild(el("div", "v", v)); }} }}
    row("Conditions", t.conditions);
    row("Interventions", t.interventions);
    row("Sponsor", t.lead_sponsor);
    row("Enrollment", t.enrollment_count);
    row("Dates", trialDates(t));
    if (t.nct_id) grid.appendChild(el("div", "k", "Registry ID"));
    if (t.nct_id) grid.appendChild(el("div", "v", t.nct_id));
    card.appendChild(grid);
    // Published results: PMIDs CT.gov links as RESULT/DERIVED publications for
    // this trial. Each renders as a PubMed link via the vetted safeLink helper.
    var resultPmids = String(t.result_pmids || "").split(";").map(function(s) {{
      return s.trim();
    }}).filter(function(s) {{ return /^\\d+$/.test(s); }});
    if (resultPmids.length) {{
      var pubs = el("div", "trial-pubs");
      pubs.appendChild(el("span", "k", "Published results:"));
      resultPmids.forEach(function(pmid) {{
        var pa = safeLink("PMID " + pmid, PUBMED + encodeURIComponent(pmid) + "/");
        if (pa) pubs.appendChild(pa);
      }});
      card.appendChild(pubs);
    }}
    var links = el("div", "links");
    var link = safeLink("View on ClinicalTrials.gov", t.url);
    if (link) links.appendChild(link);
    if (links.childNodes.length) card.appendChild(links);
    return card;
  }}

  // ---- Preprints -------------------------------------------------------------
  var PREPRINTS_NOTE = "\\u26A0 These are PREPRINTS (bioRxiv/medRxiv) \\u2014 they have " +
    "NOT been peer-reviewed. Findings may change or be retracted; interpret with " +
    "caution and do not treat them as established evidence.";
  var PREPRINTS_EMPTY = "No preprints indexed yet \\u2014 populates after the " +
    "preprints fetch runs.";
  function preprintsFeedInit() {{
    var note = document.getElementById("preprints-note");
    var toolbar = document.getElementById("preprints-toolbar");
    if (!PREPRINTS.length) {{
      note.textContent = PREPRINTS_EMPTY;
      toolbar.style.display = "none";
      document.getElementById("preprints-count").textContent = "";
      document.getElementById("preprints-list").textContent = "";
      return;
    }}
    note.textContent = PREPRINTS_NOTE;
    toolbar.style.display = "";
    fillMolSelect(document.getElementById("pp-mol"), PREPRINTS);
    renderPreprints();
  }}
  function renderPreprints() {{
    if (!PREPRINTS.length) return;
    var q = (document.getElementById("pp-q").value || "").trim().toLowerCase();
    var mol = document.getElementById("pp-mol").value;
    var sortBy = document.getElementById("pp-sort").value;
    var yr = (document.getElementById("pp-year").value || "").trim();
    var rows = PREPRINTS.filter(function(p) {{
      if (mol && (p.molecule_name || "") !== mol) return false;
      if (yr && String(p.date || "").slice(0, 4) !== yr) return false;
      if (q) {{
        var hay = ((p.title || "") + " " + (p.authors_short || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }}
      return true;
    }});
    rows = rows.map(function(p, i) {{ return [p, i]; }}).sort(function(a, b) {{
      var d = String(b[0].date || "").localeCompare(String(a[0].date || ""));
      if (sortBy === "date-asc") d = -d;
      return d !== 0 ? d : a[1] - b[1];
    }}).map(function(x) {{ return x[0]; }});
    document.getElementById("preprints-count").textContent =
      "Showing " + fmtInt(rows.length) + " of " + fmtInt(PREPRINTS.length) + " preprints";
    var list = document.getElementById("preprints-list");
    list.textContent = "";
    if (!rows.length) {{ list.appendChild(el("div", "empty", "No preprints match these filters.")); return; }}
    var frag = document.createDocumentFragment();
    rows.forEach(function(p) {{ frag.appendChild(renderPreprintCard(p)); }});
    list.appendChild(frag);
  }}
  window.renderPreprints = renderPreprints;
  function renderPreprintCard(p) {{
    var card = el("div", "pp-card");
    card.appendChild(el("h3", null, p.title || "(untitled preprint)"));
    var meta = el("div", "meta");
    if (p.molecule_name) meta.appendChild(el("span", "pill", p.molecule_name));
    if (p.server) meta.appendChild(el("span", "server-badge", p.server));
    if (p.date) meta.appendChild(el("span", "pill", p.date));
    card.appendChild(meta);
    if (p.authors_short) card.appendChild(el("div", "authors", p.authors_short));
    var links = el("div", "links");
    var link = safeLink("Read preprint", p.url) ||
      (p.doi ? safeLink("DOI", "https://doi.org/" + encodeURIComponent(p.doi)) : null);
    if (link) links.appendChild(link);
    // If url gave a link but a DOI also exists, surface DOI too.
    if (p.url && p.doi) {{
      var d = safeLink("DOI", "https://doi.org/" + encodeURIComponent(p.doi));
      if (d) links.appendChild(d);
    }}
    if (links.childNodes.length) card.appendChild(links);
    return card;
  }}

  // ---- corpus-stats strip ----------------------------------------------------
  // Compact summary near the header, e.g. "Database: 36,371 papers · 29 bioactives
  // with data · 2015-2026 · 42% with citations · updated <date>". Hidden when
  // corpus_stats is absent/empty. Every value rendered via textContent.
  function renderCorpusStrip() {{
    var strip = document.getElementById("corpus-strip");
    strip.textContent = "";
    if (!CORPUS || !CORPUS.total_papers) {{ strip.style.display = "none"; return; }}
    var parts = [];
    parts.push(["Database", fmtInt(CORPUS.total_papers) + " papers"]);
    if (CORPUS.molecules_with_data)
      parts.push([null, fmtInt(CORPUS.molecules_with_data) + " bioactives with data"]);
    var ymin = CORPUS.year_min, ymax = CORPUS.year_max;
    if (ymin && ymax) parts.push([null, (ymin === ymax ? String(ymin) : ymin + "\\u2013" + ymax)]);
    if (CORPUS.pct_citations_filled != null && CORPUS.pct_citations_filled !== "")
      parts.push([null, CORPUS.pct_citations_filled + "% with cited-by counts"]);
    // Compact "Data health" coverage line: share of records carrying each core
    // signal. Each metric is optional (shown only when present in corpus_stats).
    var health = [];
    function healthPart(label, v) {{
      if (v != null && v !== "") health.push(label + " " + v + "%");
    }}
    healthPart("abstracts", CORPUS.pct_with_abstract);
    healthPart("DOIs", CORPUS.pct_with_doi);
    healthPart("citations", CORPUS.pct_citations_filled);
    healthPart("iCite", CORPUS.pct_with_icite);
    if (health.length) parts.push(["Data health", health.join(" \\u00b7 ")]);
    var upd = String(CORPUS.generated_utc || "").slice(0, 10);
    if (upd) parts.push([null, "updated " + upd]);
    parts.forEach(function(pr, i) {{
      if (i > 0) strip.appendChild(el("span", "cs-sep", "\\u00b7"));
      if (pr[0]) {{ strip.appendChild(el("span", "cs-label", pr[0] + ":")); }}
      strip.appendChild(el("b", null, pr[1]));
    }});
    strip.style.display = "";
  }}

  // One-line descriptor per browser view.
  var BROWSER_DESC = {{
    evidence: "Every indexed paper for the bioactives RetaBase tracks (listed under the Bioactives tab) \\u2014 filter, sort, and inspect the evidence.",
    clinical: "Human data only \\u2014 clinical trials, observational studies, and evidence syntheses (no animal / in-vitro / methods)."
  }};

  // The Evidence and Clinical tabs share the SAME browser (sidebar + list); only
  // the base record set differs (all vs human-only). Bioactives / Experimental /
  // About are standalone panels.
  function showTab(name) {{
    var isBrowser = (name === "evidence" || name === "clinical");
    var isMol = name === "molecules";
    var isExp = name === "experimental";
    var isTrials = name === "trials";
    var isPreprints = name === "preprints";
    var isAbout = name === "about";
    document.getElementById("browser-view").style.display = isBrowser ? "" : "none";
    document.getElementById("molecules-view").style.display = isMol ? "" : "none";
    document.getElementById("experimental-view").style.display = isExp ? "" : "none";
    document.getElementById("trials-view").style.display = isTrials ? "" : "none";
    document.getElementById("preprints-view").style.display = isPreprints ? "" : "none";
    document.getElementById("about-view").style.display = isAbout ? "" : "none";
    document.getElementById("sidebar").style.display = isBrowser ? "" : "none";
    document.getElementById("tab-evidence").className = (name === "evidence") ? "active" : "";
    document.getElementById("tab-clinical").className = (name === "clinical") ? "active" : "";
    document.getElementById("tab-trials").className = isTrials ? "active" : "";
    document.getElementById("tab-preprints").className = isPreprints ? "active" : "";
    document.getElementById("tab-molecules").className = isMol ? "active" : "";
    document.getElementById("tab-experimental").className = isExp ? "active" : "";
    document.getElementById("tab-about").className = isAbout ? "active" : "";
    if (isTrials) trialsFeedInit();
    if (isPreprints) preprintsFeedInit();
    if (isBrowser) {{
      currentView = name;
      document.getElementById("browser-desc").textContent = BROWSER_DESC[name] || "";
      applyFilters();  // re-filter over the newly-active base set
    }}
  }}
  window.showTab = showTab;

  // ---- About / Methods -------------------------------------------------------
  // Rendered from a small data structure via textContent (no innerHTML) so the
  // page keeps its no-innerHTML posture. Formulas match the curation pipeline.
  function renderAbout() {{
    var root = document.getElementById("about-body");
    root.textContent = "";
    function h2(t) {{ root.appendChild(el("h2", null, t)); }}
    function h3(t) {{ root.appendChild(el("h3", null, t)); }}
    function p(t) {{ root.appendChild(el("p", null, t)); }}
    function formula(t) {{ root.appendChild(el("div", "formula", t)); }}
    function list(items) {{
      var ul = el("ul");
      items.forEach(function(it) {{ ul.appendChild(el("li", null, it)); }});
      root.appendChild(ul);
    }}
    h2("About RetaBase");
    p("RetaBase is a transparent, rule-based evidence dashboard for retatrutide and "
      + "related bioactives (peptides, small molecules, and related compounds). Every "
      + "paper is scored by an auditable rubric \\u2014 no black-box model decides what "
      + "ranks first. The whole site is a single offline HTML file; the underlying feed "
      + "and scoring code can be inspected and reproduced.");
    p("Scope: RetaBase indexes the biomedical literature only for the specific molecules "
      + "it tracks \\u2014 the ones listed under the Bioactives tab \\u2014 not every paper on "
      + "every peptide or drug. \\u201cAll\\u201d and \\u201cevery\\u201d on this site always mean "
      + "\\u201call of the papers found for those tracked bioactives,\\u201d and even within that set "
      + "coverage depends on what the searches have fetched so far (the historical backfill is "
      + "still filling in older years).");
    p("Each paper carries two independent axes \\u2014 how well it was conducted "
      + "(automated rigor) and how directly it applies to humans (directness) \\u2014 plus a "
      + "combined rank used for best-first ordering. The tabs let you browse the full indexed "
      + "set for the tracked bioactives, restrict to human/clinical data, list the bioactives, "
      + "or view candidate compounds.");

    h3("Automated rigor signals \\u2014 within-class study quality (0\\u2013100)");
    p("The automated rigor score is a set of RULE-BASED signals extracted from the "
      + "reported methods/abstract of each paper. Using a rubric appropriate to the "
      + "evidence class (a randomized human trial and an in-vitro assay are judged on "
      + "different rubrics), it starts from a class base score and adds/subtracts points "
      + "for design features it can detect in the text \\u2014 randomization, blinding, "
      + "controls, sample size, follow-up, reporting completeness \\u2014 clamped to "
      + "0\\u2013100. It is a within-class quality signal, NOT a measure of how human-"
      + "relevant the evidence is (that is directness).");
    p("Important: this is NOT a formal risk-of-bias assessment (such as Cochrane RoB 2 "
      + "or ROBINS-I) and NOT a GRADE certainty-of-evidence rating. No human reviewer "
      + "appraises each study, and formal risk of bias is NOT assessed \\u2014 the paper "
      + "detail view labels it \\u201cnot assessed (automated rigor signals only)\\u201d. "
      + "Treat the score as an automated triage signal, not a substitute for reading the "
      + "methods or a systematic critical appraisal.");

    h3("Directness \\u2014 translational level");
    p("Directness measures how directly the evidence bears on human outcomes: human "
      + "randomized controlled trials score highest, then human interventional and "
      + "observational studies and evidence syntheses, then animal in-vivo work, with "
      + "in-vitro / molecular studies lowest. It is derived from the evidence class and "
      + "study model, independent of study quality.");

    h3("NIH iCite signals \\u2014 impact, translation, clinical uptake");
    p("Several metrics come from NIH iCite (the Open Citation Collection), which "
      + "provides field- and time-normalized values for essentially every PubMed "
      + "article. Where iCite covers a paper, we prefer its curated values over our "
      + "own heuristics; papers iCite has not yet scored (very recent or not-yet-"
      + "indexed) fall back to the keyword-based signals.");
    list([
      "Impact \\u2014 instead of a raw citation count, ranking prefers iCite\\u2019s NIH "
        + "percentile and Relative Citation Ratio (RCR; 1.0 = the field median), so a "
        + "well-cited older paper and a fast-rising new one are compared fairly.",
      "Human / animal / in-vitro \\u2014 iCite\\u2019s MeSH-curated human/animal/molecular "
        + "fractions decide a paper\\u2019s translational compartment when our text "
        + "heuristics are unsure (precise designs like RCTs are still set by study type).",
      "APT (Approximate Potential to Translate, 0\\u20131) \\u2014 a model estimate of "
        + "translational potential that slightly nudges the directness of preclinical / "
        + "in-vitro work.",
      "Clinical influence \\u2014 how many clinical articles cite the paper (shown in its "
        + "detail view), a direct read on clinical uptake.",
    ]);
    p("The \\u201cTriangle view\\u201d toggle on the Evidence tab plots the currently-"
      + "filtered papers on the biomedicine triangle (Human, Animal, Molecular/Cellular "
      + "corners) from iCite coordinates, so you can see the translational spread of a "
      + "result set at a glance. You can also sort by impact percentile, translational "
      + "potential (APT), or clinical influence, and restrict to clinical articles only.");

    h3("Rank \\u2014 combined best-first ordering");
    p("The rank score combines six normalized (0\\u20131) components into a single "
      + "weighted sum used to order results:");
    list([
      "Directness \\u2014 translational level (human RCT high \\u2192 in-vitro low).",
      "Quality \\u2014 the automated rigor score above (within-class study quality).",
      "Relevance \\u2014 topical fit to the bioactive and its core indications/endpoints.",
      "Recency \\u2014 how recent the publication year is.",
      "Impact \\u2014 log-scaled times-cited count, i.e. how often the paper has been cited by OTHER papers (so a few extra citations matter more at the low end than the high end). This is not about whether the paper has a reference list.",
      "Venue \\u2014 journal reputation / tier."
    ]);
    formula("rank_score = 0.33\\u00b7directness + 0.28\\u00b7quality + 0.20\\u00b7relevance "
      + "+ 0.10\\u00b7recency + 0.05\\u00b7impact + 0.04\\u00b7venue");
    p("Every component and the final weighted score are shown per paper in the "
      + "\\u201cRank breakdown\\u201d of its detail view, so any ordering can be traced back "
      + "to its inputs.");

    h3("Counts");
    p("Result counts are EVIDENCE-RECORD counts (paper \\u00d7 molecule \\u00d7 rule), which is "
      + "why they can exceed the distinct-paper number shown in the corpus strip. The header "
      + "reads \\u201cShowing X of Y evidence records\\u201d where Y is the records in the current "
      + "tab (all evidence, or human-only) and X is the number passing your filters; "
      + "\\u201cZ filtered out\\u201d is Y minus X. Each filter option\\u2019s number is the count "
      + "of records with that value under all your OTHER active filters (cross-filtered).");

    h3("What's published vs. the full corpus");
    p("A few molecules (for example metformin or rapamycin) have tens of thousands of "
      + "papers. To keep the site fast in your browser, the published feed is capped per "
      + "molecule, but weighted toward what this database is about: the human-evidence "
      + "(therapeutic use), mechanism-of-action, and review sections get a high ceiling so "
      + "well-studied molecules can show a lot, while lower-value sections (methods/assays, "
      + "comparator/background, biomarkers) are limited more tightly. Within each, records "
      + "are kept best-first by rank. This is a display limit only \\u2014 the FULL set of "
      + "matching papers is retained in the project's data files; nothing is deleted, just "
      + "what loads in the browser is bounded.");

    h3("Filters");
    p("Every facet supports INCLUDE and EXCLUDE. Include is OR within a domain (a paper "
      + "matches if it has ANY selected include value); exclude drops a paper that has ANY "
      + "selected exclude value. Year filters on publication year (before / after / range), "
      + "\\u201cjournal name includes\\u201d is a case-insensitive substring, and \\u201cmin "
      + "times cited\\u201d sets a floor on how often the paper has been cited by others.");

    h3("Clinical evidence view");
    p("The Clinical evidence tab restricts to human data: papers whose evidence class is "
      + "a human clinical (controlled or interventional), human observational, or evidence "
      + "synthesis class, or whose section is Human evidence or Reviews and overviews. It "
      + "reuses the same browser and filters, pre-filtered to those records.");

    h3("Trials registry (NOT results)");
    p("The Trials registry tab lists studies from ClinicalTrials.gov. These are study "
      + "REGISTRATIONS \\u2014 trial designs, status, sponsors, and timelines \\u2014 not "
      + "published, peer-reviewed results. A registration does not imply the intervention "
      + "works. It is a distinct data type from the peer-reviewed Evidence and Clinical "
      + "tabs; use the \\u201cOngoing only\\u201d toggle, molecule filter, and search to "
      + "explore it. Populates after the trials fetch runs.");

    h3("Preprints (NOT peer-reviewed)");
    p("The Preprints tab lists bioRxiv/medRxiv preprints (via EuropePMC). Preprints have "
      + "NOT been peer-reviewed \\u2014 their findings may change or be retracted and should "
      + "be interpreted with caution. They are kept separate from the peer-reviewed evidence "
      + "for exactly this reason. Populates after the preprints fetch runs.");
  }}

  function boot() {{
    buildFilters();
    if (INTERNAL) updateApSummary();
    renderCorpusStrip();
    renderMolecules();
    renderExperimental();
    renderAbout();
    showTab("evidence");
  }}

  // In fetch mode the trials + preprints feeds are loaded at runtime like
  // site_data.json; a 404 / parse error simply leaves the array empty so the
  // tab shows its placeholder. Called before boot so the first tab click renders.
  function fetchSideFeed(name, key, target) {{
    return fetch(name).then(function(r) {{
      if (!r.ok) return null;  // 404 etc. -> empty
      return r.json();
    }}).then(function(feed) {{
      if (feed && feed[key]) target(feed[key]);
    }}).catch(function() {{ /* tolerate absence -> empty feed */ }});
  }}

  if (DATA.mode === "fetch") {{
    // Hosted mode: fetch the sibling feed, then boot with real records.
    fetch("site_data.json").then(function(r) {{ return r.json(); }}).then(function(feed) {{
      RECORDS = feed.records || [];
      MOLECULES = (feed.molecules || []).filter(function(m) {{ return true; }});
      // Prefer the feed's experimental list if present; else keep the inlined one.
      if (feed.experimental) EXPERIMENTAL = feed.experimental;
      // corpus_stats travels with the main feed; prefer it, else keep inlined.
      if (feed.corpus_stats) CORPUS = feed.corpus_stats;
      // Trials + preprints are separate sibling feeds (may 404 -> stay empty).
      return Promise.all([
        fetchSideFeed("trials_data.json", "trials", function(v) {{ TRIALS = v || []; }}),
        fetchSideFeed("preprints_data.json", "preprints", function(v) {{ PREPRINTS = v || []; }}),
      ]);
    }}).then(function() {{
      boot();
    }}).catch(function() {{
      document.getElementById("records-list").appendChild(el("div", "empty",
        "Could not load site_data.json (fetch mode requires it be served alongside this page)."));
      buildFilters(); if (INTERNAL) updateApSummary(); renderCorpusStrip(); renderExperimental(); renderAbout();
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
    ap.add_argument("--internal", action="store_true",
                    help="Internal-review build: keep the curator approve/reject/notes "
                         "and export-decisions UI. Default (omitted) is the PUBLIC build, "
                         "which omits all approval controls.")
    args = ap.parse_args()

    if not os.path.isdir(args.curated_dir):
        print(f"error: curated dir not found: {args.curated_dir}", file=sys.stderr)
        sys.exit(1)

    result = build_site(args.curated_dir, args.out_dir, mode=args.mode,
                        max_inline=args.max_inline, internal=args.internal)
    kb = result["bytes"] / 1024.0
    print(f"Built site ({result['mode']}, {'internal' if result['internal'] else 'public'}) -> {result['path']}")
    print(f"  records  : {result['records']} inlined / {result['total']} total"
          + (f" ({result['truncated']} truncated)" if result["truncated"] else ""))
    print(f"  molecules: {result['molecules']}")
    print(f"  size     : {kb:.1f} KiB ({result['bytes']} bytes)")


if __name__ == "__main__":
    main()
