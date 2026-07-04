from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, List

from .pubmed import PubMedRecord


@dataclass
class RuleClassification:
    primary_study_type: str
    study_design_tags: List[str]
    model_type: str
    species_or_population: str
    human_flag: bool
    animal_flag: bool
    in_vitro_flag: bool
    confidence: str
    notes: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["study_design_tags"] = "; ".join(self.study_design_tags)
        return data


def classify_record(record: PubMedRecord) -> RuleClassification:
    pubtypes = " ".join(record.pubtypes).lower()
    mesh = " ".join(record.mesh_terms).lower()
    text = f"{record.title} {record.abstract}".lower()
    tags = []

    human_flag = _has_mesh_any(mesh, ["humans"]) or _has_phrase_any(text, ["human subjects", "patients", "participants"])
    animal_flag = _has_mesh_any(mesh, ["animals", "mice", "rats"]) or _has_phrase_any(
        text, ["mouse", "mice", "rat", "rats", "murine", "porcine", "canine", "in vivo"]
    )
    in_vitro_flag = _has_phrase_any(text, ["in vitro", "cell line", "cultured", "fibroblast", "organoid", "hepg2", "hela"])

    title = (record.title or "").lower()

    if _has_phrase_any(pubtypes, ["meta-analysis"]) or _has_phrase_any(title, ["meta-analysis", "meta analysis"]):
        primary = "Systematic review / Meta-analysis"
        tags.append("Meta-Analysis")
    elif _has_phrase_any(pubtypes, ["systematic review"]) or "systematic review" in title:
        primary = "Systematic review / Meta-analysis"
        tags.append("Systematic Review")
    elif _has_phrase_any(pubtypes, ["randomized controlled trial"]) or _has_phrase_any(
        text, ["randomized", "randomised", "placebo", "double-blind", "single-blind"]
    ):
        primary = "RCT" if human_flag else "Randomized non-human / unclear"
        tags.append("Randomized")
    elif _has_phrase_any(pubtypes, ["clinical trial"]) or _has_phrase_any(
        text, ["clinical trial", "open-label", "single-arm", "dose-escalation", "dose escalation"]
    ):
        primary = "Human interventional non-RCT" if human_flag else "Clinical trial / unclear population"
        tags.append("Clinical Trial")
    elif _has_phrase_any(pubtypes, ["case reports"]) or _has_phrase_any(text, ["case report", "case series"]):
        primary = "Case report / Case series"
        tags.append("Case Report")
    elif human_flag and _has_phrase_any(text, ["cohort", "case-control", "cross-sectional", "observational", "registry"]):
        primary = "Human observational"
        tags.append("Observational")
    elif animal_flag:
        primary = "Animal in vivo"
        tags.append("Animal/In Vivo")
    elif in_vitro_flag:
        primary = "In vitro / cell"
        tags.append("In Vitro/Cell")
    elif "review" in pubtypes or "review" in text:
        primary = "Review / narrative"
        tags.append("Review")
    elif _has_phrase_any(text, ["mechanism", "pathway", "assay", "protocol", "validation", "method"]):
        primary = "Methods / Mechanistic"
        tags.append("Methods/Mechanistic")
    else:
        primary = "Other"

    _add_tags(tags, text, pubtypes)
    model_type = _model_type(human_flag, animal_flag, in_vitro_flag, text)
    species_or_population = _species_or_population(text, human_flag, animal_flag, in_vitro_flag)
    confidence = _confidence(primary, record)
    return RuleClassification(
        primary_study_type=primary,
        study_design_tags=sorted(set(tags)),
        model_type=model_type,
        species_or_population=species_or_population,
        human_flag=human_flag,
        animal_flag=animal_flag,
        in_vitro_flag=in_vitro_flag,
        confidence=confidence,
    )


def _add_tags(tags: List[str], text: str, pubtypes: str) -> None:
    tag_rules = [
        ("Phase 1", ["phase i", "phase 1"]),
        ("Phase 2", ["phase ii", "phase 2"]),
        ("Phase 3", ["phase iii", "phase 3"]),
        ("Phase 4", ["phase iv", "phase 4"]),
        ("Prospective", ["prospective"]),
        ("Retrospective", ["retrospective"]),
        ("RWE/Registry", ["registry", "real-world", "real world", "claims database", "ehr"]),
        ("PK/PD", ["pharmacokinetic", "pharmacodynamic", "pk/pd", "bioavailability", "half-life"]),
        ("Safety/Tolerability", ["adverse event", "safety", "tolerability", "toxicity"]),
        ("Protocol/Pilot", ["protocol", "feasibility", "pilot study"]),
        ("Guideline/Consensus", ["practice guideline", "guideline", "consensus statement"]),
    ]
    haystack = f"{text} {pubtypes}"
    for tag, terms in tag_rules:
        if _has_phrase_any(haystack, terms):
            tags.append(tag)


def _model_type(human: bool, animal: bool, in_vitro: bool, text: str) -> str:
    if human:
        return "human"
    if animal:
        return "animal"
    if in_vitro:
        return "in vitro"
    if _has_phrase_any(text, ["review"]):
        return "review"
    return "unclear"


def _species_or_population(text: str, human: bool, animal: bool, in_vitro: bool) -> str:
    if human:
        if _has_phrase_any(text, ["healthy volunteer", "healthy volunteers"]):
            return "healthy human volunteers"
        if _has_phrase_any(text, ["patients"]):
            return "patients"
        return "humans"
    if animal:
        species = [name for name in ["mice", "mouse", "rats", "rat", "murine", "porcine", "canine"] if _has_phrase_any(text, [name])]
        return ", ".join(species[:3]) if species else "animals"
    if in_vitro:
        return "cells / in vitro model"
    return "not clearly reported"


def _confidence(primary: str, record: PubMedRecord) -> str:
    if not record.abstract:
        return "low"
    if primary == "Other":
        return "low"
    if record.pubtypes or record.mesh_terms:
        return "medium"
    return "low"


def _has_mesh_any(mesh: str, terms: Iterable[str]) -> bool:
    return any(re.search(rf"(^|[;:,]\s*){re.escape(term)}($|[;:,])", mesh) for term in terms)


def _has_phrase_any(text: str, terms: Iterable[str]) -> bool:
    return any(_has_phrase(text, term) for term in terms)


def _has_phrase(text: str, term: str) -> bool:
    if not term:
        return False
    escaped = re.escape(term)
    escaped = escaped.replace(r"\ ", r"\s+")
    if re.search(r"[a-z0-9]$", term) and re.search(r"^[a-z0-9]", term):
        pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    elif re.search(r"[a-z0-9]$", term):
        pattern = rf"{escaped}(?![a-z0-9])"
    elif re.search(r"^[a-z0-9]", term):
        pattern = rf"(?<![a-z0-9]){escaped}"
    else:
        pattern = escaped
    return re.search(pattern, text) is not None
