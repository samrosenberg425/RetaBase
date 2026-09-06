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

    molecules = [
        {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "auto_published": "3",
         "total_records": "4", "record_count": "4", "human_count": "3", "density_tier": "moderate",
         "human_evidence": "3", "preclinical_evidence": "1", "max_reliability": "82",
         "top_conditions": "obesity_weight(3)",
         "regulatory_status": "investigational", "us_marketed": "False",
         "access_pathways": "clinical-trial-only; research use only; grey market",
         "max_trial_phase": "Phase 3", "trial_count": "18", "ongoing_trial_count": "9",
         "trial_stages_by_use": "obesity: Phase 3; type 2 diabetes: Phase 3",
         "reg_source": "curated demo", "reg_source_url": "https://clinicaltrials.gov/",
         "reg_retrieved_utc": "2026-08-15"},
        {"molecule_id": "metformin", "molecule_name": "Metformin", "auto_published": "1",
         "total_records": "2", "record_count": "2", "human_count": "1", "density_tier": "saturated",
         "human_evidence": "1", "preclinical_evidence": "1", "max_reliability": "78",
         "regulatory_status": "approved", "fda_approved_indications": "type 2 diabetes mellitus",
         "us_marketed": "True", "ex_us_status": "approved in EU; UK; Japan; and widely worldwide",
         "access_pathways": "physician-prescribed",
         "max_trial_phase": "Phase 4", "trial_count": "400",
         "trial_stages_by_use": "prediabetes: Phase 3; aging: Phase 2",
         "reg_source": "FDA Drugs@FDA", "reg_source_url": "https://www.accessdata.fda.gov/scripts/cder/daf/",
         "reg_retrieved_utc": "2026-08-15"},
        {"molecule_id": "tirzepatide", "molecule_name": "Tirzepatide", "auto_published": "1",
         "total_records": "1", "record_count": "1", "human_count": "1", "density_tier": "moderate",
         "human_evidence": "1", "max_reliability": "88",
         "regulatory_status": "approved", "fda_approved_indications": "type 2 diabetes; chronic weight management",
         "us_marketed": "True", "access_pathways": "physician-prescribed; compounding pharmacy (503A/503B)",
         "max_trial_phase": "Phase 3", "trial_stages_by_use": "heart failure: Phase 3; NASH: Phase 2",
         "reg_source": "FDA Drugs@FDA", "reg_source_url": "https://www.accessdata.fda.gov/scripts/cder/daf/",
         "reg_retrieved_utc": "2026-08-15"},
        {"molecule_id": "glutathione", "molecule_name": "Glutathione", "auto_published": "0",
         "total_records": "1", "record_count": "1", "human_count": "0", "density_tier": "sparse",
         "preclinical_evidence": "1", "max_reliability": "30",
         "regulatory_status": "supplement", "access_pathways": "OTC",
         "reg_source": "curated demo", "reg_retrieved_utc": "2026-08-15"},
        {"molecule_id": "selank", "molecule_name": "Selank", "auto_published": "0",
         "total_records": "1", "record_count": "1", "human_count": "0", "density_tier": "sparse",
         "preclinical_evidence": "1", "max_reliability": "35",
         "regulatory_status": "research-only", "access_pathways": "research use only; grey market",
         "reg_source": "curated demo", "reg_retrieved_utc": "2026-08-15"},
    ]
    return {
        "records": records, "molecules": molecules,
        "corpus_stats": {"total_papers": len(records), "total_evidence": len(records),
                         "molecules_with_data": len(molecules), "generated_utc": "2026-08-15T00:00:00Z",
                         "corpus_fingerprint": "previewsample", "build_sha": "local",
                         "zenodo_doi": "10.5281/zenodo.21207064", "pct_with_abstract": 100.0,
                         "pct_with_doi": 100.0, "pct_citations_filled": 100.0, "pct_with_icite": 100.0},
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


def _build_one(out_path: str, variant: str, tag_hovers: bool = True) -> None:
    import shutil
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as outdir:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(sample_feed(), fh)
        with open(os.path.join(src, "trials_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_utc": "2026-08-15T00:00:00Z", "trials": sample_trials()}, fh)
        _bps.build_site(src, outdir, mode="inline", variant=variant, tag_hovers=tag_hovers)
        shutil.copy(os.path.join(outdir, "index.html"), out_path)


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
    print("Built previews with sample data:")
    print("  " + with_hovers + "   (card tags show a definition on hover)")
    print("  " + no_hovers + "   (no tag hovers)")
    print("  " + args.out + "   (default = with hovers)")
    if args.open:
        try:
            subprocess.run(["open", with_hovers, no_hovers], check=False)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
