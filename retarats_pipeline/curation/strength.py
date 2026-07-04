"""Transparent evidence-strength / reliability scoring.

Produces a 0-100 composite ``reliability_score`` from named components so the
number is always explainable. Every record also gets:

* ``reliability_tier``          -> high | moderate | limited | low | non_efficacy
* ``reliability_components``     -> JSON breakdown of each component's points
* ``reliability_rationale``      -> short human-readable justification

This is deliberately conservative and rule-based. It scores *how much weight the
evidence can bear*, not whether the finding was positive. A large placebo-
controlled human RCT scores high; an uncontrolled in-vitro note scores low.

Weights (max points):
    study_design      40   design hierarchy (RCT/SR > human obs > animal > in vitro)
    directness        20   is the molecule a direct intervention, and in-model relevance
    sample_size       15   reported N
    comparator        10   placebo/active/vehicle control present
    completeness      10   key structured fields actually populated
    recency            5   publication recency
Non-efficacy roles (assay/synthesis/environmental) are scored 0 and tiered
``non_efficacy`` so they never compete with therapeutic evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List

MAX_POINTS = {
    "study_design": 40,
    "directness": 20,
    "sample_size": 15,
    "comparator": 10,
    "completeness": 10,
    "recency": 5,
}

NON_EFFICACY_ROLES = {"assay_or_detection", "synthesis_or_production", "environmental_or_material_use"}

_MISSING = {"", "not reported", "not clearly reported", "unclear", "na", "n/a", "none", "unknown"}


@dataclass
class ReliabilityResult:
    reliability_score: int
    reliability_tier: str
    reliability_components: str  # JSON
    reliability_rationale: str
    components: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "reliability_score": self.reliability_score,
            "reliability_tier": self.reliability_tier,
            "reliability_components": self.reliability_components,
            "reliability_rationale": self.reliability_rationale,
        }


def _is_missing(value) -> bool:
    return str(value or "").strip().lower() in _MISSING


def _study_design_points(primary: str, model: str, role: str) -> "tuple[int, str]":
    p = primary.strip()
    if p == "RCT":
        return 40, "randomized controlled trial"
    if "Meta-analysis" in p:
        return 40, "meta-analysis"
    if "Systematic review" in p:
        return 36, "systematic review"
    if p == "Human interventional non-RCT":
        return 30, "human interventional (non-RCT)"
    if p == "Human observational" or model == "human":
        return 22, "human observational / other human"
    if "Review" in p or model == "review":
        return 14, "narrative review / synthesis context"
    if model == "animal":
        return 16, "controlled animal study"
    if model == "in vitro":
        return 8, "in vitro / cell study"
    return 4, "study design unclear"


def _directness_points(role: str, model: str, relevance: str) -> "tuple[int, str]":
    pts = 0
    reasons = []
    if role == "direct_intervention" or relevance == "primary_intervention":
        pts += 12
        reasons.append("molecule is the direct intervention")
    elif role in {"biomarker_readout", "pathway_component"}:
        pts += 6
        reasons.append("molecule is a readout / mechanistic component")
    elif role == "comparator_or_background_drug":
        pts += 3
        reasons.append("molecule is a comparator / background agent")
    # model relevance adds translational directness
    if model == "human":
        pts += 8
        reasons.append("human model")
    elif model == "animal":
        pts += 4
        reasons.append("animal model")
    elif model == "in vitro":
        pts += 2
        reasons.append("in vitro model")
    return min(pts, MAX_POINTS["directness"]), "; ".join(reasons) or "role/model directness low"


def _sample_size_points(evidence: dict) -> "tuple[int, str]":
    raw = str(evidence.get("sample_size", "") or "")
    n = _extract_n(raw)
    if n is None:
        n = _extract_n(str(evidence.get("abstract_sample_size", "") or ""))
    if n is None:
        return 0, "sample size not reported"
    if n >= 1000:
        return 15, f"large sample (n≈{n})"
    if n >= 200:
        return 12, f"moderate-large sample (n≈{n})"
    if n >= 50:
        return 8, f"moderate sample (n≈{n})"
    if n >= 10:
        return 4, f"small sample (n≈{n})"
    return 2, f"very small sample (n≈{n})"


def _extract_n(raw: str):
    import re

    if _is_missing(raw):
        return None
    m = re.search(r"n\s*=?\s*(\d{1,7})", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,7})", raw)
    if m:
        return int(m.group(1))
    return None


def _comparator_points(evidence: dict) -> "tuple[int, str]":
    comp = str(evidence.get("comparator_or_control", "") or "").lower()
    if _is_missing(comp):
        comp = str(evidence.get("abstract_comparator_or_control", "") or "").lower()
    if _is_missing(comp):
        return 0, "no comparator/control reported"
    if "placebo" in comp:
        return 10, "placebo-controlled"
    if "active" in comp or "standard" in comp or "usual care" in comp:
        return 8, "active/standard comparator"
    if "vehicle" in comp:
        return 7, "vehicle control"
    if "control" in comp:
        return 6, "control group present"
    return 4, "comparator mentioned"


def _completeness_points(evidence: dict) -> "tuple[int, str]":
    key_fields = [
        "condition_tags",
        "endpoint_tags",
        "intervention_or_exposure",
        "outcome_direction",
        "dose_route",
        "duration",
    ]
    present = sum(0 if _is_missing(evidence.get(f)) else 1 for f in key_fields)
    pts = round(MAX_POINTS["completeness"] * present / len(key_fields))
    return pts, f"{present}/{len(key_fields)} key fields populated"


def _recency_points(evidence: dict) -> "tuple[int, str]":
    try:
        y = int(str(evidence.get("pub_year"))[:4])
    except (TypeError, ValueError):
        return 0, "year unknown"
    if y >= 2024:
        return 5, f"recent ({y})"
    if y >= 2020:
        return 4, f"{y}"
    if y >= 2015:
        return 2, f"{y}"
    return 1, f"older ({y})"


def _tier(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 50:
        return "moderate"
    if score >= 30:
        return "limited"
    return "low"


def score_reliability(evidence: dict) -> ReliabilityResult:
    role = str(evidence.get("role_category", "") or "")
    primary = str(evidence.get("primary_study_type", "") or "")
    model = str(evidence.get("model_type", "") or "").lower()
    relevance = str(evidence.get("molecule_relevance", "") or "")

    if role in NON_EFFICACY_ROLES:
        comps = {k: 0 for k in MAX_POINTS}
        return ReliabilityResult(
            reliability_score=0,
            reliability_tier="non_efficacy",
            reliability_components=json.dumps(comps),
            reliability_rationale="Methods/assay/synthesis/environmental record; not therapeutic evidence.",
            components=comps,
        )

    comps: Dict[str, int] = {}
    reasons: List[str] = []

    design_pts, design_reason = _study_design_points(primary, model, role)
    comps["study_design"] = design_pts
    reasons.append(design_reason)

    direct_pts, direct_reason = _directness_points(role, model, relevance)
    comps["directness"] = direct_pts
    reasons.append(direct_reason)

    for name, fn in (
        ("sample_size", _sample_size_points),
        ("comparator", _comparator_points),
        ("completeness", _completeness_points),
        ("recency", _recency_points),
    ):
        pts, reason = fn(evidence)
        comps[name] = pts
        reasons.append(reason)

    score = min(sum(comps.values()), 100)
    return ReliabilityResult(
        reliability_score=score,
        reliability_tier=_tier(score),
        reliability_components=json.dumps(comps),
        reliability_rationale="; ".join(reasons),
        components=comps,
    )
