from __future__ import annotations

from typing import Any, Dict, List, Mapping, MutableMapping, Sequence, Tuple

from retarats_pipeline.abstract_extraction import extract_from_title_abstract, merge_semicolon

from .clients import AnnotationClient, PMCFullTextClient
from .common import APIConfig, CachedHTTPClient, clean_text, first_nonblank, is_blankish, semicolon_join, text_blob, utc_now_iso
from .field_quality import (
    CONDITION_PATTERNS,
    ENDPOINT_PATTERNS,
    MECHANISM_PATTERNS,
    detect_species_model,
    detect_tags,
    extract_contextual_values,
    lane_completeness,
    merge_tags,
    should_suggest_replacement,
)
from .pmc import maybe_enrich_with_pmc

BASIC_LANES = {
    "preclinical_intervention",
    "mechanism_or_pathway",
    "biomarker_or_readout",
    "methods_assay_synthesis",
    "diagnostic_or_tool_use",
    "environmental_or_materials",
}

REQUIRED_BY_LANE = {
    "preclinical_intervention": ["model_type", "model_system_detail", "intervention_or_exposure", "comparator_or_control", "dose_route", "endpoint_tags", "outcome_direction"],
    "mechanism_or_pathway": ["model_type", "model_system_detail", "mechanistic_focus", "condition_tags", "endpoint_tags", "role_evidence_text"],
    "biomarker_or_readout": ["population_or_sample", "condition_tags", "endpoint_tags", "mechanistic_focus", "outcome_direction", "role_evidence_text"],
    "methods_assay_synthesis": ["methods_focus", "what_it_is"],
    "diagnostic_or_tool_use": ["methods_focus", "what_it_is"],
    "environmental_or_materials": ["methods_focus", "what_it_is"],
}


def enrich_basic_science(
    evidence_rows: Sequence[Mapping[str, Any]],
    paper_by_pmid: Mapping[str, Mapping[str, Any]],
    config: APIConfig,
    max_records: int = 0,
    enable_pmc: bool = False,
    pmc_max_records: int = 0,
) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    """Enrich preclinical/mechanism/biomarker/method records with provisional fields.

    The goal is not PICO completion. It is to reduce false manual-review burden by filling
    model/species, mechanisms, conditions, endpoints, and obvious preclinical dose/duration hints.
    """
    http = CachedHTTPClient(config)
    annot = AnnotationClient(http)
    pmc_client = PMCFullTextClient(http, config)

    target_rows = [dict(r) for r in evidence_rows if _is_basic_science_candidate(r)]
    if max_records:
        target_rows = target_rows[:max_records]

    updated: List[dict] = []
    audits: List[dict] = []
    annotation_rows: List[dict] = []
    pmc_audits: List[dict] = []
    pmc_attempts = 0

    for row in target_rows:
        pmid = clean_text(row.get("pmid"))
        paper = paper_by_pmid.get(pmid, {})
        title = first_nonblank(paper.get("title"), row.get("title"))
        abstract = first_nonblank(paper.get("abstract"), row.get("abstract"))
        row["enriched_source_title"] = title
        row["enriched_source_abstract"] = abstract
        blob = text_blob(title, abstract, row.get("role_evidence_text"), row.get("key_result_sentence"))
        lane = clean_text(row.get("processing_lane"))
        required = REQUIRED_BY_LANE.get(lane, [])
        original_completion, original_missing = lane_completeness(row, required)

        abstract_extraction = extract_from_title_abstract(
            title=title,
            abstract=abstract,
            mesh_terms=paper.get("mesh_terms", ""),
            pubtypes=paper.get("pubtypes", ""),
        )
        for key, value in abstract_extraction.to_dict().items():
            if value:
                row[key] = value

        heur = extract_contextual_values(title, abstract)
        for key, value in heur.items():
            if value:
                row[f"enriched_{key}"] = value

        species = detect_species_model(blob)
        mechanisms = detect_tags(blob, MECHANISM_PATTERNS)
        conditions = detect_tags(blob, CONDITION_PATTERNS)
        endpoints = detect_tags(blob, ENDPOINT_PATTERNS)
        species = merge_semicolon(row.get("abstract_model_system_detail"), species)
        mechanisms = merge_semicolon(row.get("abstract_mechanistic_focus"), mechanisms)
        conditions = merge_semicolon(row.get("abstract_condition_tags"), conditions)
        endpoints = merge_semicolon(row.get("abstract_endpoint_tags"), endpoints)

        if species:
            row["enriched_model_system_detail"] = species
            if "cell_culture" in species and all(x not in species for x in ["mouse", "rat", "zebrafish", "rabbit", "pig", "dog", "nonhuman_primate"]):
                row["enriched_model_type"] = "in vitro"
            else:
                row["enriched_model_type"] = "animal"
        if mechanisms:
            row["enriched_mechanistic_focus"] = merge_tags(row.get("mechanistic_focus"), mechanisms)
        if conditions:
            row["enriched_condition_tags"] = merge_tags(row.get("condition_tags"), conditions)
        if endpoints:
            row["enriched_endpoint_tags"] = merge_tags(row.get("endpoint_tags"), endpoints)

        # PubTator / Europe PMC annotations are additive support. They can be disabled for offline tests.
        annotation_source = []
        if config.api_enabled and pmid:
            docs, source = annot.pubtator_pmids([pmid])
            annotation_source.append(f"pubtator:{source}")
            summary = annot.summarize_pubtator_documents(docs)
            for key, val in summary.items():
                row[f"enriched_{key}"] = val
            if summary:
                annotation_rows.append({"pmid": pmid, "evidence_id": row.get("evidence_id"), "molecule_id": row.get("molecule_id"), **summary})
            epmc_anns, epmc_source = annot.europepmc_annotations(pmid)
            annotation_source.append(f"europepmc_annotations:{epmc_source}")
            if epmc_anns:
                row["enriched_europepmc_annotation_count"] = len(epmc_anns)
                annotation_rows.append({"pmid": pmid, "evidence_id": row.get("evidence_id"), "molecule_id": row.get("molecule_id"), "europepmc_annotation_count": len(epmc_anns)})

        _suggest_basic_replacements(row)
        pmc_enabled_for_row = bool(enable_pmc and (not pmc_max_records or pmc_attempts < pmc_max_records))
        row, pmc_audit = maybe_enrich_with_pmc(
            row=row,
            paper=paper,
            required_fields=required,
            pmc_client=pmc_client,
            enable_pmc=pmc_enabled_for_row,
        )
        if enable_pmc and not pmc_enabled_for_row:
            if row.get("pmc_enrichment_eligible"):
                row["pmc_enrichment_status"] = "skipped_pmc_attempt_cap"
                pmc_audit["pmc_enrichment_status"] = "skipped_pmc_attempt_cap"
            else:
                row["pmc_enrichment_status"] = "skipped_not_eligible"
                pmc_audit["pmc_enrichment_status"] = "skipped_not_eligible"
        if row.get("pmc_enrichment_attempted"):
            pmc_attempts += 1
        pmc_audits.append(pmc_audit)
        shadow = _shadow_basic(row)
        proposed_completion, proposed_missing = lane_completeness(shadow, required)
        row["enriched_basic_science_at_utc"] = utc_now_iso()
        row["enriched_annotation_sources_used"] = semicolon_join(annotation_source)
        row["enriched_basic_original_completeness"] = original_completion
        row["enriched_basic_original_missing_fields"] = original_missing
        row["enriched_basic_proposed_completeness"] = proposed_completion
        row["enriched_basic_proposed_missing_fields"] = proposed_missing
        row["enriched_basic_priority"] = _basic_priority(row)
        row["enriched_basic_review_reason"] = _basic_review_reason(row, proposed_missing)
        audits.append(_audit_row(row, paper))
        updated.append(row)

    return updated, audits, annotation_rows, pmc_audits



def _is_basic_science_candidate(row: Mapping[str, Any]) -> bool:
    lane = clean_text(row.get("processing_lane"))
    study = clean_text(row.get("primary_study_type")).lower()
    if lane in BASIC_LANES:
        return True
    if str(row.get("animal_flag", "")).lower() == "true" or str(row.get("in_vitro_flag", "")).lower() == "true":
        return True
    if "animal" in study or "in vitro" in study or "cell" in study:
        return True
    return False


def _suggest_basic_replacements(row: MutableMapping[str, Any]) -> None:
    candidates = {
        "model_type": "enriched_model_type",
        "model_system_detail": "enriched_model_system_detail",
        "mechanistic_focus": "enriched_mechanistic_focus",
        "condition_tags": "enriched_condition_tags",
        "endpoint_tags": "enriched_endpoint_tags",
        "dose_route": "enriched_heuristic_dose_route",
        "duration": "enriched_heuristic_duration",
        "sample_size": "enriched_heuristic_sample_size",
    }
    abstract_candidates = {
        "model_type": "abstract_model_type",
        "model_system_detail": "abstract_model_system_detail",
        "mechanistic_focus": "abstract_mechanistic_focus",
        "condition_tags": "abstract_condition_tags",
        "endpoint_tags": "abstract_endpoint_tags",
        "comparator_or_control": "abstract_comparator_or_control",
        "dose_route": "abstract_dose_route",
        "duration": "abstract_duration",
        "sample_size": "abstract_sample_size",
        "outcome_direction": "abstract_outcome_direction",
        "efficacy_signal": "abstract_efficacy_signal",
        "safety_signal": "abstract_safety_signal",
    }
    for original_field, abstract_field in abstract_candidates.items():
        enriched = row.get(abstract_field)
        suggest = should_suggest_replacement(original_field, row.get(original_field), enriched)
        if suggest:
            row[f"suggest_replace_{original_field}"] = True
            row[f"suggested_{original_field}"] = clean_text(enriched)
            row[f"suggested_{original_field}_source"] = "pubmed_abstract"
    for original_field, enriched_field in candidates.items():
        enriched = row.get(enriched_field)
        suggest = should_suggest_replacement(original_field, row.get(original_field), enriched)
        if suggest and not row.get(f"suggested_{original_field}"):
            row[f"suggest_replace_{original_field}"] = True
            row[f"suggested_{original_field}"] = clean_text(enriched)
            row[f"suggested_{original_field}_source"] = "pubmed_abstract"
        else:
            row.setdefault(f"suggest_replace_{original_field}", False)
            row.setdefault(f"suggested_{original_field}", "")


def _shadow_basic(row: Mapping[str, Any]) -> Dict[str, Any]:
    shadow = dict(row)
    for f in ["model_type", "model_system_detail", "mechanistic_focus", "condition_tags", "endpoint_tags", "comparator_or_control", "dose_route", "duration", "sample_size", "outcome_direction", "efficacy_signal", "safety_signal"]:
        suggested = row.get(f"suggested_{f}")
        if suggested:
            shadow[f] = suggested
    return shadow


def _basic_priority(row: Mapping[str, Any]) -> str:
    lane = clean_text(row.get("processing_lane"))
    if lane == "preclinical_intervention" and str(row.get("public_candidate", "")).lower() == "true":
        return "high_preclinical_public_candidate"
    if lane in {"mechanism_or_pathway", "biomarker_or_readout"}:
        return "background_or_mechanism_support"
    if lane in {"methods_assay_synthesis", "diagnostic_or_tool_use", "environmental_or_materials"}:
        return "internal_methods_or_noise"
    return "standard_basic_science"


def _basic_review_reason(row: Mapping[str, Any], proposed_missing: str) -> str:
    reasons = []
    if proposed_missing:
        reasons.append(f"missing_or_low_confidence: {proposed_missing}")
    if clean_text(row.get("processing_lane")) == "preclinical_intervention" and str(row.get("public_candidate", "")).lower() == "true":
        if proposed_missing:
            reasons.append("preclinical_public_candidate_incomplete")
    if clean_text(row.get("role_confidence")).lower() in {"low", "uncertain"}:
        reasons.append("low_role_confidence")
    return semicolon_join(reasons)


def _audit_row(row: Mapping[str, Any], paper: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "evidence_id": row.get("evidence_id"),
        "pmid": row.get("pmid"),
        "molecule_id": row.get("molecule_id"),
        "molecule_name": row.get("molecule_name"),
        "processing_lane": row.get("processing_lane"),
        "title": first_nonblank(paper.get("title"), row.get("title")),
        "public_candidate": row.get("public_candidate"),
        "original_completeness": row.get("enriched_basic_original_completeness"),
        "proposed_completeness": row.get("enriched_basic_proposed_completeness"),
        "original_missing_fields": row.get("enriched_basic_original_missing_fields"),
        "proposed_missing_fields": row.get("enriched_basic_proposed_missing_fields"),
        "suggested_model_type": row.get("suggested_model_type"),
        "suggested_model_system_detail": row.get("suggested_model_system_detail"),
        "suggested_mechanistic_focus": row.get("suggested_mechanistic_focus"),
        "suggested_condition_tags": row.get("suggested_condition_tags"),
        "suggested_endpoint_tags": row.get("suggested_endpoint_tags"),
        "suggested_dose_route": row.get("suggested_dose_route"),
        "suggested_duration": row.get("suggested_duration"),
        "suggested_sample_size": row.get("suggested_sample_size"),
        "suggested_sources": semicolon_join(v for k, v in row.items() if k.startswith("suggested_") and k.endswith("_source")),
        "abstract_extraction_confidence": row.get("abstract_extraction_confidence"),
        "abstract_extraction_notes": row.get("abstract_extraction_notes"),
        "pmc_enrichment_eligible": row.get("pmc_enrichment_eligible"),
        "pmc_enrichment_attempted": row.get("pmc_enrichment_attempted"),
        "pmc_enrichment_status": row.get("pmc_enrichment_status"),
        "pmcid": row.get("pmcid"),
        "basic_priority": row.get("enriched_basic_priority"),
        "review_reason": row.get("enriched_basic_review_reason"),
        "annotation_sources": row.get("enriched_annotation_sources_used"),
    }
