#!/usr/bin/env python3
"""Offline unit tests for the curation layer (facets / strength / publication / appraisal).

No network, no SQLite needed. Run:

    python3 tests/test_curation.py
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load the build_curated_database script by path (scripts/ is not a package) so
# we can unit-test its _corpus_stats / _publication_flags helpers directly.
_BCD_SPEC = importlib.util.spec_from_file_location(
    "build_curated_database",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "build_curated_database.py"),
)
bcd = importlib.util.module_from_spec(_BCD_SPEC)
sys.modules["build_curated_database"] = bcd
_BCD_SPEC.loader.exec_module(bcd)  # type: ignore

# Same trick for the curated-dataset validator so we can unit-test its
# corpus-collapse anomaly gates directly.
_VC_SPEC = importlib.util.spec_from_file_location(
    "validate_curated",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "validate_curated.py"),
)
vc = importlib.util.module_from_spec(_VC_SPEC)
sys.modules["validate_curated"] = vc
_VC_SPEC.loader.exec_module(vc)  # type: ignore

import tempfile

from retarats_pipeline.curation.facets import derive_facets, FACET_GROUPS
from retarats_pipeline.curation.reliability import assess_reliability, classify_evidence, CLASS_DIRECTNESS
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


def _make_curated_dir(tmpdir, stats):
    """Write a tiny-but-valid curated_evidence.csv + corpus_stats.json.

    The CSV satisfies every existing invariant (scores, statuses, unique ids,
    species vocab, a sane auto_publish band) so the only thing a test can trip is
    the corpus-collapse anomaly gate.
    """
    fields = [
        "evidence_id", "reliability_score", "reliability_tier", "publication_status",
        "required_fields_present", "website_section", "facet_species", "auto_publish_eligible",
        "rank_score", "pmid", "molecule_id",
    ]
    rows = [
        {"evidence_id": "e1", "reliability_score": "80", "reliability_tier": "high",
         "publication_status": "featured", "required_fields_present": "True",
         "website_section": "Human evidence", "facet_species": "human",
         "auto_publish_eligible": "True", "rank_score": "90", "pmid": "1", "molecule_id": "m1"},
        {"evidence_id": "e2", "reliability_score": "70", "reliability_tier": "moderate",
         "publication_status": "listed", "required_fields_present": "True",
         "website_section": "", "facet_species": "mouse",
         "auto_publish_eligible": "False", "rank_score": "60", "pmid": "2", "molecule_id": "m1"},
    ]
    with open(os.path.join(tmpdir, "curated_evidence.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(tmpdir, "corpus_stats.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh)


def run_anomaly_gate_tests():
    # Current run's stats (a healthy corpus).
    current = {"total_papers": 100, "molecules_with_data": 20, "featured": 10, "listed": 30}

    # 1) baseline showing a 90% total_papers drop -> catastrophic collapse -> FAIL.
    with tempfile.TemporaryDirectory() as d:
        _make_curated_dir(d, current)
        base_path = os.path.join(d, "baseline.json")
        with open(base_path, "w", encoding="utf-8") as fh:
            json.dump({"total_papers": 1000, "molecules_with_data": 20,
                       "featured": 10, "listed": 30}, fh)
        _rep, code = vc.validate(d, baseline_path=base_path)
        check("90% total_papers drop FAILS (non-zero)", code != 0)

    # 2) normal / growth baseline -> PASS (growth must never fail).
    with tempfile.TemporaryDirectory() as d:
        _make_curated_dir(d, current)
        base_path = os.path.join(d, "baseline.json")
        with open(base_path, "w", encoding="utf-8") as fh:
            json.dump({"total_papers": 90, "molecules_with_data": 18,
                       "featured": 8, "listed": 25}, fh)
        _rep, code = vc.validate(d, baseline_path=base_path)
        check("growth baseline PASSES (zero)", code == 0)

    # 3) no baseline (bootstrapping first run) -> PASS.
    with tempfile.TemporaryDirectory() as d:
        _make_curated_dir(d, current)
        _rep, code = vc.validate(d)
        check("no baseline PASSES (zero)", code == 0)

    # 4) baseline path that doesn't exist -> skipped, PASS (bootstrapping).
    with tempfile.TemporaryDirectory() as d:
        _make_curated_dir(d, current)
        _rep, code = vc.validate(d, baseline_path=os.path.join(d, "nope.json"))
        check("missing baseline file PASSES (zero)", code == 0)


def run_rigor_negation_tests():
    # Trust risk #2: rigor scoring must ignore negated / other-study design terms.
    from retarats_pipeline.curation.reliability import _score_human, _score_invivo
    genuine = _score_human({"comparator_or_control": "placebo"}, "human_clinical",
                           "methods: a randomized, double-blind, placebo-controlled trial. results: improved.")[1]
    check("genuine RCT credits blinding", genuine.get("blinding") == 8)
    check("genuine RCT credits randomization", genuine.get("randomization") == 6)

    open_label = _score_human({}, "human_clinical",
                              "methods: an open-label study, unlike double-blind trials, enrolled adults. results: ok.")[1]
    check("open-label citing double-blind earns NO blinding", open_label.get("blinding", 0) == 0)

    negated = _score_human({}, "human_clinical",
                           "methods: this was not randomized and not blinded. results: ok.")[1]
    check("not-randomized earns no randomization", not negated.get("randomization"))
    check("not-blinded earns no blinding", negated.get("blinding", 0) == 0)

    invivo = _score_invivo({}, "methods: mice were randomized with a vehicle control group. results: survival improved.")[1]
    check("in vivo genuine randomization credited", invivo.get("randomization") == 8)
    check("in vivo genuine controls credited", invivo.get("controls") == 8)

    # _has_method must not credit a method that's part of a bigger word or cited.
    from retarats_pipeline.curation.reliability import _has_method
    check("'unblinded' does not credit blinding (word boundary)",
          _has_method("this was an unblinded study", "blind", "blinded") is False)
    check("'in contrast to randomized trials' does not credit randomization",
          _has_method("in contrast to randomized trials, this cohort study", "randomi") is False)
    check("genuine 'double-blind' still credits", _has_method("a double-blind design", "double-blind", "blind") is True)


def run_pipeline_robustness_tests():
    import sqlite3
    bcd._LOAD_DROPPED.clear()
    conn = sqlite3.connect(":memory:")
    conn.execute("create table evidence(payload_json text)")
    conn.executemany("insert into evidence values(?)",
                     [('{"a":1}',), ("NOT JSON",), ('{"b":2}',)])
    rows = bcd.load_payload_table(conn, "evidence")
    check("valid payload rows parsed", len(rows) == 2)
    check("unparseable rows counted (schema-drift canary)", bcd._LOAD_DROPPED.get("evidence") == 1)
    conn.close()

    # corpus_stats carries the provenance stamp + dropped-row canary.
    stats = bcd._corpus_stats([{"molecule_id": "m", "pub_year": 2020}], [{"pmid": "1"}], [{"pmid": "1"}])
    for key in ("build_sha", "corpus_fingerprint", "zenodo_doi", "dropped_payload_rows"):
        check(f"corpus_stats has {key}", key in stats)
    check("fingerprint is a short hex", len(stats["corpus_fingerprint"]) == 12)

    # pct_citations_filled counts only real positive counts ("0.0"/junk are NOT filled).
    cstats = bcd._corpus_stats(
        [{"citation_count": "0.0", "pub_year": 2020}, {"citation_count": "5", "pub_year": 2021},
         {"citation_count": "", "pub_year": 2022}], [{"pmid": "1"}], [{"pmid": "1"}])
    check("pct_citations excludes '0.0'/blank", cstats["pct_citations_filled"] == 33.3)

    # molecule_index dedups (pmid,molecule) and counts human via model_primary.
    mrecs = [
        {"molecule_id": "m", "molecule_name": "M", "pmid": "1", "model_primary": "human",
         "model_type": "animal", "rank_score": 10, "publication_status": "featured"},
        {"molecule_id": "m", "molecule_name": "M", "pmid": "1", "model_primary": "human",
         "model_type": "animal", "rank_score": 50, "publication_status": "featured"},
        {"molecule_id": "m", "molecule_name": "M", "pmid": "2", "model_primary": "human",
         "model_type": "animal", "rank_score": 30, "publication_status": "listed"},
    ]
    mi = bcd._molecule_index(mrecs, pubchem_by_mol={})[0]
    check("molecule_index dedups (pmid,molecule)", mi["total_records"] == 2)
    check("molecule_index human via model_primary", mi["human_count"] == 2)


def run_validation_gate_tests():
    # rank_score out of range FAILs validation.
    with tempfile.TemporaryDirectory() as d:
        _make_curated_dir(d, {"total_papers": 100, "molecules_with_data": 20, "featured": 10, "listed": 30})
        # corrupt one rank_score
        import csv as _csv
        p = os.path.join(d, "curated_evidence.csv")
        rows = list(_csv.DictReader(open(p, encoding="utf-8")))
        rows[0]["rank_score"] = "250"
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        _rep, code = vc.validate(d)
        check("rank_score out-of-range FAILs", code != 0)

    # Heavy payload-drop drift FAILs; a valid build passes.
    with tempfile.TemporaryDirectory() as d:
        _make_curated_dir(d, {"total_papers": 100, "molecules_with_data": 20, "featured": 10,
                              "listed": 30, "total_evidence": 1000, "dropped_payload_rows": 500})
        _rep, code = vc.validate(d)
        check("heavy schema-drift FAILs", code != 0)
    with tempfile.TemporaryDirectory() as d:
        _make_curated_dir(d, {"total_papers": 100, "molecules_with_data": 20, "featured": 10,
                              "listed": 30, "total_evidence": 1000, "dropped_payload_rows": 0})
        _rep, code = vc.validate(d)
        check("clean build passes drift check", code == 0)


def run_density_tests():
    # _density_tier boundaries (precedence: sparse -> saturated -> moderate).
    check("density 99 total -> sparse", bcd._density_tier(99, 50) == "sparse")
    check("density 100 total -> moderate", bcd._density_tier(100, 50) == "moderate")
    check("density 1001 total -> saturated", bcd._density_tier(1001, 50) == "saturated")
    check("density 9 human -> sparse", bcd._density_tier(500, 9) == "sparse")
    check("density 201 human -> saturated", bcd._density_tier(500, 201) == "saturated")
    check("density mid -> moderate", bcd._density_tier(500, 50) == "moderate")

    # Density-aware cap: a LOW-VOLUME molecule keeps ALL records (exempt), but a
    # high-volume preclinical molecule (thin on human) is still capped -- the
    # exemption is total-volume based, NOT tier=='sparse'.
    lowvol = [{"molecule_id": "a", "website_section": "Biomarkers",
               "model_type": "animal", "rank_score": i} for i in range(50)]
    kept, _ = bcd._cap_site_feed(lowvol, other_cap=10)
    check("low-volume molecule exempt from cap (all kept)", len(kept) == 50)

    highvol = [{"molecule_id": "b", "website_section": "Biomarkers",
                "model_type": "animal", "rank_score": i} for i in range(1200)]
    kept2, _ = bcd._cap_site_feed(highvol, other_cap=10)
    check("high-volume preclinical still capped (not exempt on human-sparse)", len(kept2) == 10)

    # Duplicate (pmid, molecule) records collapse to the highest-ranked one.
    dup_recs = [
        {"molecule_id": "m", "pmid": "111", "website_section": "Human evidence",
         "model_type": "human", "rank_score": 40},
        {"molecule_id": "m", "pmid": "111", "website_section": "Human evidence",
         "model_type": "human", "rank_score": 88},  # same paper+molecule, higher rank
        {"molecule_id": "m", "pmid": "222", "website_section": "Human evidence",
         "model_type": "human", "rank_score": 50},
    ]
    kept_d, _ = bcd._cap_site_feed(dup_recs)
    keys = sorted((r["pmid"], r["rank_score"]) for r in kept_d)
    check("duplicate paper+molecule collapsed to one", len(kept_d) == 2)
    check("dedup keeps the higher-ranked instance", ("111", 88) in keys and ("111", 40) not in keys)

    # molecule_index attaches the tier + counts.
    rows = [{"molecule_id": "m", "molecule_name": "Mol",
             "model_type": "human", "publication_status": "featured"} for _ in range(5)]
    idx = {r["molecule_id"]: r for r in bcd._molecule_index(rows, pubchem_by_mol={})}
    m = idx["m"]
    check("molecule_index has density_tier", m.get("density_tier") == "sparse")
    check("molecule_index has record_count", m.get("record_count") == 5)
    check("molecule_index has human_count", m.get("human_count") == 5)


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

    # --- iCite APT directness nudge (guarded, class-bounded) ---
    pre = nhp_preclinical()
    base_pre = assess_reliability(dict(pre), None)
    apt_hi = dict(pre); apt_hi["icite_apt"] = "1.0"
    boosted = assess_reliability(apt_hi, None)
    check("APT boosts preclinical directness", boosted.evidence_directness > base_pre.evidence_directness)
    check("APT boost is small/bounded (<= +8)", boosted.evidence_directness - base_pre.evidence_directness <= 8)
    check("APT-boosted preclinical still below human directness",
          boosted.evidence_directness < CLASS_DIRECTNESS["human_observational"])
    apt_lo = dict(pre); apt_lo["icite_apt"] = "0.0"
    lowered = assess_reliability(apt_lo, None)
    check("low APT reduces preclinical directness", lowered.evidence_directness < base_pre.evidence_directness)
    check("low APT penalty small/bounded (<= 4)", base_pre.evidence_directness - lowered.evidence_directness <= 4)
    # human RCT directness is authoritative -> APT must NOT touch it
    rct_apt = human_rct(); rct_apt["icite_apt"] = "1.0"
    check("APT does not touch human RCT directness",
          assess_reliability(rct_apt, paper).evidence_directness == rel.evidence_directness)

    # --- iCite is_clinical rescue for otherwise-"other" records ---
    bare = {"role_category": "", "primary_study_type": "", "model_type": "", "model_primary": ""}
    check("bare record classifies as other", classify_evidence(dict(bare)) == "other")
    check("is_clinical=Yes rescues to human_clinical",
          classify_evidence(dict(bare, icite_is_clinical="Yes")) == "human_clinical")
    check("is_clinical=1 rescues to human_clinical",
          classify_evidence(dict(bare, icite_is_clinical=1)) == "human_clinical")
    check("is_clinical=No stays other",
          classify_evidence(dict(bare, icite_is_clinical="No")) == "other")
    # must NOT override an already-resolved non-human class
    check("is_clinical does not override in_vitro",
          classify_evidence({"model_primary": "in vitro", "icite_is_clinical": "Yes"}) == "in_vitro")
    # rescued record is scored as human interventional, not zeroed
    rescued = dict(bare, icite_is_clinical="Yes")
    check("rescued clinical record scored > 0", assess_reliability(rescued, None).reliability_score > 0)

    # --- iCite impact + clinical-status facets ---
    fev = human_rct(); fev["icite_nih_percentile"] = "95"; fev["icite_is_clinical"] = "Yes"
    ff = derive_facets(fev, paper)
    check("evidence_impact top_decile bucket", "top_decile" in ff.wide.get("facet_evidence_impact", ""))
    check("clinical_article yes", "yes" in ff.wide.get("facet_clinical_article", ""))
    fev_hi = human_rct(); fev_hi["icite_nih_percentile"] = "80"
    check("evidence_impact high bucket", derive_facets(fev_hi, paper).wide.get("facet_evidence_impact", "") == "high")
    fev_ty = human_rct(); fev_ty["icite_nih_percentile"] = "40"; fev_ty["icite_is_clinical"] = "No"
    ff_ty = derive_facets(fev_ty, paper)
    check("evidence_impact typical bucket", ff_ty.wide.get("facet_evidence_impact", "") == "typical")
    check("clinical_article no", ff_ty.wide.get("facet_clinical_article", "") == "no")
    fev_lo = human_rct(); fev_lo["icite_nih_percentile"] = "10"
    check("evidence_impact low bucket", derive_facets(fev_lo, paper).wide.get("facet_evidence_impact", "") == "low")

    # --- iCite research-article facet ---
    fra_yes = human_rct(); fra_yes["icite_is_research_article"] = "Yes"
    check("research_article yes", derive_facets(fra_yes, paper).wide.get("facet_research_article", "") == "yes")
    fra_yes1 = human_rct(); fra_yes1["icite_is_research_article"] = 1
    check("research_article truthy 1", derive_facets(fra_yes1, paper).wide.get("facet_research_article", "") == "yes")
    fra_no = human_rct(); fra_no["icite_is_research_article"] = "No"
    check("research_article no", derive_facets(fra_no, paper).wide.get("facet_research_article", "") == "no")
    fra_blank = human_rct(); fra_blank["icite_is_research_article"] = ""
    check("research_article blank -> empty", derive_facets(fra_blank, paper).wide.get("facet_research_article", "") == "")

    # --- iCite translational-compartment facet (triangle fractions) ---
    ftc_h = human_rct(); ftc_h["icite_human"] = "0.8"; ftc_h["icite_animal"] = "0.1"; ftc_h["icite_molecular"] = "0.1"
    check("translational_compartment human dominant",
          derive_facets(ftc_h, paper).wide.get("facet_translational_compartment", "") == "human")
    ftc_a = human_rct(); ftc_a["icite_human"] = "0.2"; ftc_a["icite_animal"] = "0.7"; ftc_a["icite_molecular"] = "0.1"
    check("translational_compartment animal dominant",
          derive_facets(ftc_a, paper).wide.get("facet_translational_compartment", "") == "animal")
    ftc_m = human_rct(); ftc_m["icite_human"] = "0.1"; ftc_m["icite_animal"] = "0.4"; ftc_m["icite_molecular"] = "0.5"
    check("translational_compartment molecular dominant",
          derive_facets(ftc_m, paper).wide.get("facet_translational_compartment", "") == "molecular_cellular")
    ftc_amb = human_rct(); ftc_amb["icite_human"] = "0.4"; ftc_amb["icite_animal"] = "0.3"; ftc_amb["icite_molecular"] = "0.3"
    check("translational_compartment ambiguous (<0.5) -> empty",
          derive_facets(ftc_amb, paper).wide.get("facet_translational_compartment", "") == "")
    ftc_blank = human_rct()
    check("translational_compartment blank -> empty",
          derive_facets(ftc_blank, paper).wide.get("facet_translational_compartment", "") == "")

    # --- regression: a record with NO iCite fields behaves exactly as before ---
    r_base = assess_reliability(human_rct(), paper)
    check("no-iCite RCT class unchanged", r_base.evidence_class == "human_clinical_controlled")
    check("no-iCite RCT directness == class base",
          r_base.evidence_directness == CLASS_DIRECTNESS["human_clinical_controlled"])
    check("no-iCite preclinical directness == class base",
          assess_reliability(nhp_preclinical(), None).evidence_directness == CLASS_DIRECTNESS["preclinical_invivo"])
    check("no-iCite preclinical classify unchanged",
          classify_evidence(nhp_preclinical()) == "preclinical_invivo")
    f_base = derive_facets(human_rct(), paper)
    check("no-iCite -> empty evidence_impact facet", f_base.wide.get("facet_evidence_impact", "") == "")
    check("no-iCite -> empty clinical_article facet", f_base.wide.get("facet_clinical_article", "") == "")
    check("no-iCite -> empty research_article facet", f_base.wide.get("facet_research_article", "") == "")
    check("no-iCite -> empty translational_compartment facet",
          f_base.wide.get("facet_translational_compartment", "") == "")
    # A record with NO iCite fields emits no long rows for the new facets and keeps
    # its facet_count identical to before these facets existed.
    _base_count = int(f_base.wide.get("facet_count", "0"))
    _new_groups = {"research_article", "translational_compartment"}
    check("no-iCite -> no new-facet long rows",
          not any(g in _new_groups for (g, _v, _l, _s) in f_base.long))
    f_ra = derive_facets(dict(human_rct(), icite_is_research_article="Yes"), paper)
    check("research_article adds exactly one long row + bumps facet_count",
          int(f_ra.wide.get("facet_count", "0")) == _base_count + 1
          and sum(1 for (g, *_r) in f_ra.long if g == "research_article") == 1)

    # --- publication_flag facet (retraction / correction from PubMed pubtypes) ---
    base_paper = {"pmid": "700", "title": "A study", "abstract": "text"}
    retr_paper = dict(base_paper, pubtypes=["Journal Article", "Retracted Publication"])
    corr_paper = dict(base_paper, pubtypes=["Published Erratum"])
    ok_paper = dict(base_paper, pubtypes=["Journal Article"])
    fev = human_rct()
    check("publication_flag group emitted", "publication_flag" in FACET_GROUPS)
    check("retracted bucket", derive_facets(fev, retr_paper).wide.get("facet_publication_flag", "") == "retracted")
    check("corrected bucket", derive_facets(fev, corr_paper).wide.get("facet_publication_flag", "") == "corrected")
    check("no flag -> blank", derive_facets(fev, ok_paper).wide.get("facet_publication_flag", "") == "")
    check("blank pubtypes -> blank", derive_facets(fev, base_paper).wide.get("facet_publication_flag", "") == "")
    # "Retraction of Publication" (the pointer type) also flags retracted.
    check("retraction-of bucket",
          derive_facets(fev, dict(base_paper, pubtypes="Retraction of Publication")).wide.get("facet_publication_flag", "") == "retracted")
    # "Corrected and Republished Article" flags corrected.
    check("corrected-and-republished bucket",
          derive_facets(fev, dict(base_paper, pubtypes=["Corrected and Republished Article"])).wide.get("facet_publication_flag", "") == "corrected")
    # Retraction wins over a co-present correction type.
    check("retracted precedence over corrected",
          derive_facets(fev, dict(base_paper, pubtypes=["Published Erratum", "Retracted Publication"])).wide.get("facet_publication_flag", "") == "retracted")
    # A pre-derived is_retracted flag on the evidence row also lights it up.
    check("is_retracted flag fallback",
          derive_facets(dict(fev, is_retracted=True), base_paper).wide.get("facet_publication_flag", "") == "retracted")
    # No flag -> no long row for the group + no facet_count bump.
    _pf_base = int(derive_facets(fev, ok_paper).wide.get("facet_count", "0"))
    _pf_retr = derive_facets(fev, retr_paper)
    check("retracted adds exactly one long row",
          sum(1 for (g, *_r) in _pf_retr.long if g == "publication_flag") == 1
          and int(_pf_retr.wide.get("facet_count", "0")) == _pf_base + 1)

    # --- build_curated_database publication flags (paper -> row) ---
    check("pubtypes list retracted", bcd._publication_flags({"pubtypes": ["Retracted Publication"]}, {}) == (True, False))
    check("pubtypes string corrected", bcd._publication_flags({"pubtypes": "Published Erratum"}, {}) == (False, True))
    check("pubtypes ordinary -> neither", bcd._publication_flags({"pubtypes": ["Journal Article"]}, {}) == (False, False))
    check("pubtypes absent -> neither", bcd._publication_flags({}, {}) == (False, False))

    # --- corpus_stats data-health coverage keys ---
    cs_rows = [
        {"abstract": "a", "doi": "10.1/x", "icite_rcr": "1.2", "citation_count": "5",
         "pub_year": "2020", "molecule_id": "m", "publication_status": "featured"},
        {"abstract": "", "doi": "", "icite_apt": "", "citation_count": "0",
         "pub_year": "2021", "molecule_id": "m", "publication_status": "listed"},
    ]
    cs = bcd._corpus_stats(cs_rows, [1, 2, 3], [1, 2])
    for key in ("pct_with_abstract", "pct_with_doi", "pct_with_icite", "pct_citations_filled"):
        check("corpus_stats has " + key, key in cs)
    check("pct_with_abstract 50%", cs["pct_with_abstract"] == 50.0)
    check("pct_with_doi 50%", cs["pct_with_doi"] == 50.0)
    check("pct_with_icite counts rcr OR apt (50%)", cs["pct_with_icite"] == 50.0)
    check("pct_citations_filled reused (50%)", cs["pct_citations_filled"] == 50.0)
    # Empty corpus -> 0.0, no ZeroDivision.
    cs0 = bcd._corpus_stats([], [], [])
    check("empty corpus coverage is 0.0 (no crash)",
          cs0["pct_with_abstract"] == 0.0 and cs0["pct_with_doi"] == 0.0 and cs0["pct_with_icite"] == 0.0)

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

    # --- validate_curated corpus-collapse anomaly gates ---
    run_anomaly_gate_tests()

    # --- negation-aware rigor scoring (trust risk #2) ---
    run_rigor_negation_tests()

    # --- pipeline robustness: schema-drift counter + provenance stamp ---
    run_pipeline_robustness_tests()

    # --- validation gates: rank range + schema-drift enforcement ---
    run_validation_gate_tests()

    # --- evidence-density tiers + density-aware cap ---
    run_density_tests()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
