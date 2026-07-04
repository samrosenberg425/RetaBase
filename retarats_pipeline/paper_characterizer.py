from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Iterable, List

from .abstract_extraction import extract_from_title_abstract, first_nonblank, merge_semicolon


PAPER_CHARACTERIZATION_FIELDS = [
    "paper_purpose",
    "paper_subtype",
    "what_it_is",
    "evidence_question",
    "model_system_detail",
    "population_or_sample",
    "condition_tags",
    "intervention_or_exposure",
    "comparator_or_control",
    "dose_route",
    "duration",
    "sample_size",
    "endpoint_tags",
    "outcome_direction",
    "efficacy_signal",
    "safety_signal",
    "mechanistic_focus",
    "methods_focus",
    "key_paper_parts",
    "paper_characterization_confidence",
    "paper_characterization_notes",
    "initial_extraction_source",
    "abstract_model_type",
    "abstract_model_system_detail",
    "abstract_condition_tags",
    "abstract_endpoint_tags",
    "abstract_mechanistic_focus",
    "abstract_comparator_or_control",
    "abstract_dose_route",
    "abstract_duration",
    "abstract_sample_size",
    "abstract_efficacy_signal",
    "abstract_safety_signal",
    "abstract_outcome_direction",
    "abstract_key_sentences",
    "abstract_extraction_confidence",
    "abstract_extraction_notes",
    "abstract_extraction_source",
]


@dataclass
class PaperCharacterization:
    paper_purpose: str
    paper_subtype: str
    what_it_is: str
    evidence_question: str
    model_system_detail: str
    population_or_sample: str
    condition_tags: str
    intervention_or_exposure: str
    comparator_or_control: str
    dose_route: str
    duration: str
    sample_size: str
    endpoint_tags: str
    outcome_direction: str
    efficacy_signal: str
    safety_signal: str
    mechanistic_focus: str
    methods_focus: str
    key_paper_parts: str
    paper_characterization_confidence: str
    paper_characterization_notes: str
    initial_extraction_source: str = "pubmed_title_abstract_mesh"
    abstract_model_type: str = ""
    abstract_model_system_detail: str = ""
    abstract_condition_tags: str = ""
    abstract_endpoint_tags: str = ""
    abstract_mechanistic_focus: str = ""
    abstract_comparator_or_control: str = ""
    abstract_dose_route: str = ""
    abstract_duration: str = ""
    abstract_sample_size: str = ""
    abstract_efficacy_signal: str = ""
    abstract_safety_signal: str = ""
    abstract_outcome_direction: str = ""
    abstract_key_sentences: str = ""
    abstract_extraction_confidence: str = "low"
    abstract_extraction_notes: str = ""
    abstract_extraction_source: str = "pubmed_title_abstract_mesh"

    def to_dict(self) -> dict:
        return asdict(self)


def characterize_paper(evidence: dict, paper: dict, molecule: dict) -> PaperCharacterization:
    title = str(paper.get("title", "") or "")
    abstract = str(paper.get("abstract", "") or "")
    pubtypes = _join(paper.get("pubtypes", ""))
    mesh_terms = _join(paper.get("mesh_terms", ""))
    context = _match_normalize(f"{title}. {abstract} {pubtypes} {mesh_terms}")
    abstract_extraction = extract_from_title_abstract(
        title=title,
        abstract=abstract,
        mesh_terms=paper.get("mesh_terms", ""),
        pubtypes=paper.get("pubtypes", ""),
    )

    purpose = _paper_purpose(evidence, context)
    subtype = _paper_subtype(evidence, context)
    model_detail = first_nonblank(_model_system_detail(evidence, context), abstract_extraction.abstract_model_system_detail)
    population = _population_or_sample(evidence, context)
    condition_tags = _split_merged(_tags(context, CONDITION_RULES), abstract_extraction.abstract_condition_tags)
    endpoint_tags = _split_merged(_tags(context, ENDPOINT_RULES), abstract_extraction.abstract_endpoint_tags)
    mechanism_tags = _split_merged(_tags(context, MECHANISM_RULES), abstract_extraction.abstract_mechanistic_focus)
    methods_tags = _tags(context, METHOD_RULES)
    intervention = _intervention_or_exposure(evidence, molecule, context)
    comparator = first_nonblank(_comparator_or_control(context), abstract_extraction.abstract_comparator_or_control, "not clearly reported")
    dose_route = first_nonblank(_dose_route(context), abstract_extraction.abstract_dose_route, "not clearly reported")
    duration = first_nonblank(_duration(context), abstract_extraction.abstract_duration, "not clearly reported")
    sample_size = first_nonblank(_sample_size(context), abstract_extraction.abstract_sample_size, "not clearly reported")
    efficacy = first_nonblank(_best_sentence(abstract, EFFICACY_TERMS), abstract_extraction.abstract_efficacy_signal)
    safety = first_nonblank(_best_sentence(abstract, SAFETY_TERMS), abstract_extraction.abstract_safety_signal)
    outcome = first_nonblank(_outcome_direction(context, efficacy, safety, purpose), abstract_extraction.abstract_outcome_direction, "not clearly reported")
    question = _evidence_question(purpose, evidence, molecule, condition_tags, endpoint_tags)
    what_it_is = _what_it_is(purpose, subtype, evidence, condition_tags, model_detail)
    parts = _key_parts(
        purpose=purpose,
        subtype=subtype,
        model_detail=model_detail,
        population=population,
        condition_tags=condition_tags,
        intervention=intervention,
        comparator=comparator,
        endpoint_tags=endpoint_tags,
        outcome=outcome,
    )
    confidence, notes = _confidence_notes(paper, evidence, purpose, endpoint_tags, condition_tags)

    return PaperCharacterization(
        paper_purpose=purpose,
        paper_subtype=subtype,
        what_it_is=what_it_is,
        evidence_question=question,
        model_system_detail=model_detail,
        population_or_sample=population,
        condition_tags="; ".join(condition_tags),
        intervention_or_exposure=intervention,
        comparator_or_control=comparator,
        dose_route=dose_route,
        duration=duration,
        sample_size=sample_size,
        endpoint_tags="; ".join(endpoint_tags),
        outcome_direction=outcome,
        efficacy_signal=efficacy or "not reported",
        safety_signal=safety or "not reported",
        mechanistic_focus="; ".join(mechanism_tags) if mechanism_tags else "not clearly reported",
        methods_focus="; ".join(methods_tags) if methods_tags else "not clearly reported",
        key_paper_parts=parts,
        paper_characterization_confidence=confidence,
        paper_characterization_notes=notes,
        **abstract_extraction.to_dict(),
    )


def characterize_many(evidence_rows: Iterable[dict], paper_by_pmid: dict, molecule_by_id: dict) -> List[dict]:
    out = []
    for row in evidence_rows:
        row = dict(row)
        pmid = str(row.get("pmid", ""))
        molecule_id = str(row.get("molecule_id", ""))
        paper = paper_by_pmid.get(pmid, {})
        molecule = molecule_by_id.get(molecule_id, {})
        row.update(characterize_paper(row, paper, molecule).to_dict())
        out.append(row)
    return out


CONDITION_RULES = {
    "obesity_weight": ["obesity", "overweight", "body weight", "weight loss", "adiposity"],
    "diabetes_glycemic": ["diabetes", "glycemic", "glycaemic", "glucose", "hba1c", "insulin resistance"],
    "liver_mash": ["mash", "nash", "steatohepatitis", "fatty liver", "hepatic steatosis"],
    "cardiovascular": ["cardiovascular", "heart failure", "atherosclerosis", "hypertension", "myocardial"],
    "kidney": ["kidney", "renal", "ckd", "nephropathy"],
    "neurocognitive": ["brain", "cognitive", "alzheimer", "parkinson", "neurodegenerative", "dementia"],
    "oncology": ["cancer", "tumor", "tumour", "carcinoma", "leukemia", "melanoma"],
    "inflammation_autoimmune": ["inflammation", "inflammatory", "psoriasis", "arthritis", "autoimmune"],
    "mitochondrial_disease": ["mitochondrial disease", "mitochondriopathy"],
    "reproductive": ["fertility", "sperm", "ovary", "pcos", "testosterone"],
    "musculoskeletal": ["muscle", "sarcopenia", "tendon", "bone", "joint"],
    "infectious_immunity": ["infection", "bacterial", "viral", "immune", "antimicrobial"],
}

ENDPOINT_RULES = {
    "body_weight": ["body weight", "weight loss", "bmi", "adiposity", "fat mass"],
    "glycemic_control": ["glucose", "hba1c", "glycemic", "glycaemic", "insulin"],
    "lipids_metabolic": ["lipid", "cholesterol", "triglyceride", "metabolic"],
    "liver_histology": ["fibrosis", "steatosis", "steatohepatitis", "alt", "ast"],
    "cardiovascular_endpoint": ["blood pressure", "cardiovascular", "heart", "atherosclerosis"],
    "renal_endpoint": ["renal", "kidney", "egfr", "albuminuria", "creatinine"],
    "inflammation": ["inflammation", "cytokine", "tnf", "interleukin", "nf-kb", "nfkb"],
    "oxidative_stress": ["oxidative stress", "redox", "ros", "glutathione", "antioxidant"],
    "mitochondrial_function": ["mitochondria", "mitochondrial", "atp", "oxphos", "respiration"],
    "autophagy_mtor": ["autophagy", "mtor", "torc1", "lysosomal", "mitophagy"],
    "senescence": ["senescence", "senolytic", "sasp"],
    "pharmacokinetics": ["pharmacokinetic", "bioavailability", "half-life", "auc", "cmax"],
    "safety_tolerability": ["safety", "adverse event", "tolerability", "toxicity"],
    "mortality_survival": ["mortality", "survival", "death"],
}

MECHANISM_RULES = {
    "incretin_receptor": ["glp-1", "gip", "glucagon receptor", "incretin"],
    "nad_metabolism": ["nad+", "nadh", "nicotinamide", "sirtuin", "cd38"],
    "mtor_autophagy": ["mtor", "torc1", "autophagy", "lysosomal", "mitophagy"],
    "redox_antioxidant": ["oxidative stress", "redox", "ros", "antioxidant", "glutathione"],
    "mitochondrial": ["mitochondria", "mitochondrial", "oxphos", "electron transport"],
    "inflammation": ["inflammation", "nf-kb", "nfkb", "cytokine", "inflammasome"],
    "senescence": ["senescence", "senolytic", "sasp"],
    "ampk_metabolic": ["ampk", "metabolic", "insulin sensitivity"],
}

METHOD_RULES = {
    "analytical_detection": ["detection", "quantification", "lc-ms", "lc/ms", "mass spectrometry", "sensor", "assay"],
    "synthesis_formulation": ["synthesis", "formulation", "encapsulation", "nanoparticle", "solid-phase"],
    "environmental_material": ["wastewater", "adsorption", "photocatalytic", "dye degradation", "aqueous solution"],
    "review_synthesis": ["systematic review", "meta-analysis", "narrative review", "review"],
}

EFFICACY_TERMS = [
    "improved", "reduced", "increased", "decreased", "lowered", "attenuated", "protected",
    "significant", "effective", "associated with", "benefit", "weight loss", "superior",
]

SAFETY_TERMS = [
    "adverse", "safety", "tolerability", "toxicity", "nausea", "vomiting", "diarrhea",
    "hypoglycemia", "hypoglycaemia", "serious adverse", "death", "mortality",
]


def _paper_purpose(evidence: dict, context: str) -> str:
    role = str(evidence.get("role_category", ""))
    primary = str(evidence.get("primary_study_type", ""))
    relevance = str(evidence.get("molecule_relevance", ""))
    if "Meta-analysis" in primary or "Systematic review" in primary:
        return "evidence_synthesis"
    if "Review" in primary:
        return "narrative_review_or_background"
    if relevance == "primary_intervention" and primary in {"RCT", "Human interventional non-RCT", "Clinical trial / unclear population"}:
        return "intervention_efficacy_safety"
    if role == "direct_intervention":
        return "intervention_efficacy_safety"
    if role == "comparator_or_background_drug":
        return "comparator_or_background_therapy"
    if role == "biomarker_readout":
        return "biomarker_measurement"
    if role == "pathway_component":
        return "mechanism_or_pathway"
    if role == "clinical_tool_or_diagnostic":
        return "diagnostic_or_procedural_tool"
    if role == "assay_or_detection":
        return "assay_or_detection_method"
    if role == "synthesis_or_production":
        return "synthesis_formulation_or_production"
    if role == "environmental_or_material_use":
        return "environmental_or_materials_use"
    if "observational" in primary.lower():
        return "observational_association"
    if relevance == "background_mention":
        return "background_mention"
    return "unclear_or_mixed_purpose"


def _paper_subtype(evidence: dict, context: str) -> str:
    primary = str(evidence.get("primary_study_type", ""))
    tags = str(evidence.get("study_design_tags", ""))
    bits = [primary] if primary else []
    for term in ["Phase 1", "Phase 2", "Phase 3", "Phase 4", "Randomized", "Prospective", "Retrospective", "PK/PD"]:
        if term.lower() in tags.lower() or _contains(context, [term]):
            bits.append(term)
    return "; ".join(dict.fromkeys(bits)) if bits else "not clearly reported"


def _model_system_detail(evidence: dict, context: str) -> str:
    model = str(evidence.get("model_type", "") or "unclear")
    species = str(evidence.get("species_or_population", "") or "")
    model_terms = []
    for term in ["mice", "mouse", "rats", "rat", "murine", "zebrafish", "c. elegans", "drosophila", "hepg2", "hela", "fibroblast", "organoid"]:
        if _contains(context, [term]):
            model_terms.append(term)
    detail = "; ".join(dict.fromkeys([x for x in [model, species] + model_terms if x and x != "not clearly reported"]))
    return detail or "not clearly reported"


def _population_or_sample(evidence: dict, context: str) -> str:
    if str(evidence.get("model_type", "")) == "human":
        age = "older adults" if _contains(context, ["older adults", "elderly"]) else ""
        health = "healthy volunteers" if _contains(context, ["healthy volunteer", "healthy volunteers"]) else ""
        patients = "patients" if _contains(context, ["patients"]) else ""
        return "; ".join(x for x in [age, health, patients] if x) or "human participants"
    if str(evidence.get("model_type", "")) == "animal":
        return str(evidence.get("species_or_population", "") or "animal model")
    if str(evidence.get("model_type", "")) == "in vitro":
        return "cell or in vitro sample"
    return "not clearly reported"


def _intervention_or_exposure(evidence: dict, molecule: dict, context: str) -> str:
    name = str(molecule.get("display_name", "") or evidence.get("molecule_name", "") or evidence.get("molecule_id", "matched molecule"))
    role = str(evidence.get("role_category", ""))
    if role == "direct_intervention":
        return f"{name} as direct intervention"
    if role == "biomarker_readout":
        return f"{name} measured as biomarker/readout"
    if role == "pathway_component":
        return f"{name} discussed as pathway/mechanism component"
    if role == "comparator_or_background_drug":
        return f"{name} as comparator, combination component, or background therapy"
    if role in {"assay_or_detection", "synthesis_or_production", "environmental_or_material_use"}:
        return f"{name} in {role.replace('_', ' ')} context"
    return name


def _comparator_or_control(context: str) -> str:
    if _contains(context, ["placebo"]):
        return "placebo"
    if _contains(context, ["vehicle"]):
        return "vehicle control"
    if _contains(context, ["standard of care", "usual care"]):
        return "standard/usual care"
    if _contains(context, ["compared with", "versus", " vs "]):
        return "active comparator mentioned"
    if _contains(context, ["control group", "controls"]):
        return "control group"
    return "not clearly reported"


def _dose_route(context: str) -> str:
    routes = []
    for route in ["oral", "subcutaneous", "intravenous", "intraperitoneal", "topical", "inhaled"]:
        if _contains(context, [route]):
            routes.append(route)
    doses = re.findall(r"\b\d+(?:\.\d+)?\s*(?:mg|ug|microgram|g|nmol|umol|mmol|mg/kg|ug/kg)(?:/[a-z]+)?\b", context)
    parts = routes + doses[:3]
    return "; ".join(dict.fromkeys(parts)) if parts else "not clearly reported"


def _duration(context: str) -> str:
    matches = re.findall(r"\b\d+\s*(?:day|days|week|weeks|month|months|year|years)\b", context)
    return "; ".join(dict.fromkeys(matches[:3])) if matches else "not clearly reported"


def _sample_size(context: str) -> str:
    match = re.search(r"\bn\s*=?\s*(\d+)\b", context)
    if match:
        return match.group(0)[:120]
    for match in re.finditer(r"\b(\d+)\s+(patients|participants|subjects|volunteers|mice|rats)\b", context):
        value = int(match.group(1))
        noun = match.group(2)
        if noun in {"mice", "rats"} or value >= 5:
            return match.group(0)[:120]
    return "not clearly reported"


def _outcome_direction(context: str, efficacy: str, safety: str, purpose: str) -> str:
    efficacy_l = efficacy.lower()
    safety_l = safety.lower()
    if purpose in {"assay_or_detection_method", "synthesis_formulation_or_production", "environmental_or_materials_use"}:
        return "method_or_non_efficacy_outcome"
    if any(term in efficacy_l for term in ["improved", "reduced", "decreased", "attenuated", "protected", "weight loss", "superior"]):
        return "beneficial_or_desired_signal"
    if any(term in efficacy_l for term in ["increased", "elevated"]) and _contains(efficacy_l, ["risk", "toxicity", "mortality"]):
        return "harmful_signal"
    if any(term in safety_l for term in ["serious adverse", "toxicity", "death"]):
        return "safety_signal_present"
    if _contains(context, ["no significant", "not significant", "did not improve"]):
        return "neutral_or_no_clear_effect"
    if efficacy:
        return "effect_reported_direction_unclear"
    return "not clearly reported"


def _evidence_question(purpose: str, evidence: dict, molecule: dict, condition_tags: List[str], endpoint_tags: List[str]) -> str:
    name = str(molecule.get("display_name", "") or evidence.get("molecule_name", "") or "the molecule")
    condition = condition_tags[0] if condition_tags else "the studied context"
    endpoint = endpoint_tags[0] if endpoint_tags else "reported outcomes"
    templates = {
        "intervention_efficacy_safety": f"Does {name} affect {endpoint} in {condition}?",
        "mechanism_or_pathway": f"What role does {name} have in mechanisms/pathways related to {condition}?",
        "biomarker_measurement": f"Are {name} levels or related measures associated with {condition} or {endpoint}?",
        "evidence_synthesis": f"What does the review/meta-analysis say about {name} or its treatment class?",
        "assay_or_detection_method": f"How is {name} detected, quantified, synthesized, or measured?",
    }
    return templates.get(purpose, f"What does this paper contribute about {name}?")


def _what_it_is(purpose: str, subtype: str, evidence: dict, condition_tags: List[str], model_detail: str) -> str:
    condition = condition_tags[0].replace("_", " ") if condition_tags else "unspecified condition"
    return f"{purpose.replace('_', ' ')} paper; {subtype}; model/context: {model_detail}; focus: {condition}"


def _key_parts(**kwargs) -> str:
    labels = [
        ("purpose", kwargs["purpose"]),
        ("subtype", kwargs["subtype"]),
        ("model", kwargs["model_detail"]),
        ("population", kwargs["population"]),
        ("conditions", "; ".join(kwargs["condition_tags"]) or "not clearly reported"),
        ("intervention/exposure", kwargs["intervention"]),
        ("comparator", kwargs["comparator"]),
        ("endpoints", "; ".join(kwargs["endpoint_tags"]) or "not clearly reported"),
        ("outcome", kwargs["outcome"]),
    ]
    return " | ".join(f"{label}: {value}" for label, value in labels)


def _confidence_notes(paper: dict, evidence: dict, purpose: str, endpoint_tags: List[str], condition_tags: List[str]) -> tuple:
    notes = []
    if not paper.get("abstract"):
        notes.append("no abstract available")
    if purpose == "unclear_or_mixed_purpose":
        notes.append("paper purpose unclear from title/abstract")
    if not endpoint_tags:
        notes.append("endpoints not clearly extracted")
    if not condition_tags:
        notes.append("condition area not clearly extracted")
    if not notes and paper.get("abstract"):
        return "medium", "rule-based extraction from title/abstract"
    if paper.get("abstract") and len(notes) <= 1:
        return "medium", "; ".join(notes)
    return "low", "; ".join(notes) if notes else "limited metadata"


def _tags(text: str, rules: dict) -> List[str]:
    return [tag for tag, terms in rules.items() if _contains(text, terms)]


def _split_merged(existing: List[str], inferred: str) -> List[str]:
    merged = merge_semicolon("; ".join(existing), inferred)
    return [part.strip() for part in merged.split(";") if part.strip()]


def _contains(text: str, terms: Iterable[str]) -> bool:
    return any(_phrase_in_text(text, term) for term in terms)


def _phrase_in_text(text: str, term: str) -> bool:
    term = _match_normalize_term(term)
    if not term:
        return False
    return f" {term} " in f" {text} "


def _best_sentence(abstract: str, terms: List[str]) -> str:
    for sentence in _sentences(abstract):
        lower = sentence.lower()
        if any(term in lower for term in terms):
            return sentence[:700]
    return ""


def _sentences(text: str) -> List[str]:
    text = " ".join((text or "").split())
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _join(value) -> str:
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return str(value or "")


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _match_normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


@lru_cache(maxsize=2048)
def _match_normalize_term(term: str) -> str:
    return _match_normalize(term)
