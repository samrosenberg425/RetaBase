"""Rule-based evidence appraisal + LLM-ready summary scaffold.

For each record we emit a short, defensible read of the evidence:

    appraisal_strengths     semicolon list of strengths (design, controls, N, ...)
    appraisal_limitations   semicolon list of caveats (no comparator, animal-only, ...)
    appraisal_summary       one-line rule-based synopsis (what + direction + caveat)
    appraisal_confidence    high | medium | low  (how sure the rules are)

    llm_summary             EMPTY now -> a future cheap/free LLM pass fills this
    llm_summary_status      not_generated  (queue flag for the optional LLM layer)
    summary_provenance      JSON: {"source": "...", "inputs": [...]}

No LLM or network is used here. The scaffold columns exist so an optional local /
batched LLM step can populate ``llm_summary`` later without reshaping the schema.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import List

_MISSING = {"", "not reported", "not clearly reported", "unclear", "na", "n/a", "none", "unknown", "nan"}

_PROVENANCE = json.dumps(
    {
        "source": "rule_based_v1",
        "inputs": ["primary_study_type", "model_type", "role_category", "comparator", "sample_size", "outcome_direction"],
        "llm": "not_used",
    }
)

APPRAISAL_FIELDS = [
    "appraisal_strengths",
    "appraisal_limitations",
    "appraisal_summary",
    "appraisal_confidence",
    "llm_summary",
    "llm_summary_status",
    "summary_provenance",
]


@dataclass
class Appraisal:
    appraisal_strengths: str
    appraisal_limitations: str
    appraisal_summary: str
    appraisal_confidence: str
    llm_summary: str = ""
    llm_summary_status: str = "not_generated"
    summary_provenance: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _missing(value) -> bool:
    return str(value or "").strip().lower() in _MISSING


def _val(evidence: dict, *keys: str) -> str:
    for k in keys:
        v = evidence.get(k)
        if not _missing(v):
            return str(v)
    return ""


def appraise_evidence(evidence: dict) -> Appraisal:
    primary = str(evidence.get("primary_study_type", "") or "")
    model = str(evidence.get("model_type", "") or "").lower()
    role = str(evidence.get("role_category", "") or "")
    tier = str(evidence.get("reliability_tier", "") or "")

    strengths: List[str] = []
    limits: List[str] = []

    # --- strengths ---
    if primary == "RCT":
        strengths.append("randomized controlled design")
    if "Meta-analysis" in primary:
        strengths.append("meta-analysis pooling multiple studies")
    elif "Systematic review" in primary:
        strengths.append("systematic review of the literature")
    if model == "human":
        strengths.append("evidence in humans")
    comparator = _val(evidence, "comparator_or_control", "abstract_comparator_or_control")
    if comparator and "placebo" in comparator.lower():
        strengths.append("placebo-controlled")
    elif comparator:
        strengths.append(f"comparator reported ({comparator[:40]})")
    n = _val(evidence, "sample_size", "abstract_sample_size")
    if n:
        strengths.append(f"sample size reported ({n[:30]})")
    safety = _val(evidence, "safety_signal", "abstract_safety_signal")
    if safety:
        strengths.append("safety/tolerability discussed")

    # --- limitations ---
    if model == "animal":
        limits.append("preclinical (animal) evidence; may not translate to humans")
    elif model == "in vitro":
        limits.append("in vitro / cell-level evidence only")
    elif model in {"", "unclear"}:
        limits.append("study model unclear from abstract")
    if not comparator:
        limits.append("no comparator/control clearly reported")
    if not n:
        limits.append("sample size not reported in abstract")
    if _missing(evidence.get("outcome_direction")) or "unclear" in str(evidence.get("outcome_direction", "")).lower():
        limits.append("outcome direction not clearly stated")
    if _missing(evidence.get("dose_route")):
        limits.append("dose/route not reported")
    if _missing(evidence.get("duration")):
        limits.append("study duration not reported")
    if role != "direct_intervention" and role not in {"biomarker_readout", "pathway_component"}:
        limits.append(f"molecule role is '{role or 'unclear'}', not a direct treatment")
    src = str(evidence.get("initial_extraction_source", "") or evidence.get("abstract_extraction_source", ""))
    if "pmc" not in src.lower():
        limits.append("extracted from title/abstract only (no full text)")
    if "Meta-analysis" not in primary and "Systematic review" not in primary and primary != "RCT":
        limits.append("single study; not corroborated here")

    # --- one-line synopsis ---
    what = _val(evidence, "what_it_is")
    outcome = _val(evidence, "outcome_direction").replace("_", " ")
    endpoint = _first_tag(evidence.get("endpoint_tags"))
    condition = _first_tag(evidence.get("condition_tags"))
    mol = str(evidence.get("molecule_name", "") or evidence.get("molecule_id", "the molecule"))
    bits = []
    if condition or endpoint:
        target = " / ".join(x for x in [condition, endpoint] if x)
        bits.append(f"{mol} studied for {target}")
    else:
        bits.append(f"{mol} evidence record")
    if outcome:
        bits.append(f"reported outcome: {outcome}")
    if tier:
        bits.append(f"reliability tier: {tier}")
    summary = "; ".join(bits)

    # confidence in the appraisal itself
    char_conf = str(evidence.get("paper_characterization_confidence", "") or "").lower()
    if primary in {"RCT", "Meta-analysis", "Systematic review"} and char_conf != "low":
        confidence = "high"
    elif char_conf == "low" or model in {"", "unclear"}:
        confidence = "low"
    else:
        confidence = "medium"

    provenance = _PROVENANCE

    return Appraisal(
        appraisal_strengths="; ".join(strengths) or "none clearly identified",
        appraisal_limitations="; ".join(limits) or "none clearly identified",
        appraisal_summary=summary,
        appraisal_confidence=confidence,
        summary_provenance=provenance,
    )


def _first_tag(raw) -> str:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        import re

        items = re.split(r"[;,]", str(raw or ""))
    for item in items:
        t = item.strip()
        if t and t.lower() not in _MISSING:
            return t.replace("_", " ")
    return ""
