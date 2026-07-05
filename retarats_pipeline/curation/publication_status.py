"""Publication-status decision layer.

Replaces the single ``public_candidate`` flag with an auditable, tunable set of
fields that decide whether/where a record shows on the public site:

    publication_status        auto_published | review_candidate |
                              held_low_evidence | held_missing_fields | held_out_of_scope
    website_section           target public section (from PUBLICATION_RULES.csv)
    auto_publish_eligible     bool  (moderate policy: strong + complete records only)
    review_reason             why a record is queued rather than auto-published
    publish_rule_id           which rule matched (audit trail)
    display_priority          ordering weight within a section (higher = first)
    required_fields_present   bool
    missing_required_fields   semicolon list

Policy = "moderate": strong human interventional evidence, strong reviews, and
strong *complete* preclinical intervention records auto-publish. Everything else
accumulates in review queues rather than blocking. Thresholds live in
``config/PUBLICATION_RULES.csv`` so the aggressiveness is tunable without code
changes (the previous layer was too conservative).
"""

from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

_DEFAULT_RULES = os.path.join("config", "PUBLICATION_RULES.csv")
_DEFAULT_REQUIRED = os.path.join("config", "REQUIRED_FIELDS.csv")

_MISSING = {"", "not reported", "not clearly reported", "unclear", "na", "n/a", "none", "unknown", "nan"}

# For *required-field presence* we use a narrower "blank" test: a value like
# "unclear" or "review" is a legitimate populated classification, not an absence.
_BLANK_FOR_REQUIRED = {"", "na", "n/a", "none", "null", "nan", "not reported", "not clearly reported"}

PUBLICATION_FIELDS = [
    "publication_status",
    "website_section",
    "auto_publish_eligible",
    "review_reason",
    "publish_rule_id",
    "display_priority",
    "required_fields_present",
    "missing_required_fields",
]


@dataclass
class PublicationRule:
    rule_id: str
    website_section: str
    match_lanes: Tuple[str, ...]
    match_roles: Tuple[str, ...]
    match_model: Tuple[str, ...]
    min_score_auto: int
    min_score_candidate: int
    display_priority_base: int
    notes: str = ""


@dataclass
class RequiredField:
    field_id: str
    evidence_field: str
    requirement: str
    applies_to: str
    display_label: str


@dataclass
class PublicationDecision:
    publication_status: str
    website_section: str
    auto_publish_eligible: bool
    review_reason: str
    publish_rule_id: str
    display_priority: int
    required_fields_present: bool
    missing_required_fields: str

    def to_dict(self) -> dict:
        return asdict(self)


def _split(value: str) -> Tuple[str, ...]:
    value = (value or "").strip()
    if not value or value == "*":
        return ("*",)
    return tuple(p.strip() for p in value.split("|") if p.strip())


@lru_cache(maxsize=4)
def load_publication_rules(path: str = _DEFAULT_RULES) -> Tuple[PublicationRule, ...]:
    rules: List[PublicationRule] = []
    if not os.path.exists(path):
        return tuple(rules)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rid = (row.get("rule_id") or "").strip()
            if not rid:
                continue
            rules.append(
                PublicationRule(
                    rule_id=rid,
                    website_section=(row.get("website_section") or "").strip(),
                    match_lanes=_split(row.get("match_lanes", "")),
                    match_roles=_split(row.get("match_roles", "")),
                    match_model=_split(row.get("match_model", "")),
                    min_score_auto=_int(row.get("min_score_auto"), 999),
                    min_score_candidate=_int(row.get("min_score_candidate"), 999),
                    display_priority_base=_int(row.get("display_priority_base"), 0),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return tuple(rules)


@lru_cache(maxsize=4)
def load_required_fields(path: str = _DEFAULT_REQUIRED) -> Tuple[RequiredField, ...]:
    out: List[RequiredField] = []
    if not os.path.exists(path):
        return tuple(out)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            fid = (row.get("field_id") or "").strip()
            if not fid:
                continue
            out.append(
                RequiredField(
                    field_id=fid,
                    evidence_field=(row.get("evidence_field") or "").strip(),
                    requirement=(row.get("requirement") or "optional").strip().lower(),
                    applies_to=(row.get("applies_to") or "*").strip(),
                    display_label=(row.get("display_label") or fid).strip(),
                )
            )
    return tuple(out)


def _int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _is_missing(value) -> bool:
    return str(value or "").strip().lower() in _MISSING


def _blank_for_required(value) -> bool:
    return str(value or "").strip().lower() in _BLANK_FOR_REQUIRED


def _matches(rule: PublicationRule, lane: str, role: str, model: str) -> bool:
    def ok(field: str, allowed: Tuple[str, ...]) -> bool:
        return "*" in allowed or field in allowed

    return ok(lane, rule.match_lanes) and ok(role, rule.match_roles) and ok(model, rule.match_model)


def check_required_fields(
    evidence: dict, required: Sequence[RequiredField] | None = None
) -> Tuple[bool, List[str]]:
    """Return (all_required_present, missing_display_labels)."""
    if required is None:
        required = load_required_fields()
    missing: List[str] = []
    for rf in required:
        if rf.requirement != "required":
            continue
        val = evidence.get(rf.evidence_field)
        if rf.field_id == "identifier":
            # PMID or DOI satisfies the identifier requirement.
            if _blank_for_required(val) and _blank_for_required(evidence.get("doi")):
                missing.append(rf.display_label)
            continue
        if _blank_for_required(val):
            missing.append(rf.display_label)
    return (len(missing) == 0, missing)


# --- broad-inclusion policy -------------------------------------------------
#
# Reliability is a *label*, not a gate: everything on-topic is included and
# filterable. Only genuinely off-topic records are excluded. Statuses:
#   featured        included + spotlighted (high directness AND decent quality)
#   listed          included and browsable (the broad default)
#   review          included but flagged for a curator (missing required fields)
#   excluded_noise  off-topic / non-biomedical (the only hard exclusion)

# Public profile section, chosen from processing lane first (it distinguishes
# mechanism vs biomarker vs comparator), then evidence class as a fallback.
SECTION_BY_LANE = {
    "human_intervention": "Human evidence",
    "review_or_meta_analysis": "Reviews and overviews",
    "preclinical_intervention": "Preclinical evidence",
    "mechanism_or_pathway": "Mechanisms and pathways",
    "biomarker_or_readout": "Biomarkers and readouts",
    "comparator_or_background": "Comparator and background",
    "diagnostic_or_tool_use": "Methods, assays, and tools",
    "methods_assay_synthesis": "Methods, assays, and tools",
    "general_context": "Background and context",
    "unclear_manual_triage": "Background and context",
}
SECTION_BY_CLASS = {
    "human_clinical_controlled": "Human evidence",
    "human_clinical": "Human evidence",
    "human_observational": "Human evidence",
    "evidence_synthesis": "Reviews and overviews",
    "preclinical_invivo": "Preclinical evidence",
    "in_vitro": "Mechanisms and pathways",
    "methods_tool": "Methods, assays, and tools",
    "narrative_review": "Background and context",
    "other": "Background and context",
}
# Rough section ordering for display_priority (higher = shown earlier).
SECTION_PRIORITY = {
    "Human evidence": 95,
    "Reviews and overviews": 90,
    "Preclinical evidence": 80,
    "Mechanisms and pathways": 70,
    "Biomarkers and readouts": 68,
    "Comparator and background": 55,
    "Methods, assays, and tools": 45,
    "Background and context": 35,
}
_OFF_TOPIC_LANES = {"environmental_or_materials"}


def decide_publication(
    evidence: dict,
    rules: Sequence[PublicationRule] | None = None,
    required: Sequence[RequiredField] | None = None,
) -> PublicationDecision:
    lane = str(evidence.get("processing_lane", "") or "")
    ev_class = str(evidence.get("evidence_class", "") or "")
    quality = _int(evidence.get("reliability_score"), 0)
    directness_tier = str(evidence.get("directness_tier", "") or "")
    quality_tier = str(evidence.get("reliability_tier", "") or "")
    keep = str(evidence.get("keep_for_final_database", "True")).strip().lower() != "false"

    present, missing = check_required_fields(evidence, required)

    # 1) Off-topic is the only hard exclusion.
    if ev_class == "off_topic" or lane in _OFF_TOPIC_LANES or not keep:
        return PublicationDecision(
            publication_status="excluded_noise",
            website_section="",
            auto_publish_eligible=False,
            review_reason="off-topic / non-biomedical record excluded as noise",
            publish_rule_id="broad_v1:off_topic",
            display_priority=0,
            required_fields_present=present,
            missing_required_fields="; ".join(missing),
        )

    # 1b) Off-focus noise: opinion / infodemiology papers (public discourse about a
    #     bioactive, not its therapeutic use or mechanism). Flag set upstream from
    #     the title/abstract; excluded so the evidence base stays use/MoA-focused.
    off_focus = str(evidence.get("off_focus_reason", "") or "")
    if off_focus:
        return PublicationDecision(
            publication_status="excluded_noise",
            website_section="",
            auto_publish_eligible=False,
            review_reason=off_focus,
            publish_rule_id="broad_v1:off_focus",
            display_priority=0,
            required_fields_present=present,
            missing_required_fields="; ".join(missing),
        )

    section = SECTION_BY_LANE.get(lane) or SECTION_BY_CLASS.get(ev_class) or "Background and context"

    # 2) Missing required metadata -> included but flagged for a curator.
    if not present:
        status, auto, reason, rule_id = (
            "review",
            False,
            "included but needs review — missing: " + "; ".join(missing),
            "broad_v1:needs_fields",
        )
    # 3) Featured = translationally direct AND at least moderate quality, or a
    #    strong evidence synthesis. These are the spotlight records.
    elif (directness_tier == "high" and quality >= 50) or (ev_class == "evidence_synthesis" and quality >= 60):
        status, auto, reason, rule_id = ("featured", True, "", "broad_v1:featured")
    else:
        status, auto, reason, rule_id = ("listed", False, "", "broad_v1:listed")

    # Priority: section band dominates, then quality orders within a section.
    display_priority = SECTION_PRIORITY.get(section, 30) * 100 + min(quality, 99)

    return PublicationDecision(
        publication_status=status,
        website_section=section,
        auto_publish_eligible=auto,
        review_reason=reason,
        publish_rule_id=rule_id,
        display_priority=display_priority,
        required_fields_present=present,
        missing_required_fields="; ".join(missing),
    )


def _first_match(
    rules: Sequence[PublicationRule], lane: str, role: str, model: str
) -> Optional[PublicationRule]:
    for rule in rules:
        if _matches(rule, lane, role, model):
            return rule
    return None
