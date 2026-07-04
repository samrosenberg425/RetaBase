from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from .clients import ClinicalTrialsClient, IdentifierMetadataClient
from .common import APIConfig, CachedHTTPClient, clean_text, find_nct_ids, first_nonblank, is_blankish, semicolon_join, split_semicolon, text_blob, utc_now_iso
from .field_quality import extract_contextual_values, lane_completeness, looks_low_confidence, should_suggest_replacement

HUMAN_REQUIRED = [
    "population_or_sample", "intervention_or_exposure", "comparator_or_control", "dose_route",
    "duration", "sample_size", "endpoint_tags", "efficacy_signal", "safety_signal",
]



def required_fields_for_human_row(row: Mapping[str, Any]) -> List[str]:
    study = clean_text(row.get("primary_study_type")).lower()
    purpose = clean_text(row.get("paper_purpose")).lower()
    if "review" in study or "meta" in study or "evidence_synthesis" in purpose:
        return ["population_or_sample", "intervention_or_exposure", "endpoint_tags", "efficacy_signal", "safety_signal"]
    if "case" in study:
        return ["population_or_sample", "intervention_or_exposure", "dose_route", "endpoint_tags", "safety_signal"]
    if "observational" in study or "cohort" in study:
        return ["population_or_sample", "intervention_or_exposure", "sample_size", "endpoint_tags", "efficacy_signal", "safety_signal"]
    return HUMAN_REQUIRED


def should_query_clinicaltrials(row: Mapping[str, Any], nct_ids: Sequence[str]) -> bool:
    if nct_ids:
        return True
    study = clean_text(row.get("primary_study_type")).lower()
    purpose = clean_text(row.get("paper_purpose")).lower()
    if "review" in study or "meta" in study or "case" in study:
        return False
    return any(token in study for token in ["rct", "interventional", "trial"]) or "intervention" in purpose

COMPARATOR_RE = re.compile(r"\b(placebo|standard care|usual care|lifestyle|vehicle|control|comparator|active comparator|metformin|semaglutide|tirzepatide|liraglutide)\b", re.I)


def enrich_human_interventions(
    evidence_rows: Sequence[Mapping[str, Any]],
    paper_by_pmid: Mapping[str, Mapping[str, Any]],
    config: APIConfig,
    max_records: int = 0,
) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    """Return updated evidence rows, audit rows, CT.gov match rows, and registry-only trial rows.

    This function writes only enriched_* and suggested_replace_* fields. It never overwrites
    original extraction fields.
    """
    http = CachedHTTPClient(config)
    ct = ClinicalTrialsClient(http)
    meta = IdentifierMetadataClient(http, config)

    updated: List[dict] = []
    audits: List[dict] = []
    trial_matches: List[dict] = []
    registry_records: Dict[str, dict] = {}

    target_rows = [dict(r) for r in evidence_rows if _is_human_intervention_candidate(r)]
    if max_records:
        target_rows = target_rows[:max_records]

    for row in target_rows:
        pmid = clean_text(row.get("pmid"))
        paper = paper_by_pmid.get(pmid, {})
        title = first_nonblank(paper.get("title"), row.get("title"))
        abstract = first_nonblank(paper.get("abstract"), row.get("abstract"))
        row["enriched_source_title"] = title
        row["enriched_source_abstract"] = abstract
        doi = first_nonblank(paper.get("doi"), row.get("doi"))
        molecule = first_nonblank(row.get("molecule_name"), row.get("molecule_id"))
        condition = clean_text(row.get("condition_tags", ""))
        now = utc_now_iso()

        required_fields = required_fields_for_human_row(row)
        original_completion, original_missing = lane_completeness(row, required_fields)
        heur = extract_contextual_values(title, abstract)

        # Identifier/text rescue metadata. Keep this lightweight; PubMed EFetch is already upstream.
        enriched_identifier_source = []
        if is_blankish(doi):
            id_data, id_source = meta.pmc_idconv(pmid) if pmid else (None, "no_pmid")
            enriched_identifier_source.append(f"pmc_idconv:{id_source}")
            doi_candidate = _doi_from_pmc_idconv(id_data)
            if doi_candidate:
                row["enriched_doi"] = doi_candidate
        else:
            row["enriched_doi"] = doi

        nct_ids = find_nct_ids(title, abstract, row.get("role_evidence_text"), row.get("key_result_sentence"))
        row["enriched_nct_ids_from_text"] = semicolon_join(nct_ids)

        ct_matches: List[dict] = []
        ct_sources: List[str] = []
        if nct_ids:
            for nct in nct_ids:
                study, source = ct.fetch_nct(nct)
                ct_sources.append(f"{nct}:{source}")
                if study and not study.get("error"):
                    parsed = ct.parse_study(study)
                    parsed["match_method"] = "nct_in_pubmed_text"
                    parsed["match_confidence"] = "high"
                    ct_matches.append(parsed)
                    registry_records[parsed.get("nct_id") or nct] = _registry_record(parsed, row, "linked_to_pubmed")
        elif config.api_enabled and should_query_clinicaltrials(row, nct_ids):
            query = _trial_search_query(molecule, title, condition)
            studies, source = ct.search(query, page_size=config.max_trial_search_results)
            ct_sources.append(f"search:{source}")
            for study in studies:
                parsed = ct.parse_study(study)
                score, reasons = _score_trial_match(parsed, row, title, molecule, condition)
                if score >= 2:
                    parsed["match_method"] = "ctgov_search"
                    parsed["match_score"] = score
                    parsed["match_reasons"] = semicolon_join(reasons)
                    parsed["match_confidence"] = "medium" if score >= 3 else "low"
                    ct_matches.append(parsed)
                    registry_records[parsed.get("nct_id") or parsed.get("brief_title", "")] = _registry_record(parsed, row, "candidate_match_to_pubmed")

        best_ct = _best_ct_match(ct_matches)
        if best_ct:
            _apply_ct_enriched_fields(row, best_ct)
            for match in ct_matches:
                trial_matches.append(_trial_match_row(row, paper, match))

        # Heuristic fields are useful even offline. They are especially useful for animal papers too,
        # but here they provide fallbacks for human intervention audit.
        for key, value in heur.items():
            if value:
                row[f"enriched_{key}"] = value

        _suggest_replacements(row)
        row["enriched_identifier_sources_used"] = semicolon_join(enriched_identifier_source)
        row["enriched_clinicaltrials_sources_used"] = semicolon_join(ct_sources)
        row["enriched_human_intervention_at_utc"] = now
        enriched_shadow = _shadow_row_with_enriched(row)
        enriched_completion, enriched_missing = lane_completeness(enriched_shadow, required_fields)
        row["enriched_human_original_completeness"] = original_completion
        row["enriched_human_original_missing_fields"] = original_missing
        row["enriched_human_proposed_completeness"] = enriched_completion
        row["enriched_human_proposed_missing_fields"] = enriched_missing
        row["enriched_needs_human_review"] = bool(enriched_completion < 0.75 and str(row.get("public_candidate", "")).lower() == "true")
        row["enriched_human_review_reason"] = _human_review_reason(row, enriched_missing)
        audits.append(_audit_row(row, paper))
        updated.append(row)

    # Registry-only discovery: for now, this uses molecule+human keywords for target rows where no paper-linked NCT was found.
    # It intentionally stays separate from PubMed papers/evidence.
    if config.api_enabled:
        discovered = _discover_registry_only_trials(target_rows, ct, registry_records, limit_per_molecule=3)
        registry_records.update(discovered)

    return updated, audits, trial_matches, list(registry_records.values())



def _is_human_intervention_candidate(row: Mapping[str, Any]) -> bool:
    if clean_text(row.get("processing_lane")) != "human_intervention":
        return False
    if str(row.get("animal_flag", "")).lower() == "true" or str(row.get("in_vitro_flag", "")).lower() == "true":
        return False
    study = clean_text(row.get("primary_study_type")).lower()
    model = clean_text(row.get("model_type")).lower()
    return ("human" in model or any(t in study for t in ["rct", "human", "observational", "case", "review", "meta"]))


def _trial_search_query(molecule: str, title: str, condition_tags: str) -> str:
    title_words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", title) if w.lower() not in {"study", "trial", "effect", "effects", "patients", "adults"}]
    title_fragment = " ".join(title_words[:8])
    condition_terms = condition_tags.replace(";", " ").replace("_", " ")
    return clean_text(f"{molecule} {condition_terms} {title_fragment}")[:500]


def _doi_from_pmc_idconv(data: Optional[Mapping[str, Any]]) -> str:
    if not data:
        return ""
    records = data.get("records") if isinstance(data, Mapping) else None
    if isinstance(records, list):
        for rec in records:
            doi = clean_text((rec or {}).get("doi", ""))
            if doi:
                return doi
    return ""


def _score_trial_match(parsed: Mapping[str, Any], row: Mapping[str, Any], title: str, molecule: str, condition: str) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []
    blob = text_blob(parsed.get("brief_title"), parsed.get("official_title"), parsed.get("interventions"), parsed.get("conditions"))
    if molecule and re.search(re.escape(molecule), blob, re.I):
        score += 2
        reasons.append("molecule_name_match")
    mol_id = clean_text(row.get("molecule_id"))
    if mol_id and mol_id.replace("_", " ").lower() in blob.lower():
        score += 1
        reasons.append("molecule_id_match")
    condition_tokens = [t for t in re.split(r"[;_\s]+", condition.lower()) if len(t) >= 5]
    if any(t in blob.lower() for t in condition_tokens):
        score += 1
        reasons.append("condition_overlap")
    title_tokens = set(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{4,}", title))
    registry_tokens = set(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{4,}", blob))
    overlap = len(title_tokens & registry_tokens)
    if overlap >= 4:
        score += 1
        reasons.append(f"title_token_overlap_{overlap}")
    if clean_text(parsed.get("study_type")).lower() == "interventional":
        score += 1
        reasons.append("interventional_registry_record")
    return score, reasons


def _best_ct_match(matches: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if not matches:
        return None
    def key(m: Mapping[str, Any]) -> Tuple[int, int]:
        conf = {"high": 3, "medium": 2, "low": 1}.get(clean_text(m.get("match_confidence")).lower(), 0)
        return (conf, int(m.get("match_score") or 0))
    return sorted(matches, key=key, reverse=True)[0]


def _apply_ct_enriched_fields(row: MutableMapping[str, Any], ct_row: Mapping[str, Any]) -> None:
    row["enriched_nct_id"] = clean_text(ct_row.get("nct_id"))
    row["enriched_trial_status"] = clean_text(ct_row.get("overall_status"))
    row["enriched_trial_phase"] = clean_text(ct_row.get("phases"))
    row["enriched_trial_enrollment"] = clean_text(ct_row.get("enrollment_count"))
    row["enriched_trial_enrollment_type"] = clean_text(ct_row.get("enrollment_type"))
    row["enriched_trial_conditions"] = clean_text(ct_row.get("conditions"))
    row["enriched_trial_arms"] = clean_text(ct_row.get("arms"))
    row["enriched_trial_interventions"] = clean_text(ct_row.get("interventions"))
    row["enriched_trial_primary_outcomes"] = clean_text(ct_row.get("primary_outcomes"))
    row["enriched_trial_secondary_outcomes"] = clean_text(ct_row.get("secondary_outcomes"))
    row["enriched_trial_eligibility_summary"] = clean_text(ct_row.get("eligibility_summary"))[:1500]
    row["enriched_trial_adverse_events_available"] = bool(ct_row.get("adverse_events_available"))
    row["enriched_trial_has_results"] = bool(ct_row.get("has_results"))
    row["enriched_trial_match_confidence"] = clean_text(ct_row.get("match_confidence"))
    row["enriched_trial_match_method"] = clean_text(ct_row.get("match_method"))

    row["enriched_sample_size"] = first_nonblank(row.get("enriched_sample_size"), ct_row.get("enrollment_count"))
    row["enriched_comparator_or_control"] = first_nonblank(_extract_comparator_from_arms(ct_row.get("arms")), row.get("enriched_comparator_or_control"))
    row["enriched_population_or_sample"] = first_nonblank(_population_from_ct(ct_row), row.get("enriched_population_or_sample"))
    row["enriched_endpoint_tags_text"] = semicolon_join([ct_row.get("primary_outcomes"), ct_row.get("secondary_outcomes")])
    dose = _extract_dose_from_interventions(ct_row.get("interventions"))
    if dose:
        row["enriched_dose_route"] = dose
    duration = _extract_duration_from_outcomes(ct_row.get("primary_outcomes"), ct_row.get("secondary_outcomes"))
    if duration:
        row["enriched_duration"] = duration


def _extract_comparator_from_arms(arms: Any) -> str:
    text = clean_text(arms)
    if not text:
        return ""
    hits = COMPARATOR_RE.findall(text)
    return semicolon_join(hits[:5])


def _extract_dose_from_interventions(interventions: Any) -> str:
    text = clean_text(interventions)
    if not text:
        return ""
    dose_re = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|µg|ug|mcg|g|IU|U)(?:\s*/\s*(?:kg|day|week))?\b", re.I)
    return semicolon_join(m.group(0) for m in dose_re.finditer(text))


def _extract_duration_from_outcomes(*outcomes: Any) -> str:
    text = text_blob(*outcomes)
    if not text:
        return ""
    dur_re = re.compile(r"\b(?:week|month|day)\s*\d+\b|\b\d+\s*(?:weeks?|months?|days?)\b", re.I)
    return semicolon_join(m.group(0) for m in dur_re.finditer(text))


def _population_from_ct(ct_row: Mapping[str, Any]) -> str:
    parts = []
    if ct_row.get("sex"):
        parts.append(f"sex: {ct_row.get('sex')}")
    ages = semicolon_join([ct_row.get("minimum_age"), ct_row.get("maximum_age")])
    if ages:
        parts.append(f"age: {ages}")
    if ct_row.get("conditions"):
        parts.append(f"conditions: {ct_row.get('conditions')}")
    return semicolon_join(parts)


def _suggest_replacements(row: MutableMapping[str, Any]) -> None:
    mapping = {
        "population_or_sample": "enriched_population_or_sample",
        "comparator_or_control": "enriched_comparator_or_control",
        "dose_route": "enriched_dose_route",
        "duration": "enriched_duration",
        "sample_size": "enriched_sample_size",
    }
    # Heuristic fallbacks if registry did not fill the field.
    fallback = {
        "dose_route": "enriched_heuristic_dose_route",
        "duration": "enriched_heuristic_duration",
        "sample_size": "enriched_heuristic_sample_size",
    }
    for original_field, enriched_field in mapping.items():
        enriched = first_nonblank(row.get(enriched_field), row.get(fallback.get(original_field, "")))
        if enriched:
            row[enriched_field] = enriched
        suggest = should_suggest_replacement(original_field, row.get(original_field), enriched)
        row[f"suggest_replace_{original_field}"] = bool(suggest)
        row[f"suggested_{original_field}"] = enriched if suggest else ""


def _shadow_row_with_enriched(row: Mapping[str, Any]) -> Dict[str, Any]:
    shadow = dict(row)
    for f in ["population_or_sample", "comparator_or_control", "dose_route", "duration", "sample_size"]:
        suggested = row.get(f"suggested_{f}")
        if suggested:
            shadow[f] = suggested
    if row.get("enriched_trial_primary_outcomes") and is_blankish(shadow.get("endpoint_tags")):
        shadow["endpoint_tags"] = row.get("enriched_trial_primary_outcomes")
    return shadow


def _human_review_reason(row: Mapping[str, Any], missing: str) -> str:
    reasons = []
    if missing:
        reasons.append(f"missing_or_low_confidence: {missing}")
    if not row.get("enriched_nct_id") and not row.get("enriched_nct_ids_from_text") and clean_text(row.get("primary_study_type")).lower() in {"rct", "human interventional non-rct"}:
        reasons.append("no_trial_registry_match")
    if clean_text(row.get("role_confidence")).lower() in {"low", "uncertain"}:
        reasons.append("low_role_confidence")
    return semicolon_join(reasons)


def _audit_row(row: Mapping[str, Any], paper: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "evidence_id": row.get("evidence_id"),
        "pmid": row.get("pmid"),
        "molecule_id": row.get("molecule_id"),
        "molecule_name": row.get("molecule_name"),
        "title": first_nonblank(paper.get("title"), row.get("title")),
        "public_candidate": row.get("public_candidate"),
        "primary_study_type": row.get("primary_study_type"),
        "original_completeness": row.get("enriched_human_original_completeness"),
        "proposed_completeness": row.get("enriched_human_proposed_completeness"),
        "original_missing_fields": row.get("enriched_human_original_missing_fields"),
        "proposed_missing_fields": row.get("enriched_human_proposed_missing_fields"),
        "nct_ids_from_text": row.get("enriched_nct_ids_from_text"),
        "matched_nct_id": row.get("enriched_nct_id"),
        "trial_match_confidence": row.get("enriched_trial_match_confidence"),
        "suggested_population_or_sample": row.get("suggested_population_or_sample"),
        "suggested_comparator_or_control": row.get("suggested_comparator_or_control"),
        "suggested_dose_route": row.get("suggested_dose_route"),
        "suggested_duration": row.get("suggested_duration"),
        "suggested_sample_size": row.get("suggested_sample_size"),
        "needs_human_review": row.get("enriched_needs_human_review"),
        "human_review_reason": row.get("enriched_human_review_reason"),
        "api_sources": semicolon_join([row.get("enriched_identifier_sources_used"), row.get("enriched_clinicaltrials_sources_used")]),
    }


def _trial_match_row(row: Mapping[str, Any], paper: Mapping[str, Any], match: Mapping[str, Any]) -> Dict[str, Any]:
    out = {"evidence_id": row.get("evidence_id"), "pmid": row.get("pmid"), "molecule_id": row.get("molecule_id"), "molecule_name": row.get("molecule_name"), "paper_title": first_nonblank(paper.get("title"), row.get("title"))}
    for key in ["nct_id", "brief_title", "overall_status", "phases", "enrollment_count", "conditions", "arms", "interventions", "primary_outcomes", "secondary_outcomes", "match_method", "match_confidence", "match_score", "match_reasons", "has_results", "adverse_events_available"]:
        out[f"ctgov_{key}"] = match.get(key, "")
    return out


def _registry_record(parsed: Mapping[str, Any], row: Mapping[str, Any], registry_status: str) -> Dict[str, Any]:
    out = {"registry_layer_status": registry_status, "linked_molecule_id": row.get("molecule_id", ""), "linked_molecule_name": row.get("molecule_name", ""), "linked_pmid": row.get("pmid", ""), "linked_evidence_id": row.get("evidence_id", "")}
    for key, value in parsed.items():
        out[f"ctgov_{key}"] = value
    return out


def _discover_registry_only_trials(
    target_rows: Sequence[Mapping[str, Any]],
    ct: ClinicalTrialsClient,
    registry_records: Mapping[str, dict],
    limit_per_molecule: int = 3,
) -> Dict[str, dict]:
    discovered: Dict[str, dict] = {}
    seen_queries = set()
    by_mol: Dict[str, Mapping[str, Any]] = {}
    for row in target_rows:
        mol = clean_text(row.get("molecule_name") or row.get("molecule_id"))
        if mol and mol.lower() not in by_mol:
            by_mol[mol.lower()] = row
    for mol_key, row in list(by_mol.items())[:50]:
        mol = clean_text(row.get("molecule_name") or row.get("molecule_id"))
        cond = clean_text(row.get("condition_tags", "")).replace(";", " ").replace("_", " ")
        query = clean_text(f"{mol} {cond}")
        if not query or query.lower() in seen_queries:
            continue
        seen_queries.add(query.lower())
        studies, _source = ct.search(query, page_size=limit_per_molecule)
        for study in studies[:limit_per_molecule]:
            parsed = ct.parse_study(study)
            nct = clean_text(parsed.get("nct_id"))
            if not nct or nct in registry_records or nct in discovered:
                continue
            score, reasons = _score_trial_match(parsed, row, "", mol, cond)
            if score >= 2:
                parsed["match_method"] = "registry_only_molecule_search"
                parsed["match_score"] = score
                parsed["match_reasons"] = semicolon_join(reasons)
                parsed["match_confidence"] = "medium" if score >= 3 else "low"
                discovered[nct] = _registry_record(parsed, row, "registry_only_no_pubmed_link_yet")
    return discovered
