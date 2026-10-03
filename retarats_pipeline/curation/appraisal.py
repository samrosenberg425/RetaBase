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
        "inputs": ["primary_study_type", "model_type", "role_category", "comparator", "sample_size", "refined_n",
                   "outcome_direction", "abstract", "fulltext_methods", "fulltext_results"],
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


def _has_text(evidence: dict, paper, *keys: str) -> bool:
    """True when any of ``keys`` holds non-blank text on the paper or the evidence row."""
    for k in keys:
        for src in (paper or {}, evidence):
            if str(src.get(k, "") or "").strip():
                return True
    return False


def _text_source(evidence: dict, paper) -> tuple:
    """(has_abstract, has_full_text, label) describing what text the extractors could read.

    Full text means the open-access Methods/Results the extractors actually use
    (``fulltext_methods`` / ``fulltext_results``); the old "pmc" substring test on an
    extraction-source field never matched, so every record claimed to have no full text.
    """
    has_abs = _has_text(evidence, paper, "abstract")
    has_ft = _has_text(evidence, paper, "fulltext_methods", "fulltext_results")
    if has_ft:
        label = "abstract and open-access full text" if has_abs else "title and open-access full text"
    elif has_abs:
        label = "title and abstract"
    else:
        label = "title only"
    return has_abs, has_ft, label


def appraise_evidence(evidence: dict, paper: dict | None = None) -> Appraisal:
    """Rule-based strengths / limitations / synopsis.

    Missing information is reported as "not found in the available text" (or as "source
    unavailable" when there is no abstract or full text to read). It is never presented as
    a finding that the study lacked the feature.
    """
    primary = str(evidence.get("primary_study_type", "") or "")
    model = str(evidence.get("model_type", "") or "").lower()
    role = str(evidence.get("role_category", "") or "")
    has_abs, has_ft, src_label = _text_source(evidence, paper)
    source_unavailable = not has_abs and not has_ft

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
    # Same sources, same order as the rigor rubric's sample-size credit (refined_n first),
    # so this text can never contradict a parsed N shown elsewhere on the record.
    n = ""
    for _k in ("refined_sample_size", "refined_n", "sample_size", "abstract_sample_size"):
        _v = _val(evidence, _k)
        if _v and any(ch.isdigit() for ch in _v):
            n = _v
            break
    if n:
        strengths.append(f"sample size reported ({n[:30]})")
    safety = _val(evidence, "safety_signal", "abstract_safety_signal")
    if safety:
        strengths.append("safety/tolerability discussed")

    # --- limitations ---
    # Items 1..n are things the study design implies; the "not found" items only say what the
    # automated reader did not find in the text it had (and are skipped, replaced by a single
    # source-unavailable note, when there was no abstract or full text to read).
    def not_found(what: str) -> None:
        if not source_unavailable:
            limits.append(f"{what} not found in the {src_label}")

    if model == "animal":
        limits.append("preclinical (animal) evidence; may not translate to humans")
    elif model == "in vitro":
        limits.append("in vitro / cell-level evidence only")
    elif model in {"", "unclear"} and not source_unavailable:
        limits.append(f"study model unclear from the {src_label}")
    if source_unavailable:
        limits.append("no abstract or open-access full text available: design details could not be assessed")
    if not comparator:
        not_found("comparator/control")
    if not n:
        not_found("sample size")
    if _missing(evidence.get("outcome_direction")) or "unclear" in str(evidence.get("outcome_direction", "")).lower():
        if not source_unavailable:
            limits.append(f"outcome direction not clearly stated in the {src_label}")
    if _missing(evidence.get("dose_route")):
        not_found("dose/route")
    if _missing(evidence.get("duration")):
        not_found("study duration")
    if role != "direct_intervention" and role not in {"biomarker_readout", "pathway_component"}:
        limits.append(f"molecule role is '{role or 'unclear'}', not a direct treatment")
    if has_abs and not has_ft:
        limits.append("analysed from the title and abstract only; no open-access full text was available")
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
