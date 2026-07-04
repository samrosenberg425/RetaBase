from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

from retarats_pipeline.abstract_extraction import (
    extract_from_title_abstract,
    first_nonblank,
    merge_semicolon,
    semicolon_join,
)

from .clients import PMCFullTextClient
from .common import clean_text, is_blankish, utc_now_iso
from .field_quality import lane_completeness, should_suggest_replacement


PMC_ELIGIBLE_LANES = {
    "preclinical_intervention",
    "mechanism_or_pathway",
    "biomarker_or_readout",
    "review_or_meta_analysis",
}

PMC_USEFUL_MISSING_FIELDS = {
    "model_type",
    "model_system_detail",
    "mechanistic_focus",
    "condition_tags",
    "endpoint_tags",
    "comparator_or_control",
    "dose_route",
    "duration",
    "sample_size",
    "outcome_direction",
    "efficacy_signal",
    "safety_signal",
    "role_evidence_text",
}


def maybe_enrich_with_pmc(
    row: MutableMapping[str, Any],
    paper: Mapping[str, Any],
    required_fields: Sequence[str],
    pmc_client: PMCFullTextClient,
    enable_pmc: bool,
) -> Tuple[MutableMapping[str, Any], dict]:
    """Optionally add PMC full-text suggestions to one evidence row.

    This function is deliberately conservative: it only proposes `pmc_*` and
    `suggested_*` fields. It does not replace core fields directly.
    """
    decision, reason = pmc_fallback_decision(row, required_fields)
    row["pmc_enrichment_eligible"] = bool(decision)
    row["pmc_enrichment_reason"] = reason
    row["pmc_enrichment_attempted"] = False
    row["pmc_enrichment_status"] = "skipped_disabled" if not enable_pmc else "skipped_not_eligible"

    audit = _audit_base(row)
    if not enable_pmc or not decision:
        audit.update(_audit_pmc_fields(row))
        return row, audit

    row["pmc_enrichment_attempted"] = True
    row["pmc_enrichment_at_utc"] = utc_now_iso()
    pmid = clean_text(row.get("pmid"))
    bioc_data, bioc_source = pmc_client.fetch_bioc_json(pmid)
    row["pmc_bioc_source"] = bioc_source
    parsed = parse_bioc_json(bioc_data)
    if parsed.get("status") == "bioc_json_parsed":
        _apply_parsed_full_text(row, parsed, paper)
        audit.update(_audit_pmc_fields(row))
        return row, audit

    pmcid, lookup_source = pmc_client.pmcid_for_pmid(pmid)
    row["pmc_lookup_source"] = lookup_source
    row["pmcid"] = pmcid
    if not pmcid:
        row["pmc_enrichment_status"] = "no_pmc_link_found"
        audit.update(_audit_pmc_fields(row))
        return row, audit

    xml_text, fetch_source = pmc_client.fetch_pmc_xml(pmcid)
    row["pmc_fetch_source"] = fetch_source
    if not xml_text or xml_text.startswith("ERROR"):
        row["pmc_enrichment_status"] = "pmc_fetch_failed"
        row["pmc_fetch_error"] = xml_text[:500]
        audit.update(_audit_pmc_fields(row))
        return row, audit

    parsed = parse_pmc_xml(xml_text)
    _apply_parsed_full_text(row, parsed, paper)
    row["pmc_full_text_format"] = "jats_xml"
    audit.update(_audit_pmc_fields(row))
    return row, audit


def pmc_fallback_decision(row: Mapping[str, Any], required_fields: Sequence[str]) -> Tuple[bool, str]:
    lane = clean_text(row.get("processing_lane"))
    if lane not in PMC_ELIGIBLE_LANES:
        return False, f"lane_not_pmc_target:{lane or 'missing'}"
    if lane == "review_or_meta_analysis":
        return False, "review_pmc_extraction_deferred"
    completion, missing = lane_completeness(row, required_fields)
    missing_fields = {m.strip() for m in re.split(r"[;|]", missing) if m.strip()}
    useful_missing = sorted(missing_fields & PMC_USEFUL_MISSING_FIELDS)
    if not useful_missing and str(row.get("public_candidate", "")).lower() != "true":
        return False, "no_high_value_missing_fields"
    if lane == "preclinical_intervention" and str(row.get("public_candidate", "")).lower() == "true":
        return True, f"high_value_preclinical_missing:{semicolon_join(useful_missing) or 'none'}"
    if completion < 0.9 and useful_missing:
        return True, f"incomplete_{lane}:{semicolon_join(useful_missing)}"
    if lane in {"mechanism_or_pathway", "biomarker_or_readout"} and useful_missing:
        return True, f"mechanism_or_biomarker_missing:{semicolon_join(useful_missing)}"
    return False, "complete_enough_after_abstract"


def parse_pmc_xml(xml_text: str) -> Dict[str, str]:
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except Exception as exc:
        return {"status": "xml_parse_failed", "parse_error": str(exc)[:300]}
    sections: Dict[str, List[str]] = {}
    section_titles: List[str] = []
    for sec in root.findall(".//{*}sec"):
        title = _first_text(sec, "./{*}title")
        label = _section_label(title)
        if not label:
            continue
        text = _section_text(sec)
        if not text:
            continue
        sections.setdefault(label, []).append(text)
        section_titles.append(title or label)
    license_text = semicolon_join(_node_text(node) for node in root.findall(".//{*}license-p"))
    methods = _joined_sections(sections, ["methods"])
    results = _joined_sections(sections, ["results"])
    discussion = _joined_sections(sections, ["discussion", "conclusion"])
    abstract = semicolon_join(_node_text(node) for node in root.findall(".//{*}abstract"))
    extraction_text = _limit_text(" ".join([methods, results, discussion, abstract]), 18000)
    return {
        "status": "pmc_xml_parsed",
        "sections_found": semicolon_join(section_titles[:30]),
        "sections_used": semicolon_join(k for k in ["methods", "results", "discussion", "conclusion", "abstract"] if sections.get(k) or (k == "abstract" and abstract)),
        "license": _limit_text(license_text, 500),
        "methods_excerpt": _limit_text(methods, 3000),
        "results_excerpt": _limit_text(results, 3000),
        "discussion_excerpt": _limit_text(discussion, 2000),
        "extraction_text": extraction_text,
    }


def parse_bioc_json(data: Any) -> Dict[str, str]:
    if not isinstance(data, Mapping):
        return {"status": "bioc_unavailable_or_unparsed"}
    documents = data.get("documents") or []
    if not isinstance(documents, list) or not documents:
        return {"status": "bioc_unavailable_or_unparsed"}
    sections: Dict[str, List[str]] = {}
    section_titles: List[str] = []
    for document in documents:
        for passage in document.get("passages") or []:
            infons = passage.get("infons") or {}
            raw_section = clean_text(
                infons.get("section_type")
                or infons.get("section")
                or infons.get("type")
                or infons.get("iao_name")
                or infons.get("title")
            )
            text = clean_text(passage.get("text"))
            label = _section_label(raw_section) or _section_label(text[:80])
            if not label or not text:
                continue
            sections.setdefault(label, []).append(text)
            section_titles.append(raw_section or label)
    methods = _joined_sections(sections, ["methods"])
    results = _joined_sections(sections, ["results"])
    discussion = _joined_sections(sections, ["discussion", "conclusion"])
    abstract = _joined_sections(sections, ["abstract"])
    extraction_text = _limit_text(" ".join([methods, results, discussion, abstract]), 18000)
    if not extraction_text:
        return {"status": "bioc_no_target_sections"}
    return {
        "status": "bioc_json_parsed",
        "sections_found": semicolon_join(section_titles[:30]),
        "sections_used": semicolon_join(k for k in ["methods", "results", "discussion", "conclusion", "abstract"] if sections.get(k)),
        "methods_excerpt": _limit_text(methods, 3000),
        "results_excerpt": _limit_text(results, 3000),
        "discussion_excerpt": _limit_text(discussion, 2000),
        "extraction_text": extraction_text,
    }


def _apply_parsed_full_text(row: MutableMapping[str, Any], parsed: Mapping[str, str], paper: Mapping[str, Any]) -> None:
    row["pmc_enrichment_status"] = parsed.get("status", "parsed")
    row["pmc_sections_found"] = parsed.get("sections_found", "")
    row["pmc_license"] = parsed.get("license", "")
    row["pmc_methods_excerpt"] = parsed.get("methods_excerpt", "")
    row["pmc_results_excerpt"] = parsed.get("results_excerpt", "")
    row["pmc_discussion_excerpt"] = parsed.get("discussion_excerpt", "")
    row["pmc_sections_used"] = parsed.get("sections_used", "")
    if parsed.get("status") == "bioc_json_parsed":
        row["pmc_full_text_format"] = "bioc_json"
    extraction_text = parsed.get("extraction_text", "")
    if extraction_text:
        extracted = extract_from_title_abstract(
            title=first_nonblank(paper.get("title"), row.get("enriched_source_title")),
            abstract=extraction_text,
            mesh_terms=paper.get("mesh_terms", ""),
            pubtypes=paper.get("pubtypes", ""),
        ).to_dict()
        _apply_pmc_extraction(row, extracted)
    _suggest_pmc_replacements(row)


def _apply_pmc_extraction(row: MutableMapping[str, Any], extracted: Mapping[str, Any]) -> None:
    mapping = {
        "pmc_model_type": "abstract_model_type",
        "pmc_model_system_detail": "abstract_model_system_detail",
        "pmc_condition_tags": "abstract_condition_tags",
        "pmc_endpoint_tags": "abstract_endpoint_tags",
        "pmc_mechanistic_focus": "abstract_mechanistic_focus",
        "pmc_comparator_or_control": "abstract_comparator_or_control",
        "pmc_dose_route": "abstract_dose_route",
        "pmc_duration": "abstract_duration",
        "pmc_sample_size": "abstract_sample_size",
        "pmc_efficacy_signal": "abstract_efficacy_signal",
        "pmc_safety_signal": "abstract_safety_signal",
        "pmc_outcome_direction": "abstract_outcome_direction",
        "pmc_key_sentences": "abstract_key_sentences",
        "pmc_extraction_confidence": "abstract_extraction_confidence",
        "pmc_extraction_notes": "abstract_extraction_notes",
    }
    for out_field, source_field in mapping.items():
        value = extracted.get(source_field)
        if not is_blankish(value):
            row[out_field] = value


def _suggest_pmc_replacements(row: MutableMapping[str, Any]) -> None:
    candidates = {
        "model_type": "pmc_model_type",
        "model_system_detail": "pmc_model_system_detail",
        "mechanistic_focus": "pmc_mechanistic_focus",
        "condition_tags": "pmc_condition_tags",
        "endpoint_tags": "pmc_endpoint_tags",
        "comparator_or_control": "pmc_comparator_or_control",
        "dose_route": "pmc_dose_route",
        "duration": "pmc_duration",
        "sample_size": "pmc_sample_size",
        "outcome_direction": "pmc_outcome_direction",
        "efficacy_signal": "pmc_efficacy_signal",
        "safety_signal": "pmc_safety_signal",
    }
    tag_fields = {"mechanistic_focus", "condition_tags", "endpoint_tags", "model_system_detail"}
    for original_field, pmc_field in candidates.items():
        pmc_value = row.get(pmc_field)
        if is_blankish(pmc_value):
            continue
        current_suggestion = row.get(f"suggested_{original_field}")
        if original_field in tag_fields:
            merged = merge_semicolon(current_suggestion, pmc_value)
            if merged:
                row[f"suggested_{original_field}"] = merged
                row[f"suggest_replace_{original_field}"] = True
                row[f"suggested_{original_field}_source"] = _source_merge(row.get(f"suggested_{original_field}_source"), "pmc_full_text")
            continue
        if current_suggestion and not should_suggest_replacement(original_field, row.get(original_field), current_suggestion):
            continue
        if should_suggest_replacement(original_field, row.get(original_field), pmc_value):
            row[f"suggested_{original_field}"] = clean_text(pmc_value)
            row[f"suggest_replace_{original_field}"] = True
            row[f"suggested_{original_field}_source"] = _source_merge(row.get(f"suggested_{original_field}_source"), "pmc_full_text")


def _audit_base(row: Mapping[str, Any]) -> dict:
    return {
        "evidence_id": row.get("evidence_id"),
        "pmid": row.get("pmid"),
        "molecule_id": row.get("molecule_id"),
        "molecule_name": row.get("molecule_name"),
        "processing_lane": row.get("processing_lane"),
        "public_candidate": row.get("public_candidate"),
    }


def _audit_pmc_fields(row: Mapping[str, Any]) -> dict:
    return {
        "pmc_enrichment_eligible": row.get("pmc_enrichment_eligible"),
        "pmc_enrichment_attempted": row.get("pmc_enrichment_attempted"),
        "pmc_enrichment_reason": row.get("pmc_enrichment_reason"),
        "pmc_enrichment_status": row.get("pmc_enrichment_status"),
        "pmcid": row.get("pmcid"),
        "pmc_bioc_source": row.get("pmc_bioc_source"),
        "pmc_full_text_format": row.get("pmc_full_text_format"),
        "pmc_lookup_source": row.get("pmc_lookup_source"),
        "pmc_fetch_source": row.get("pmc_fetch_source"),
        "pmc_sections_used": row.get("pmc_sections_used"),
        "pmc_model_system_detail": row.get("pmc_model_system_detail"),
        "pmc_mechanistic_focus": row.get("pmc_mechanistic_focus"),
        "pmc_condition_tags": row.get("pmc_condition_tags"),
        "pmc_endpoint_tags": row.get("pmc_endpoint_tags"),
        "pmc_comparator_or_control": row.get("pmc_comparator_or_control"),
        "pmc_dose_route": row.get("pmc_dose_route"),
        "pmc_duration": row.get("pmc_duration"),
        "pmc_sample_size": row.get("pmc_sample_size"),
    }


def _section_label(title: str) -> str:
    title_l = clean_text(title).lower()
    if not title_l:
        return ""
    if any(term in title_l for term in ["method", "materials", "experimental procedures", "study design"]):
        return "methods"
    if "result" in title_l or "finding" in title_l:
        return "results"
    if "discussion" in title_l:
        return "discussion"
    if "conclusion" in title_l:
        return "conclusion"
    return ""


def _joined_sections(sections: Mapping[str, Sequence[str]], labels: Iterable[str]) -> str:
    texts: List[str] = []
    for label in labels:
        texts.extend(sections.get(label, []))
    return _limit_text(" ".join(texts), 9000)


def _section_text(node: ET.Element) -> str:
    parts = []
    for p in node.findall(".//{*}p"):
        text = _node_text(p)
        if text:
            parts.append(text)
    return clean_text(" ".join(parts))


def _node_text(node: ET.Element) -> str:
    return clean_text(" ".join(node.itertext()))


def _first_text(node: ET.Element, path: str) -> str:
    found = node.find(path)
    return _node_text(found) if found is not None else ""


def _limit_text(text: str, max_chars: int) -> str:
    text = clean_text(text)
    return text[:max_chars]


def _source_merge(existing: Any, new: str) -> str:
    return merge_semicolon(existing, new)
