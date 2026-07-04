#!/usr/bin/env python3
"""Offline unit tests for the additive extraction-refinement layer.

Covers dose/route/duration/N parsing on crafted abstracts and the model
disambiguation rules. No network, no SQLite. Run:

    python3 tests/test_extractors.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.curation.extractors import (
    classify_outcome,
    disambiguate_model,
    parse_dose,
    parse_duration,
    parse_route,
    parse_sample_size,
    refine_extraction,
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


def run():
    # --- dose parsing ---
    check("dose mg", "10 mg" in parse_dose("Patients received 10 mg orally."))
    check("dose mg/kg", "0.5 mg/kg" in parse_dose("Mice were dosed at 0.5 mg/kg."))
    check("dose nmol", "300 nmol" in parse_dose("A 300 nmol bolus was given."))
    check("dose mg/kg/day", "2 mg/kg/day" in parse_dose("Treatment was 2 mg/kg/day for 4 weeks."))
    check("dose absent -> empty", parse_dose("No numeric dose here, just prose.") == "")

    # --- route parsing ---
    check("route oral", "oral" in parse_route("Given orally each morning."))
    check("route subcutaneous", "subcutaneous" in parse_route("Administered subcutaneously (s.c.)."))
    check("route iv", "intravenous" in parse_route("A single i.v. infusion."))
    check("route intranasal", "intranasal" in parse_route("Delivered intranasally to rats."))
    check("route absent -> empty", parse_route("No route mentioned at all.") == "")

    # --- duration parsing ---
    check("duration weeks", "12 weeks" in parse_duration("Treatment continued for 12 weeks."))
    check("duration hyphenated", parse_duration("An 8-week trial of the drug.") != "")
    check("duration months", "6 months" in parse_duration("Follow-up over 6 months."))
    # age distractor should NOT be picked up as a study duration
    check("duration ignores age", parse_duration("Adults aged 65 years old were enrolled.") == "")

    # --- sample size parsing ---
    disp, n = parse_sample_size("A total of n=338 patients were randomized.")
    check("N from n=", n == 338)
    disp2, n2 = parse_sample_size("We enrolled 120 participants across sites.")
    check("N from noun", n2 == 120)
    disp3, n3 = parse_sample_size("Arms had n=50 and n=48 subjects respectively.")
    check("N sums arms", n3 == 98 and "sum of arms" in disp3)
    disp4, n4 = parse_sample_size("The compound was dosed at 10 mg/kg.")
    check("N absent -> empty", n4 is None and disp4 == "")

    # --- outcome classification ---
    check(
        "outcome beneficial",
        classify_outcome({"efficacy_signal": "Body weight was significantly reduced."}, "") == "beneficial",
    )
    check(
        "outcome neutral",
        classify_outcome({"efficacy_signal": "There was no significant difference between groups."}, "")
        == "neutral",
    )
    check(
        "outcome harmful",
        classify_outcome({"safety_signal": "Serious adverse events and increased mortality were noted."}, "")
        == "harmful",
    )

    # --- disambiguation case 1: clinical study with cell mention => human ---
    ev_clin = {
        "human_flag": True,
        "animal_flag": False,
        "in_vitro_flag": True,
        "model_type": "human",
        "primary_study_type": "RCT",
        "species_or_population": "patients",
    }
    paper_clin = {
        "title": "A randomized controlled trial of drug X in patients with obesity",
        "abstract": "In a sub-study, drug X was also tested in HepG2 cell line cultures in vitro.",
    }
    mp, flags, conf, reason = disambiguate_model(ev_clin, paper_clin)
    check("clinical+cell => human", mp == "human")
    check("clinical flags list includes in_vitro", "in_vitro" in flags)
    check("clinical reason mentions incidental", "incidental" in reason or "outweigh" in reason)

    # --- disambiguation case 2: explicit animal species => animal ---
    ev_animal = {
        "human_flag": False,
        "animal_flag": True,
        "in_vitro_flag": False,
        "model_type": "animal",
        "primary_study_type": "Animal in vivo",
        "species_or_population": "mice",
    }
    paper_animal = {
        "title": "Effect of drug Y in a mouse model of MASH",
        "abstract": "C57BL/6 mice received drug Y; primary myotube cultures were also examined.",
    }
    mp2, flags2, conf2, reason2 = disambiguate_model(ev_animal, paper_animal)
    check("animal species => animal", mp2 == "animal")

    # --- disambiguation case 3: in-vitro only => in_vitro ---
    ev_vitro = {
        "human_flag": False,
        "animal_flag": False,
        "in_vitro_flag": True,
        "model_type": "in vitro",
        "primary_study_type": "In vitro study",
        "species_or_population": "cell line",
    }
    paper_vitro = {
        "title": "Drug Z modulates signaling in HEK293 cells",
        "abstract": "Cultured HEK293 cells were treated with drug Z in vitro.",
    }
    mp3, flags3, conf3, reason3 = disambiguate_model(ev_vitro, paper_vitro)
    check("in-vitro only => in_vitro", mp3 == "in_vitro")

    # --- disambiguation does NOT overwrite existing model_type, and refine_extraction is additive ---
    refined = refine_extraction(ev_clin, paper_clin)
    for key in [
        "refined_dose",
        "refined_route",
        "refined_duration",
        "refined_sample_size",
        "refined_n",
        "refined_outcome_direction",
        "model_primary",
        "model_flags",
        "model_confidence",
        "model_disambiguation_reason",
    ]:
        check(f"refine_extraction returns {key}", key in refined)
    check("refine_extraction does not touch model_type", "model_type" not in refined)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
