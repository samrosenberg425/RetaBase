#!/usr/bin/env python3
"""Build the curated, backend-agnostic evidence database.

Reads the broad internal SQLite (``molecules``/``papers``/``evidence`` payload
tables), applies the curation layer (facets -> reliability -> publication status
-> appraisal), and writes a clean set of CSVs designed to drop straight into
Google Sheets now and Airtable later:

    exports/curated/curated_evidence.csv   one row per evidence record (wide)
    exports/curated/facets_long.csv        (evidence_id, group, value, label, source)
    exports/curated/public_records.csv     auto_publish_eligible == True
    exports/curated/review_queue.csv       records awaiting human review
    exports/curated/molecule_index.csv     per-molecule rollup
    exports/curated/field_dictionary.csv   human-readable schema
    exports/curated/schema.json            machine-readable schema (Sheets/Airtable)

Pure stdlib (csv/sqlite3/json) so it runs anywhere, including offline. No LLM,
no network. Every tag/score/decision is explainable from the config files.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Dict, Iterable, List

# Make the package importable when run as a script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.curation.appraisal import APPRAISAL_FIELDS, appraise_evidence
from retarats_pipeline.curation.extractors import REFINED_FIELDS, off_focus_reason, refine_extraction
from retarats_pipeline.curation.facets import FACET_GROUPS, derive_facets, load_facet_defs
from retarats_pipeline.curation.journal import JOURNAL_FIELDS, journal_reputation
from retarats_pipeline.curation.publication_status import (
    PUBLICATION_FIELDS,
    decide_publication,
    load_publication_rules,
    load_required_fields,
)
from retarats_pipeline.curation.ranking import RANK_FIELDS, compute_rank
from retarats_pipeline.curation.reliability import RELIABILITY_FIELDS as _RELIABILITY_FIELDS, assess_reliability

# Paper fields we merge onto each evidence row (identity + links + text for facets).
PAPER_MERGE_FIELDS = [
    "title", "abstract", "doi", "pubmed_url", "journal", "mesh_terms", "keywords", "chemicals",
    "citation_count",
    # NIH iCite metrics (from run_icite_backfill.py) merged onto each evidence row so
    # curation/ranking can use them. All optional; everything falls back if absent.
    "icite_rcr", "icite_nih_percentile", "icite_apt",
    "icite_human", "icite_animal", "icite_molecular",
    "icite_x_coord", "icite_y_coord", "icite_is_clinical",
    "icite_citation_count", "icite_field_citation_rate",
]

IDENTITY_FIELDS = [
    "evidence_id", "molecule_id", "molecule_name", "pmid", "doi", "pubmed_url",
    "title", "journal", "pub_year", "authors_short", "first_author", "author_count",
]

CORE_STRUCTURED_FIELDS = [
    "primary_study_type", "model_type", "species_or_population", "role_category",
    "processing_lane", "database_section", "paper_purpose",
    "condition_tags", "endpoint_tags", "mechanistic_focus",
    "intervention_or_exposure", "comparator_or_control", "dose_route", "duration",
    "sample_size", "outcome_direction", "efficacy_signal", "safety_signal",
    "evidence_summary",
]

RELIABILITY_FIELDS = _RELIABILITY_FIELDS

FACET_WIDE_FIELDS = [f"facet_{g}" for g in FACET_GROUPS] + ["facet_all", "facet_count"]


def load_payload_table(conn: sqlite3.Connection, table: str) -> List[dict]:
    rows: List[dict] = []
    try:
        cur = conn.execute(f"select payload_json from {table}")
    except sqlite3.OperationalError:
        return rows
    for (payload,) in cur:
        try:
            rows.append(json.loads(payload))
        except (TypeError, json.JSONDecodeError):
            continue
    return rows


def build(db_path: str, out_dir: str, limit: int = 0) -> dict:
    conn = sqlite3.connect(db_path)
    papers = load_payload_table(conn, "papers")
    evidence = load_payload_table(conn, "evidence")
    conn.close()

    paper_by_pmid: Dict[str, dict] = {}
    for p in papers:
        pmid = str(p.get("pmid", "") or "")
        if pmid:
            paper_by_pmid[pmid] = p

    facet_defs = load_facet_defs()
    rules = load_publication_rules()
    required = load_required_fields()

    os.makedirs(out_dir, exist_ok=True)

    curated_rows: List[dict] = []
    facets_long_rows: List[dict] = []
    stats = {
        "processed": 0,
        "publication_status": Counter(),
        "website_section": Counter(),
        "reliability_tier": Counter(),
        "auto_publish": 0,
        "facet_species": Counter(),
        "missing_required": 0,
        "model_disambiguation_changed": 0,
    }

    for i, ev in enumerate(evidence):
        if limit and i >= limit:
            break
        row = dict(ev)
        pmid = str(row.get("pmid", "") or "")
        paper = paper_by_pmid.get(pmid, {})
        for f in PAPER_MERGE_FIELDS:
            if f not in row or _blank(row.get(f)):
                row[f] = paper.get(f, "")

        # Authors are already stored on the paper; surface a short display form
        # ("First A; Second B; Third C et al.") plus first author + count so the
        # UI can show who wrote each article in every section.
        authors = paper.get("authors") or row.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in authors.split(";") if a.strip()]
        row["first_author"] = authors[0] if authors else ""
        row["author_count"] = len(authors)
        row["authors_short"] = "; ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")

        # 1) facets
        fr = derive_facets(row, paper, facet_defs)
        row.update(fr.wide)
        eid = str(row.get("evidence_id", "") or f"row_{i}")
        for (group, value, label, source) in fr.long:
            facets_long_rows.append(
                {
                    "evidence_id": eid,
                    "molecule_id": row.get("molecule_id", ""),
                    "facet_group": group,
                    "facet_value": value,
                    "facet_label": label,
                    "facet_source": source,
                }
            )

        # 2) additive extraction refinement + parallel model disambiguation.
        #    Runs first so reliability can use model_primary and refined_n.
        refined = refine_extraction(row, paper)
        row.update(refined)
        model_type_norm = str(row.get("model_type", "") or "").lower().replace(" ", "_")
        if refined["model_primary"] and refined["model_primary"] != model_type_norm:
            stats["model_disambiguation_changed"] += 1

        # 3) reliability: two-axis, section-appropriate (uses paper text + refined fields).
        rel = assess_reliability(row, paper)
        row.update(rel.to_dict())

        # 3b) journal reputation (curated, auditable venue signal). Emitted as
        #     columns and consumed by ranking's `venue` axis (below).
        jr = journal_reputation(str(row.get("journal", "") or ""))
        row["journal_reputation"] = jr.journal_reputation
        row["journal_tier"] = jr.journal_tier
        row["journal_rationale"] = jr.journal_rationale

        # 4a) off-focus noise (opinion / infodemiology) the search can't catch.
        row["off_focus_reason"] = off_focus_reason(row, paper)

        # 4) publication decision (broad inclusion; reads evidence_class + directness).
        decision = decide_publication(row, rules, required)
        row.update(decision.to_dict())

        # 5) appraisal + LLM-ready scaffold
        row.update(appraise_evidence(row).to_dict())

        # 6) combined ranking (reliability + directness + relevance + recency + impact)
        row.update(compute_rank(row).to_dict())

        curated_rows.append(row)

        # stats
        stats["processed"] += 1
        stats["publication_status"][decision.publication_status] += 1
        if decision.website_section:
            stats["website_section"][decision.website_section] += 1
        stats["reliability_tier"][rel.reliability_tier] += 1
        if decision.auto_publish_eligible:
            stats["auto_publish"] += 1
        if not decision.required_fields_present:
            stats["missing_required"] += 1
        for sp in fr.wide.get("facet_species", "").split("; "):
            if sp:
                stats["facet_species"][sp] += 1

    # --- write curated_evidence.csv ---
    curated_cols = (
        IDENTITY_FIELDS
        + CORE_STRUCTURED_FIELDS
        + FACET_WIDE_FIELDS
        + RELIABILITY_FIELDS
        + JOURNAL_FIELDS
        + PUBLICATION_FIELDS
        + RANK_FIELDS
        + APPRAISAL_FIELDS
        + REFINED_FIELDS
    )
    # Order everything best-first by the combined rank so downstream consumers
    # (CSV, site) surface the most reliable + impactful evidence at the top.
    curated_rows.sort(key=lambda r: _int(r.get("rank_score")), reverse=True)
    _write_csv(os.path.join(out_dir, "curated_evidence.csv"), curated_rows, curated_cols)

    # --- facets_long.csv ---
    _write_csv(
        os.path.join(out_dir, "facets_long.csv"),
        facets_long_rows,
        ["evidence_id", "molecule_id", "facet_group", "facet_value", "facet_label", "facet_source"],
    )

    # --- public_records.csv (broad browsable feed = everything on-topic) ---
    public = [r for r in curated_rows if r.get("publication_status") in {"featured", "listed"}]
    _write_csv(os.path.join(out_dir, "public_records.csv"), public, curated_cols)

    # --- featured_records.csv (spotlight subset) ---
    featured = [r for r in curated_rows if r.get("publication_status") == "featured"]
    _write_csv(os.path.join(out_dir, "featured_records.csv"), featured, curated_cols)

    # --- corpus progress stats (cheap; computed from rows already in memory) ---
    corpus_stats = _corpus_stats(curated_rows, papers, evidence)
    with open(os.path.join(out_dir, "corpus_stats.json"), "w", encoding="utf-8") as fh:
        json.dump(corpus_stats, fh, indent=2)

    # --- site_data.json (compact feed a hosted site fetches; rank-sorted) ---
    # Cap the PUBLISHED feed for very high-volume molecules so the browser-loaded
    # JSON stays light; the full corpus is retained in public_records.csv above.
    feed, feed_stats = _cap_site_feed(public)
    corpus_stats["feed"] = feed_stats
    if feed_stats["capped_molecule_count"]:
        print(f"  site feed capped : {feed_stats['published_records']} of "
              f"{feed_stats['total_public_records']} published "
              f"({feed_stats['capped_molecule_count']} molecule(s) capped; "
              f"focus<= {feed_stats['focus_cap']}, other<= {feed_stats['other_cap']})")
    _write_site_json(os.path.join(out_dir, "site_data.json"), feed, _molecule_index(curated_rows), corpus_stats)

    # --- review_queue.csv ---
    queue = [r for r in curated_rows if r.get("publication_status") == "review"]
    queue.sort(key=lambda r: _int(r.get("rank_score")), reverse=True)
    _write_csv(
        os.path.join(out_dir, "review_queue.csv"),
        queue,
        ["evidence_id", "molecule_name", "pmid", "doi", "title", "website_section",
         "publication_status", "review_reason", "missing_required_fields",
         "reliability_score", "reliability_tier", "display_priority",
         "appraisal_summary", "appraisal_limitations"],
    )

    # --- molecule_index.csv ---
    mol_rows = _molecule_index(curated_rows)
    _write_csv(
        os.path.join(out_dir, "molecule_index.csv"),
        mol_rows,
        ["molecule_id", "molecule_name", "total_records", "auto_published", "listed",
         "review_candidates", "held", "human_evidence", "preclinical_evidence",
         "reviews", "max_reliability", "top_conditions", "sections_present", "pubchem_cid"],
    )

    # --- schema files ---
    _write_schema(out_dir, curated_cols, required)

    return {"stats": stats, "curated": len(curated_rows), "facets_long": len(facets_long_rows),
            "public": len(public), "queue": len(queue), "molecules": len(mol_rows),
            "corpus_stats": corpus_stats}


def _corpus_stats(curated_rows: List[dict], papers: List[dict], evidence: List[dict]) -> dict:
    """Compute lightweight "progress of the growing database" stats.

    Cheap: derived from rows already in memory during the build. Note ``papers``
    / ``evidence`` are the raw corpus tables (so ``total_papers`` reflects the
    full corpus, not just the limited slice ``curated_rows`` may represent).
    """
    import datetime as _dt

    years: List[int] = []
    for r in curated_rows:
        y = _int(r.get("pub_year"))
        if y and 1900 < y < 2100:
            years.append(y)

    filled = sum(1 for r in curated_rows if _int(r.get("citation_count")) > 0 or str(r.get("citation_count", "")).strip() not in {"", "0"})
    total_curated = len(curated_rows)
    pct_citations = round(100.0 * filled / total_curated, 1) if total_curated else 0.0

    molecules_with_data = len({str(r.get("molecule_id", "")) for r in curated_rows if r.get("molecule_id")})
    featured = sum(1 for r in curated_rows if r.get("publication_status") == "featured")
    listed = sum(1 for r in curated_rows if r.get("publication_status") == "listed")

    return {
        "generated_utc": _dt.datetime.utcnow().isoformat() + "Z",
        "total_papers": len(papers),
        "total_evidence": len(evidence),
        "molecules_with_data": molecules_with_data,
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "pct_citations_filled": pct_citations,
        "featured": featured,
        "listed": listed,
    }


# Compact field set the browsable site needs (keeps site_data.json small).
SITE_JSON_FIELDS = [
    "molecule_id", "molecule_name", "pmid", "doi", "title", "journal", "pub_year",
    "authors_short", "first_author", "author_count", "citation_count",
    "icite_rcr", "icite_nih_percentile", "icite_apt",
    "icite_human", "icite_animal", "icite_molecular",
    "icite_x_coord", "icite_y_coord", "icite_is_clinical",
    "website_section", "evidence_class", "evidence_class_label", "publication_status",
    "reliability_score", "reliability_tier", "evidence_directness", "directness_tier",
    "reliability_components", "rank_components",
    "journal_reputation", "journal_tier",
    "rank_score", "rank_tier", "appraisal_summary", "appraisal_strengths", "appraisal_limitations",
    "refined_dose", "refined_route", "refined_duration", "refined_sample_size", "refined_outcome_direction",
    "facet_species", "facet_indication", "facet_endpoint", "facet_study_type",
    "facet_model_system", "facet_route",
    "facet_drug_class", "facet_population", "facet_sex", "facet_formulation", "facet_evidence_direction",
    # NIH iCite-derived facets (impact tier + clinical-article flag) so the site can
    # offer them as filters. Absent on un-enriched papers -> empty string.
    "facet_evidence_impact", "facet_clinical_article",
    "facet_all",
]


def _load_experimental(path: str = os.path.join("config", "EXPERIMENTAL_MOLECULES.csv")) -> List[dict]:
    """Candidate molecules proposed for the experimental section (no data yet).

    These are surfaced so a user can see what's queued to be added; they have no
    evidence until they're promoted into MOLECULES/SEARCH_RULES and fetched.
    """
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not (row.get("molecule_id") or "").strip():
                continue
            # Only surface candidates that are genuinely still experimental / not
            # yet indexed. Rows promoted into the live molecule list (marked e.g.
            # status=live_deduped) are excluded so the public "Experimental" tab
            # never presents already-live molecules as "not yet indexed".
            status = (row.get("status") or "").strip().lower()
            if "live" in status or status in {"promoted", "indexed"}:
                continue
            out.append({k: (v or "").strip() for k, v in row.items()})
    return out


# --- published-feed cap -----------------------------------------------------
# The FULL corpus is always retained (public_records.csv + the SQLite cache).
# But the browser downloads and filters site_data.json entirely in memory, so a
# few very high-volume molecules (rapamycin ~52k, metformin ~35k, ...) would make
# it too heavy for a static host. We therefore cap what each molecule PUBLISHES,
# keeping the signal and shedding bulk:
#   - every review / meta-analysis / synthesis is kept (uncapped; it's the signal),
#   - the remaining budget is filled best-first by rank_score, which already
#     weights human directness, impact (citations) and recency -- so older,
#     low-impact preclinical papers are what drop out first (the user's
#     "dimensionality reduction": keep findings/methods of the important papers).
# Molecules at or under the cap are published in full. Raising FEED_CAP_PER_MOLECULE
# (or removing the cap) is all that's needed once hosting can handle more data.
# The published feed is capped per molecule so the browser-loaded JSON stays light,
# but the cap is SECTION-AWARE rather than a flat number: the two things RetaBase is
# about -- therapeutic use (Human evidence) and mechanism of action (Mechanisms and
# pathways), plus Reviews -- get a high ceiling so well-studied molecules can show
# lots of evidence; the lower-value sections (methods/assays, comparator/background,
# biomarkers, background/context) are limited hard so they don't bloat the feed.
# site_data.json is ~230 bytes/record, so even a big molecule at ~4400 records is a
# few MB. Raise these if you move to a heavier-duty host.
FEED_FOCUS_SECTIONS = {"Human evidence", "Mechanisms and pathways", "Reviews and overviews"}
FEED_FOCUS_CAP = 6000   # per molecule, for the therapy + MoA + review sections
FEED_OTHER_CAP = 500    # per molecule, for every other (lower-value) section
# Regardless of section/cap, ALWAYS publish the highest-signal papers so landmark
# older work is never dropped: every evidence synthesis (review / meta-analysis)
# and anything at/above this NIH percentile (iCite, field/time-normalized impact).
FEED_KEEP_PERCENTILE = 90.0


def _is_landmark(r: dict) -> bool:
    """High-signal record that is always published regardless of the section cap."""
    if str(r.get("evidence_class", "")) == "evidence_synthesis":
        return True
    try:
        return float(r.get("icite_nih_percentile")) >= FEED_KEEP_PERCENTILE
    except (TypeError, ValueError):
        return False


def _cap_site_feed(records: List[dict], focus_cap: int = FEED_FOCUS_CAP, other_cap: int = FEED_OTHER_CAP):
    """Bound the PUBLISHED feed per molecule, section-aware; return (feed, stats).

    ``records`` arrive globally rank-sorted (best first); grouping preserves that
    order, so slicing keeps the top-ranked records within each bucket.
    """
    from collections import defaultdict

    by_mol: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_mol[str(r.get("molecule_id", ""))].append(r)

    kept: List[dict] = []
    capped: Dict[str, dict] = {}
    for mol, recs in by_mol.items():
        landmark = [r for r in recs if _is_landmark(r)]                       # always kept
        rest = [r for r in recs if not _is_landmark(r)]
        focus = [r for r in rest if str(r.get("website_section", "")) in FEED_FOCUS_SECTIONS]
        other = [r for r in rest if str(r.get("website_section", "")) not in FEED_FOCUS_SECTIONS]
        chosen = landmark + focus[:focus_cap] + other[:other_cap]
        kept.extend(chosen)
        if len(chosen) < len(recs):
            capped[mol] = {"total": len(recs), "published": len(chosen), "landmark": len(landmark),
                           "focus": min(len(focus), focus_cap), "other": min(len(other), other_cap)}

    kept.sort(key=lambda r: _int(r.get("rank_score")), reverse=True)
    stats = {
        "focus_cap": focus_cap,
        "other_cap": other_cap,
        "focus_sections": sorted(FEED_FOCUS_SECTIONS),
        "total_public_records": len(records),
        "published_records": len(kept),
        "capped_molecule_count": len(capped),
        "capped_molecules": capped,
    }
    return kept, stats


def _write_site_json(path: str, records: List[dict], molecules: List[dict], corpus_stats: dict | None = None) -> None:
    """Compact, rank-sorted JSON feed for a hosted (fetch-based) site."""
    import datetime as _dt

    trimmed = [{k: _flat(r.get(k, "")) for k in SITE_JSON_FIELDS} for r in records]
    experimental = _load_experimental()
    payload = {
        "generated_utc": _dt.datetime.utcnow().isoformat() + "Z",
        "record_count": len(trimmed),
        "molecule_count": len(molecules),
        "records": trimmed,
        "molecules": molecules,
        "experimental": experimental,
        "corpus_stats": corpus_stats or {},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)


# Optional PubChem enrichment (scripts/enrich_pubchem.py). NETWORK is required to
# generate it, so the file may be absent on a fresh/offline build. Everything here
# degrades gracefully: no file, or a molecule with no CID, simply yields "".
PUBCHEM_CIDS_PATH = os.path.join("config", "pubchem_cids.csv")


def _load_pubchem_cids(path: str = PUBCHEM_CIDS_PATH) -> Dict[str, str]:
    """Map molecule_id -> pubchem_cid from the optional enrichment CSV.

    Fully optional: a missing/unreadable file or a row without a CID contributes
    nothing, so the build never errors on its absence.
    """
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                mid = str(row.get("molecule_id", "") or "").strip()
                cid = str(row.get("pubchem_cid", "") or "").strip()
                if mid and cid:
                    out[mid] = cid
    except (OSError, csv.Error):
        return {}
    return out


def _molecule_index(rows: List[dict], pubchem_by_mol: Dict[str, str] | None = None) -> List[dict]:
    if pubchem_by_mol is None:
        pubchem_by_mol = _load_pubchem_cids()
    by_mol = defaultdict(list)
    for r in rows:
        by_mol[str(r.get("molecule_id", ""))].append(r)
    out = []
    for mol_id, recs in sorted(by_mol.items()):
        name = next((r.get("molecule_name") for r in recs if r.get("molecule_name")), mol_id)
        statuses = Counter(r.get("publication_status") for r in recs)
        sections = Counter(r.get("website_section") for r in recs if r.get("website_section"))
        conditions: Counter = Counter()
        for r in recs:
            for c in str(r.get("facet_indication", "")).split("; "):
                if c:
                    conditions[c] += 1
        human = sum(1 for r in recs if r.get("model_type") == "human")
        preclin = sum(1 for r in recs if r.get("model_type") in {"animal", "in vitro"})
        reviews = sum(1 for r in recs if r.get("model_type") == "review")
        max_rel = max((_int(r.get("reliability_score")) for r in recs), default=0)
        out.append(
            {
                "molecule_id": mol_id,
                "molecule_name": name,
                "total_records": len(recs),
                "auto_published": statuses.get("featured", 0),   # featured = spotlight
                "listed": statuses.get("listed", 0),
                "review_candidates": statuses.get("review", 0),
                "held": statuses.get("excluded_noise", 0),
                "human_evidence": human,
                "preclinical_evidence": preclin,
                "reviews": reviews,
                "max_reliability": max_rel,
                "top_conditions": "; ".join(f"{c}({n})" for c, n in conditions.most_common(5)),
                "sections_present": "; ".join(f"{s}({n})" for s, n in sections.most_common()),
                # Optional PubChem CID for the "View on PubChem" link on the
                # Bioactives page. "" when unknown or the enrichment file is absent.
                "pubchem_cid": pubchem_by_mol.get(mol_id, ""),
            }
        )
    out.sort(key=lambda r: r["auto_published"], reverse=True)
    return out


def _write_schema(out_dir: str, curated_cols: List[str], required) -> None:
    required_ids = {rf.evidence_field for rf in required if rf.requirement == "required"}
    field_dict_rows = []
    descriptions = _field_descriptions()
    for col in curated_cols:
        field_dict_rows.append(
            {
                "field": col,
                "group": _field_group(col),
                "required": "yes" if col in required_ids or col in {"pmid"} else "",
                "description": descriptions.get(col, _auto_desc(col)),
            }
        )
    _write_csv(os.path.join(out_dir, "field_dictionary.csv"), field_dict_rows,
               ["field", "group", "required", "description"])

    schema = {
        "version": 1,
        "generated_by": "build_curated_database.py",
        "backend_notes": "Flat tables; load curated_evidence + facets_long into Google Sheets now; "
                         "the same shape maps to Airtable (curated_evidence = main table, facets_long = "
                         "linked 'Facets' table keyed by evidence_id).",
        "tables": {
            "curated_evidence": {
                "primary_key": "evidence_id",
                "columns": curated_cols,
                "purpose": "One row per (molecule, paper, matching-rule) evidence record.",
            },
            "facets_long": {
                "primary_key": ["evidence_id", "facet_group", "facet_value"],
                "columns": ["evidence_id", "molecule_id", "facet_group", "facet_value", "facet_label", "facet_source"],
                "purpose": "Long/tidy facet table for filtering (e.g. facet_group=species, facet_value=nonhuman_primate).",
            },
            "public_records": {"purpose": "auto_publish_eligible subset for the public site."},
            "review_queue": {"purpose": "Records awaiting human review before publishing."},
            "molecule_index": {"primary_key": "molecule_id", "purpose": "Per-molecule rollup for profile pages."},
        },
        "facet_groups": list(FACET_GROUPS),
        "publication_statuses": [
            "auto_published", "review_candidate", "held_low_evidence",
            "held_missing_fields", "held_out_of_scope",
        ],
    }
    with open(os.path.join(out_dir, "schema.json"), "w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2)


def _field_group(col: str) -> str:
    if col in IDENTITY_FIELDS:
        return "identity"
    if col.startswith("facet_"):
        return "facet"
    if col.startswith("reliability_"):
        return "reliability"
    if col.startswith("journal_"):
        return "journal"
    if col in PUBLICATION_FIELDS:
        return "publication"
    if col in APPRAISAL_FIELDS:
        return "appraisal"
    if col in REFINED_FIELDS:
        return "refined"
    return "structured"


def _field_descriptions() -> Dict[str, str]:
    return {
        "evidence_id": "Stable key: pmid:molecule:rule.",
        "reliability_score": "0-100 composite evidence strength (see reliability_components).",
        "reliability_tier": "high/moderate/limited/low/non_efficacy bucket of reliability_score.",
        "reliability_components": "JSON breakdown of points per scoring component.",
        "publication_status": "auto_published | review_candidate | held_* decision.",
        "website_section": "Public profile section this record would appear in.",
        "auto_publish_eligible": "True if strong+complete enough to publish without review.",
        "review_reason": "Why the record is queued rather than auto-published.",
        "publish_rule_id": "PUBLICATION_RULES.csv rule that matched (audit trail).",
        "display_priority": "Ordering weight within a section (higher shows first).",
        "missing_required_fields": "Required fields absent for this record.",
        "facet_all": "Human-readable blob of all facet labels for free-text search.",
        "llm_summary": "Reserved for an optional future LLM summary (empty now).",
        "appraisal_summary": "Rule-based one-line synopsis of the evidence.",
        "appraisal_limitations": "Rule-based caveats/weaknesses of the evidence.",
    }


def _auto_desc(col: str) -> str:
    if col.startswith("facet_"):
        return f"Normalized facet: {col[len('facet_'):]} (semicolon-joined; filterable)."
    return col.replace("_", " ")


def _blank(v) -> bool:
    return str(v or "").strip() == ""


def _int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _write_csv(path: str, rows: Iterable[dict], columns: List[str]) -> None:
    rows = list(rows)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: _flat(r.get(c, "")) for c in columns})


def _flat(v):
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v)
    return v


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the curated evidence database from local SQLite.")
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite")
    ap.add_argument("--out-dir", default="exports/curated")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N evidence rows (0 = all).")
    args = ap.parse_args()

    result = build(args.db, args.out_dir, args.limit)
    stats = result["stats"]
    print(f"Curated {result['curated']} evidence rows -> {args.out_dir}/")
    print(f"  facets_long rows : {result['facets_long']}")
    print(f"  public_records   : {result['public']}")
    print(f"  review_queue     : {result['queue']}")
    print(f"  molecule_index   : {result['molecules']}")
    print(f"  auto_publish_eligible: {stats['auto_publish']}")
    print(f"  missing required fields: {stats['missing_required']}")
    print(f"  model_primary != model_type (disambiguation impact): {stats['model_disambiguation_changed']}")
    print("  publication_status:", dict(stats["publication_status"]))
    print("  reliability_tier  :", dict(stats["reliability_tier"]))
    print("  website_section   :", dict(stats["website_section"].most_common()))
    print("  species facets    :", dict(stats["facet_species"].most_common()))


if __name__ == "__main__":
    main()
