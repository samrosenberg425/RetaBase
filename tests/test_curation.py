#!/usr/bin/env python3
"""Offline unit tests for the curation layer (facets / strength / publication / appraisal).

No network, no SQLite needed. Run:

    python3 tests/test_curation.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.curation.facets import derive_facets
from retarats_pipeline.curation.reliability import assess_reliability, classify_evidence
from retarats_pipeline.curation.publication_status import decide_publication, check_required_fields
from retarats_pipeline.curation.appraisal import appraise_evidence
from retarats_pipeline.curation.journal import journal_reputation
from retarats_pipeline.curation.ranking import RANK_WEIGHTS, compute_rank
from retarats_pipeline.enrichment.clients import (
    semanticscholar_authors,
    semanticscholar_citation_count,
    semanticscholar_influential_count,
)


PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def human_rct():
    return {
        "evidence_id": "1:retatrutide:r1",
        "molecule_id": "retatrutide",
        "molecule_name": "Retatrutide",
        "pmid": "111",
        "title": "Retatrutide in obesity: a randomized placebo-controlled trial",
        "pub_year": "2025",
        "primary_study_type": "RCT",
        "model_type": "human",
        "role_category": "direct_intervention",
        "molecule_relevance": "primary_intervention",
        "processing_lane": "human_intervention",
        "condition_tags": "obesity_weight; diabetes_glycemic",
        "endpoint_tags": "body_weight; glycemic_control",
        "intervention_or_exposure": "Retatrutide as direct intervention",
        "comparator_or_control": "placebo",
        "dose_route": "subcutaneous; 12 mg",
        "duration": "48 weeks",
        "sample_size": "n=338",
        "outcome_direction": "beneficial_or_desired_signal",
        "safety_signal": "nausea and vomiting reported",
        "keep_for_final_database": True,
    }


def nhp_preclinical():
    return {
        "evidence_id": "2:x:r1",
        "molecule_id": "kisspeptin",
        "molecule_name": "Kisspeptin",
        "pmid": "222",
        "title": "Kisspeptin administration in cynomolgus macaque",
        "pub_year": "2023",
        "primary_study_type": "Animal study",
        "model_type": "animal",
        "role_category": "direct_intervention",
        "processing_lane": "preclinical_intervention",
        "species_or_population": "animals",
        "keep_for_final_database": True,
    }


def methods_noise():
    return {
        "evidence_id": "3:x:r1",
        "molecule_id": "dye",
        "molecule_name": "SomeDye",
        "pmid": "",
        "pub_year": "2019",
        "primary_study_type": "",
        "model_type": "unclear",
        "role_category": "assay_or_detection",
        "processing_lane": "methods_assay_synthesis",
        "keep_for_final_database": True,
    }


def run():
    # --- facets ---
    ev = human_rct()
    paper = {"pmid": "111", "title": "Retatrutide in obesity: a randomized placebo-controlled trial",
             "abstract": "Adults with obesity received subcutaneous retatrutide versus placebo. Body weight and HbA1c improved.",
             "mesh_terms": "Humans; Obesity"}
    fr = derive_facets(ev, paper)
    check("human facet", "human" in fr.wide["facet_species"])
    check("obesity indication", "obesity_weight" in fr.wide["facet_indication"])
    check("rct study_type", "rct" in fr.wide["facet_study_type"])
    check("route subcutaneous", "subcutaneous" in fr.wide["facet_route"])
    check("facets long non-empty", len(fr.long) > 3)

    # non-human primate detection from free text
    nhp = nhp_preclinical()
    nhp_paper = {"pmid": "222", "title": "Kisspeptin administration in cynomolgus macaque",
                 "abstract": "The effect of kisspeptin was studied in the rhesus macaque model."}
    fr2 = derive_facets(nhp, nhp_paper)
    check("nonhuman_primate facet detected", "nonhuman_primate" in fr2.wide["facet_species"])

    # --- reliability (two-axis, section-appropriate) ---
    rel = assess_reliability(ev, paper)
    check("RCT class = human controlled", rel.evidence_class == "human_clinical_controlled")
    check("RCT high study quality", rel.reliability_tier in {"high", "moderate"})
    check("RCT high directness", rel.directness_tier == "high")
    check("RCT design 60 pts", rel.quality_components.get("design") == 60)
    check("placebo comparator pts", rel.quality_components.get("comparator") == 12)
    # methods/assay records are now SCORED (within class), not zeroed/excluded
    m_rel = assess_reliability(methods_noise(), None)
    check("methods = methods_tool class", m_rel.evidence_class == "methods_tool")
    check("methods scored, not zero", m_rel.reliability_score > 0)
    check("methods low directness", m_rel.directness_tier in {"low", "limited"})
    # genuinely off-topic (environmental) is the only hard exclusion
    env = dict(methods_noise()); env["role_category"] = "environmental_or_material_use"
    check("environmental class off_topic", classify_evidence(env) == "off_topic")

    # --- required fields ---
    present, missing = check_required_fields(ev)
    check("human RCT has required fields", present and not missing)
    incomplete = dict(ev); incomplete["model_type"] = ""; incomplete.pop("pmid"); incomplete["doi"] = ""
    present2, missing2 = check_required_fields(incomplete)
    check("missing model+identifier flagged", (not present2) and len(missing2) >= 2)
    unclear = dict(ev); unclear["model_type"] = "unclear"
    present3, _ = check_required_fields(unclear)
    check("'unclear' model counts as present", present3)

    # --- publication decision (broad inclusion) ---
    ev.update(rel.to_dict())
    dec = decide_publication(ev)
    check("human RCT is featured", dec.auto_publish_eligible and dec.publication_status == "featured")
    check("human RCT -> Human evidence section", dec.website_section == "Human evidence")
    check("publish_rule recorded", dec.publish_rule_id == "broad_v1:featured")

    # a methods record is INCLUDED (listed or review), never excluded as noise
    methods = methods_noise(); methods.update(assess_reliability(methods, None).to_dict())
    mdec = decide_publication(methods)
    check("methods record included (not excluded)", mdec.publication_status in {"listed", "review"})
    check("methods record not featured", not mdec.auto_publish_eligible)
    # a complete methods record is fully listed
    methods2 = methods_noise(); methods2["pmid"] = "999"; methods2["primary_study_type"] = "Methods"
    methods2["title"] = "A validated LC-MS assay for peptide quantification"
    methods2.update(assess_reliability(methods2, None).to_dict())
    check("complete methods record listed", decide_publication(methods2).publication_status == "listed")
    # environmental record is excluded as noise
    envr = dict(methods_noise()); envr["role_category"] = "environmental_or_material_use"
    envr.update(assess_reliability(envr, None).to_dict())
    edec = decide_publication(envr)
    check("environmental excluded_noise", edec.publication_status == "excluded_noise")

    # --- appraisal ---
    app = appraise_evidence(ev)
    check("appraisal names RCT strength", "randomized" in app.appraisal_strengths.lower())
    check("appraisal has llm scaffold", app.llm_summary == "" and app.llm_summary_status == "not_generated")
    check("appraisal summary mentions molecule", "Retatrutide" in app.appraisal_summary)
    app2 = appraise_evidence(nhp)
    check("animal appraisal flags translation caveat", "animal" in app2.appraisal_limitations.lower())

    # --- journal reputation ---
    jr_nejm = journal_reputation("New England Journal of Medicine")
    check("NEJM flagship tier", jr_nejm.journal_tier == "flagship" and jr_nejm.journal_reputation >= 90)
    jr_dc = journal_reputation("Diabetes Care")
    check("Diabetes Care top tier", jr_dc.journal_tier == "top")
    jr_cochrane = journal_reputation("Cochrane Database of Systematic Reviews")
    check("Cochrane recognized as strong+", jr_cochrane.journal_reputation >= 72)
    jr_unknown = journal_reputation("Journal of Obscure Peptide Studies")
    check("unknown journal is neutral 50 (not punished)", jr_unknown.journal_reputation == 50 and jr_unknown.journal_tier == "standard")
    jr_blank = journal_reputation("")
    check("blank journal neutral, not zero", jr_blank.journal_reputation == 50)
    check("journal has rationale", bool(jr_nejm.journal_rationale))

    # --- ranking venue axis ---
    check("rank weights sum to 1.0", abs(sum(RANK_WEIGHTS.values()) - 1.0) < 1e-9)
    check("directness+quality dominant", RANK_WEIGHTS["directness"] + RANK_WEIGHTS["quality"] >= 0.55)
    check("venue axis present and small", 0 < RANK_WEIGHTS["venue"] <= 0.10)
    base_ev = human_rct(); base_ev.update(assess_reliability(base_ev, paper).to_dict())
    hi = dict(base_ev); hi["journal"] = "New England Journal of Medicine"
    lo = dict(base_ev); lo["journal"] = "Journal of Obscure Peptide Studies"
    check("flagship venue ranks >= unknown venue", compute_rank(hi).rank_score >= compute_rank(lo).rank_score)
    comps = json.loads(compute_rank(hi).rank_components)
    check("venue is an audited rank component", "venue" in comps)

    # --- new literature-informed facets ---
    inc_paper = {
        "pmid": "555",
        "title": "Once-weekly survodutide, a GLP-1/glucagon dual agonist, in postmenopausal women with obesity",
        "abstract": "This oral peptide long-acting formulation significantly improved body weight in older adults. "
                    "Both male and female participants were enrolled.",
    }
    inc_ev = human_rct(); inc_ev["molecule_name"] = "Survodutide"
    fr_inc = derive_facets(inc_ev, inc_paper)
    check("drug_class glp1 facet", "glp1_agonist" in fr_inc.wide.get("facet_drug_class", ""))
    check("drug_class glucagon facet", "glucagon_agonist" in fr_inc.wide.get("facet_drug_class", ""))
    check("population older_adults facet", "older_adults" in fr_inc.wide.get("facet_population", ""))
    check("formulation long_acting facet", "long_acting" in fr_inc.wide.get("facet_formulation", ""))
    check("formulation oral_peptide facet", "oral_peptide" in fr_inc.wide.get("facet_formulation", ""))
    check("evidence_direction positive facet", "positive" in fr_inc.wide.get("facet_evidence_direction", ""))
    check("sex both facet", "both_sexes" in fr_inc.wide.get("facet_sex", "") or "female" in fr_inc.wide.get("facet_sex", ""))

    # --- Semantic Scholar extractors ---
    s2 = {
        "citationCount": 42,
        "influentialCitationCount": 7,
        "venue": "Nature Medicine",
        "authors": [
            {"name": "Jane Doe", "authorId": "123", "url": "https://www.semanticscholar.org/author/123"},
            {"name": "John Roe", "authorId": "456"},
        ],
    }
    check("s2 citation count", semanticscholar_citation_count(s2) == 42)
    check("s2 influential count", semanticscholar_influential_count(s2) == 7)
    s2_auth = semanticscholar_authors(s2)
    check("s2 authors parsed", len(s2_auth) == 2 and s2_auth[0]["name"] == "Jane Doe")
    check("s2 author url filled from id", s2_auth[1]["url"].endswith("/author/456"))
    check("s2 empty on non-record", semanticscholar_citation_count(None) is None and semanticscholar_authors(None) == [])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
