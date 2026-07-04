from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, List


PROCESSING_ROUTE_FIELDS = [
    "processing_lane",
    "next_postprocessing_script",
    "database_section",
    "processing_priority",
    "processing_depth",
    "route_reason",
    "keep_for_final_database",
]


@dataclass
class ProcessingRoute:
    processing_lane: str
    next_postprocessing_script: str
    database_section: str
    processing_priority: int
    processing_depth: str
    route_reason: str
    keep_for_final_database: bool

    def to_dict(self) -> dict:
        return asdict(self)


def route_evidence(evidence: dict) -> ProcessingRoute:
    role = str(evidence.get("role_category", ""))
    purpose = str(evidence.get("paper_purpose", ""))
    primary = str(evidence.get("primary_study_type", ""))
    model = str(evidence.get("model_type", ""))
    strength = int(float(evidence.get("evidence_strength_score") or 0))
    confidence = str(evidence.get("paper_characterization_confidence", ""))

    if purpose == "evidence_synthesis":
        return _route(
            lane="review_or_meta_analysis",
            script="scripts/postprocess_reviews.py",
            section="Evidence Syntheses",
            priority=90 if strength >= 4 else 70,
            depth="review_structured_extraction",
            reason="Review, systematic review, or meta-analysis needs synthesis-specific extraction.",
        )

    if role == "direct_intervention" or purpose == "intervention_efficacy_safety":
        if model == "human" or primary in {"RCT", "Human interventional non-RCT"}:
            return _route(
                lane="human_intervention",
                script="scripts/postprocess_interventions.py",
                section="Intervention Evidence",
                priority=95,
                depth="full_structured_extraction",
                reason="Direct intervention in human/clinical context.",
            )
        return _route(
            lane="preclinical_intervention",
            script="scripts/postprocess_interventions.py",
            section="Preclinical Intervention Evidence",
            priority=85,
            depth="full_structured_extraction",
            reason="Direct intervention in animal or in-vitro context.",
        )

    if role == "biomarker_readout":
        return _route(
            lane="biomarker_or_readout",
            script="scripts/postprocess_biomarkers.py",
            section="Biomarkers and Readouts",
            priority=75,
            depth="biomarker_context_extraction",
            reason="Molecule appears to be measured as a biomarker or endpoint.",
        )

    if role == "pathway_component":
        return _route(
            lane="mechanism_or_pathway",
            script="scripts/postprocess_mechanisms.py",
            section="Mechanisms and Pathways",
            priority=80 if model in {"human", "animal"} else 65,
            depth="mechanism_extraction",
            reason="Molecule appears in mechanistic, pathway, receptor, enzyme, or signaling context.",
        )

    if role == "comparator_or_background_drug":
        return _route(
            lane="comparator_or_background",
            script="scripts/postprocess_comparators.py",
            section="Comparator and Background Therapy Context",
            priority=70,
            depth="contextual_extraction",
            reason="Molecule appears as comparator, background medication, combination component, or class context.",
        )

    if role == "clinical_tool_or_diagnostic":
        return _route(
            lane="diagnostic_or_tool_use",
            script="scripts/postprocess_methods.py",
            section="Diagnostic and Tool Uses",
            priority=60,
            depth="tool_use_extraction",
            reason="Molecule appears as clinical tool, diagnostic dye, imaging/procedural aid, or perturbation tool.",
        )

    if role in {"assay_or_detection", "synthesis_or_production"}:
        return _route(
            lane="methods_assay_synthesis",
            script="scripts/postprocess_methods.py",
            section="Methods, Assays, Synthesis, and Formulation",
            priority=50,
            depth="methods_extraction",
            reason="Methods-focused record needs analytical/synthesis/formulation extraction, not efficacy extraction.",
        )

    if role == "environmental_or_material_use":
        return _route(
            lane="environmental_or_materials",
            script="scripts/postprocess_methods.py",
            section="Environmental and Materials Context",
            priority=35,
            depth="materials_context_extraction",
            reason="Molecule appears in environmental/materials context.",
        )

    if confidence == "low":
        return _route(
            lane="unclear_manual_triage",
            script="scripts/postprocess_unclear.py",
            section="Unclear Matches",
            priority=40,
            depth="manual_or_enhanced_rule_review",
            reason="Insufficient metadata or unclear paper purpose.",
        )

    return _route(
        lane="general_context",
        script="scripts/postprocess_unclear.py",
        section="General Context",
        priority=45,
        depth="light_structured_extraction",
        reason="Record is relevant but does not match a more specific lane.",
    )


def route_many(evidence_rows: Iterable[dict]) -> List[dict]:
    out = []
    for row in evidence_rows:
        row = dict(row)
        row.update(route_evidence(row).to_dict())
        out.append(row)
    return out


def _route(
    *,
    lane: str,
    script: str,
    section: str,
    priority: int,
    depth: str,
    reason: str,
    keep: bool = True,
) -> ProcessingRoute:
    return ProcessingRoute(
        processing_lane=lane,
        next_postprocessing_script=script,
        database_section=section,
        processing_priority=priority,
        processing_depth=depth,
        route_reason=reason,
        keep_for_final_database=keep,
    )
