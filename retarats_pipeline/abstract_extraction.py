from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, List, Mapping, Sequence, Tuple


MISSING_VALUES = {
    "",
    "na",
    "n/a",
    "none",
    "null",
    "nan",
    "not reported",
    "not clearly reported",
    "unclear",
    "unknown",
    "not applicable",
    "not_applicable",
    "nr",
}


SPECIES_PATTERNS = [
    ("mouse", r"\b(mouse|mice|murine)\b"),
    ("rat", r"\b(rat|rats)\b"),
    ("zebrafish", r"\bzebrafish\b"),
    ("drosophila", r"\b(drosophila|fruit fly)\b"),
    ("c_elegans", r"\b(C\.\s*elegans|Caenorhabditis elegans)\b"),
    ("pig", r"\b(pig|porcine|swine)\b"),
    ("rabbit", r"\brabbit\b"),
    ("dog", r"\b(dog|canine)\b"),
    ("nonhuman_primate", r"\b(monkey|macaque|primate)\b"),
    ("cell_culture", r"\b(cell line|cells|culture|in vitro|HEK|HepG2|C2C12|3T3|RAW\s*264\.7|SH-SY5Y|myotubes?|organoids?)\b"),
]

MECHANISM_PATTERNS = [
    ("inflammation", r"\b(inflamm|cytokine|TNF|IL-6|IL-1|NF-?κ?B|NF-kB|NLRP3|macrophage|inflammasome)\b"),
    ("oxidative_stress", r"\b(oxidative stress|ROS|reactive oxygen|glutathione|Nrf2|SOD|catalase|MDA|redox)\b"),
    ("mitochondrial_function", r"\b(mitochond|ATP|oxidative phosphorylation|respiration|PGC-1|complex I|complex II|OXPHOS)\b"),
    ("insulin_glucose", r"\b(insulin|glucose|glycemic|glycaemic|HbA1c|HOMA|GLUT4|beta cell)\b"),
    ("appetite_weight", r"\b(appetite|satiety|food intake|body weight|adiposity|obesity|weight loss)\b"),
    ("angiogenesis_repair", r"\b(angiogenesis|VEGF|wound|healing|tendon|ligament|fibroblast|collagen|repair)\b"),
    ("neuroprotection", r"\b(neuroprotect|neuron|dopamine|synaptic|cognition|memory|brain|spinal cord|nerve|retinal)\b"),
    ("muscle_performance", r"\b(muscle|exercise|endurance|strength|atrophy|hypertrophy|myotube)\b"),
    ("receptor_signaling", r"\b(receptor|agonist|antagonist|GLP-1|GIP|glucagon|ERR|PPAR|AMPK|mTOR|AKT|ERK|PI3K)\b"),
    ("autophagy_mitophagy", r"\b(autophagy|mitophagy|lysosomal|beclin|LC3|p62)\b"),
    ("senescence", r"\b(senescence|senolytic|SASP|p16|p21)\b"),
]

CONDITION_PATTERNS = [
    ("obesity_weight", r"\b(obesity|overweight|body weight|weight loss|adiposity|fat mass)\b"),
    ("diabetes_glycemic", r"\b(diabetes|glycemic|glycaemic|glucose|HbA1c|insulin resistance|T2D|type 2 diabetes)\b"),
    ("liver_mash", r"\b(MASH|NASH|steatohepatitis|fatty liver|hepatic steatosis|fibrosis)\b"),
    ("cardiovascular", r"\b(cardiovascular|heart failure|atherosclerosis|myocardial|blood pressure|stroke|hypertension)\b"),
    ("kidney", r"\b(kidney|renal|CKD|nephropathy)\b"),
    ("neurocognitive", r"\b(cognition|memory|Alzheimer|Parkinson|depression|brain|neuro|retinal degeneration)\b"),
    ("oncology", r"\b(cancer|tumou?r|carcinoma|leukemia|melanoma|lymphoma)\b"),
    ("musculoskeletal", r"\b(muscle|tendon|ligament|bone|cartilage|joint|arthritis|osteoarthritis)\b"),
    ("inflammation_autoimmune", r"\b(inflammation|autoimmune|colitis|arthritis|cytokine|psoriasis)\b"),
    ("injury_repair", r"\b(injury|wound|healing|repair|ulcer|trauma|ischemia|reperfusion)\b"),
    ("infectious_immunity", r"\b(infection|bacterial|viral|immune|antimicrobial|microbiota)\b"),
]

ENDPOINT_PATTERNS = [
    ("body_weight", r"\b(body weight|weight loss|BMI|adiposity|fat mass)\b"),
    ("glycemic_control", r"\b(HbA1c|glucose|glycemic|glycaemic|insulin|HOMA)\b"),
    ("safety_tolerability", r"\b(adverse event|safety|tolerability|nausea|vomiting|diarrhea|discontinuation|toxicity|well tolerated)\b"),
    ("inflammation", r"\b(inflamm|cytokine|TNF|IL-6|CRP|NF-?κ?B|inflammasome)\b"),
    ("oxidative_stress", r"\b(oxidative stress|ROS|glutathione|Nrf2|MDA|SOD|redox)\b"),
    ("mitochondrial_function", r"\b(mitochond|ATP|respiration|oxidative phosphorylation|OXPHOS)\b"),
    ("cardiovascular_endpoint", r"\b(MACE|cardiovascular|blood pressure|heart failure|stroke|myocardial)\b"),
    ("functional_repair", r"\b(healing|repair|strength|function|locomotor|recovery|collagen)\b"),
    ("pharmacokinetics", r"\b(pharmacokinetic|bioavailability|half-life|AUC|Cmax|Tmax)\b"),
    ("mortality_survival", r"\b(mortality|survival|death)\b"),
]

DOSE_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?|\d+/\d+)\s*(?:mg|µg|ug|mcg|g|IU|U|nmol|pmol|mmol|mol|"
    r"mg/kg|µg/kg|ug/kg|mg/kg/day|mg\s*/\s*kg|mg\s*/\s*kg\s*/\s*day)"
    r"\b(?:\s*(?:once|twice|daily|weekly|per day|q\d+[dhw])[^.;,)]*)?",
    re.I,
)
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:hours?|days?|weeks?|months?|years?)\b", re.I)
SAMPLE_RE = re.compile(
    r"\b(?:n\s*=\s*|N\s*=\s*|enrolled\s+|included\s+|randomized\s+|assigned\s+)?"
    r"(\d{1,3}(?:,\d{3})+|\d{1,6})\s+(?:participants|patients|subjects|adults|volunteers|mice|rats|animals|samples)\b",
    re.I,
)
BARE_N_RE = re.compile(r"\b[Nn]\s*=\s*(\d{1,3}(?:,\d{3})+|\d{1,6})\b")

DOSE_CONTEXT = re.compile(r"\b(receiv|administer|assign|dose|dosage|treat|injection|oral|subcutaneous|intraperitoneal|intravenous|gavage|infusion|therapy|intervention|randomi[sz]ed)\b", re.I)
DURATION_CONTEXT = re.compile(r"\b(treatment|follow-up|follow up|duration|for\s+\d|week\s*\d|endpoint|trial|study period|intervention)\b", re.I)
SAMPLE_CONTEXT = re.compile(r"\b(enrolled|included|randomi[sz]ed|assigned|participants|patients|subjects|mice|rats|animals|samples|cohort)\b", re.I)
EXCLUDE_CONTEXT = re.compile(r"\b(age|aged|years old|BMI|body mass index|HbA1c|mmol/mol|confidence interval|95% CI|diabetes duration|disease duration|baseline)\b", re.I)
SAMPLE_EXCLUDE_CONTEXT = re.compile(
    r"\b(CFU|LD\s*50|LD50|mg|µg|ug|mcg|g/kg|mg/kg|IU|U/kg|mmol|mol|dose|dosage|"
    r"administered daily dose|pathway|PICRUSt2|16\s*S|rRNA)\b|[×x]\s*10|/\s*kg",
    re.I,
)

COMPARATOR_RE = re.compile(r"\b(placebo|vehicle|control group|controls|standard care|usual care|active comparator|sham|untreated|wild-type|wild type)\b", re.I)
ROUTE_RE = re.compile(r"\b(oral|subcutaneous|intravenous|intraperitoneal|intranasal|topical|inhaled|gavage|infusion|injection)\b", re.I)

EFFICACY_TERMS = [
    "improved",
    "reduced",
    "increased",
    "decreased",
    "lowered",
    "attenuated",
    "protected",
    "significant",
    "effective",
    "associated with",
    "benefit",
    "weight loss",
    "superior",
    "ameliorated",
]

SAFETY_TERMS = [
    "adverse",
    "safety",
    "tolerability",
    "toxicity",
    "nausea",
    "vomiting",
    "diarrhea",
    "hypoglycemia",
    "hypoglycaemia",
    "serious adverse",
    "death",
    "mortality",
]


@dataclass
class AbstractExtraction:
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


def extract_from_title_abstract(
    title: Any,
    abstract: Any,
    mesh_terms: Any = "",
    pubtypes: Any = "",
) -> AbstractExtraction:
    title_text = clean_text(title)
    abstract_text = clean_text(abstract)
    mesh_text = join_text(mesh_terms)
    pubtype_text = join_text(pubtypes)
    body = text_blob(title_text, abstract_text)
    context = text_blob(title_text, abstract_text, mesh_text, pubtype_text)

    species = detect_species_model(context)
    model_type = infer_model_type(species, context, mesh_text)
    mechanisms = detect_tags(context, MECHANISM_PATTERNS)
    conditions = detect_tags(context, CONDITION_PATTERNS)
    endpoints = detect_tags(context, ENDPOINT_PATTERNS)
    contextual = extract_contextual_values(title_text, abstract_text)
    comparator = extract_comparator(context)
    efficacy = best_sentence(abstract_text, EFFICACY_TERMS)
    safety = best_sentence(abstract_text, SAFETY_TERMS)
    outcome = infer_outcome_direction(context, efficacy, safety)
    key_sentences = semicolon_join([
        best_sentence(abstract_text, EFFICACY_TERMS + SAFETY_TERMS),
        best_sentence(abstract_text, ["model", "mice", "rats", "cells", "treated", "administered", "randomized"]),
    ])
    confidence, notes = extraction_confidence(
        abstract_text=abstract_text,
        species=species,
        mechanisms=mechanisms,
        conditions=conditions,
        endpoints=endpoints,
        contextual=contextual,
    )

    return AbstractExtraction(
        abstract_model_type=model_type,
        abstract_model_system_detail=species,
        abstract_condition_tags=conditions,
        abstract_endpoint_tags=endpoints,
        abstract_mechanistic_focus=mechanisms,
        abstract_comparator_or_control=comparator,
        abstract_dose_route=contextual.get("dose_route", ""),
        abstract_duration=contextual.get("duration", ""),
        abstract_sample_size=contextual.get("sample_size", ""),
        abstract_efficacy_signal=efficacy,
        abstract_safety_signal=safety,
        abstract_outcome_direction=outcome,
        abstract_key_sentences=key_sentences,
        abstract_extraction_confidence=confidence,
        abstract_extraction_notes=notes,
    )


def extract_contextual_values(title: Any, abstract: Any) -> dict:
    sentences = sentence_split(text_blob(title, abstract))
    doses: List[str] = []
    routes: List[str] = []
    durations: List[str] = []
    samples: List[str] = []
    for sent in sentences:
        if DOSE_CONTEXT.search(sent) and not EXCLUDE_CONTEXT.search(sent):
            doses.extend(m.group(0).strip() for m in DOSE_RE.finditer(sent))
            routes.extend(m.group(0).strip() for m in ROUTE_RE.finditer(sent))
        if DURATION_CONTEXT.search(sent) and not re.search(r"\b(age|aged|years old|diabetes duration|disease duration)\b", sent, re.I):
            durations.extend(m.group(0).strip() for m in DURATION_RE.finditer(sent))
        if SAMPLE_CONTEXT.search(sent):
            for m in SAMPLE_RE.finditer(sent):
                n = m.group(1)
                if _sample_match_is_likely_non_sample(sent, m.start(), m.end()):
                    continue
                n_int = _sample_int(n)
                if 3 <= n_int <= 1000000:
                    samples.append(str(n_int))
            for m in BARE_N_RE.finditer(sent):
                n = m.group(1)
                if _sample_match_is_likely_non_sample(sent, m.start(), m.end()):
                    continue
                n_int = _sample_int(n)
                if 1 <= n_int <= 1000000:
                    samples.append(str(n_int))
    return {
        "dose_route": semicolon_join(routes + doses[:6]),
        "duration": semicolon_join(durations[:6]),
        "sample_size": semicolon_join(samples[:3]),
    }


def infer_model_type(species_detail: str, context: str, mesh_text: str = "") -> str:
    lowered = f"{context} {mesh_text}".lower()
    if re.search(r"\b(humans?|patients|participants|volunteers|clinical trial|randomized controlled trial)\b", lowered):
        if not species_detail or re.search(r"\bhumans?\b", mesh_text.lower()):
            return "human"
    if "cell_culture" in species_detail:
        return "in vitro"
    if species_detail:
        return "animal"
    if re.search(r"\bin vitro|cell line|cells|organoid|myotube\b", lowered):
        return "in vitro"
    return ""


def infer_outcome_direction(context: str, efficacy: str, safety: str) -> str:
    eff = efficacy.lower()
    safe = safety.lower()
    if any(term in eff for term in ["improved", "reduced", "decreased", "attenuated", "protected", "ameliorated", "weight loss", "superior"]):
        return "beneficial_or_desired_signal"
    if any(term in eff for term in ["increased", "elevated"]) and re.search(r"\b(risk|toxicity|mortality|injury|inflammation)\b", eff):
        return "harmful_signal"
    if any(term in safe for term in ["serious adverse", "toxicity", "death"]):
        return "safety_signal_present"
    if re.search(r"\b(no significant|not significant|did not improve|failed to)\b", context, re.I):
        return "neutral_or_no_clear_effect"
    if efficacy:
        return "effect_reported_direction_unclear"
    return ""


def extraction_confidence(
    abstract_text: str,
    species: str,
    mechanisms: str,
    conditions: str,
    endpoints: str,
    contextual: Mapping[str, str],
) -> Tuple[str, str]:
    if not abstract_text:
        return "low", "no abstract available"
    signals = sum(bool(x) for x in [species, mechanisms, conditions, endpoints])
    detail_signals = sum(bool(contextual.get(k)) for k in ["dose_route", "duration", "sample_size"])
    if signals >= 3 or (signals >= 2 and detail_signals):
        return "medium", "rule-based extraction from PubMed title/abstract/MeSH"
    if signals >= 1:
        return "medium", "limited rule-based extraction from PubMed title/abstract/MeSH"
    return "low", "few structured fields detected from title/abstract"


def detect_species_model(text: Any) -> str:
    blob = clean_text(text)
    hits = []
    for label, pattern in SPECIES_PATTERNS:
        if re.search(pattern, blob, re.I):
            hits.append(label)
    return semicolon_join(hits)


def detect_tags(text: Any, patterns: Iterable[Tuple[str, str]]) -> str:
    blob = clean_text(text)
    hits = []
    for label, pattern in patterns:
        if re.search(pattern, blob, re.I):
            hits.append(label)
    return semicolon_join(hits)


def extract_comparator(text: Any) -> str:
    hits = []
    for match in COMPARATOR_RE.finditer(clean_text(text)):
        hit = match.group(0).lower()
        if hit == "controls":
            hit = "control group"
        hits.append(hit)
    return semicolon_join(hits[:5])


def best_sentence(text: Any, terms: Sequence[str]) -> str:
    for sentence in sentence_split(clean_text(text)):
        lower = sentence.lower()
        if any(term.lower() in lower for term in terms):
            return sentence[:700]
    return ""


def sentence_split(text: Any) -> List[str]:
    text = clean_text(text)
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9([])", text) if s.strip()]


def should_suggest_replacement(field: str, original: Any, enriched: Any) -> bool:
    if is_blankish(enriched):
        return False
    return is_blankish(original) or looks_low_confidence(field, original)


def looks_low_confidence(field: str, value: Any) -> bool:
    if is_blankish(value):
        return True
    text = clean_text(value)
    if len(text) > 300 and field in {"dose_route", "duration", "sample_size", "comparator_or_control"}:
        return True
    if field == "sample_size" and not re.search(r"\d", text):
        return True
    if field == "dose_route" and re.search(r"\b(HbA1c|BMI|body mass index|95%\s*CI|CI|mmol\s*/?\s*mol)\b", text, re.I):
        return True
    if field == "duration" and re.search(r"\b(age|aged|years old|mean age|median age|diabetes duration|disease duration)\b", text, re.I):
        return True
    return False


def _sample_int(value: str) -> int:
    try:
        return int(str(value).replace(",", ""))
    except Exception:
        return -1


def _sample_match_is_likely_non_sample(sentence: str, start: int, end: int) -> bool:
    window = sentence[max(0, start - 80): min(len(sentence), end + 80)]
    if SAMPLE_EXCLUDE_CONTEXT.search(window):
        return True
    if start > 0 and sentence[start - 1] == "-":
        return True
    matched = sentence[start:end]
    n_match = re.search(r"\d{1,6}", matched)
    if n_match and int(n_match.group(0)) < 3 and not re.search(r"\b[Nn]\s*=", matched):
        return True
    return False


def is_blankish(value: Any) -> bool:
    if value is None:
        return True
    text = clean_text(value)
    return text.lower() in MISSING_VALUES


def split_semicolon(value: Any) -> List[str]:
    if is_blankish(value):
        return []
    return [p.strip() for p in re.split(r"[;|]", str(value)) if p.strip()]


def semicolon_join(values: Iterable[Any]) -> str:
    seen = set()
    out = []
    for value in values:
        s = clean_text(value)
        if not s:
            continue
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return "; ".join(out)


def merge_semicolon(*values: Any) -> str:
    parts: List[str] = []
    for value in values:
        parts.extend(split_semicolon(value))
    return semicolon_join(parts)


def first_nonblank(*values: Any) -> str:
    for value in values:
        if not is_blankish(value):
            return clean_text(value)
    return ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def join_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(clean_text(v) for v in value if clean_text(v))
    return clean_text(value)


def text_blob(*parts: Any) -> str:
    return " ".join(clean_text(p) for p in parts if clean_text(p))
