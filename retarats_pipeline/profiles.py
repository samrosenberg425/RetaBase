from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from typing import Dict, Iterable, List


PROFILE_FIELDS = [
    "molecule_id", "molecule_name", "total_evidence_links", "website_include_count",
    "public_candidate_count", "curator_review_count", "background_only_count",
    "exclude_noise_count", "primary_intervention_count", "direct_intervention_role_count",
    "human_study_count", "rct_count", "review_count",
    "animal_count", "in_vitro_count", "synthesis_or_assay_count",
    "environmental_or_material_count", "background_or_unclear_count",
    "latest_pub_year", "strongest_evidence_level", "top_role_categories",
    "top_mechanism_tags", "top_therapeutic_area_tags", "concise_evidence_profile",
    "profile_updated_at_utc",
]


def load_evidence_payloads(local_db: str) -> List[dict]:
    conn = sqlite3.connect(local_db)
    rows = []
    for (payload_json,) in conn.execute("select payload_json from evidence"):
        rows.append(json.loads(payload_json))
    return rows


def build_molecule_profiles(molecules: Dict[str, dict], evidence_rows: Iterable[dict], *, updated_at: str) -> List[dict]:
    grouped = defaultdict(list)
    for row in evidence_rows:
        grouped[str(row.get("molecule_id", ""))].append(row)

    profiles = []
    for molecule_id, molecule in molecules.items():
        rows = grouped.get(molecule_id, [])
        profiles.append(_profile_for_molecule(molecule_id, molecule, rows, updated_at))
    return profiles


def _profile_for_molecule(molecule_id: str, molecule: dict, rows: List[dict], updated_at: str) -> dict:
    primary_types = Counter(row.get("primary_study_type", "") for row in rows)
    relevance = Counter(row.get("molecule_relevance", "") for row in rows)
    role_categories = Counter(row.get("role_category", "") for row in rows if row.get("role_category"))
    review_buckets = Counter(row.get("role_review_bucket", "") for row in rows if row.get("role_review_bucket"))
    included = [row for row in rows if bool(row.get("website_include"))]
    latest_year = _latest_year(rows)
    strongest = _strongest_evidence_level(rows)
    top_mechanisms = _top_tags(rows, "mechanism_tags")
    top_areas = _top_tags(rows, "therapeutic_area_tags")
    profile = _concise_profile(molecule, rows, included, primary_types, relevance, role_categories, review_buckets, strongest)
    return {
        "molecule_id": molecule_id,
        "molecule_name": molecule.get("display_name", molecule_id),
        "total_evidence_links": len(rows),
        "website_include_count": len(included),
        "public_candidate_count": review_buckets.get("public_candidate", 0),
        "curator_review_count": review_buckets.get("curator_review", 0),
        "background_only_count": review_buckets.get("background_only", 0),
        "exclude_noise_count": review_buckets.get("exclude_noise", 0),
        "primary_intervention_count": relevance.get("primary_intervention", 0),
        "direct_intervention_role_count": role_categories.get("direct_intervention", 0),
        "human_study_count": _count_model(rows, "human"),
        "rct_count": primary_types.get("RCT", 0),
        "review_count": sum(v for k, v in primary_types.items() if "review" in k.lower() or "meta-analysis" in k.lower()),
        "animal_count": _count_model(rows, "animal"),
        "in_vitro_count": _count_model(rows, "in vitro"),
        "synthesis_or_assay_count": relevance.get("synthesis_or_assay", 0),
        "environmental_or_material_count": relevance.get("environmental_or_material_use", 0),
        "background_or_unclear_count": sum(relevance.get(k, 0) for k in ["background_mention", "unclear_match"]),
        "latest_pub_year": latest_year,
        "strongest_evidence_level": strongest,
        "top_role_categories": _format_counter(role_categories),
        "top_mechanism_tags": _format_counter(top_mechanisms),
        "top_therapeutic_area_tags": _format_counter(top_areas),
        "concise_evidence_profile": profile,
        "profile_updated_at_utc": updated_at,
    }


def _concise_profile(
    molecule: dict,
    rows: List[dict],
    included: List[dict],
    primary_types: Counter,
    relevance: Counter,
    role_categories: Counter,
    review_buckets: Counter,
    strongest: str,
) -> str:
    name = molecule.get("display_name", "This molecule")
    if not rows:
        return f"{name}: no matched PubMed evidence in the current local database."
    parts = [
        f"{name}: {len(rows)} matched evidence links",
        f"{review_buckets.get('public_candidate', len(included))} public candidates",
        f"{review_buckets.get('curator_review', 0)} need curator review",
        f"strongest level: {strongest}",
    ]
    if primary_types.get("RCT"):
        parts.append(f"{primary_types['RCT']} RCT")
    if relevance.get("primary_intervention"):
        parts.append(f"{relevance['primary_intervention']} primary-intervention paper")
    if role_categories.get("direct_intervention"):
        parts.append(f"{role_categories['direct_intervention']} direct-intervention role")
    if relevance.get("synthesis_or_assay") or relevance.get("environmental_or_material_use"):
        excluded = relevance.get("synthesis_or_assay", 0) + relevance.get("environmental_or_material_use", 0)
        parts.append(f"{excluded} likely non-efficacy/method/material records")
    return "; ".join(parts) + "."


def _strongest_evidence_level(rows: List[dict]) -> str:
    if any(row.get("primary_study_type") == "RCT" for row in rows):
        return "human_rct"
    if any("Meta-analysis" in str(row.get("primary_study_type", "")) for row in rows):
        return "systematic_review_or_meta_analysis"
    if any(row.get("model_type") == "human" for row in rows):
        return "human_non_rct"
    if any(row.get("model_type") == "animal" for row in rows):
        return "animal"
    if any(row.get("model_type") == "in vitro" for row in rows):
        return "in_vitro"
    if rows:
        return "unclear_or_non_efficacy"
    return "none"


def _count_model(rows: List[dict], model_type: str) -> int:
    return sum(1 for row in rows if row.get("model_type") == model_type)


def _latest_year(rows: List[dict]) -> str:
    years = []
    for row in rows:
        value = str(row.get("pub_year", "") or row.get("fetched_at_utc", ""))
        for token in value.replace("-", " ").split():
            if token.isdigit() and len(token) == 4:
                years.append(int(token))
                break
    return str(max(years)) if years else ""


def _top_tags(rows: List[dict], field: str) -> Counter:
    out = Counter()
    for row in rows:
        for tag in str(row.get(field, "") or "").split(";"):
            tag = tag.strip()
            if tag:
                out[tag] += 1
    return out


def _format_counter(counter: Counter, n: int = 5) -> str:
    return "; ".join(f"{key}:{value}" for key, value in counter.most_common(n))
