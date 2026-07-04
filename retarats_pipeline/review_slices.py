from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

import pandas as pd


SLICE_FIELDS = [
    "slice_id", "slice_name", "question_type", "molecule_ids", "processing_lanes",
    "paper_purposes", "model_types", "primary_study_types", "role_categories",
    "condition_tags_include", "endpoint_tags_include", "evidence_strength_min",
    "year_min", "year_max", "include_reviews", "include_methods", "include_unclear",
    "text_include", "text_exclude", "notes",
]


@dataclass
class ReviewSlice:
    slice_id: str
    slice_name: str
    question_type: str
    molecule_ids: List[str]
    processing_lanes: List[str]
    paper_purposes: List[str]
    model_types: List[str]
    primary_study_types: List[str]
    role_categories: List[str]
    condition_tags_include: List[str]
    endpoint_tags_include: List[str]
    evidence_strength_min: int
    year_min: int
    year_max: int
    include_reviews: bool
    include_methods: bool
    include_unclear: bool
    text_include: List[str]
    text_exclude: List[str]
    notes: str

    def to_dict(self) -> dict:
        return {
            "slice_id": self.slice_id,
            "slice_name": self.slice_name,
            "question_type": self.question_type,
            "molecule_ids": "|".join(self.molecule_ids),
            "processing_lanes": "|".join(self.processing_lanes),
            "paper_purposes": "|".join(self.paper_purposes),
            "model_types": "|".join(self.model_types),
            "primary_study_types": "|".join(self.primary_study_types),
            "role_categories": "|".join(self.role_categories),
            "condition_tags_include": "|".join(self.condition_tags_include),
            "endpoint_tags_include": "|".join(self.endpoint_tags_include),
            "evidence_strength_min": self.evidence_strength_min,
            "year_min": self.year_min or "",
            "year_max": self.year_max or "",
            "include_reviews": self.include_reviews,
            "include_methods": self.include_methods,
            "include_unclear": self.include_unclear,
            "text_include": "|".join(self.text_include),
            "text_exclude": "|".join(self.text_exclude),
            "notes": self.notes,
        }


def load_review_slices(path: str) -> List[ReviewSlice]:
    slices: List[ReviewSlice] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            slice_id = _clean(row.get("slice_id", ""))
            if not slice_id:
                continue
            slices.append(
                ReviewSlice(
                    slice_id=slice_id,
                    slice_name=_clean(row.get("slice_name", "")) or slice_id,
                    question_type=_clean(row.get("question_type", "")) or "unspecified",
                    molecule_ids=_split(row.get("molecule_ids", "")),
                    processing_lanes=_split(row.get("processing_lanes", "")),
                    paper_purposes=_split(row.get("paper_purposes", "")),
                    model_types=_split(row.get("model_types", "")),
                    primary_study_types=_split(row.get("primary_study_types", "")),
                    role_categories=_split(row.get("role_categories", "")),
                    condition_tags_include=_split(row.get("condition_tags_include", "")),
                    endpoint_tags_include=_split(row.get("endpoint_tags_include", "")),
                    evidence_strength_min=_int(row.get("evidence_strength_min", ""), 0),
                    year_min=_int(row.get("year_min", ""), 0),
                    year_max=_int(row.get("year_max", ""), 0),
                    include_reviews=_bool(row.get("include_reviews", "")),
                    include_methods=_bool(row.get("include_methods", "")),
                    include_unclear=_bool(row.get("include_unclear", "")),
                    text_include=_split(row.get("text_include", "")),
                    text_exclude=_split(row.get("text_exclude", "")),
                    notes=_clean(row.get("notes", "")),
                )
            )
    return slices


def apply_review_slice(evidence: pd.DataFrame, review_slice: ReviewSlice) -> Tuple[pd.DataFrame, pd.DataFrame]:
    current = evidence.copy()
    flow_rows = [_flow_row(review_slice, "records_available", len(current), "All characterized evidence rows.")]

    current = _filter_exact(current, "molecule_id", review_slice.molecule_ids)
    flow_rows.append(_flow_row(review_slice, "after_molecule_filter", len(current), _rule_text("molecule_id", review_slice.molecule_ids)))

    current = _filter_exact(current, "processing_lane", review_slice.processing_lanes)
    flow_rows.append(_flow_row(review_slice, "after_processing_lane_filter", len(current), _rule_text("processing_lane", review_slice.processing_lanes)))

    current = _filter_exact(current, "paper_purpose", review_slice.paper_purposes)
    flow_rows.append(_flow_row(review_slice, "after_paper_purpose_filter", len(current), _rule_text("paper_purpose", review_slice.paper_purposes)))

    current = _filter_exact(current, "model_type", review_slice.model_types)
    flow_rows.append(_flow_row(review_slice, "after_model_filter", len(current), _rule_text("model_type", review_slice.model_types)))

    current = _filter_exact(current, "primary_study_type", review_slice.primary_study_types)
    flow_rows.append(_flow_row(review_slice, "after_study_type_filter", len(current), _rule_text("primary_study_type", review_slice.primary_study_types)))

    current = _filter_exact(current, "role_category", review_slice.role_categories)
    flow_rows.append(_flow_row(review_slice, "after_role_filter", len(current), _rule_text("role_category", review_slice.role_categories)))

    current = _filter_tags(current, "condition_tags", review_slice.condition_tags_include)
    flow_rows.append(_flow_row(review_slice, "after_condition_filter", len(current), _rule_text("condition_tags", review_slice.condition_tags_include)))

    current = _filter_tags(current, "endpoint_tags", review_slice.endpoint_tags_include)
    flow_rows.append(_flow_row(review_slice, "after_endpoint_filter", len(current), _rule_text("endpoint_tags", review_slice.endpoint_tags_include)))

    current = _filter_strength(current, review_slice.evidence_strength_min)
    flow_rows.append(_flow_row(review_slice, "after_evidence_strength_filter", len(current), f"evidence_strength_score >= {review_slice.evidence_strength_min}"))

    current = _filter_year(current, review_slice.year_min, review_slice.year_max)
    flow_rows.append(_flow_row(review_slice, "after_year_filter", len(current), _year_rule_text(review_slice)))

    current = _filter_default_exclusions(current, review_slice)
    flow_rows.append(_flow_row(review_slice, "after_default_exclusions", len(current), _default_exclusion_text(review_slice)))

    current = _filter_text(current, review_slice.text_include, include=True)
    flow_rows.append(_flow_row(review_slice, "after_text_include_filter", len(current), _rule_text("text_include", review_slice.text_include)))

    current = _filter_text(current, review_slice.text_exclude, include=False)
    flow_rows.append(_flow_row(review_slice, "after_text_exclude_filter", len(current), _rule_text("text_exclude", review_slice.text_exclude)))

    current = current.copy()
    current.insert(0, "slice_id", review_slice.slice_id)
    current.insert(1, "slice_name", review_slice.slice_name)
    current.insert(2, "slice_question_type", review_slice.question_type)
    current.insert(3, "slice_notes", review_slice.notes)

    return current, pd.DataFrame(flow_rows)


def build_exclusion_summary(flow: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for slice_id, group in flow.groupby("slice_id", sort=False):
        previous = None
        for _, row in group.iterrows():
            count = int(row["records_remaining"])
            if previous is not None:
                excluded = max(previous - count, 0)
                if excluded:
                    rows.append({
                        "slice_id": slice_id,
                        "slice_name": row["slice_name"],
                        "step": row["step"],
                        "excluded_count": excluded,
                        "rule": row["rule"],
                    })
            previous = count
    return pd.DataFrame(rows)


def methods_summary(slices: List[ReviewSlice], flow: pd.DataFrame) -> str:
    lines = [
        "# Review Slice Methods Summary",
        "",
        "This output is a PRISMA-S-informed evidence-map export, not a complete PRISMA systematic review.",
        "Slices are saved filters applied to the broad PubMed evidence database.",
        "Filters are OR within each slice field and AND across fields.",
        "",
        "## Slice Definitions",
        "",
    ]
    for review_slice in slices:
        lines.extend([
            f"### {review_slice.slice_name}",
            "",
            f"- `slice_id`: `{review_slice.slice_id}`",
            f"- Question type: {review_slice.question_type}",
            f"- Molecules: {_display(review_slice.molecule_ids)}",
            f"- Processing lanes: {_display(review_slice.processing_lanes)}",
            f"- Paper purposes: {_display(review_slice.paper_purposes)}",
            f"- Models: {_display(review_slice.model_types)}",
            f"- Study types: {_display(review_slice.primary_study_types)}",
            f"- Condition tags: {_display(review_slice.condition_tags_include)}",
            f"- Endpoint tags: {_display(review_slice.endpoint_tags_include)}",
            f"- Minimum evidence strength: {review_slice.evidence_strength_min}",
            f"- Notes: {review_slice.notes or 'not specified'}",
            "",
        ])
        subset = flow[flow["slice_id"] == review_slice.slice_id]
        if not subset.empty:
            final = subset.iloc[-1]
            lines.append(f"Final included records: {int(final['records_remaining'])}")
            lines.append("")
    return "\n".join(lines) + "\n"


def _filter_exact(df: pd.DataFrame, field: str, values: List[str]) -> pd.DataFrame:
    if not values or field not in df:
        return df
    allowed = {v.lower() for v in values}
    return df[df[field].fillna("").astype(str).str.lower().isin(allowed)].copy()


def _filter_tags(df: pd.DataFrame, field: str, values: List[str]) -> pd.DataFrame:
    if not values or field not in df:
        return df
    return df[df[field].fillna("").astype(str).map(lambda x: _has_any_tag(x, values))].copy()


def _filter_strength(df: pd.DataFrame, minimum: int) -> pd.DataFrame:
    if not minimum or "evidence_strength_score" not in df:
        return df
    score = pd.to_numeric(df["evidence_strength_score"], errors="coerce").fillna(0)
    return df[score >= minimum].copy()


def _filter_year(df: pd.DataFrame, year_min: int, year_max: int) -> pd.DataFrame:
    if not year_min and not year_max:
        return df
    if "pub_year" not in df:
        return df
    year = pd.to_numeric(df["pub_year"], errors="coerce").fillna(0)
    mask = pd.Series(True, index=df.index)
    if year_min:
        mask &= year >= year_min
    if year_max:
        mask &= year <= year_max
    return df[mask].copy()


def _filter_default_exclusions(df: pd.DataFrame, review_slice: ReviewSlice) -> pd.DataFrame:
    current = df
    if not review_slice.include_reviews and "processing_lane" in current:
        current = current[current["processing_lane"].astype(str) != "review_or_meta_analysis"]
    if not review_slice.include_methods and "processing_lane" in current:
        current = current[~current["processing_lane"].astype(str).isin({
            "methods_assay_synthesis", "diagnostic_or_tool_use", "environmental_or_materials",
        })]
    if not review_slice.include_unclear and "processing_lane" in current:
        current = current[~current["processing_lane"].astype(str).isin({"unclear_manual_triage", "general_context"})]
    return current.copy()


def _filter_text(df: pd.DataFrame, terms: List[str], *, include: bool) -> pd.DataFrame:
    if not terms:
        return df
    haystack = _text_haystack(df)
    match = haystack.map(lambda x: _has_any_text(x, terms))
    return df[match if include else ~match].copy()


def _text_haystack(df: pd.DataFrame) -> pd.Series:
    fields = [
        "title", "abstract", "evidence_summary", "what_it_is", "condition_tags",
        "endpoint_tags", "mechanistic_focus", "intervention_or_exposure",
    ]
    present = [field for field in fields if field in df]
    if not present:
        return pd.Series("", index=df.index)
    return df[present].fillna("").astype(str).agg(" ".join, axis=1).str.lower()


def _has_any_tag(value: str, terms: List[str]) -> bool:
    tags = {tag.strip().lower() for tag in str(value or "").replace("|", ";").split(";") if tag.strip()}
    wanted = {term.lower() for term in terms}
    return bool(tags & wanted)


def _has_any_text(value: str, terms: List[str]) -> bool:
    value = str(value or "").lower()
    return any(_phrase_in_text(value, term.lower()) for term in terms)


def _phrase_in_text(text: str, term: str) -> bool:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None


def _flow_row(review_slice: ReviewSlice, step: str, count: int, rule: str) -> dict:
    return {
        "slice_id": review_slice.slice_id,
        "slice_name": review_slice.slice_name,
        "step": step,
        "records_remaining": count,
        "rule": rule,
    }


def _split(value: str) -> List[str]:
    return [part.strip() for part in str(value or "").replace(";", "|").split("|") if part.strip()]


def _clean(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _int(value: str, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _rule_text(field: str, values: List[str]) -> str:
    return f"{field}: {' OR '.join(values)}" if values else f"{field}: no filter"


def _year_rule_text(review_slice: ReviewSlice) -> str:
    if review_slice.year_min and review_slice.year_max:
        return f"pub_year between {review_slice.year_min} and {review_slice.year_max}"
    if review_slice.year_min:
        return f"pub_year >= {review_slice.year_min}"
    if review_slice.year_max:
        return f"pub_year <= {review_slice.year_max}"
    return "pub_year: no filter"


def _default_exclusion_text(review_slice: ReviewSlice) -> str:
    excluded = []
    if not review_slice.include_reviews:
        excluded.append("review_or_meta_analysis")
    if not review_slice.include_methods:
        excluded.extend(["methods_assay_synthesis", "diagnostic_or_tool_use", "environmental_or_materials"])
    if not review_slice.include_unclear:
        excluded.extend(["unclear_manual_triage", "general_context"])
    return "excluded lanes: " + ", ".join(excluded) if excluded else "no default lane exclusions"


def _display(values: List[str]) -> str:
    return ", ".join(values) if values else "not filtered"
