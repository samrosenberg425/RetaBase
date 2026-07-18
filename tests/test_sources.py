#!/usr/bin/env python3
"""Offline unit tests for the registry sources (CT.gov trials + EuropePMC preprints).

No network. Feeds sample payloads to parse_study + the registry normalizers and
asserts field mapping, the `ongoing` flag, id/url construction, and the feed
builders' empty-DB behavior. Run:

    python3 tests/test_sources.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.clients import ClinicalTrialsClient
from retarats_pipeline.enrichment.registry import (
    europepmc_results,
    is_ongoing,
    molecule_query_terms,
    normalize_preprint,
    normalize_trial,
    preprint_id,
    preprints_query,
    trial_url,
    trials_query,
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


# --- Sample CT.gov v2 study (shape of /api/v2/studies studies[] element) ------
def sample_study(status="RECRUITING"):
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT05723458",
                "briefTitle": "Retatrutide for Obesity",
                "officialTitle": "A Phase 3 Study of Retatrutide in Obesity",
            },
            "statusModule": {
                "overallStatus": status,
                "startDateStruct": {"date": "2024-01-15", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {"date": "2026-06-01"},
                "completionDateStruct": {"date": "2026-12-01"},
            },
            "designModule": {
                "studyType": "INTERVENTIONAL",
                "phases": ["PHASE3"],
                "enrollmentInfo": {"count": 600, "type": "ESTIMATED"},
            },
            "conditionsModule": {"conditions": ["Obesity", "Overweight"]},
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": "Retatrutide", "description": "SC injection"}
                ]
            },
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Eli Lilly and Company"}},
            "referencesModule": {
                "references": [
                    {"pmid": "37345678", "type": "RESULT", "citation": "Foo et al. 2023"},
                    {"pmid": "37000000", "type": "DERIVED", "citation": "Bar et al. 2023"},
                    {"pmid": "36000000", "type": "BACKGROUND", "citation": "Baz et al. 2022"},
                    {"type": "BACKGROUND", "citation": "No pmid here"},
                    {"pmid": "not-a-number", "type": "RESULT"},
                ]
            },
        },
        "resultsSection": {},
    }


def sample_europepmc_payload():
    return {
        "resultList": {
            "result": [
                {
                    "id": "PPR712345",
                    "source": "PPR",
                    "doi": "10.1101/2024.05.01.591234",
                    "title": "Preclinical effects of Retatrutide on hepatic steatosis",
                    "authorString": "Smith J, Doe A, Roe B, Lin C, Park D.",
                    "firstPublicationDate": "2024-05-02",
                    "publisher": "bioRxiv",
                },
                {
                    "id": "PPR998877",
                    "source": "PPR",
                    "title": "A preprint with no DOI",
                    "authorString": "Only A.",
                    "firstPublicationDate": "2023-11-10",
                },
            ]
        }
    }


def run():
    # --- parse_study (existing client) maps core fields ---
    parsed = ClinicalTrialsClient.parse_study(sample_study())
    check("parse_study nct", parsed["nct_id"] == "NCT05723458")
    check("parse_study title", parsed["brief_title"] == "Retatrutide for Obesity")
    check("parse_study status", parsed["overall_status"] == "RECRUITING")
    check("parse_study phase", parsed["phases"] == "PHASE3")
    check("parse_study enrollment", parsed["enrollment_count"] == 600)
    check("parse_study conditions", "Obesity" in parsed["conditions"])
    check("parse_study sponsor", parsed["lead_sponsor"] == "Eli Lilly and Company")
    check("parse_study start_date", parsed["start_date"] == "2024-01-15")

    # --- parse_study: linked publications from referencesModule ---
    refs = parsed["references"]
    check("parse_study references keep only numeric pmids", len(refs) == 3)
    check("parse_study references preserve pmid+type",
          refs[0] == {"pmid": "37345678", "type": "RESULT"})
    check("parse_study references drops non-numeric pmid",
          all(r["pmid"].isdigit() for r in refs))
    check("parse_study references types preserved",
          {r["type"] for r in refs} == {"RESULT", "DERIVED", "BACKGROUND"})

    # --- normalize_trial: molecule attribution + url + ongoing flag ---
    trial = normalize_trial(parsed, molecule_id="retatrutide", molecule_name="Retatrutide")
    check("trial molecule_id", trial["molecule_id"] == "retatrutide")
    check("trial molecule_name", trial["molecule_name"] == "Retatrutide")
    check("trial url", trial["url"] == "https://clinicaltrials.gov/study/NCT05723458")
    check("trial ongoing (recruiting)", trial["ongoing"] is True)
    check("trial has_results False", trial["has_results"] is False)
    # result_pmids = RESULT + DERIVED only; reference_pmids = every linked pmid.
    check("trial result_pmids RESULT+DERIVED", trial["result_pmids"] == "37345678; 37000000")
    check("trial reference_pmids all", trial["reference_pmids"] == "37345678; 37000000; 36000000")
    check("trial keys compact", set(trial) >= {
        "nct_id", "molecule_id", "molecule_name", "brief_title", "overall_status",
        "phases", "study_type", "conditions", "interventions", "enrollment_count",
        "start_date", "primary_completion_date", "completion_date", "lead_sponsor",
        "has_results", "result_pmids", "reference_pmids", "url", "ongoing",
    })

    # blank-safe: a parsed study with no references -> empty pmid strings
    no_refs = normalize_trial({"nct_id": "NCT00000000", "overall_status": "COMPLETED"})
    check("trial result_pmids blank-safe", no_refs["result_pmids"] == "")
    check("trial reference_pmids blank-safe", no_refs["reference_pmids"] == "")

    # --- ongoing flag across statuses (v2 enum + humanized) ---
    check("ongoing RECRUITING", is_ongoing("RECRUITING"))
    check("ongoing 'Recruiting'", is_ongoing("Recruiting"))
    check("ongoing NOT_YET_RECRUITING", is_ongoing("NOT_YET_RECRUITING"))
    check("ongoing 'Active, not recruiting'", is_ongoing("Active, not recruiting"))
    check("ongoing ENROLLING_BY_INVITATION", is_ongoing("ENROLLING_BY_INVITATION"))
    check("not ongoing COMPLETED", not is_ongoing("COMPLETED"))
    check("not ongoing Terminated", not is_ongoing("Terminated"))
    check("not ongoing blank", not is_ongoing(""))

    # completed trial -> ongoing False on the normalized row
    done = normalize_trial(ClinicalTrialsClient.parse_study(sample_study("COMPLETED")))
    check("completed trial not ongoing", done["ongoing"] is False)

    check("trial_url upper", trial_url("nct05723458") == "https://clinicaltrials.gov/study/NCT05723458")

    # --- EuropePMC preprint normalizer ---
    results = europepmc_results(sample_europepmc_payload())
    check("europepmc extract 2 results", len(results) == 2)

    pp = normalize_preprint(results[0], molecule_id="retatrutide", molecule_name="Retatrutide")
    check("preprint id from doi", pp["id"] == "10.1101/2024.05.01.591234")
    check("preprint doi", pp["doi"] == "10.1101/2024.05.01.591234")
    check("preprint url from doi", pp["url"] == "https://doi.org/10.1101/2024.05.01.591234")
    check("preprint server bioRxiv", pp["server"] == "bioRxiv")
    check("preprint date", pp["date"] == "2024-05-02")
    check("preprint molecule", pp["molecule_id"] == "retatrutide")
    check("preprint authors trimmed", pp["authors_short"] == "Smith J; Doe A; Roe B et al.")

    # no-DOI preprint: id falls back to EuropePMC id, url to europepmc article link
    pp2 = normalize_preprint(results[1])
    check("preprint id fallback to ppr id", pp2["id"] == "PPR998877")
    check("preprint url fallback europepmc", pp2["url"] == "https://europepmc.org/article/PPR/PPR998877")
    check("preprint short authors single", pp2["authors_short"] == "Only A")

    check("preprint_id prefers doi", preprint_id({"doi": "10.1/X", "id": "PPR1"}) == "10.1/x")
    check("europepmc_results empty on junk", europepmc_results(None) == [] and europepmc_results({}) == [])

    # --- query builders ---
    mol = {"display_name": "Retatrutide", "synonyms_csv": "Retatrutide, LY-3437943, LY3437943"}
    terms = molecule_query_terms(mol)
    check("query terms include display name", "Retatrutide" in terms)
    check("query terms include synonym", "LY-3437943" in terms)
    tq = trials_query(mol)
    check("trials_query OR", " OR " in tq and "Retatrutide" in tq)
    pq = preprints_query(mol)
    check("preprints_query has SRC:PPR", pq.endswith("AND SRC:PPR"))

    # --- feed builders: empty/absent DB -> valid empty feed ---
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import build_trials_json  # noqa: E402
    import build_preprints_json  # noqa: E402

    tj = build_trials_json.build(db_path="/nonexistent/does_not_exist.sqlite", out_path="/tmp/_test_trials.json")
    check("empty trials feed count 0", tj["count"] == 0 and tj["ongoing_count"] == 0)
    import json as _json
    with open("/tmp/_test_trials.json") as fh:
        tpayload = _json.load(fh)
    check("trials feed keys", set(tpayload) == {"generated_utc", "count", "ongoing_count", "trials"})

    ppj = build_preprints_json.build(db_path="/nonexistent/nope.sqlite", out_path="/tmp/_test_preprints.json")
    check("empty preprints feed count 0", ppj["count"] == 0)
    with open("/tmp/_test_preprints.json") as fh:
        ppayload = _json.load(fh)
    check("preprints feed keys", set(ppayload) == {"generated_utc", "count", "preprints"})

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
