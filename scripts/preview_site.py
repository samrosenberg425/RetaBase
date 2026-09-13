#!/usr/bin/env python3
"""Build a self-contained PREVIEW of the site with representative sample data.

For eyeballing look-and-feel (themes, the regulatory panel, cards, the triangle)
WITHOUT needing the full corpus or a web server. Inline mode embeds the data, so the
output opens by double-click.

    python3 scripts/preview_site.py            # -> exports/preview.html, then open it
    python3 scripts/preview_site.py --open      # build and open it (macOS)

To preview the REAL site with the full corpus instead (needs data/retarats_pubmed.sqlite):
    python3 scripts/build_curated_database.py --db data/retarats_pubmed.sqlite --out-dir exports/curated
    python3 scripts/build_public_site.py --curated-dir exports/curated --out-dir exports/site --mode fetch
    cp exports/curated/site_data.json exports/site/            # + trials_data.json / preprints_data.json if built
    (cd exports/site && python3 -m http.server 8000)           # then open http://localhost:8000
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "build_public_site", os.path.join(ROOT, "scripts", "build_public_site.py"))
_bps = importlib.util.module_from_spec(_spec)
sys.modules["build_public_site"] = _bps
_spec.loader.exec_module(_bps)
from retarats_pipeline.curation.ontology import annotate


_HIER = {h["key"]: h for h in _bps._load_hierarchy()}


def _level_key(cls, study_type):
    """Map a sample record's class/study-type to an evidence-hierarchy ladder key."""
    st = (study_type or "").strip()
    if st in _HIER:
        return st
    alias = {"guideline": "clinical_practice_guideline", "observational": "observational_other",
             "in_vivo": "preclinical_invivo"}
    if st in alias:
        return alias[st]
    m = {"clinical_guideline": "clinical_practice_guideline", "evidence_synthesis": "systematic_review",
         "human_clinical_controlled": "rct", "human_clinical": "nonrandomized_trial",
         "human_observational": "observational_other", "preclinical_invivo": "preclinical_invivo",
         "in_vitro": "in_vitro", "narrative_review": "narrative_review"}
    return m.get(cls, "other")


def _record(mol_id, mol_name, i, cls, section, human, animal, molec, **extra):
    r = {
        "molecule_id": mol_id, "molecule_name": mol_name, "pmid": str(40000000 + i),
        "doi": f"10.1000/demo.{i}", "title": f"{mol_name}: {extra.pop('t', 'a study of effects')}",
        "journal": extra.pop("journal", "Journal of Demonstration Medicine"),
        "pub_year": str(extra.pop("year", 2024)),
        "authors_short": "Doe J; Smith A; Lee K et al.", "first_author": "Doe J", "author_count": "6",
        "citation_count": str(extra.pop("cites", 12)),
        "website_section": section, "evidence_class": cls,
        "evidence_class_label": extra.pop("label", cls.replace("_", " ").title()),
        "publication_status": extra.pop("status", "featured"),
        "reliability_score": str(extra.pop("rel", 70)), "reliability_tier": extra.pop("rtier", "high"),
        "evidence_directness": str(extra.pop("dir", 80)), "directness_tier": extra.pop("dtier", "high"),
        "rank_score": str(extra.pop("rank", 85)), "rank_tier": "high",
        "refined_dose": extra.pop("dose", "5 mg once weekly"), "refined_route": extra.pop("route", "subcutaneous"),
        "refined_duration": extra.pop("dur", "24 weeks"), "refined_sample_size": extra.pop("n", "n=120"),
        "refined_outcome_direction": extra.pop("outcome", "beneficial"),
        "icite_human": str(human), "icite_animal": str(animal), "icite_molecular": str(molec),
        "icite_nih_percentile": str(extra.pop("pct", 88)), "icite_rcr": str(extra.pop("rcr", 2.4)),
        # Full facet set so the preview cards show the same tag richness as the real
        # corpus (species, indication, endpoint, study type, model, route, drug class,
        # population, sex, formulation, evidence direction). Each is overridable per record.
        "facet_indication": extra.pop("indication", "obesity_weight; diabetes_glycemic; cardiovascular"),
        "facet_species": extra.pop("species", "human"),
        "facet_endpoint": extra.pop("endpoint", "body_weight; blood_pressure; glycemic_control; safety_tolerability"),
        "facet_study_type": extra.pop("study_type", "rct"),
        "facet_model_system": extra.pop("model_system", "human"),
        "facet_route": extra.pop("facet_route", "subcutaneous"),
        "facet_drug_class": extra.pop("drug_class", "glp1_agonist; amylin_analog"),
        "facet_population": extra.pop("population", "comorbid_metabolic"),
        "facet_sex": extra.pop("sex", "female; male"),
        "facet_formulation": extra.pop("formulation", "long_acting"),
        "facet_evidence_direction": extra.pop("evidence_direction", "positive"),
        "facet_all": f"{mol_name} {section}".lower(),
    }
    r.update(extra)
    # Preview titles are synthetic; exercise the same ontology path as the corpus.
    o, _ = annotate(dict(r, model_type="human" if human == 1 else "animal" if animal == 1 else "in vitro",
                         primary_study_type=r["facet_study_type"].replace("_", " ")))
    r.update(o)
    # Evidence-hierarchy level (so the L# badge + hierarchy-first sort work in preview).
    _lk = _level_key(cls, r.get("facet_study_type", ""))
    _h = _HIER.get(_lk, {"rank": 99, "label": "Other / unclear", "short": "Other"})
    r["evidence_level_key"] = _lk
    r["evidence_level_rank"] = str(_h["rank"])
    r["evidence_level_label"] = _h["label"]
    r["evidence_level_short"] = _h["short"]
    return r


def sample_feed():
    records = []
    i = 0
    plans = [
        ("retatrutide", "Retatrutide", [
            ("clinical_guideline", "Human evidence", 1, 0, 0, {"t": "consensus report on incretin-based obesity pharmacotherapy", "rel": 0, "rtier": "not_applicable", "dir": 92, "dtier": "high", "label": "Clinical practice guideline", "rank": 93, "dose": "", "study_type": "guideline", "drug_class": "glp1_agonist; gip_agonist; glucagon_agonist"}),
            ("human_clinical_controlled", "Human evidence", 1, 0, 0, {"t": "phase 2 RCT in obesity", "rel": 82, "rank": 94, "drug_class": "glp1_agonist; gip_agonist; glucagon_agonist", "endpoint": "body_weight; glycemic_control; safety_tolerability"}),
            ("human_observational", "Human evidence", 0.9, 0.1, 0, {"t": "real-world cohort", "rel": 55, "rank": 70, "study_type": "observational", "sex": "female", "population": "older_adults", "indication": "obesity_weight; cardiovascular"}),
            ("evidence_synthesis", "Reviews and overviews", 1, 0, 0, {"t": "systematic review and meta-analysis", "n": "12 studies; 4,530 participants", "dose": "", "rank": 90, "study_type": "systematic_review"}),
            ("in_vivo", "Mechanisms and pathways", 0, 1, 0, {"t": "mouse model of diet-induced obesity", "species": "mouse", "model_system": "mouse", "study_type": "in_vivo", "dir": 40, "dtier": "low", "route": "intraperitoneal", "facet_route": "intraperitoneal", "dose": "10 mg/kg", "sex": "male", "population": "", "indication": "obesity_weight", "endpoint": "body_weight", "drug_class": "glp1_agonist; gip_agonist", "formulation": ""}),
        ]),
        ("metformin", "Metformin", [
            ("human_clinical_controlled", "Human evidence", 1, 0, 0, {"t": "randomized trial in type 2 diabetes", "dose": "1000 mg twice daily", "rel": 78, "drug_class": "", "indication": "diabetes_glycemic", "endpoint": "glycemic_control; body_weight", "route": "oral", "facet_route": "oral", "formulation": ""}),
            ("in_vitro", "Mechanisms and pathways", 0, 0, 1, {"t": "AMPK activation in hepatocytes", "species": "cell_line", "model_system": "in_vitro", "study_type": "in_vitro", "dir": 20, "dtier": "low", "dose": "", "sex": "", "population": "", "indication": "diabetes_glycemic", "endpoint": "mitochondrial_function", "drug_class": "", "formulation": ""}),
        ]),
        ("tirzepatide", "Tirzepatide", [
            ("human_clinical_controlled", "Human evidence", 1, 0, 0, {"t": "SURPASS trial", "rel": 88, "rank": 96, "is_retracted": "", "cites": 210, "pct": 97, "drug_class": "glp1_agonist; gip_agonist", "endpoint": "glycemic_control; body_weight; safety_tolerability"}),
        ]),
        ("glutathione", "Glutathione", [
            ("in_vitro", "Mechanisms and pathways", 0, 0, 1, {"t": "antioxidant assay", "species": "cell_line", "model_system": "in_vitro", "study_type": "in_vitro", "dir": 15, "dtier": "low", "dose": "", "rank": 40, "sex": "", "population": "", "indication": "aging_longevity", "endpoint": "oxidative_stress", "drug_class": "", "formulation": ""}),
        ]),
        ("selank", "Selank", [
            ("in_vivo", "Behavioural", 0, 1, 0, {"t": "anxiolytic effects in rats", "species": "rat", "model_system": "rat", "study_type": "in_vivo", "dir": 35, "dtier": "low", "route": "intranasal", "facet_route": "", "dose": "300 µg/kg", "rank": 45, "sex": "male", "population": "", "indication": "neurocognitive", "endpoint": "safety_tolerability", "drug_class": "peptide_hormone", "formulation": ""}),
        ]),
    ]
    for mol_id, mol_name, recs in plans:
        for cls, section, h, a, m, extra in recs:
            i += 1
            records.append(_record(mol_id, mol_name, i, cls, section, h, a, m, **extra))

    # --- bulk generator: many more molecules + records for a realistic feed -----
    # Deterministic (index-driven, no randomness) so builds are reproducible. All
    # facet values are drawn from the real vocabulary (config/FACETS.csv).
    IND = ["obesity_weight", "diabetes_glycemic", "cardiovascular", "liver_mash", "kidney",
           "neurocognitive", "musculoskeletal", "inflammation_autoimmune", "aging_longevity", "oncology"]
    END = ["body_weight", "glycemic_control", "lipids", "blood_pressure", "liver_histology",
           "inflammation_markers", "oxidative_stress", "mitochondrial_function", "muscle_performance",
           "mortality_survival", "pharmacokinetics"]
    SEX = ["female; male", "female", "male", "both_sexes"]
    POP = ["comorbid_metabolic", "older_adults", "comorbid_cardiorenal", "pediatric", "women_pregnancy", "male"]
    ROUTE = ["subcutaneous", "oral", "intravenous", "intraperitoneal", "topical", "inhaled"]
    FORM = ["long_acting", "oral_peptide", "nanoparticle", ""]
    # (id, name, drug_class, primary indication, regulatory_status, us_marketed, access)
    catalog = [
        ("semaglutide", "Semaglutide", "glp1_agonist", "obesity_weight", "approved", "True", "physician-prescribed"),
        ("cagrilintide", "Cagrilintide", "amylin_analog", "obesity_weight", "investigational", "False", "clinical-trial-only"),
        ("survodutide", "Survodutide", "glucagon_agonist", "liver_mash", "investigational", "False", "clinical-trial-only"),
        ("dulaglutide", "Dulaglutide", "glp1_agonist", "diabetes_glycemic", "approved", "True", "physician-prescribed"),
        ("empagliflozin", "Empagliflozin", "sglt2_inhibitor", "cardiovascular", "approved", "True", "physician-prescribed"),
        ("nmn", "NMN", "nad_precursor", "aging_longevity", "supplement", "True", "OTC"),
        ("rapamycin", "Rapamycin", "mtor_inhibitor", "aging_longevity", "approved", "True", "physician-prescribed; off-label"),
        ("bpc157", "BPC-157", "peptide_hormone", "injury_repair", "research-only", "False", "research use only; grey market"),
        ("motsc", "MOTS-c", "mito_err_agonist", "aging_longevity", "research-only", "False", "research use only; grey market"),
        ("semax", "Semax", "peptide_hormone", "neurocognitive", "research-only", "False", "research use only; grey market"),
        ("epithalon", "Epithalon", "peptide_hormone", "aging_longevity", "research-only", "False", "research use only; grey market"),
        ("follistatin", "Follistatin", "myostatin_inhibitor", "musculoskeletal", "research-only", "False", "research use only; grey market"),
    ]
    # (class, section, species, model, study_type, rigor, directness, (human,animal,molec))
    # Spans the pyramid: systematic review > meta-analysis > guideline > RCT > cohort >
    # case-control > case report > narrative review > preclinical > in vitro.
    tiers = [
        ("evidence_synthesis", "Reviews and overviews", "human", "human", "systematic_review", 82, 90, (1, 0, 0)),
        ("evidence_synthesis", "Reviews and overviews", "human", "human", "meta_analysis", 78, 90, (1, 0, 0)),
        ("clinical_guideline", "Human evidence", "human", "human", "guideline", 0, 92, (1, 0, 0)),
        ("human_clinical_controlled", "Human evidence", "human", "human", "rct", 84, 95, (1, 0, 0)),
        ("human_observational", "Human evidence", "human", "human", "cohort", 62, 66, (0.9, 0.1, 0)),
        ("human_observational", "Human evidence", "human", "human", "case_control", 52, 66, (0.9, 0.1, 0)),
        ("human_observational", "Human evidence", "human", "human", "case_report", 40, 66, (0.8, 0.2, 0)),
        ("narrative_review", "Background and context", "human", "human", "narrative_review", 40, 42, (0.8, 0.2, 0)),
        ("preclinical_invivo", "Mechanisms and pathways", "mouse", "mouse", "in_vivo", 60, 45, (0, 1, 0)),
        ("in_vitro", "Mechanisms and pathways", "cell_line", "in_vitro", "in_vitro", 45, 25, (0, 0, 1)),
    ]
    titles = {
        "systematic_review": "systematic review",
        "meta_analysis": "meta-analysis",
        "guideline": "clinical practice guideline",
        "rct": "phase 3 randomized trial",
        "cohort": "prospective cohort study",
        "case_control": "case-control study",
        "case_report": "case report",
        "narrative_review": "narrative review",
        "in_vivo": "preclinical study in mice",
        "in_vitro": "mechanistic cell study",
    }
    for mi, (mol_id, name, dc, ind, reg, usm, access) in enumerate(catalog):
        nrec = 3 + (mi % 4)  # 3..6 records per molecule
        for k in range(nrec):
            cls, section, species, model, stype, rig, dirn, tri = tiers[(mi + k) % len(tiers)]
            is_guide = cls == "clinical_guideline"
            i += 1
            rank = max(20, min(97, int(0.30 * dirn + 0.28 * rig + 18 + ((mi * 7 + k * 13) % 22))))
            rtier = "not_applicable" if is_guide else ("high" if rig >= 70 else "moderate" if rig >= 50 else "limited")
            extra = {
                "t": titles.get(stype, cls.replace("_", " ")) + " in " + ind.replace("_", " "),
                "year": 2019 + ((mi + k) % 7),
                "rel": 0 if is_guide else rig, "rtier": rtier,
                "dir": dirn, "dtier": "high" if dirn >= 80 else "moderate" if dirn >= 55 else "low",
                "rank": rank, "rank_tier": "high" if rank >= 70 else "moderate" if rank >= 50 else "limited",
                "species": species, "model_system": model, "study_type": stype,
                "indication": ind + "; " + IND[(mi + k) % len(IND)],
                "endpoint": END[(mi + k) % len(END)] + "; " + END[(mi + k + 3) % len(END)] + "; safety_tolerability",
                "drug_class": dc,
                "sex": "" if species == "cell_line" else SEX[(mi + k) % len(SEX)],
                "population": "" if species != "human" else POP[(mi + k) % len(POP)],
                "facet_route": "" if species == "cell_line" else ROUTE[(mi + k) % len(ROUTE)],
                "formulation": FORM[(mi + k) % len(FORM)] if species == "human" else "",
                "evidence_direction": ["positive", "positive", "mixed", "null"][(mi + k) % 4],
                "pct": 37 + (mi * 5 + k * 11) % 60, "rcr": round(0.5 + ((mi + k) % 40) / 8.0, 1),
                "cites": (mi * 37 + k * 17) % 320,
                "label": "Clinical practice guideline" if is_guide else cls.replace("_", " ").title(),
                "status": "featured" if (dirn >= 80 and rig >= 50) or is_guide or cls == "evidence_synthesis" else "listed",
                "dose": "" if (is_guide or cls in ("evidence_synthesis", "narrative_review")) else "5 mg once weekly",
            }
            records.append(_record(mol_id, name, i, cls, section, tri[0], tri[1], tri[2], **extra))

    # --- molecule cards aggregated from the records + regulatory demo data ------
    from collections import defaultdict
    reg_extra = {
        "retatrutide": {"regulatory_status": "investigational", "us_marketed": "False",
                        "access_pathways": "clinical-trial-only; research use only; grey market",
                        "max_trial_phase": "Phase 3", "trial_count": "18", "ongoing_trial_count": "9",
                        "reg_source": "curated demo", "reg_source_url": "https://clinicaltrials.gov/",
                        "reg_retrieved_utc": "2026-08-15"},
        "metformin": {"regulatory_status": "approved", "fda_approved_indications": "type 2 diabetes mellitus",
                      "us_marketed": "True", "access_pathways": "physician-prescribed",
                      "reg_source": "FDA Drugs@FDA", "reg_retrieved_utc": "2026-08-15"},
        "tirzepatide": {"regulatory_status": "approved", "fda_approved_indications": "type 2 diabetes; chronic weight management",
                        "us_marketed": "True", "access_pathways": "physician-prescribed; compounding pharmacy (503A/503B)",
                        "reg_source": "FDA Drugs@FDA", "reg_retrieved_utc": "2026-08-15"},
        "glutathione": {"regulatory_status": "supplement", "access_pathways": "OTC", "reg_source": "curated demo",
                        "reg_retrieved_utc": "2026-08-15"},
        "selank": {"regulatory_status": "research-only", "access_pathways": "research use only; grey market",
                   "reg_source": "curated demo", "reg_retrieved_utc": "2026-08-15"},
    }
    for (mid, nm, dc, ind, reg, usm, access) in catalog:
        reg_extra[mid] = {"regulatory_status": reg, "us_marketed": usm, "access_pathways": access,
                          "reg_source": "curated demo", "reg_retrieved_utc": "2026-08-15"}
    bymol = defaultdict(list)
    for r in records:
        bymol[r["molecule_id"]].append(r)
    molecules = []
    for mid, rs in bymol.items():
        human = sum(1 for r in rs if r.get("facet_species") == "human")
        featured = sum(1 for r in rs if r.get("publication_status") == "featured")
        maxrel = max((int(r.get("reliability_score") or 0) for r in rs), default=0)
        n = len(rs)
        mol = {"molecule_id": mid, "molecule_name": rs[0]["molecule_name"],
               "total_records": str(n), "record_count": str(n), "human_count": str(human),
               "human_evidence": str(human), "preclinical_evidence": str(n - human),
               "auto_published": str(featured), "max_reliability": str(maxrel),
               "density_tier": "saturated" if n >= 6 else "moderate" if n >= 3 else "sparse"}
        mol.update(reg_extra.get(mid, {}))
        molecules.append(mol)
    # Full-corpus breakdowns for the home figures (computed from the sample records).
    from collections import Counter as _Ctr
    _lvl = {}
    for r in records:
        lab = (r.get("evidence_level_short") or r.get("evidence_level_label") or "Other").strip() or "Other"
        rk = int(r.get("evidence_level_rank") or 99)
        _lvl[(rk, lab)] = _lvl.get((rk, lab), 0) + 1
    by_level = [{"rank": rk, "label": lab, "count": c} for (rk, lab), c in sorted(_lvl.items())]
    _yrs = [int(r.get("pub_year")) for r in records if str(r.get("pub_year", "")).isdigit()]
    _yc = _Ctr(_yrs)
    by_year = [{"year": y, "count": _yc[y]} for y in sorted(_yc)]
    featured = sum(1 for r in records if r.get("publication_status") == "featured")

    def _multi(field):
        c = _Ctr()
        for r in records:
            for tok in str(r.get(field, "") or "").split(";"):
                tok = tok.strip()
                if tok:
                    c[tok] += 1
        return c
    _indc = _multi("facet_indication")
    by_indication = [{"label": k, "count": v} for k, v in _indc.most_common(12)]  # raw key; client polishes
    n_indications = len(_indc)

    _DOSING = {"human_clinical_controlled", "human_clinical", "preclinical_invivo", "in_vitro"}
    _NSAMPLE = {"human_clinical_controlled", "human_clinical", "human_observational", "preclinical_invivo"}
    _HUMANC = {"human_clinical_controlled", "human_clinical", "human_observational"}
    _PRIMARY = {"human_clinical_controlled", "human_clinical", "human_observational", "preclinical_invivo", "in_vitro"}

    def _cov(field, applies):
        denom = [r for r in records if applies is None or str(r.get("evidence_class", "") or "").strip() in applies]
        if not denom:
            return None
        present = sum(1 for r in denom if str(r.get(field, "") or "").strip())
        return {"label": "", "pct": round(100.0 * present / len(denom), 1), "present": present,
                "applicable": len(denom), "scoped": applies is not None}
    completeness = []
    for lab, fld, ap in [("Abstract", "title", None), ("Study design", "evidence_class", None),
                          ("Sample size", "refined_sample_size", _NSAMPLE), ("Dose", "refined_dose", _DOSING),
                          ("Route", "refined_route", _DOSING), ("Duration", "refined_duration", _DOSING),
                          ("Outcome", "refined_outcome_direction", _PRIMARY), ("Population", "facet_population", _HUMANC),
                          ("DOI", "doi", None)]:
        c = _cov(fld, ap)
        if c:
            c["label"] = lab
            completeness.append(c)
    completeness.append({"label": "Citation data", "pct": 71.0, "present": int(len(records) * 0.71),
                         "applicable": len(records), "scoped": False})
    _mod = _Ctr()
    for r in records:
        m = str(r.get("facet_model_system") or r.get("facet_species") or "").strip()
        if m:
            _mod[m] += 1
    by_model = [{"label": k, "count": v} for k, v in _mod.most_common(9)]
    _jr = _Ctr(str(r.get("journal", "") or "").strip() for r in records if str(r.get("journal", "") or "").strip())
    by_journal = [{"label": k, "count": v} for k, v in _jr.most_common(10)]
    return {
        "records": records, "molecules": molecules,
        "corpus_stats": {"total_papers": len(records), "total_evidence": len(records),
                         "molecules_with_data": len(molecules), "generated_utc": "2026-08-15T00:00:00Z",
                         "corpus_fingerprint": "previewsample", "build_sha": "local",
                         "zenodo_doi": "10.5281/zenodo.21207064", "pct_with_abstract": 96.0,
                         "pct_with_doi": 88.0, "pct_citations_filled": 71.0, "pct_with_icite": 64.0,
                         "year_min": min(_yrs) if _yrs else None, "year_max": max(_yrs) if _yrs else None,
                         "featured": featured, "by_level": by_level, "by_year": by_year,
                         "by_indication": by_indication, "n_indications": n_indications,
                         "by_model": by_model, "by_journal": by_journal, "completeness": completeness},
    }


# Stylistic variants to build with --all (label -> variant key; "" = base Clinical look).
VARIANTS = [
    ("clinical", ""),
    ("indigo", "indigo"),
    ("emerald", "emerald"),
    ("slate", "slate"),
    ("warm", "warm"),
]


def sample_trials():
    """A handful of registry trials so the Trials tab + filter sidebar can be tested."""
    def t(nct, mol, mname, title, status, phase, stype, cond, interv, n, start, comp, sponsor, results, ongoing):
        return {"nct_id": nct, "molecule_id": mol, "molecule_name": mname, "brief_title": title,
                "overall_status": status, "phases": phase, "study_type": stype, "conditions": cond,
                "interventions": interv, "enrollment_count": n, "start_date": start,
                "primary_completion_date": comp, "completion_date": comp, "lead_sponsor": sponsor,
                "has_results": results, "result_pmids": "", "reference_pmids": "",
                "url": "https://clinicaltrials.gov/study/" + nct, "ongoing": ongoing}
    return [
        t("NCT05000001", "retatrutide", "Retatrutide", "Retatrutide for Obesity (Phase 3, TRIUMPH-style)",
          "Recruiting", "Phase 3", "Interventional", "Obesity; Overweight", "Retatrutide; Placebo",
          "600", "2024-02-01", "2026-12-01", "Demo Pharma", "", True),
        t("NCT04000002", "retatrutide", "Retatrutide", "Retatrutide in Type 2 Diabetes",
          "Completed", "Phase 2", "Interventional", "Type 2 Diabetes", "Retatrutide; Placebo",
          "281", "2021-05-01", "2023-04-01", "Demo Pharma", "True", False),
        t("NCT05000003", "tirzepatide", "Tirzepatide", "Tirzepatide Cardiovascular Outcomes",
          "Active, not recruiting", "Phase 3", "Interventional", "Cardiovascular Disease; Obesity",
          "Tirzepatide; Placebo", "12000", "2022-09-01", "2027-06-01", "Demo Pharma", "", True),
        t("NCT03000004", "metformin", "Metformin", "Metformin for Prediabetes Prevention",
          "Completed", "Phase 4", "Interventional", "Prediabetes", "Metformin", "1500",
          "2018-01-01", "2022-01-01", "Academic Consortium", "True", False),
        t("NCT05000005", "selank", "Selank", "Selank Intranasal for Generalized Anxiety (pilot)",
          "Recruiting", "Phase 1", "Interventional", "Anxiety", "Selank", "40",
          "2025-01-01", "2026-03-01", "Demo Neuro", "", True),
        t("NCT05000006", "glutathione", "Glutathione", "Observational Glutathione Levels Study",
          "Enrolling by invitation", "N/A", "Observational", "Oxidative Stress", "None (observational)",
          "200", "2024-06-01", "2025-12-01", "Demo Labs", "", True),
    ]


def _googleform_feedback():
    """A feedback config in 'googleform' mode: the per-card report reroutes to a
    PREFILLED Google Form (one response row per report). The form_url + entry IDs are
    placeholders to swap for a real form; auto_open is off so the demo shows the built
    reroute URL instead of launching a 404. Reuses the real aspects/intro from config."""
    base = json.loads(json.dumps(_bps._load_feedback() or {}))
    base["submit"] = {
        "mode": "googleform",
        "form_url": "https://docs.google.com/forms/d/e/1FAIpQLSc_EXAMPLE_REPLACE_ME/viewform",
        "auto_open": False,  # demo: show the link rather than open a placeholder form
        "entry_map": {
            "report_id": "entry.1000000", "pmid": "entry.1000001", "molecule": "entry.1000002",
            "title": "entry.1000003", "aspects_text": "entry.1000004", "suggested": "entry.1000005",
            "note": "entry.1000006", "current_evidence_level": "entry.1000007",
            "current_rigor": "entry.1000008", "current_directness": "entry.1000009",
            "current_rank": "entry.1000010",
        },
    }
    base["thanks"] = ("Thanks — in production this opens a prefilled Google Form so you can "
                      "submit in one click. (Example form: replace the URL + entry IDs with your own.)")
    return base


def _build_one(out_path: str, variant: str, tag_hovers: bool = True, feedback_override=None) -> None:
    import shutil
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    _orig_fb = _bps._load_feedback
    if feedback_override is not None:
        _bps._load_feedback = lambda: feedback_override
    try:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as outdir:
            with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
                json.dump(sample_feed(), fh)
            with open(os.path.join(src, "trials_data.json"), "w", encoding="utf-8") as fh:
                json.dump({"generated_utc": "2026-08-15T00:00:00Z", "trials": sample_trials()}, fh)
            _bps.build_site(src, outdir, mode="inline", variant=variant, tag_hovers=tag_hovers)
            shutil.copy(os.path.join(outdir, "index.html"), out_path)
    finally:
        _bps._load_feedback = _orig_fb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join("exports", "preview.html"))
    ap.add_argument("--variant", default="", help="one of: indigo, emerald, slate, warm (blank = Clinical base)")
    ap.add_argument("--all", action="store_true", help="build all 5 stylistic variants -> exports/preview_<name>.html")
    ap.add_argument("--open", action="store_true", help="open the file afterward (macOS `open`)")
    args = ap.parse_args()

    if args.all:
        outdir = os.path.dirname(os.path.abspath(args.out)) or "."
        built = []
        for label, variant in VARIANTS:
            p = os.path.join(outdir, f"preview_{label}.html")
            _build_one(p, variant)
            built.append(p)
        print("Built 5 stylistic variants:")
        for p in built:
            print("  " + p)
        if args.open:
            try:
                subprocess.run(["open"] + built, check=False)
            except Exception:  # noqa: BLE001
                pass
        return

    # Default: build TWO versions so the tag-hover behaviour can be compared side by side.
    outdir = os.path.dirname(os.path.abspath(args.out)) or "."
    with_hovers = os.path.join(outdir, "preview_tags_hover.html")
    no_hovers = os.path.join(outdir, "preview_tags_plain.html")
    _build_one(with_hovers, args.variant, tag_hovers=True)
    _build_one(no_hovers, args.variant, tag_hovers=False)
    # Also refresh the plain preview.html (with hovers) for continuity.
    _build_one(args.out, args.variant, tag_hovers=True)
    # Feedback prototype: a second copy whose per-card "Report an issue" reroutes to a
    # prefilled Google Form (vs. the default preview.html, which uses the stub collector).
    gform = os.path.join(outdir, "preview_feedback_gform.html")
    _build_one(gform, args.variant, tag_hovers=True, feedback_override=_googleform_feedback())
    _fbmode = (_bps._load_feedback().get("submit", {}) or {}).get("mode", "stub")
    print("Built previews with sample data:")
    print("  " + with_hovers + "   (card tags show a definition on hover)")
    print("  " + no_hovers + "   (no tag hovers)")
    print("  " + args.out + "   (default = with hovers; feedback collector = " + _fbmode + ")")
    print("  " + gform + "   (feedback reroutes to a prefilled Google Form)")
    if args.open:
        try:
            subprocess.run(["open", args.out, gform], check=False)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
