"""Two-axis, section-appropriate evidence reliability.

The first scoring pass (``strength.py``) used a single clinical-evidence
hierarchy (RCT = 40 design points, in-vitro = 8). That is a fine *directness*
signal but a poor *quality* signal: it structurally rates all preclinical and
mechanistic work as weak, which then caused ~60% of the database (mostly
in-vitro mechanism and methods papers) to be hidden. For a peptide research
database that is exactly the content researchers want.

This module separates the two things that were conflated:

1. **Study quality** (``reliability_score`` / ``reliability_tier``) — internal
   validity scored *within the paper's evidence class*, so a rigorous in-vitro
   study can score "high for its type" and a sloppy RCT can score low. Rubrics
   are informed by GRADE (clinical), SYRCLE/ARRIVE (animal), and standard
   in-vitro rigor criteria (controls, orthogonal validation, dose-response,
   replication, physiological relevance).

2. **Evidence directness** (``evidence_directness`` / ``directness_tier``) —
   how directly the result speaks to a human therapeutic question, across
   classes: human RCT/synthesis high → human observational → animal in-vivo →
   in-vitro → assay/methods low. This is shown *alongside* quality so a
   high-quality mechanism paper is never mistaken for clinical proof.

Decoupling these means reliability stops being a hide/exclude gate. Everything
on-topic is scored and can be listed and filtered; only genuinely off-topic
records (environmental/materials chemistry) get zeroed as ``off_topic``.

Every score ships with a component breakdown (``reliability_components``) and a
one-line ``reliability_rationale`` so it is fully auditable. Rule-based, offline,
no LLM, no network.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

_MISSING = {"", "na", "n/a", "none", "null", "nan", "not reported", "not clearly reported", "unclear", "unknown", "nr"}

# Roles that are off-topic for a peptide *therapeutic/biomedical* evidence base.
_OFF_TOPIC_ROLES = {"environmental_or_material_use"}

# Cross-class translational directness (0-100) by resolved evidence class.
CLASS_DIRECTNESS = {
    "human_clinical_controlled": 95,
    "clinical_guideline": 92,
    "evidence_synthesis": 90,
    "human_clinical": 80,
    "human_observational": 66,
    "narrative_review": 42,
    "preclinical_invivo": 45,
    "in_vitro": 25,
    "methods_tool": 16,
    "other": 22,
    "off_topic": 0,
}

CLASS_LABELS = {
    "human_clinical_controlled": "Human — controlled trial",
    "clinical_guideline": "Clinical practice guideline",
    "evidence_synthesis": "Evidence synthesis",
    "human_clinical": "Human — interventional",
    "human_observational": "Human — observational",
    "narrative_review": "Narrative review",
    "preclinical_invivo": "Preclinical (in vivo)",
    "in_vitro": "In vitro / molecular",
    "methods_tool": "Methods / assay / tool",
    "other": "Other / unclear",
    "off_topic": "Off-topic (non-biomedical)",
}


@dataclass
class Reliability:
    evidence_class: str
    evidence_class_label: str
    reliability_score: int          # study quality within class (headline)
    reliability_tier: str
    evidence_directness: int        # cross-class translational directness
    directness_tier: str
    reliability_components: str     # JSON of quality sub-scores
    reliability_rationale: str
    quality_components: Dict[str, int] = field(default_factory=dict)
    # Evidence-hierarchy axis (the pyramid): the PRIMARY feed ordering. rank 1 = top.
    evidence_level_key: str = "other"
    evidence_level_rank: int = 99
    evidence_level_label: str = "Other / unclear"
    evidence_level_short: str = "Other"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("quality_components", None)
        return d


def _blob(evidence: dict, paper: Optional[dict]) -> str:
    parts = [
        str((paper or {}).get("title", "") or evidence.get("title", "") or ""),
        str((paper or {}).get("abstract", "") or evidence.get("abstract", "") or ""),
        str(evidence.get("study_design_tags", "") or ""),
        str(evidence.get("efficacy_signal", "") or ""),
        str(evidence.get("safety_signal", "") or ""),
    ]
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def _has(text: str, *terms: str) -> bool:
    return any(t in text for t in terms)


# Negation / other-study attribution cues. If one of these sits just before a
# design-rigor term, the paper is saying it did NOT use that method (or is citing
# another study that did) -- so we must not credit it. Window is local to the term.
_NEGATORS = (
    "not ", "non-", "non ", "no ", "without", "lack", "absence of", "absent",
    "unlike", "rather than", "instead of", "as opposed to", "failed to",
    "was not", "were not", "wasn't", "weren't", "un-blinded", "unblinded",
    "open-label", "open label",
    # contrast / other-study attribution: crediting a method the study only cites.
    "in contrast to", "contrast to", "compared with", "compared to", "versus",
    "relative to",
)

# Letters that, immediately before a term, mean it's part of a bigger word (e.g.
# "blind" inside "unblinded", "randomi" inside "nonrandomized") -> not a match.
_WORDCHAR = set("abcdefghijklmnopqrstuvwxyz0123456789")


def _has_method(text: str, *terms: str) -> bool:
    """Like ``_has`` but ignores a locally negated / attributed occurrence.

    A design term (randomized / double-blind / control) is NOT credited when it is
    (a) part of a larger word -- "unblinded"/"nonrandomized" must not credit
    blinding/randomization -- or (b) preceded within a short window by a negation
    or contrast cue ("an open-label study, unlike double-blind trials", "in
    contrast to randomized trials", "not randomized"). Any single clean occurrence
    still earns the credit.
    """
    for t in terms:
        start = 0
        while True:
            i = text.find(t, start)
            if i == -1:
                break
            prev = text[i - 1] if i > 0 else ""
            if prev not in _WORDCHAR and not any(neg in text[max(0, i - 30):i] for neg in _NEGATORS):
                return True
            start = i + len(t)
    return False


def _methods_span(text: str) -> str:
    """Methods/design section of a structured abstract, or '' if none is marked.

    Restricting design-rigor keyword matching to this span stops background/title
    mentions ("in contrast to randomized trials, we ...") from leaking rigor credit.
    """
    m = re.search(r"\b(?:methods?|design|study design|patients and methods|materials and methods)\b\s*:?", text)
    if not m:
        return ""
    start = m.end()
    r = re.search(r"\b(?:results?|findings?|conclusions?|interpretation|discussion)\b\s*:?", text[start:])
    end = start + r.start() if r else len(text)
    return text[start:end]


def _count_present(text: str, groups: Tuple[Tuple[str, ...], ...]) -> int:
    return sum(1 for g in groups if _has(text, *g))


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _sample_n(evidence: dict) -> Optional[int]:
    for key in ("refined_n", "sample_size", "abstract_sample_size"):
        v = evidence.get(key)
        if v in (None, ""):
            continue
        m = re.search(r"\d[\d,]*", str(v))
        if m:
            return int(m.group(0).replace(",", ""))
    return None


def _icite_float(evidence: dict, key: str) -> Optional[float]:
    """Parse a numeric iCite field, returning None when absent/blank/unparseable."""
    v = evidence.get(key)
    if v in (None, ""):
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _truthy(value) -> bool:
    """Loose truthiness for iCite flag fields that arrive as "Yes"/"No", 1/0,
    "1"/"0", or booleans. Blank / None / unparseable -> False.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in {"yes", "y", "true", "t"}:
        return True
    if s in {"no", "n", "false", "f", ""}:
        return False
    try:
        return float(s) != 0.0
    except ValueError:
        return False


# Classes whose directness may receive a small iCite-APT nudge. All are
# non-human, non-synthesis; their base directness is low enough that the capped
# +8 can never lift a preclinical/in-vitro record above genuine human evidence.
_APT_ADJUSTABLE = {"preclinical_invivo", "in_vitro", "methods_tool", "other"}


def _apt_adjust(directness: int, cls: str, evidence: dict) -> int:
    """Tiny, bounded nudge to translational directness from NIH iCite's
    Approximate Potential to Translate (APT, 0-1). Applied ONLY to the
    ``_APT_ADJUSTABLE`` classes so it re-orders records *within* a class without
    ever leapfrogging one above human/synthesis evidence. Absent APT -> unchanged.
    """
    if cls not in _APT_ADJUSTABLE:
        return directness
    apt = _icite_float(evidence, "icite_apt")
    if apt is None:
        return directness
    adj = 0
    if apt >= 0.5:
        adj = round(8 * min(1.0, (apt - 0.5) / 0.5))
    elif apt <= 0.2:
        adj = -round(4 * min(1.0, (0.2 - apt) / 0.2))
    return max(0, min(100, directness + adj))


def _icite_model(evidence: dict) -> str:
    """Dominant translational compartment from NIH iCite's triangle fractions
    (human / animal / molecular_cellular), or "" if unknown/ambiguous. Only used
    to break ties when our keyword model is silent, never to override a clear
    keyword-derived study design.
    """
    def _f(key: str):
        v = evidence.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    vals = {"human": _f("icite_human"), "animal": _f("icite_animal"), "in_vitro": _f("icite_molecular")}
    known = {k: v for k, v in vals.items() if v is not None}
    if not known:
        return ""
    top = max(known, key=lambda k: known[k])
    return top if known[top] is not None and known[top] >= 0.5 else ""


# Clinical practice guidelines are authoritative, synthesized recommendations, not
# primary studies -- our study-conduct rubric doesn't apply, and grading their quality
# is a separate formal instrument (AGREE II) whose signals we don't have. So we detect
# them by PubMed's publication type (the gold-standard, auditable trigger) and treat
# them as a distinct class: high directness, rigor NOT graded (tier not_applicable).
_GUIDELINE_PUBTYPES = {"guideline", "practice guideline"}
# Conservative title fallback for guidelines that aren't yet MeSH pubtype-indexed.
# Requires an explicit guideline/consensus phrase and excludes papers merely ABOUT
# guidelines (adherence/implementation/barriers) to avoid false positives.
_GUIDELINE_TITLE_RE = re.compile(
    r"\b(?:clinical practice guideline|practice guideline|consensus statement|"
    r"consensus recommendations?|consensus guideline)\b", re.IGNORECASE)
_GUIDELINE_TITLE_NEG = re.compile(
    r"\b(?:adherence|adherent|implementation|implementing|barriers?|awareness|"
    r"uptake|compliance|knowledge of|attitudes?)\b", re.IGNORECASE)


def _pubtypes_of(evidence: dict, paper: Optional[dict]) -> set:
    """Lowercased set of PubMed publication types, tolerant of list or ;/,-joined string."""
    raw = (paper or {}).get("pubtypes") if (paper and (paper or {}).get("pubtypes")) else evidence.get("pubtypes", "")
    if not raw:
        return set()
    items = [str(x) for x in raw] if isinstance(raw, (list, tuple)) else str(raw).replace(",", ";").split(";")
    return {t.strip().lower() for t in items if t.strip()}


def is_guideline(evidence: dict, paper: Optional[dict] = None) -> bool:
    if _GUIDELINE_PUBTYPES & _pubtypes_of(evidence, paper):
        return True
    title = str((paper or {}).get("title", "") or evidence.get("title", "") or "")
    return bool(_GUIDELINE_TITLE_RE.search(title) and not _GUIDELINE_TITLE_NEG.search(title))


# --- evidence hierarchy (the pyramid) ---------------------------------------
# The design-strength ladder, loaded from an editable config so placements can be
# retuned without code changes. rank 1 = strongest (top of the pyramid). This is a
# SEPARATE axis from directness (human-relevance) and rigor (within-class quality):
# it is the PRIMARY ordering of the feed; the other scores only order WITHIN a level.
def _load_hierarchy() -> "OrderedDict":
    from collections import OrderedDict
    import csv as _csv
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "config", "evidence_hierarchy.csv")
    out = OrderedDict()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                key = (row.get("key") or "").strip()
                if not key:
                    continue
                try:
                    lvl = int(row.get("level") or 0)
                except ValueError:
                    lvl = len(out) + 1
                out[key] = {"rank": lvl, "label": (row.get("label") or key).strip(),
                            "short": (row.get("short") or row.get("label") or key).strip()}
    except OSError:
        pass
    return out


EVIDENCE_HIERARCHY = _load_hierarchy()
_HIER_FALLBACK = {"rank": 99, "label": "Other / unclear", "short": "Other"}


def hierarchy_of(key: str) -> dict:
    return EVIDENCE_HIERARCHY.get(key, _HIER_FALLBACK)


def evidence_level(evidence: dict, paper: Optional[dict] = None, cls: Optional[str] = None) -> str:
    """Rule-based place on the evidence pyramid -> a config key. PubMed publication
    types are the primary, auditable signal; evidence_class + title cues fill gaps."""
    pt = _pubtypes_of(evidence, paper)
    title = str((paper or {}).get("title", "") or evidence.get("title", "") or "").lower()
    if cls is None:
        cls = classify_evidence(evidence)
    if is_guideline(evidence, paper):
        return "clinical_practice_guideline"
    # Synthesis: systematic review outranks meta-analysis (per house ordering); a paper
    # tagged both counts as a systematic review.
    if "systematic review" in pt or "systematic review" in title:
        return "systematic_review"
    if "meta-analysis" in pt or "meta-analysis" in title or "meta analysis" in title:
        return "meta_analysis"
    if cls == "evidence_synthesis":
        return "systematic_review"
    if "randomized controlled trial" in pt or cls == "human_clinical_controlled":
        return "rct"
    if "controlled clinical trial" in pt or "clinical trial" in pt or cls == "human_clinical":
        return "nonrandomized_trial"
    if "case reports" in pt or "case report" in title:
        return "case_report"
    if "case series" in title:
        return "case_series"
    if cls == "human_observational" or "observational study" in pt or "comparative study" in pt:
        if "cohort" in title:
            return "cohort"
        if "case-control" in title or "case control" in title:
            return "case_control"
        if "cross-sectional" in title or "cross sectional" in title:
            return "observational_other"
        return "observational_other"
    if cls == "narrative_review" or ("review" in pt and cls not in ("evidence_synthesis",)):
        return "narrative_review"
    if cls == "preclinical_invivo":
        return "preclinical_invivo"
    if cls == "in_vitro":
        return "in_vitro"
    return "other"


def classify_evidence(evidence: dict) -> str:
    role = str(evidence.get("role_category", "") or "")
    model = str(evidence.get("model_type", "") or "").lower()
    # prefer disambiguated model when available (from extractors.py)
    model_primary = str(evidence.get("model_primary", "") or "").lower()
    primary = str(evidence.get("primary_study_type", "") or "")

    if role in _OFF_TOPIC_ROLES:
        return "off_topic"
    if "Meta-analysis" in primary or "Systematic review" in primary:
        return "evidence_synthesis"
    if primary == "RCT":
        return "human_clinical_controlled"
    if primary == "Human interventional non-RCT" or primary == "Clinical trial / unclear population":
        return "human_clinical"
    if primary == "Human observational":
        return "human_observational"
    if role in {"assay_or_detection", "synthesis_or_production", "clinical_tool_or_diagnostic"}:
        return "methods_tool"
    # Coarse translational compartment (human / animal / in-vitro). PREFER iCite's
    # NLM/MeSH-curated triangle when it's present and confident -- it's more reliable
    # than our title/abstract keyword model. Fall back to the keyword-derived model
    # for the recent / not-yet-indexed papers iCite can't see (its fields are blank
    # until PubMed assigns MeSH). The precise study designs above (RCT, synthesis,
    # human observational) already returned, so this never downgrades a known design.
    resolved = _icite_model(evidence) or model_primary or model
    if resolved == "human":
        return "human_observational"  # human context without a clearer clinical design
    if resolved == "animal":
        return "preclinical_invivo"
    if resolved in ("in vitro", "in_vitro"):
        return "in_vitro"
    if resolved == "review" or "Review" in primary:
        return "narrative_review"
    # iCite rescue: an article iCite flags as clinical is human interventional
    # evidence -- only reached when nothing else resolved, so it just upgrades an
    # otherwise-"other" record.
    if _truthy(evidence.get("icite_is_clinical")):
        return "human_clinical"
    return "other"


# --- per-class study-quality rubrics ---------------------------------------


def _score_human(evidence: dict, cls: str, text: str) -> Tuple[int, Dict[str, int]]:
    c: Dict[str, int] = {}
    if cls == "human_clinical_controlled":
        c["design"] = 60
    elif cls == "human_clinical":
        c["design"] = 45
    else:  # observational
        c["design"] = 34
    comp = str(evidence.get("comparator_or_control", "") or "").lower()
    if "placebo" in comp or _has(text, "placebo"):
        c["comparator"] = 12
    elif _has(text, "active comparator", "standard of care", "usual care") or comp not in _MISSING:
        c["comparator"] = 8
    else:
        c["comparator"] = 0
    # Design-rigor credits are matched over the methods span (when present) and are
    # negation-aware, so a study is not credited for blinding/randomization it
    # explicitly lacks or merely cites in other trials.
    dt = _methods_span(text) or text
    if _has_method(dt, "double-blind", "double blind"):
        c["blinding"] = 8
    elif _has_method(dt, "single-blind", "single blind", "blinded"):
        c["blinding"] = 4
    else:
        c["blinding"] = 0
    if cls != "human_clinical_controlled" and _has_method(dt, "randomi"):
        c["randomization"] = 6
    n = _sample_n(evidence)
    c["sample_size"] = _n_points(n, (1000, 300, 100, 30), (14, 11, 8, 4, 1))
    if _has(text, "multicenter", "multi-centre", "prospective", "pre-registered", "registered trial"):
        c["rigor_extras"] = 4
    return _sum_cap(c), c


def _score_synthesis(evidence: dict, text: str) -> Tuple[int, Dict[str, int]]:
    c: Dict[str, int] = {"design": 60}
    if _has(text, "systematic", "prisma", "predefined search", "prospero"):
        c["systematic_search"] = 12
    if _has(text, "meta-analysis", "pooled", "random-effects", "forest plot"):
        c["quantitative_pooling"] = 8
    m = re.search(r"(\d{1,4})\s+(?:studies|trials|articles|records|rcts)", text)
    if m:
        k = int(m.group(1))
        c["evidence_base"] = 12 if k >= 10 else 6 if k >= 3 else 3
    if _has(text, "grade", "risk of bias", "cochrane", "quality assessment"):
        c["appraisal"] = 6
    if _has(text, "low heterogeneity", "i2 = 0", "consistent"):
        c["consistency"] = 4
    return _sum_cap(c), c


def _score_invivo(evidence: dict, text: str) -> Tuple[int, Dict[str, int]]:
    c: Dict[str, int] = {"design": 45}
    dt = _methods_span(text) or text
    if _has_method(dt, "randomi"):
        c["randomization"] = 8
    if _has_method(dt, "blind", "masked"):
        c["blinding"] = 8
    if _has_method(dt, "vehicle", "sham", "littermate", "control group", "untreated control"):
        c["controls"] = 8
    n = _sample_n(evidence)
    c["sample_size"] = _n_points(n, (40, 16, 8, 4), (8, 6, 4, 2, 1))
    if _has(text, "dose-dependent", "dose dependent", "dose-response", "dose response", "multiple doses"):
        c["dose_response"] = 10
    if _has(text, "time course", "timepoints", "time points", "longitudinal"):
        c["timecourse"] = 4
    if _has(text, "independent experiments", "replicat", "reproduc", "two cohorts", "confirmed in"):
        c["replication"] = 8
    if _has(text, "survival", "behavior", "behaviour", "histolog", "function", "phenotype"):
        c["invivo_outcome"] = 5
    return _sum_cap(c), c


def _score_invitro(evidence: dict, text: str) -> Tuple[int, Dict[str, int]]:
    c: Dict[str, int] = {"design": 40}
    dt = _methods_span(text) or text
    if _has_method(dt, "vehicle", "negative control", "untreated control", "scramble", "mock", "isotype"):
        c["controls"] = 12
    methods = (
        ("western", "immunoblot"), ("qpcr", "rt-pcr", "quantitative pcr"), ("immunofluor", "immunostain", "immunohisto"),
        ("flow cytometry", "facs"), ("knockdown", "sirna", "shrna", "crispr", "knockout"), ("reporter", "luciferase"),
        ("mass spectrometry", "proteomic", "rna-seq", "sequencing"),
    )
    n_methods = _count_present(text, methods)
    c["orthogonal_methods"] = 14 if n_methods >= 3 else 8 if n_methods == 2 else 0
    if _has(text, "dose-dependent", "dose dependent", "dose-response", "concentration-dependent", "ic50", "ec50"):
        c["dose_response"] = 12
    if _has(text, "triplicate", "n = 3", "n=3", "independent experiments", "biological replicate", "replicat"):
        c["replication"] = 12
    if _has(text, "primary cells", "primary human", "patient-derived", "ipsc", "organoid", "ex vivo", "primary neurons"):
        c["physiological_relevance"] = 10
    return _sum_cap(c), c


def _score_methods(evidence: dict, text: str) -> Tuple[int, Dict[str, int]]:
    c: Dict[str, int] = {"design": 40}
    if _has(text, "sensitivity", "specificity", "limit of detection", "lod", "recovery", "accuracy", "precision", "linearity"):
        c["validation_metrics"] = 18
    if _has(text, "gold standard", "reference method", "compared with", "validated against", "cross-validated"):
        c["reference_comparison"] = 14
    if _has(text, "reproduc", "inter-assay", "intra-assay", "robust", "repeatability"):
        c["reproducibility"] = 10
    if _has(text, "novel", "first", "new method", "rapid", "high-throughput", "cost-effective"):
        c["novelty_utility"] = 8
    return _sum_cap(c), c


def _score_narrative(evidence: dict, text: str) -> Tuple[int, Dict[str, int]]:
    c: Dict[str, int] = {"design": 45}
    if _has(text, "comprehensive", "systematically", "critically", "state of the art", "up to date"):
        c["scope"] = 10
    return _sum_cap(c), c


def _n_points(n: Optional[int], thresholds: Tuple[int, ...], points: Tuple[int, ...]) -> int:
    if n is None:
        return 0
    for t, p in zip(thresholds, points):
        if n >= t:
            return p
    return points[-1]  # reported but below smallest threshold


def _sum_cap(components: Dict[str, int]) -> int:
    return min(sum(components.values()), 100)


def _tier(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 50:
        return "moderate"
    if score >= 30:
        return "limited"
    return "low"


def _directness_tier(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 55:
        return "moderate"
    if score >= 35:
        return "limited"
    if score <= 0:
        return "none"
    return "low"


def assess_reliability(evidence: dict, paper: Optional[dict] = None) -> Reliability:
    cls = classify_evidence(evidence)
    text = _blob(evidence, paper)
    # Evidence-hierarchy level (the pyramid). Computed once and attached to every path.
    _lk = evidence_level(evidence, paper, cls)
    _lh = hierarchy_of(_lk)
    _lvl = dict(evidence_level_key=_lk, evidence_level_rank=_lh["rank"],
                evidence_level_label=_lh["label"], evidence_level_short=_lh["short"])

    if cls == "off_topic":
        return Reliability(
            evidence_class=cls,
            evidence_class_label=CLASS_LABELS[cls],
            reliability_score=0,
            reliability_tier="not_applicable",
            evidence_directness=0,
            directness_tier="none",
            reliability_components=json.dumps({}),
            reliability_rationale="Off-topic (non-biomedical) record; not scored as therapeutic evidence.",
            quality_components={},
            **_lvl,
        )

    # Clinical practice guidelines: authoritative synthesized recommendations. We do
    # NOT grade rigor (no study-conduct to score; guideline quality is a separate
    # instrument we lack the inputs for), so rigor is not_applicable. Directness is
    # high (they are about human clinical care); ranking gives them a high proxy so
    # they still surface near the top (see ranking.compute_rank).
    if is_guideline(evidence, paper):
        gdir = CLASS_DIRECTNESS["clinical_guideline"]
        return Reliability(
            evidence_class="clinical_guideline",
            evidence_class_label=CLASS_LABELS["clinical_guideline"],
            reliability_score=0,
            reliability_tier="not_applicable",
            evidence_directness=gdir,
            directness_tier=_directness_tier(gdir),
            reliability_components=json.dumps({}),
            reliability_rationale=(
                "Clinical practice guideline (PubMed publication type): authoritative "
                "synthesized recommendations. Rigor is not graded on study-conduct "
                "criteria; treated as a high-directness, authoritative source."),
            quality_components={},
            **_lvl,
        )

    if cls in ("human_clinical_controlled", "human_clinical", "human_observational"):
        score, comps = _score_human(evidence, cls, text)
    elif cls == "evidence_synthesis":
        score, comps = _score_synthesis(evidence, text)
    elif cls == "preclinical_invivo":
        score, comps = _score_invivo(evidence, text)
    elif cls == "in_vitro":
        score, comps = _score_invitro(evidence, text)
    elif cls == "methods_tool":
        score, comps = _score_methods(evidence, text)
    elif cls == "narrative_review":
        score, comps = _score_narrative(evidence, text)
    else:
        score, comps = 30, {"design": 30}

    directness = CLASS_DIRECTNESS.get(cls, 20)
    directness = _apt_adjust(directness, cls, evidence)  # no-op when APT absent
    rationale = _rationale(cls, comps, directness)
    return Reliability(
        evidence_class=cls,
        evidence_class_label=CLASS_LABELS.get(cls, cls),
        reliability_score=score,
        reliability_tier=_tier(score),
        evidence_directness=directness,
        directness_tier=_directness_tier(directness),
        reliability_components=json.dumps(comps),
        reliability_rationale=rationale,
        quality_components=comps,
        **_lvl,
    )


def _rationale(cls: str, comps: Dict[str, int], directness: int) -> str:
    top = sorted(comps.items(), key=lambda kv: kv[1], reverse=True)
    drivers = ", ".join(f"{k} +{v}" for k, v in top if v > 0) or "minimal quality signals detected"
    return f"{CLASS_LABELS.get(cls, cls)} (directness {directness}/100); quality drivers: {drivers}"


# Fields this module contributes to the curated schema.
RELIABILITY_FIELDS = [
    "evidence_class",
    "evidence_class_label",
    "reliability_score",
    "reliability_tier",
    "evidence_directness",
    "directness_tier",
    "reliability_components",
    "reliability_rationale",
    "evidence_level_key",
    "evidence_level_rank",
    "evidence_level_label",
    "evidence_level_short",
]
