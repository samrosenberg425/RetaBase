from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple


ROLE_FIELDS = [
    "molecule_role",
    "role_category",
    "role_confidence",
    "role_review_bucket",
    "role_evidence_text",
    "mechanism_tags",
    "therapeutic_area_tags",
    "function_tags",
    "role_rule_id",
    "evidence_strength_score",
    "evidence_strength_label",
    "public_candidate",
]


@dataclass
class RoleRule:
    rule_id: str
    molecule_id: str
    role_category: str
    include_terms: List[str]
    exclude_terms: List[str]
    mechanism_tags: List[str]
    therapeutic_area_tags: List[str]
    function_tags: List[str]
    priority: int
    notes: str = ""


@dataclass
class RoleClassification:
    molecule_role: str
    role_category: str
    role_confidence: str
    role_review_bucket: str
    role_evidence_text: str
    mechanism_tags: str
    therapeutic_area_tags: str
    function_tags: str
    role_rule_id: str
    evidence_strength_score: int
    evidence_strength_label: str
    public_candidate: bool

    def to_dict(self) -> dict:
        return asdict(self)


def load_role_rules(path: str) -> List[RoleRule]:
    rules: List[RoleRule] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=1):
            rules.append(
                RoleRule(
                    rule_id=f"role_rule_{index:04d}",
                    molecule_id=_clean(row.get("molecule_id", "*")).lower() or "*",
                    role_category=_clean(row.get("role_category", "")),
                    include_terms=_split_terms(row.get("include_terms", "")),
                    exclude_terms=_split_terms(row.get("exclude_terms", "")),
                    mechanism_tags=_split_tags(row.get("mechanism_tags", "")),
                    therapeutic_area_tags=_split_tags(row.get("therapeutic_area_tags", "")),
                    function_tags=_split_tags(row.get("function_tags", "")),
                    priority=int(float(row.get("priority") or 0)),
                    notes=_clean(row.get("notes", "")),
                )
            )
    return sorted(rules, key=lambda r: r.priority, reverse=True)


def classify_role(evidence: dict, paper: dict, molecule: dict, rules: List[RoleRule]) -> RoleClassification:
    title = str(paper.get("title", "") or "")
    abstract = str(paper.get("abstract", "") or "")
    context = _context(title, abstract)
    molecule_id = str(evidence.get("molecule_id", "") or molecule.get("molecule_id", "")).lower()

    candidates = [
        rule for rule in rules
        if rule.molecule_id in {"*", molecule_id}
    ]
    best: Optional[Tuple[RoleRule, int, str]] = None
    for rule in candidates:
        matched_term, score = _score_rule(rule, context, molecule)
        if score <= 0:
            continue
        if best is None or score > best[1] or (score == best[1] and rule.priority > best[0].priority):
            best = (rule, score, matched_term)

    if best is None:
        role = _fallback_role(evidence)
        return _build_classification(role, None, "", evidence, paper, context, molecule)

    rule, score, matched_term = best
    return _build_classification(rule.role_category, rule, matched_term, evidence, paper, context, molecule, score=score)


def classify_many(evidence_rows: Iterable[dict], paper_by_pmid: Dict[str, dict], molecule_by_id: Dict[str, dict], rules: List[RoleRule]) -> List[dict]:
    out = []
    for row in evidence_rows:
        row = dict(row)
        pmid = str(row.get("pmid", ""))
        molecule_id = str(row.get("molecule_id", ""))
        role = classify_role(row, paper_by_pmid.get(pmid, {}), molecule_by_id.get(molecule_id, {}), rules)
        row.update(role.to_dict())
        row["website_include"] = role.public_candidate
        row["review_status"] = _review_status_from_bucket(role.role_review_bucket)
        out.append(row)
    return out


def _score_rule(rule: RoleRule, context: dict, molecule: dict) -> Tuple[str, int]:
    if _matches_any(rule.exclude_terms, context, molecule):
        return "", 0
    best_term = ""
    best_score = 0
    for term in rule.include_terms:
        expanded = _expand_term(term, molecule)
        if not expanded:
            continue
        score = _term_score(expanded, context)
        if score > best_score:
            best_score = score
            best_term = expanded
    if best_score <= 0:
        return "", 0
    return best_term, best_score + rule.priority


def _build_classification(
    role_category: str,
    rule: Optional[RoleRule],
    matched_term: str,
    evidence: dict,
    paper: dict,
    context: dict,
    molecule: dict,
    score: int = 0,
) -> RoleClassification:
    strength_score, strength_label = evidence_strength(evidence, role_category)
    review_bucket = review_bucket_for(role_category, evidence, strength_score)
    confidence = role_confidence(role_category, evidence, score, rule is not None)
    evidence_text = _evidence_text(matched_term, context, paper)
    mechanism_tags = sorted(set((rule.mechanism_tags if rule else []) + inferred_mechanism_tags(context)))
    therapeutic_tags = sorted(set((rule.therapeutic_area_tags if rule else []) + inferred_therapeutic_area_tags(context)))
    function_tags = sorted(set((rule.function_tags if rule else []) + inferred_function_tags(role_category, evidence)))
    return RoleClassification(
        molecule_role=role_category,
        role_category=role_category,
        role_confidence=confidence,
        role_review_bucket=review_bucket,
        role_evidence_text=evidence_text,
        mechanism_tags="; ".join(mechanism_tags),
        therapeutic_area_tags="; ".join(therapeutic_tags),
        function_tags="; ".join(function_tags),
        role_rule_id=rule.rule_id if rule else "fallback",
        evidence_strength_score=strength_score,
        evidence_strength_label=strength_label,
        public_candidate=review_bucket == "public_candidate",
    )


def evidence_strength(evidence: dict, role_category: str) -> Tuple[int, str]:
    primary = str(evidence.get("primary_study_type", ""))
    model = str(evidence.get("model_type", ""))
    if role_category in {"environmental_or_material_use", "assay_or_detection", "synthesis_or_production"}:
        return 0, "non_efficacy_or_methods"
    if primary == "RCT":
        return 5, "human_rct"
    if "Meta-analysis" in primary or "Systematic review" in primary:
        return 5 if role_category == "direct_intervention" else 4, "systematic_review_or_meta_analysis"
    if primary == "Human interventional non-RCT":
        return 4, "human_interventional_non_rct"
    if primary == "Human observational" or model == "human":
        return 3, "human_observational_or_context"
    if model == "animal":
        return 2, "animal"
    if model == "in vitro":
        return 1, "in_vitro_or_cell"
    return 0, "unclear_or_background"


def review_bucket_for(role_category: str, evidence: dict, strength_score: int) -> str:
    if role_category in {"environmental_or_material_use", "assay_or_detection", "synthesis_or_production"}:
        return "exclude_noise"
    if role_category == "direct_intervention" and strength_score >= 2:
        return "public_candidate"
    if role_category in {"clinical_tool_or_diagnostic", "tool_compound_or_positive_control"}:
        return "curator_review"
    if role_category in {"biomarker_readout", "pathway_component", "comparator_or_background_drug", "endogenous_metabolite"}:
        return "background_only" if strength_score <= 1 else "curator_review"
    if role_category == "primary_topic_unclear_role":
        return "curator_review"
    return "curator_review"


def role_confidence(role_category: str, evidence: dict, score: int, matched_rule: bool) -> str:
    relevance_conf = str(evidence.get("relevance_confidence", "")).lower()
    if role_category in {"environmental_or_material_use", "assay_or_detection", "synthesis_or_production"} and matched_rule:
        return "high"
    if matched_rule and score >= 105:
        return "high"
    if matched_rule or relevance_conf == "high":
        return "medium"
    return "low"


def inferred_mechanism_tags(context: dict) -> List[str]:
    text = context["all"]
    tag_rules = {
        "redox": ["oxidative stress", "redox", "ROS", "antioxidant"],
        "mitochondrial": ["mitochondria", "mitochondrial", "OXPHOS"],
        "mTOR_autophagy": ["mTOR", "TORC1", "autophagy", "lysosomal", "mitophagy"],
        "incretin": ["GLP-1", "GIP", "glucagon receptor", "incretin"],
        "NAD_metabolism": ["NAD+", "NADH", "nicotinamide", "sirtuin"],
        "inflammation": ["inflammation", "inflammatory", "NF-kB", "NFkB", "cytokine"],
        "senescence": ["senescence", "senolytic", "SASP"],
    }
    return [tag for tag, terms in tag_rules.items() if _contains_any(text, terms)]


def inferred_therapeutic_area_tags(context: dict) -> List[str]:
    text = context["all"]
    tag_rules = {
        "obesity": ["obesity", "body weight", "weight loss", "overweight"],
        "diabetes_metabolic": ["diabetes", "glycemic", "glycaemic", "insulin", "metabolic"],
        "cardiovascular": ["cardiovascular", "heart", "atherosclerosis", "hypertension"],
        "liver_mash": ["MASH", "NASH", "steatohepatitis", "liver"],
        "kidney": ["kidney", "renal", "CKD"],
        "neuro": ["neuro", "brain", "cognitive", "Alzheimer", "Parkinson"],
        "oncology": ["cancer", "tumor", "tumour", "oncology", "carcinoma"],
        "musculoskeletal": ["muscle", "tendon", "bone", "joint", "sarcopenia"],
        "reproductive": ["fertility", "PCOS", "ovary", "testosterone", "sperm"],
    }
    return [tag for tag, terms in tag_rules.items() if _contains_any(text, terms)]


def inferred_function_tags(role_category: str, evidence: dict) -> List[str]:
    tags = []
    if role_category == "direct_intervention":
        tags.append("intervention")
    if "Review" in str(evidence.get("primary_study_type", "")):
        tags.append("review_context")
    if str(evidence.get("model_type", "")) == "human":
        tags.append("human_context")
    if str(evidence.get("model_type", "")) == "animal":
        tags.append("animal_context")
    return tags


def _fallback_role(evidence: dict) -> str:
    relevance = str(evidence.get("molecule_relevance", ""))
    if relevance == "primary_intervention":
        return "direct_intervention"
    if relevance == "secondary_intervention_or_comparator":
        return "comparator_or_background_drug"
    if relevance == "biomarker_or_mechanism":
        return "pathway_component"
    if relevance == "synthesis_or_assay":
        return "assay_or_detection"
    if relevance == "environmental_or_material_use":
        return "environmental_or_material_use"
    if relevance == "primary_topic_unclear_role":
        return "primary_topic_unclear_role"
    return "background_or_unclear"


def _review_status_from_bucket(bucket: str) -> str:
    if bucket == "public_candidate":
        return "machine_public_candidate"
    if bucket == "exclude_noise":
        return "machine_exclude"
    if bucket == "background_only":
        return "machine_background_only"
    return "needs_review"


def _matches_any(terms: Iterable[str], context: dict, molecule: dict) -> bool:
    return any(_term_score(_expand_term(term, molecule), context) > 0 for term in terms)


def _term_score(term: str, context: dict) -> int:
    if not term:
        return 0
    term_norm = _normalize_for_match(term)
    if not term_norm:
        return 0
    if _has_norm_phrase(context["title_norm"], term_norm):
        return 30
    if _has_norm_phrase(context["abstract_norm"], term_norm):
        return 10
    return 0


def _context(title: str, abstract: str) -> dict:
    title_l = str(title or "").lower()
    abstract_l = str(abstract or "").lower()
    all_l = f"{title or ''} {abstract or ''}".lower()
    return {
        "title": title_l,
        "abstract": abstract_l,
        "all": all_l,
        "title_norm": _normalize_for_match(title_l),
        "abstract_norm": _normalize_for_match(abstract_l),
        "all_norm": _normalize_for_match(all_l),
    }


def _evidence_text(matched_term: str, context: dict, paper: dict) -> str:
    if matched_term:
        sentence = _sentence_with_term(context["title"], matched_term) or _sentence_with_term(context["abstract"], matched_term)
        if sentence:
            return sentence[:500]
    abstract = str(paper.get("abstract", "") or "")
    if abstract:
        return _sentences(abstract)[0][:500] if _sentences(abstract) else abstract[:500]
    return str(paper.get("title", "") or "")[:500]


def _sentence_with_term(text: str, term: str) -> str:
    for sentence in _sentences(text):
        if _has_phrase(sentence, term):
            return sentence
    return ""


def _sentences(text: str) -> List[str]:
    text = " ".join(str(text or "").split())
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(_has_phrase(text, term) for term in terms)


def _has_phrase(text: str, term: str) -> bool:
    return _has_norm_phrase(_normalize_for_match(str(text or "")), _normalize_for_match(str(term or "")))


def _has_norm_phrase(text_norm: str, term_norm: str) -> bool:
    if not text_norm or not term_norm:
        return False
    return f" {term_norm} " in f" {text_norm} "


@lru_cache(maxsize=8192)
def _normalize_for_match(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(value.split())


def _expand_term(term: str, molecule: dict) -> str:
    term = str(term or "").strip()
    replacements = {
        "{display_name}": str(molecule.get("display_name", "") or ""),
        "{molecule_id}": str(molecule.get("molecule_id", "") or ""),
    }
    for key, value in replacements.items():
        term = term.replace(key, value)
    return term.strip()


def _split_terms(value: str) -> List[str]:
    return [_clean(v) for v in str(value or "").split("|") if _clean(v)]


def _split_tags(value: str) -> List[str]:
    return [_clean(v) for v in str(value or "").replace(",", ";").split(";") if _clean(v)]


def _clean(value: str) -> str:
    return str(value or "").strip()
