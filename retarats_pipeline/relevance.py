from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, List

from .pubmed import PubMedRecord


@dataclass
class MoleculeRelevance:
    molecule_relevance: str
    relevance_confidence: str
    website_include: bool
    relevance_notes: str

    def to_dict(self) -> dict:
        return asdict(self)


METHOD_TERMS = [
    "synthesis", "analytical", "workflow", "determination", "lc-ms", "lc/ms",
    "mass spectrometry", "detection", "biosensor", "assay", "stability",
    "characterization", "cocrystal", "chromatography", "phytochemical",
]

ENVIRONMENTAL_MATERIAL_TERMS = [
    "dye", "wastewater", "waste water", "degradation", "photocatalytic",
    "adsorption", "sorption", "remediation", "nanocluster", "hydrogel",
    "cotton fabrics", "aqueous solution", "pollutant",
]

INTERVENTION_TERMS = [
    "treated", "treatment", "administered", "received", "randomized",
    "randomised", "placebo", "dose", "once-weekly", "subcutaneous",
    "oral", "intraperitoneal", "injected", "therapy", "monotherapy",
]

BIOMARKER_TERMS = [
    "level", "levels", "biomarker", "expression", "oxidative stress",
    "pathway", "receptor", "signaling", "signalling", "mechanism",
    "metabolite", "glutathione peroxidase", "gsh",
]


def classify_molecule_relevance(record: PubMedRecord, molecule: dict) -> MoleculeRelevance:
    title = record.title or ""
    abstract = record.abstract or ""
    text = f"{title} {abstract}".lower()
    title_l = title.lower()
    terms = _molecule_terms(molecule)
    term_in_title = any(_has_phrase(title_l, term) for term in terms)
    term_in_text = any(_has_phrase(text, term) for term in terms)

    methodish = _has_any(text, METHOD_TERMS)
    environmental = _has_any(text, ENVIRONMENTAL_MATERIAL_TERMS)
    interventional = _has_any(text, INTERVENTION_TERMS)
    biomarker = _has_any(text, BIOMARKER_TERMS)

    if _is_methylene_blue_environmental(molecule, text):
        return MoleculeRelevance(
            molecule_relevance="environmental_or_material_use",
            relevance_confidence="high",
            website_include=False,
            relevance_notes="Methylene blue appears to be used as a dye/materials or remediation target, not therapeutic evidence.",
        )

    if methodish and not interventional:
        return MoleculeRelevance(
            molecule_relevance="synthesis_or_assay",
            relevance_confidence="high" if term_in_title else "medium",
            website_include=False,
            relevance_notes="Paper appears focused on synthesis, detection, assay, formulation, or analytical workflow.",
        )

    if term_in_title and interventional:
        return MoleculeRelevance(
            molecule_relevance="primary_intervention",
            relevance_confidence="high",
            website_include=True,
            relevance_notes="Molecule is named in the title and the abstract/title contains intervention language.",
        )

    if term_in_title and biomarker:
        return MoleculeRelevance(
            molecule_relevance="biomarker_or_mechanism",
            relevance_confidence="medium",
            website_include=True,
            relevance_notes="Molecule is named in the title and appears in a mechanistic or biomarker context.",
        )

    if term_in_title:
        return MoleculeRelevance(
            molecule_relevance="primary_topic_unclear_role",
            relevance_confidence="medium",
            website_include=True,
            relevance_notes="Molecule is named in the title, but role should be reviewed.",
        )

    if term_in_text and interventional:
        return MoleculeRelevance(
            molecule_relevance="secondary_intervention_or_comparator",
            relevance_confidence="medium",
            website_include=True,
            relevance_notes="Molecule is mentioned in intervention context but is not clearly the main title topic.",
        )

    if term_in_text and biomarker:
        return MoleculeRelevance(
            molecule_relevance="biomarker_or_mechanism",
            relevance_confidence="medium",
            website_include=True,
            relevance_notes="Molecule appears in a mechanistic, pathway, or biomarker context.",
        )

    if term_in_text:
        return MoleculeRelevance(
            molecule_relevance="background_mention",
            relevance_confidence="low",
            website_include=False,
            relevance_notes="Molecule is mentioned, but the paper may not provide direct evidence about it.",
        )

    return MoleculeRelevance(
        molecule_relevance="unclear_match",
        relevance_confidence="low",
        website_include=False,
        relevance_notes="Search rule matched, but no clear molecule mention was detected in parsed title/abstract.",
    )


def _molecule_terms(molecule: dict) -> List[str]:
    out = []
    for value in [molecule.get("display_name", ""), molecule.get("molecule_id", "")]:
        if value:
            out.append(str(value))
    for term in str(molecule.get("synonyms_csv", "")).split(","):
        term = term.strip()
        if term:
            out.append(term)
    return sorted({t.lower() for t in out if len(t.strip()) >= 2}, key=len, reverse=True)


def _is_methylene_blue_environmental(molecule: dict, text: str) -> bool:
    molecule_id = str(molecule.get("molecule_id", "")).lower()
    display = str(molecule.get("display_name", "")).lower()
    if molecule_id != "methylene_blue" and "methylene blue" not in display:
        return False
    return _has_any(text, ENVIRONMENTAL_MATERIAL_TERMS)


def _has_any(text: str, terms: Iterable[str]) -> bool:
    return any(_has_phrase(text, term) for term in terms)


def _has_phrase(text: str, term: str) -> bool:
    if not term:
        return False
    escaped = re.escape(term.lower()).replace(r"\ ", r"\s+")
    if re.search(r"[a-z0-9]$", term.lower()) and re.search(r"^[a-z0-9]", term.lower()):
        pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    else:
        pattern = escaped
    return re.search(pattern, text.lower()) is not None
