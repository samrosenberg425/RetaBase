#!/usr/bin/env python3
"""Offline unit tests for the additive extraction-refinement layer.

Covers dose/route/duration/N parsing on crafted abstracts and the model
disambiguation rules. No network, no SQLite. Run:

    python3 tests/test_extractors.py
"""

from __future__ import annotations

import os
import re
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

    # --- dose regression: middle-dot (U+00B7) decimals & no phantom bare-kg ---
    # BMI range with middle-dot decimals and "kg/m²" must not yield a "9 kg" dose.
    _bmi = parse_dose(
        "in participants aged 18-55 years with overweight or obesity "
        "(BMI 27·0-39·9 kg/m²). treated"
    )
    _bmi_toks = [t.strip() for t in _bmi.split(";") if t.strip()]
    check("dose BMI no phantom '9 kg'", "9 kg" not in _bmi)
    check(
        "dose BMI no bare-kg token",
        not any(re.fullmatch(r"\d+(?:[.·]\d+)?\s*kg", t) for t in _bmi_toks),
    )

    # Middle-dot doses parse whole (not split at the "·").
    _md = parse_dose("escalated from 0·3 mg to 60 mg once weekly then 1·25 mg")
    check("dose middle-dot 0·3 mg whole", "0·3 mg" in _md)
    check("dose middle-dot 60 mg present", "60 mg" in _md)
    check("dose middle-dot 1·25 mg whole", "1·25 mg" in _md)

    # Per-weight compound still works (kg retained only as /kg denominator).
    check("dose per-weight mg/kg retained", "mg/kg" in parse_dose("2 mg/kg/day"))

    # Plain units still parse.
    check("dose plain 5 mg", "5 mg" in parse_dose("5 mg"))
    check("dose plain 250 µg", "250 µg" in parse_dose("250 µg"))

    # Standalone body-weight kg no longer yields a dose.
    check("dose standalone body-weight kg -> none", parse_dose("lost 9 kg of body weight") == "")

    # Period-decimal still works.
    check("dose period-decimal 1.5 mg", "1.5 mg" in parse_dose("1.5 mg"))

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
    # Cohort-flow counts (same cohort at different stages) are NOT summed -> max.
    disp5, n5 = parse_sample_size("We enrolled n=50 participants; n=48 were analyzed.")
    check("enrolled/analyzed -> max, not summed", n5 == 50 and "sum of arms" not in disp5)
    # A flow cue overrides a stray "groups"/"vs" token (audit finding).
    _, n6 = parse_sample_size("We enrolled n=50 patients; n=48 completed. Two groups were compared.")
    check("flow cue overrides group word -> max", n6 == 50)
    # A genuine 2-arm RCT with no flow cue still sums.
    _, n7 = parse_sample_size("Randomized to drug (n=50) or placebo (n=48).")
    check("2-arm RCT (no flow cue) still sums", n7 == 98)
    # Lab concentrations are not doses; bare molar amounts still are.
    check("dose rejects mmol/L concentration", parse_dose("glucose was 7.2 mmol/L") == "")
    check("dose rejects mg/dL concentration", parse_dose("LDL 140 mg/dL") == "")
    check("dose rejects mg/mL concentration", parse_dose("formulated at 5 mg/mL") == "")
    check("dose keeps bare molar amount", "300 nmol" in parse_dose("a 300 nmol bolus"))
    # Spelled-out and hyphenated units were previously missed entirely.
    check("spelled-out micrograms parsed", "500 micrograms" in parse_dose("given 500 micrograms daily"))
    check("hyphenated unit parsed", "250-µg" in parse_dose("a 250-µg dose"))
    check("spelled-out milligrams parsed", "2 milligrams" in parse_dose("2 milligrams once daily"))
    # Frequency qualifiers are clinically essential and must be retained.
    check("frequency 'twice a day' kept", parse_dose("500 mg twice a day") == "500 mg twice a day")
    check("frequency 'BID' kept", parse_dose("500 mg BID") == "500 mg BID")
    check("frequency 'three times daily' kept",
          parse_dose("2 g three times daily") == "2 g three times daily")
    # A dose belonging to the placebo/control arm is not the drug's dose.
    check("placebo-arm dose excluded",
          parse_dose("tirzepatide 5 mg or matching placebo 10 mg") == "5 mg")

    # Reviews/meta-analyses report k studies + pooled N, not one cohort.
    from retarats_pipeline.curation.extractors import parse_synthesis_scale
    disp_s, n_s = parse_synthesis_scale("We included 12 studies with 4,530 participants.")
    check("synthesis keeps study count", "12 studies" in disp_s)
    check("synthesis keeps pooled N", n_s == 4530 and "4530 participants" in disp_s)
    disp_w, _ = parse_synthesis_scale("Twelve trials (n=3,201 participants) were pooled.")
    check("synthesis handles word numbers", "12 studies" in disp_w)
    r_syn = refine_extraction(
        {"evidence_class": "evidence_synthesis", "molecule_name": "Metformin"},
        {"title": "Metformin: a systematic review and meta-analysis",
         "abstract": "We included 12 studies with 4,530 participants."})
    check("review routed to synthesis scale", "12 studies" in r_syn["refined_sample_size"])

    # --- open-access full text feeds STRUCTURED extraction only ---
    _ev = {"molecule_name": "Metformin"}
    _abs = {"title": "Metformin in GA", "abstract": "We assessed oral metformin on geographic atrophy."}
    check("no dose from abstract alone", refine_extraction(_ev, _abs)["refined_dose"] == "")
    _ft = dict(_abs, fulltext_methods="Patients in the metformin arm were instructed to "
                                      "increase the metformin dose to 1000 mg twice daily for 18 months.")
    _r = refine_extraction(_ev, _ft)
    check("dose recovered from full text", _r["refined_dose"] == "1000 mg twice daily")
    check("duration recovered from full text", "18 months" in _r["refined_duration"])
    # Full text must NOT feed model/outcome classification (it would destabilise them).
    _noise = dict(_abs, fulltext_results="In mice, survival improved markedly.")
    check("full text does not flip model classification",
          refine_extraction(_noise and _ev, _noise)["model_primary"]
          == refine_extraction(_ev, _abs)["model_primary"])
    check("full text does not flip outcome direction",
          refine_extraction(_ev, _noise)["refined_outcome_direction"]
          == refine_extraction(_ev, _abs)["refined_outcome_direction"])

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
    # Negation-aware: a negated harm cue is NOT harmful.
    check("negated harm ('no reduction in mortality') is not harmful",
          classify_outcome({"safety_signal": "There was no reduction in mortality."}, "") != "harmful")
    check("'reduced mortality' is beneficial, not harmful",
          classify_outcome({"efficacy_signal": "Treatment reduced mortality."}, "") == "beneficial")
    check("'increased mortality' is harmful",
          classify_outcome({"safety_signal": "Increased mortality was observed."}, "") == "harmful")
    check("'no serious adverse events' is not harmful",
          classify_outcome({"safety_signal": "No serious adverse events occurred."}, "") != "harmful")

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
    check("refine_extraction returns refined_extraction_scope", "refined_extraction_scope" in refined)
    check("refine_extraction does not touch model_type", "model_type" not in refined)

    # --- experimental LLM comparison tool (opt-in; offline-testable plumbing) ---
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "experimental_llm_extract",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "experimental_llm_extract.py"))
    _llm = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_llm)
    check("llm json parser pulls object from noisy text",
          _llm.parse_llm_json('pre {"dose":"5 mg","route":"oral"} post') == {"dose": "5 mg", "route": "oral"})
    check("llm json parser tolerates no-JSON", _llm.parse_llm_json("no json") == {})
    _demo = {"pmid": "1", "molecule_name": "Demo", "title": "t",
             "abstract": "Demo 5 mg subcutaneously for 24 weeks; n=101; weight reduced.",
             "_evidence": {"molecule_name": "Demo", "efficacy_signal": "weight reduced"},
             "_paper": {"title": "t", "abstract": "Demo 5 mg subcutaneously for 24 weeks; n=101."}}
    _cmp = _llm.compare_paper(_demo, "ollama", "http://x", "m", None, mock=True)
    check("llm compare yields rules+llm field maps",
          set(_cmp["rules"]) == set(_llm.FIELDS) and set(_cmp["llm"]) == set(_llm.FIELDS))
    check("llm compare flags disagreements as a list", isinstance(_cmp["disagree"], list))

    # --- molecule-scoped extraction (trust risk #1: no comparator misattribution) ---
    # Single-drug paper: whole-text extraction, scope "document".
    r_single = refine_extraction(
        {"molecule_name": "Retatrutide"},
        {"title": "Retatrutide in obesity",
         "abstract": "Participants received 5 mg once weekly for 24 weeks."})
    # Frequency is part of the dose: "5 mg once weekly" != "5 mg once daily".
    check("single-drug dose extracted with frequency", r_single["refined_dose"] == "5 mg once weekly")
    check("single-drug scope is document", r_single["refined_extraction_scope"] == "document")

    # Comparator paper, doses in SEPARATE sentences: keep only this molecule's dose.
    r_cmp = refine_extraction(
        {"molecule_name": "Tirzepatide"},
        {"title": "Tirzepatide versus semaglutide",
         "abstract": "Tirzepatide 5 mg was compared with semaglutide. Semaglutide 1 mg was given weekly."})
    check("comparator keeps this molecule's dose", "5 mg" in r_cmp["refined_dose"])
    check("comparator drops the other drug's dose", "1 mg" not in r_cmp["refined_dose"])
    check("comparator scope is molecule_local", r_cmp["refined_extraction_scope"] == "molecule_local")

    # Comparator paper, both doses in ONE clause: proximity resolves ownership --
    # the dose adjacent to OUR molecule is ours, the one after "versus" is not.
    r_amb = refine_extraction(
        {"molecule_name": "Tirzepatide"},
        {"title": "Head-to-head trial",
         "abstract": "Tirzepatide 5 mg versus semaglutide 1 mg were compared over 40 weeks."})
    check("adjacent dose attributed to our molecule", r_amb["refined_dose"] == "5 mg")
    check("comparator's dose excluded", "1 mg" not in r_amb["refined_dose"])
    check("comparator scope flagged", r_amb["refined_extraction_scope"] == "molecule_local")

    # No adjacency evidence at all -> still refuse rather than guess.
    r_far = refine_extraction(
        {"molecule_name": "Tirzepatide"},
        {"title": "x",
         "abstract": "Doses of 5 mg and 1 mg were compared between the two agents "
                     "tirzepatide and semaglutide."})
    check("non-adjacent multi-drug doses omitted", r_far["refined_dose"] == "")
    check("non-adjacent scope flagged ambiguous",
          r_far["refined_extraction_scope"] == "ambiguous_multidrug")

    # BMI middle-dot false positive stays gone under the new path.
    r_bmi = refine_extraction(
        {"molecule_name": "Amycretin"},
        {"title": "Amycretin phase 1b/2a",
         "abstract": "Adults with obesity (BMI 27.0-39.9 kg/m2) received amycretin escalated to 60 mg."})
    check("BMI kg not a dose under scoping", "kg" not in r_bmi["refined_dose"] and r_bmi["refined_dose"] == "60 mg")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
