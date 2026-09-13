"""Versioned, conservative ontology annotations alongside legacy topic facets.

An annotation is a text-supported candidate, not a curator-verified assertion.
Legacy broad tags never become studied conditions through a vocabulary crosswalk.
Pure stdlib; no source mutation, network, or inferred numerical findings.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

VERSION = "1.0.0"
CONFIG = Path(__file__).resolve().parents[2] / "config"
FACET_AXES = ("condition_studied", "outcome_measured", "research_area",
              "experimental_system", "synthesis_method")
FIELDS = ["ontology_version", "evidence_scope", "ontology_review_reason", "ontology_annotations"] + [
    "facet_" + axis for axis in FACET_AXES]
ANNOTATION_FIELDS = ["annotation_id", "evidence_id", "term_id", "relationship",
                     "source_field", "source_text", "rule_id", "ontology_version",
                     "review_status"]


@lru_cache(maxsize=1)
def terms():
    with (CONFIG / "ONTOLOGY_TERMS.csv").open(newline="", encoding="utf-8") as f:
        return tuple(csv.DictReader(f))


@lru_cache(maxsize=1)
def rules():
    with (CONFIG / "ONTOLOGY_RULES.csv").open(newline="", encoding="utf-8") as f:
        return tuple((r, re.compile(r["pattern"], re.I)) for r in csv.DictReader(f))


def validate():
    """Fail on dangling mappings, duplicate IDs, missing criteria, or invalid regex."""
    rows = terms()
    ids = {r["term_id"] for r in rows}
    errors = []
    if len(ids) != len(rows):
        errors.append("duplicate ontology term IDs")
    required = ("term_id", "axis", "value", "label", "definition", "inclusion_rule",
                "exclusion_rule", "example", "counterexample", "version")
    for r in rows:
        if any(not r.get(k) for k in required):
            errors.append("missing term criteria: " + r["term_id"])
        if r["version"] != VERSION:
            errors.append("term version mismatch: " + r["term_id"])
        for p in filter(None, r.get("parents", "").split(";")):
            if p not in ids:
                errors.append("unknown parent: " + p)
    parents = {r["term_id"]: list(filter(None, r.get("parents", "").split(";"))) for r in rows}
    def cycle(node, path):
        if node in path:
            return True
        return any(cycle(p, path | {node}) for p in parents.get(node, []))
    if any(cycle(node, set()) for node in parents):
        errors.append("ontology parent cycle")
    rule_ids = set()
    for r, _ in rules():
        if r["term_id"] not in ids or r["rule_id"] in rule_ids:
            errors.append("invalid/duplicate rule: " + r["rule_id"])
        if r["relationship"] not in {"studied_condition", "measured_outcome"}:
            errors.append("invalid relationship: " + r["rule_id"])
        rule_ids.add(r["rule_id"])
    with (CONFIG / "ONTOLOGY_CROSSWALK.csv").open(newline="", encoding="utf-8") as f:
        mappings = list(csv.DictReader(f))
    for r in mappings:
        if any(t not in ids for t in r["target_terms"].split(";") if t):
            errors.append("unknown crosswalk target: " + r["source_value"])
    with (CONFIG / "FACETS.csv").open(newline="", encoding="utf-8") as f:
        expected = {(r["facet_group"], r["facet_value"]) for r in csv.DictReader(f)}
    actual = {(r["source_axis"], r["source_value"]) for r in mappings if r["source_file"] == "FACETS.csv"}
    if expected - actual:
        errors.append("unmapped current facets: " + repr(sorted(expected - actual)))
    return errors


def _pubtypes(evidence, paper):
    raw = paper.get("pubtypes") or evidence.get("pubtypes") or []
    return " ".join(raw) if isinstance(raw, list) else str(raw)


def synthesis_methods(evidence, paper=None):
    paper = paper or {}
    title = str(paper.get("title") or evidence.get("title") or "")
    primary = str(evidence.get("primary_study_type", ""))
    # The historical slash category means either method, not confirmation of both.
    if "/" in primary:
        primary = ""
    text = " ".join([title, _pubtypes(evidence, paper), primary]).lower()
    found = []
    if "systematic review" in text:
        found.append("systematic_review")
    if re.search(r"meta[- ]analysis", text):
        found.append("meta_analysis")
    return found


def is_narrative_review(evidence, paper=None):
    paper = paper or {}
    if synthesis_methods(evidence, paper):
        return False
    primary = str(evidence.get("primary_study_type", "")).lower()
    abstract = str(paper.get("abstract") or evidence.get("abstract") or "")
    return ("review" in primary and "systematic" not in primary) or bool(re.search(
        r"\bthis review (?:summarizes|summarises|provides|discusses|highlights)\b", abstract, re.I))


def _segments(paper, evidence):
    """Keep locatable source text and structured-abstract section context."""
    title = str(paper.get("title") or evidence.get("title") or "")
    abstract = str(paper.get("abstract") or evidence.get("abstract") or "")
    yield "title", title, True
    section = ""
    for part in re.split(r"(?<=[.!?])\s+|\n+|(?=\b(?:BACKGROUND|OBJECTIVES?|METHODS?|RESULTS|CONCLUSIONS?)\s*:)", abstract):
        heading = re.match(r"\s*(background|objectives?|methods?|results|conclusions?)\s*:", part, re.I)
        if heading:
            section = heading.group(1).lower().rstrip("s")
        current = section in {"method", "result"} or bool(re.search(
            r"\b(?:we (?:subsequently )?(?:enrolled|recruited|randomized|randomised|measured|assessed|evaluated|studied|investigated|tested|performed|conducted|report|present)|"
            r"this study (?:investigated|examined|evaluated|assessed)|"
            r"^in (?:mice|rats|patients|adults)\b|"
            r"(?:patients|participants|subjects|mice|rats) were (?:enrolled|recruited|randomized|randomised|treated)|"
            r"(?:primary|secondary) (?:outcomes?|endpoints?))\b", part, re.I))
        yield "abstract", part, current and section not in {"background", "objective"}


# Scope is conservative: review metadata alone is not human evidence. Only explicit
# model fields or descriptions of the included studies establish synthesis scope.
def evidence_scope(evidence, paper=None):
    paper = paper or {}
    is_synthesis = bool(synthesis_methods(evidence, paper)) or "Systematic review" in str(evidence.get("primary_study_type", ""))
    if is_narrative_review(evidence, paper):
        return "unknown"
    title = str(paper.get("title") or evidence.get("title") or "")
    model = str(evidence.get("model_primary") or evidence.get("model_type") or "").lower()
    if not is_synthesis:
        scopes = set()
        for source, sentence, current in _segments(paper, evidence):
            if not current or re.search(r"\b(?:previous|prior|background|future)\b", sentence, re.I):
                continue
            if re.search(r"\b(?:mice|rats|rodents?|mouse|cell[- ]free|in silico|cell lines?|cell culture|in vitro|organoids?|primary cells)\b", sentence, re.I):
                scopes.add("nonclinical")
            if re.search(r"\b(?:participants|adults|healthy volunteers|clinical (?:trial|studies)|phase [123ivx]+[ab]? trial|patients? (?:with|were)|\d+-year-old (?:\w+ ){0,2}(?:woman|man|male|female))\b", sentence, re.I):
                scopes.add("human")
        if len(scopes) > 1:
            return "mixed"
        if scopes:
            return next(iter(scopes))
        if model in {"human", "animal", "in_vitro", "in vitro"}:
            return "human" if model == "human" else "nonclinical"
        return "unknown"
    scopes = set()
    for field, sentence, current in _segments(paper, evidence):
        if field != "title" and not current and not re.search(r"\b(?:included|including|eligible|involved|comprising)\b", sentence, re.I):
            continue
        if re.search(r"\b(?:excluded|excluding|not included|no (?:human|animal))\b", sentence, re.I):
            continue
        if re.search(r"\b(?:animal|preclinical|mouse|mice|rat|rats|rodent|in vitro|cell culture|cell lines?)\b", sentence, re.I):
            scopes.add("nonclinical")
        # Human-derived cells alone never satisfy the human pattern.
        if re.search(r"\b(?:patients|participants|adults|healthy volunteers|clinical (?:trials|studies)|human (?:studies|trials|participants))\b", sentence, re.I):
            scopes.add("human")
    if len(scopes) > 1:
        return "mixed"
    return next(iter(scopes), "unknown")


def annotate(evidence, paper=None):
    paper = paper or {}
    eid = str(evidence.get("evidence_id", ""))
    by_id = {r["term_id"]: r for r in terms()}
    annotations = []
    seen = set()
    facets = {axis: set() for axis in FACET_AXES}

    def add(term_id, relationship, source, passage, rule_id):
        key = (term_id, relationship, source, passage)
        if key in seen:
            return
        seen.add(key)
        term = by_id[term_id]
        if term["axis"] in facets:
            facets[term["axis"]].add(term["value"])
        digest = hashlib.sha256(json.dumps([eid, VERSION, key], ensure_ascii=False).encode()).hexdigest()[:24]
        annotations.append(dict(zip(ANNOTATION_FIELDS, [digest, eid, term_id, relationship,
            source, passage, rule_id, VERSION, "machine_unreviewed"])))

    primary = str(evidence.get("primary_study_type", "")).lower()
    narrative = is_narrative_review(evidence, paper)
    for source, passage, current in _segments(paper, evidence):
        if not current or narrative:
            continue
        # Conservative whole-sentence veto; trades recall for readable provenance.
        if re.search(r"\b(?:excluded|excluding|previous(?:ly)?|prior studies|other studies|not measured|not assessed|did not measure)\b", passage, re.I):
            continue
        for rule, pattern in rules():
            if rule["relationship"] == "studied_condition" and re.search(
                    r"\b(?:potential|may|might|could|with or without|future|proposed)\b", passage, re.I):
                continue
            if pattern.search(passage):
                add(rule["term_id"], rule["relationship"], source, passage, rule["rule_id"])
                for parent in filter(None, by_id[rule["term_id"]]["parents"].split(";")):
                    if by_id[parent]["axis"] == "research_area":
                        add(parent, "research_context", source, passage, rule["rule_id"] + ":parent")

    for method in synthesis_methods(evidence, paper):
        add("synthesis_method:" + method, "synthesis_method", "title/pubtypes/study_type",
            " | ".join([str(paper.get("title") or evidence.get("title") or ""),
                        _pubtypes(evidence, paper), str(evidence.get("primary_study_type", ""))]), "synthesis_metadata_v1")

    title = str(paper.get("title") or evidence.get("title") or "")
    systems = []
    for value, pattern in [
        ("cell_free", r"\bcell[- ]free\b"), ("in_silico", r"\b(?:in silico|molecular docking)\b"),
        ("organoid", r"\borganoids?\b"), ("primary_cells", r"\bprimary cells\b"),
        ("cell_line", r"\bcell (?:lines?|culture)\b"), ("ex_vivo", r"\bex vivo\b"),
        ("in_vitro_unspecified", r"\bin vitro\b")]:
        if re.search(pattern, title, re.I):
            systems.append(value)
            add("experimental_system:" + value, "experimental_system", "title", title, "system_title_v1")
    if not synthesis_methods(evidence, paper) and not systems:
        model_key = "model_primary" if evidence.get("model_primary") else "model_type"
        model = str(evidence.get(model_key, "")).lower()
        value = {"human": "living_human", "animal": "animal_in_vivo", "in vitro": "in_vitro_unspecified", "in_vitro": "in_vitro_unspecified"}.get(model)
        if value:
            add("experimental_system:" + value, "experimental_system", model_key, model, "system_structured_v1")
    scope = evidence_scope(evidence, paper)
    reasons = []
    if scope in {"unknown", "mixed"}:
        reasons.append("scope_" + scope)
    if "systematic review" in primary and not synthesis_methods(evidence, paper):
        reasons.append("legacy_synthesis_method_unconfirmed")
    if primary == "rct" and re.search(r"\bnon[- ]randomi[sz]ed\b", title, re.I):
        reasons.append("legacy_rct_conflicts_with_title")
    if narrative and "review" not in primary:
        reasons.append("legacy_design_conflicts_with_review_text")
    if scope == "nonclinical" and primary in {"rct", "human observational", "human interventional non-rct"}:
        reasons.append("legacy_human_design_conflicts_with_scope")
    result = {"ontology_version": VERSION, "evidence_scope": scope,
              "ontology_review_reason": "; ".join(reasons),
              "ontology_annotations": json.dumps(annotations, ensure_ascii=False)}
    result.update({"facet_" + axis: "; ".join(sorted(values)) for axis, values in facets.items()})
    return result, annotations
