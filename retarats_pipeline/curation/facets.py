"""Faceted tagging + filtering layer.

Goal: let an end user slice the database like "drug X for use Y",
"non-human primate data", "human RCTs on glycemic control", etc.

Each evidence record is mapped to a set of normalized *facets*. A facet is a
(group, value) pair drawn from a controlled vocabulary (``config/FACETS.csv``)
plus a handful of structured passthrough fields. We emit two shapes:

* wide  -> ``facet_<group>`` columns (semicolon-joined) for a flat sheet, and a
           ``facet_all`` blob for free-text search.
* long  -> a list of ``(evidence_id, facet_group, facet_value, facet_label,
           facet_source)`` rows for a proper filter/pivot table.

Everything is regex + structured-field based, so every tag is explainable:
``facet_source`` records whether it came from a structured field or a text hit.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Sequence, Tuple

# Facet groups that come purely from the controlled-vocabulary CSV. The new
# literature-informed groups (drug_class/population/sex/formulation/
# evidence_direction) are pattern-driven too, so they ride the same fast index.
PATTERN_GROUPS = (
    "species", "indication", "endpoint", "mechanism", "route",
    "drug_class", "population", "sex", "formulation", "evidence_direction",
)

# All facet groups we emit as wide columns (pattern groups + structured ones).
FACET_GROUPS = (
    "molecule",
    "species",
    "model_system",
    "study_type",
    "indication",
    "endpoint",
    "mechanism",
    "route",
    "drug_class",
    "population",
    "sex",
    "formulation",
    "evidence_direction",
    "molecule_role",
    "signal",
    "year_bucket",
)

_DEFAULT_FACETS_CSV = os.path.join("config", "FACETS.csv")


@dataclass
class FacetDef:
    group: str
    value: str
    label: str
    regex: "re.Pattern"
    notes: str = ""
    raw_patterns: Tuple[str, ...] = ()


# Regex metacharacters that force us onto the slow regex path.
_REGEX_META = set(r"\?*+()[]{}|^$.")


@dataclass
class ValueMatcher:
    """Fast presence-matcher for one facet value.

    Precomputes cheap paths so we avoid lookaround regex when possible:
      * words   -> single alnum tokens; matched by set intersection (O(1) each)
      * phrases -> plain multi-token strings; matched by substring `in`
      * regexes -> only patterns that actually contain regex metacharacters
    """

    group: str
    value: str
    label: str
    words: frozenset
    phrases: Tuple[str, ...]
    regexes: Tuple["re.Pattern", ...]

    def matches(self, token_set: frozenset, blob_lower: str) -> bool:
        if self.words & token_set:
            return True
        for ph in self.phrases:
            if ph in blob_lower:
                return True
        for rx in self.regexes:
            if rx.search(blob_lower):
                return True
        return False


def _classify_pattern(pattern: str):
    """Return ('word', w) | ('phrase', p) | ('regex', compiled).

    The regex path compiles against a *lowercased* blob (no IGNORECASE flag,
    which is measurably faster) with cheap ascii boundary lookarounds.
    """
    low = pattern.lower()
    if re.fullmatch(r"[a-z0-9]+", low):
        return ("word", low)
    if not (_REGEX_META & set(pattern)):
        # plain text possibly with spaces/hyphens -> substring
        return ("phrase", low)
    compiled = re.compile(rf"(?<![a-z0-9])(?:{low})(?![a-z0-9])")
    return ("regex", compiled)


@dataclass
class FacetResult:
    """Wide + long representation of one record's facets."""

    wide: Dict[str, str] = field(default_factory=dict)
    long: List[Tuple[str, str, str, str]] = field(default_factory=list)
    # long tuples are (group, value, label, source)


@lru_cache(maxsize=4)
def load_facet_defs(path: str = _DEFAULT_FACETS_CSV) -> Tuple[FacetDef, ...]:
    """Load and compile the controlled facet vocabulary from CSV.

    Cached by path so repeated calls in a run are cheap.
    """
    defs: List[FacetDef] = []
    if not os.path.exists(path):
        return tuple(defs)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            group = (row.get("facet_group") or "").strip()
            value = (row.get("facet_value") or "").strip()
            if not group or not value:
                continue
            patterns = [p.strip() for p in (row.get("patterns") or "").split("||") if p.strip()]
            if not patterns:
                continue
            # Word-ish boundaries; patterns may themselves contain regex (e.g. NF-?kB).
            alternation = "|".join(f"(?:{p})" for p in patterns)
            compiled = re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])", re.IGNORECASE)
            defs.append(
                FacetDef(
                    group=group,
                    value=value,
                    label=(row.get("display_label") or value).strip(),
                    regex=compiled,
                    notes=(row.get("notes") or "").strip(),
                    raw_patterns=tuple(patterns),
                )
            )
    return tuple(defs)


@lru_cache(maxsize=4)
def _compiled_index(path: str = _DEFAULT_FACETS_CSV):
    """Performance helper cached per path.

    Returns (matchers, known_labels):
      * matchers:     tuple of ValueMatcher (fast word/phrase/regex presence tests)
      * known_labels: {(group, value): label} for mapping pre-extracted tags.
    """
    defs = load_facet_defs(path)
    matchers: List[ValueMatcher] = []
    known_labels: Dict[Tuple[str, str], str] = {}
    for d in defs:
        known_labels[(d.group, d.value)] = d.label
        words, phrases, regexes = set(), [], []
        for pat in d.raw_patterns:
            kind, payload = _classify_pattern(pat)
            if kind == "word":
                words.add(payload)
            elif kind == "phrase":
                phrases.append(payload)
            else:
                regexes.append(payload)
        matchers.append(
            ValueMatcher(
                group=d.group,
                value=d.value,
                label=d.label,
                words=frozenset(words),
                phrases=tuple(phrases),
                regexes=tuple(regexes),
            )
        )
    return tuple(matchers), known_labels


def _text_blob(paper: dict, evidence: dict) -> str:
    parts = [
        str(paper.get("title", "") or ""),
        str(paper.get("abstract", "") or ""),
        _flatten(paper.get("mesh_terms", "")),
        _flatten(paper.get("keywords", "")),
        _flatten(paper.get("chemicals", "")),
    ]
    return " ".join(p for p in parts if p)


def _flatten(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(x) for x in value)
    return str(value or "")


# --- structured-field derived facets ----------------------------------------

_STUDY_TYPE_MAP = {
    "RCT": ("rct", "Randomized controlled trial"),
    "Meta-analysis": ("meta_analysis", "Meta-analysis"),
    "Systematic review": ("systematic_review", "Systematic review"),
    "Human interventional non-RCT": ("human_nonrct", "Human interventional (non-RCT)"),
    "Human observational": ("human_observational", "Human observational"),
    "Clinical trial / unclear population": ("clinical_trial_unclear", "Clinical trial (population unclear)"),
    "Review": ("review", "Narrative review"),
}

_MODEL_SYSTEM_MAP = {
    "human": ("human", "Human"),
    "animal": ("animal", "Animal / preclinical"),
    "in vitro": ("in_vitro", "In vitro / cell"),
    "review": ("review", "Review / synthesis"),
    "unclear": ("unclear", "Unclear model"),
}

_ROLE_LABELS = {
    "direct_intervention": "Direct intervention",
    "comparator_or_background_drug": "Comparator / background",
    "biomarker_readout": "Biomarker / readout",
    "pathway_component": "Mechanism / pathway",
    "clinical_tool_or_diagnostic": "Clinical tool / diagnostic",
    "tool_compound_or_positive_control": "Tool compound / control",
    "assay_or_detection": "Assay / detection",
    "synthesis_or_production": "Synthesis / production",
    "environmental_or_material_use": "Environmental / materials",
    "primary_topic_unclear_role": "Primary topic, unclear role",
    "background_or_unclear": "Background / unclear",
}


def derive_facets(evidence: dict, paper: dict, facet_defs: Sequence[FacetDef] | None = None) -> FacetResult:
    """Return wide + long facets for one evidence record."""
    if facet_defs is None:
        facet_defs = load_facet_defs()
    blob = _text_blob(paper, evidence)

    long: List[Tuple[str, str, str, str]] = []
    by_group: Dict[str, "dict[str, str]"] = {g: {} for g in FACET_GROUPS}

    def add(group: str, value: str, label: str, source: str) -> None:
        if not value:
            return
        bucket = by_group.setdefault(group, {})
        if value not in bucket:  # first source wins for the label, but keep de-duped
            bucket[value] = label
            long.append((group, value, label, source))

    # 1) molecule (structured passthrough)
    mol = str(evidence.get("molecule_id", "") or "")
    mol_name = str(evidence.get("molecule_name", "") or mol)
    add("molecule", mol, mol_name, "structured:molecule_id")

    # 2) model_system (structured)
    model_type = str(evidence.get("model_type", "") or "").lower().strip()
    if model_type in _MODEL_SYSTEM_MAP:
        v, l = _MODEL_SYSTEM_MAP[model_type]
        add("model_system", v, l, "structured:model_type")

    # 3) study_type (structured)
    primary = str(evidence.get("primary_study_type", "") or "").strip()
    if primary in _STUDY_TYPE_MAP:
        v, l = _STUDY_TYPE_MAP[primary]
        add("study_type", v, l, "structured:primary_study_type")

    # 4) molecule_role (structured)
    role = str(evidence.get("role_category", "") or "").strip()
    if role:
        add("molecule_role", role, _ROLE_LABELS.get(role, role.replace("_", " ").title()), "structured:role_category")

    # 5) signals (structured)
    for sig_val, sig_label, src in _signal_facets(evidence):
        add("signal", sig_val, sig_label, src)

    # 6) year bucket (structured)
    yb = _year_bucket(evidence.get("pub_year"))
    if yb:
        add("year_bucket", yb, yb.replace("_", " "), "structured:pub_year")

    # 7) species: structured field first, then text patterns
    for v, l, src in _species_from_structured(evidence):
        add("species", v, l, src)

    # 8) pattern-driven facets from controlled vocabulary (species/indication/endpoint/mechanism/route)
    # Fast path: tokenize once, then use set-intersection / substring tests; only
    # patterns with real regex metacharacters hit the (slow) regex engine.
    matchers, known_labels = _compiled_index()
    blob_lower = blob.lower()
    token_set = frozenset(re.findall(r"[a-z0-9]+", blob_lower))
    for vm in matchers:
        if vm.matches(token_set, blob_lower):
            add(vm.group, vm.value, vm.label, "text:pattern")

    # 9) seed indication/endpoint/mechanism from already-extracted structured tags
    _add_tag_field(add, evidence.get("condition_tags"), "indication", "structured:condition_tags", known_labels)
    _add_tag_field(add, evidence.get("endpoint_tags"), "endpoint", "structured:endpoint_tags", known_labels)
    _add_tag_field(add, evidence.get("mechanistic_focus"), "mechanism", "structured:mechanistic_focus", known_labels)
    _add_tag_field(add, evidence.get("mechanism_tags"), "mechanism", "structured:mechanism_tags", known_labels)

    wide: Dict[str, str] = {}
    for group in FACET_GROUPS:
        vals = list(by_group.get(group, {}).keys())
        wide[f"facet_{group}"] = "; ".join(vals)
    # human-readable "search everything" blob (labels)
    all_labels = sorted({l for (_g, _v, l, _s) in long})
    wide["facet_all"] = " | ".join(all_labels)
    wide["facet_count"] = str(len(long))

    return FacetResult(wide=wide, long=long)


def _species_from_structured(evidence: dict) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    model_type = str(evidence.get("model_type", "") or "").lower()
    sp = str(evidence.get("species_or_population", "") or "").lower()
    if model_type == "human" or "patient" in sp or "human" in sp or "participant" in sp:
        out.append(("human", "Human", "structured:model_type/species"))
    for token, (val, label) in {
        "mice": ("mouse", "Mouse"),
        "mouse": ("mouse", "Mouse"),
        "murine": ("mouse", "Mouse"),
        "rat": ("rat", "Rat"),
        "rats": ("rat", "Rat"),
        "cell": ("cell_line", "Cell line / in vitro"),
        "in vitro": ("cell_line", "Cell line / in vitro"),
    }.items():
        if token in sp:
            out.append((val, label, "structured:species_or_population"))
    return out


def _add_tag_field(add, raw, group: str, source: str, known_labels: Dict[Tuple[str, str], str]) -> None:
    """Map a pre-extracted semicolon tag field onto the controlled vocabulary.

    The existing pipeline already emits condition/endpoint/mechanism tag ids that
    mostly match our facet vocabulary; we accept exact-id matches and otherwise
    keep the raw id (so nothing is silently dropped).
    """
    if not raw:
        return
    for tag in _split_tags(raw):
        label = known_labels.get((group, tag))
        if label:
            add(group, tag, label, source)
        else:
            # keep unknown ids visible but flagged as source so they can be curated
            add(group, tag, tag.replace("_", " "), source + ":unmapped")


def _split_tags(raw) -> List[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[;,]", str(raw or ""))
    out = []
    for item in items:
        t = item.strip()
        if t and t.lower() not in {"not clearly reported", "not reported", "unclear", ""}:
            out.append(t)
    return out


def _signal_facets(evidence: dict) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    outcome = str(evidence.get("outcome_direction", "") or "").lower()
    if "beneficial" in outcome or "desired" in outcome:
        out.append(("efficacy_beneficial", "Beneficial efficacy signal", "structured:outcome_direction"))
    if "harmful" in outcome:
        out.append(("efficacy_harmful", "Harmful signal", "structured:outcome_direction"))
    if "neutral" in outcome or "no clear" in outcome:
        out.append(("efficacy_neutral", "Neutral / no clear effect", "structured:outcome_direction"))
    safety = str(evidence.get("safety_signal", "") or "").lower()
    if safety and safety not in {"not reported", "not clearly reported", ""}:
        if any(t in safety for t in ["adverse", "toxicity", "serious", "nausea", "vomiting", "death", "hypoglycem"]):
            out.append(("safety_concern", "Safety concern mentioned", "structured:safety_signal"))
    return out


def _year_bucket(year) -> str:
    try:
        y = int(str(year)[:4])
    except (TypeError, ValueError):
        return ""
    if y >= 2024:
        return "2024_plus"
    if y >= 2020:
        return "2020_2023"
    if y >= 2015:
        return "2015_2019"
    if y >= 2000:
        return "2000_2014"
    return "pre_2000"
