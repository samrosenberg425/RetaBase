from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .common import clean_text, is_blankish, sentence_split, semicolon_join, split_semicolon, text_blob

LOW_CONFIDENCE_PATTERNS = {
    "dose_route": [
        r"\bmmol\s*/?\s*mol\b", r"\bHbA1c\b", r"\bBMI\b", r"\bbody mass index\b",
        r"\baged?\b", r"\byears old\b", r"\bdiabetes duration\b", r"\bage\b",
        r"\bkg/m\s*2\b", r"\bkg/m2\b", r"\b95%\s*CI\b", r"\bCI\b",
    ],
    "duration": [
        r"\baged?\b", r"\byears old\b", r"\bdiabetes duration\b", r"\bdisease duration\b",
        r"\bfollowed since birth\b", r"\bmean age\b", r"\bmedian age\b",
    ],
    "sample_size": [
        r"\bmg\b", r"\bmmol\b", r"\bweeks?\b", r"\byears?\b", r"\bBMI\b",
    ],
}

DOSE_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?|\d+/\d+)\s*(?:mg|µg|ug|mcg|g|IU|U|nmol|pmol|mmol|mol|mg/kg|µg/kg|ug/kg|mg/kg/day|mg\s*/\s*kg|mg\s*/\s*kg\s*/\s*day)\b(?:\s*(?:once|twice|daily|weekly|per day|q\d+[dhw])[^.;,)]*)?",
    re.I,
)
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:hours?|days?|weeks?|months?|years?)\b", re.I)
SAMPLE_RE = re.compile(r"\b(?:n\s*=\s*|N\s*=\s*|enrolled\s+|included\s+|randomized\s+|assigned\s+)?(\d{1,3}(?:,\d{3})+|\d{1,6})\s+(?:participants|patients|subjects|adults|mice|rats|animals|cells|samples)\b", re.I)
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
    ("cell_culture", r"\b(cell line|cells|culture|in vitro|HEK|HepG2|C2C12|3T3|RAW\s*264\.7|SH-SY5Y)\b"),
]

MECHANISM_PATTERNS = [
    ("inflammation", r"\b(inflamm|cytokine|TNF|IL-6|IL-1|NF-?κ?B|NLRP3|macrophage)\b"),
    ("oxidative_stress", r"\b(oxidative stress|ROS|reactive oxygen|glutathione|Nrf2|SOD|catalase|MDA)\b"),
    ("mitochondrial_function", r"\b(mitochond|ATP|oxidative phosphorylation|respiration|PGC-1|complex I|complex II)\b"),
    ("insulin_glucose", r"\b(insulin|glucose|glycemic|HbA1c|HOMA|GLUT4|beta cell)\b"),
    ("appetite_weight", r"\b(appetite|satiety|food intake|body weight|adiposity|obesity|weight loss)\b"),
    ("angiogenesis_repair", r"\b(angiogenesis|VEGF|wound|healing|tendon|ligament|fibroblast|collagen|repair)\b"),
    ("neuroprotection", r"\b(neuroprotect|neuron|dopamine|synaptic|cognition|memory|brain|spinal cord|nerve)\b"),
    ("muscle_performance", r"\b(muscle|exercise|endurance|strength|atrophy|hypertrophy|myotube)\b"),
    ("receptor_signaling", r"\b(receptor|agonist|antagonist|GLP-1|GIP|glucagon|ERR|PPAR|AMPK|mTOR|AKT|ERK)\b"),
]

CONDITION_PATTERNS = [
    ("obesity_weight", r"\b(obesity|overweight|body weight|weight loss|adiposity|fat mass)\b"),
    ("diabetes_glycemic", r"\b(diabetes|glycemic|glucose|HbA1c|insulin resistance|T2D|type 2 diabetes)\b"),
    ("cardiovascular", r"\b(cardiovascular|heart failure|atherosclerosis|myocardial|blood pressure|stroke)\b"),
    ("neurocognitive", r"\b(cognition|memory|Alzheimer|Parkinson|depression|brain|neuro)\b"),
    ("musculoskeletal", r"\b(muscle|tendon|ligament|bone|cartilage|joint|arthritis)\b"),
    ("inflammation_autoimmune", r"\b(inflammation|autoimmune|colitis|arthritis|cytokine)\b"),
    ("injury_repair", r"\b(injury|wound|healing|repair|ulcer|trauma|ischemia)\b"),
]

ENDPOINT_PATTERNS = [
    ("body_weight", r"\b(body weight|weight loss|BMI|adiposity|fat mass)\b"),
    ("glycemic_control", r"\b(HbA1c|glucose|glycemic|insulin|HOMA)\b"),
    ("safety_tolerability", r"\b(adverse event|safety|tolerability|nausea|vomiting|diarrhea|discontinuation|toxicity)\b"),
    ("inflammation", r"\b(inflamm|cytokine|TNF|IL-6|CRP|NF-?κ?B)\b"),
    ("oxidative_stress", r"\b(oxidative stress|ROS|glutathione|Nrf2|MDA|SOD)\b"),
    ("mitochondrial_function", r"\b(mitochond|ATP|respiration|oxidative phosphorylation)\b"),
    ("cardiovascular_endpoint", r"\b(MACE|cardiovascular|blood pressure|heart failure|stroke|myocardial)\b"),
    ("functional_repair", r"\b(healing|repair|strength|function|locomotor|recovery|collagen)\b"),
]


def looks_low_confidence(field: str, value: Any) -> bool:
    if is_blankish(value):
        return True
    text = clean_text(value)
    if len(text) > 300 and field in {"dose_route", "duration", "sample_size", "comparator_or_control"}:
        return True
    for pattern in LOW_CONFIDENCE_PATTERNS.get(field, []):
        if re.search(pattern, text, re.I):
            return True
    if field == "sample_size" and not re.search(r"\d", text):
        return True
    return False


def should_suggest_replacement(field: str, original: Any, enriched: Any) -> bool:
    if is_blankish(enriched):
        return False
    return is_blankish(original) or looks_low_confidence(field, original)


def extract_contextual_values(title: Any, abstract: Any) -> Dict[str, str]:
    sentences = sentence_split(text_blob(title, abstract))
    doses: List[str] = []
    durations: List[str] = []
    samples: List[str] = []
    for sent in sentences:
        if DOSE_CONTEXT.search(sent) and not EXCLUDE_CONTEXT.search(sent):
            doses.extend(m.group(0).strip() for m in DOSE_RE.finditer(sent))
        if DURATION_CONTEXT.search(sent) and not re.search(r"\b(age|aged|years old|diabetes duration|disease duration)\b", sent, re.I):
            durations.extend(m.group(0).strip() for m in DURATION_RE.finditer(sent))
        if SAMPLE_CONTEXT.search(sent):
            for m in SAMPLE_RE.finditer(sent):
                n = m.group(1)
                if _sample_match_is_likely_dose(sent, m.start(), m.end()):
                    continue
                n_int = _sample_int(n)
                if 1 <= n_int <= 1000000:
                    samples.append(str(n_int))
            for m in BARE_N_RE.finditer(sent):
                n = m.group(1)
                if _sample_match_is_likely_dose(sent, m.start(), m.end()):
                    continue
                n_int = _sample_int(n)
                if 1 <= n_int <= 1000000:
                    samples.append(str(n_int))
    return {
        "heuristic_dose_route": semicolon_join(doses[:6]),
        "heuristic_duration": semicolon_join(durations[:6]),
        "heuristic_sample_size": semicolon_join(samples[:3]),
    }


def _sample_match_is_likely_dose(sentence: str, start: int, end: int) -> bool:
    window = sentence[max(0, start - 80): min(len(sentence), end + 80)]
    if bool(SAMPLE_EXCLUDE_CONTEXT.search(window)):
        return True
    if start > 0 and sentence[start - 1] == "-":
        return True
    matched = sentence[start:end]
    n_match = re.search(r"\d{1,6}", matched)
    if n_match and int(n_match.group(0)) < 3 and not re.search(r"\b[Nn]\s*=", matched):
        return True
    return False


def _sample_int(value: str) -> int:
    try:
        return int(str(value).replace(",", ""))
    except Exception:
        return -1


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


def merge_tags(original: Any, inferred: Any) -> str:
    return semicolon_join(split_semicolon(original) + split_semicolon(inferred))


def lane_completeness(row: Mapping[str, Any], required_fields: Iterable[str]) -> Tuple[float, str]:
    fields = list(required_fields)
    if not fields:
        return 1.0, ""
    missing = [f for f in fields if is_blankish(row.get(f)) or looks_low_confidence(f, row.get(f))]
    return round((len(fields) - len(missing)) / len(fields), 3), semicolon_join(missing)
