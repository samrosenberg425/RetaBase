#!/usr/bin/env python3
"""WS5A (grading honesty pass) tests: Featured safety, appraisal text, removed UI, copy.

Deterministic and offline:

    python3 tests/test_ws5a_honesty.py
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from retarats_pipeline.curation.appraisal import appraise_evidence  # noqa: E402
from retarats_pipeline.curation.publication_status import (  # noqa: E402
    decide_publication,
    featured_block_reason,
)
from retarats_pipeline.curation.reliability import assess_reliability  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def human_rct():
    return {
        "evidence_id": "1:retatrutide:r1", "molecule_id": "retatrutide", "molecule_name": "Retatrutide",
        "pmid": "111", "title": "Retatrutide in obesity: a randomized placebo-controlled trial",
        "pub_year": "2025", "primary_study_type": "RCT", "model_type": "human",
        "role_category": "direct_intervention", "molecule_relevance": "primary_intervention",
        "processing_lane": "human_intervention", "condition_tags": "obesity_weight",
        "endpoint_tags": "body_weight", "intervention_or_exposure": "Retatrutide as direct intervention",
        "comparator_or_control": "placebo", "dose_route": "subcutaneous; 12 mg", "duration": "48 weeks",
        "sample_size": "n=338", "outcome_direction": "beneficial_or_desired_signal",
        "keep_for_final_database": True,
    }


def graded(ev, paper=None):
    ev = dict(ev)
    ev.update(assess_reliability(ev, paper or {}).to_dict())
    return ev


# ---------------------------------------------------------------- A. Featured safety
def run_featured_safety():
    base = graded(human_rct())
    check("baseline human RCT is Featured", decide_publication(base).publication_status == "featured")
    check("baseline rule id unchanged", decide_publication(base).publish_rule_id == "broad_v1:featured")

    def decide(**extra):
        ev = dict(base)
        ev.update(extra)
        return decide_publication(ev)

    # retracted: by flag, by either retraction pubtype, in any serialised shape
    for label, extra in [
        ("is_retracted True", {"is_retracted": True}),
        ("is_retracted 'True'", {"is_retracted": "True"}),
        ("pubtype Retracted Publication", {"pubtypes": ["Randomized Controlled Trial", "Retracted Publication"]}),
        ("pubtype string form", {"pubtypes": "Randomized Controlled Trial; Retracted Publication"}),
        ("retraction notice", {"pubtypes": ["Retraction of Publication"]}),
        ("build-supplied block reason", {"featured_block_reason": "retracted"}),
    ]:
        d = decide(**extra)
        check(f"retracted never Featured ({label})", d.publication_status == "listed" and not d.auto_publish_eligible)
    d = decide(is_retracted=True)
    check("retracted record is still published (listed), not excluded", d.publication_status == "listed")
    check("retracted record keeps its website section", d.website_section == "Human evidence")
    check("retracted rule id is auditable", d.publish_rule_id == "ws5a:listed_retracted")

    # PubMed-indexed preprint (same policy as other preprints)
    for pt in (["Preprint"], ["Randomized Controlled Trial", "Preprint"], "Journal Article; Preprint"):
        d = decide(pubtypes=pt)
        check(f"PubMed preprint never Featured ({pt!r})", d.publication_status == "listed")
    check("preprint rule id", decide(pubtypes=["Preprint"]).publish_rule_id == "ws5a:listed_preprint")

    # non-research
    for pt in (["Letter"], ["Editorial"], ["Comment"], ["News"], ["Published Erratum"], ["Interview"],
               ["Letter", "Comment"], ["Newspaper Article"]):
        d = decide(pubtypes=pt)
        check(f"non-research never Featured ({pt!r})", d.publication_status == "listed")
    check("non-research rule id", decide(pubtypes=["Letter"]).publish_rule_id == "ws5a:listed_non_research")

    # strict reply/correction title prefixes (PubMed did not tag these as Letter/Comment)
    for t in ("Re: Metformin for Patients with Metastatic Prostate Cancer", "Re.: \"Reduced risk of lung cancer\"",
              "Reply: A Randomized Phase 3 Trial of Metformin", "Reply to Smith et al.", "In reply",
              "Comment on Byun et al.: teriparatide", "Letter to the Editor: semaglutide", "Erratum: Retatrutide in obesity",
              "[Re: something]"):
        check(f"reply/correction title never Featured ({t[:30]!r})", decide(title=t).publication_status == "listed")
    for t in ("Response to sirolimus in capillary lymphatic venous malformations", "Correction of anemia by dapagliflozin",
              "Reproducibility of retatrutide dosing", "Replication of the STEP trial", "Retatrutide in obesity: a randomized trial"):
        check(f"research title is not caught by the title rule ({t[:30]!r})", decide(title=t).publication_status == "featured")

    # guard: a primary-design pubtype keeps a commentary-tagged research article eligible
    check("RCT that also carries a 'Comment' tag stays Featured",
          decide(pubtypes=["Randomized Controlled Trial", "Comment"]).publication_status == "featured")
    check("erratum notice is non-research even if tagged with a design type",
          decide(pubtypes=["Randomized Controlled Trial", "Published Erratum"]).publication_status == "listed")
    check("plain research article pubtypes stay Featured",
          decide(pubtypes=["Journal Article", "Randomized Controlled Trial", "Research Support, Non-U.S. Gov't"]
                 ).publication_status == "featured")
    check("no pubtypes at all -> unchanged (fail-open)", decide().publication_status == "featured")
    check("falsy retraction flags do not block", all(
        decide(is_retracted=v).publication_status == "featured" for v in (False, "False", "", None, "0")))

    # syntheses and guidelines are blocked too
    syn = graded({**human_rct(), "primary_study_type": "Meta-analysis", "title": "Meta-analysis of randomized trials"},
                 {"pubtypes": ["Meta-Analysis"], "abstract": "Systematic search PRISMA of randomized trials in patients."})
    check("synthesis baseline is Featured", decide_publication(syn).publication_status == "featured")
    check("retracted synthesis not Featured", decide_publication({**syn, "is_retracted": True}).publication_status == "listed")
    check("letter graded as meta-analysis not Featured",
          decide_publication({**syn, "pubtypes": ["Letter"]}).publication_status == "listed")
    g = graded({**human_rct(), "title": "ADA Standards of Care 2026"}, {"pubtypes": ["Practice Guideline"]})
    check("guideline baseline is Featured", decide_publication(g).publication_status == "featured")
    check("retracted guideline not Featured", decide_publication({**g, "is_retracted": True}).publication_status == "listed")
    check("preprint guideline not Featured", decide_publication({**g, "pubtypes": ["Preprint"]}).publication_status == "listed")

    # the helper itself
    check("block reason: none", featured_block_reason({}, {"pubtypes": ["Journal Article"]}) == "")
    check("block reason: retraction beats preprint",
          featured_block_reason({"is_retracted": True}, {"pubtypes": ["Preprint"]}) == "retracted")
    check("block reason reads the paper's pubtypes", featured_block_reason({}, {"pubtypes": ["Preprint"]}) == "preprint")

    # Records that were never going to be Featured are unchanged
    low = graded({**human_rct(), "primary_study_type": "Animal in vivo", "model_type": "animal",
                  "processing_lane": "preclinical_intervention", "role_category": "direct_intervention"})
    check("non-featured record unaffected", decide_publication(low).publish_rule_id == "broad_v1:listed")

    # PUBLISHED counts are unchanged: the block only moves featured -> listed
    pool = []
    for i, extra in enumerate([{}, {"is_retracted": True}, {"pubtypes": ["Preprint"]}, {"pubtypes": ["Letter"]},
                               {"pubtypes": ["Published Erratum"]}, {}, {"is_retracted": "True"}]):
        pool.append({**base, "pmid": str(i), **extra})
    published_with = sum(decide_publication(r).publication_status in {"featured", "listed"} for r in pool)
    published_without = sum(
        decide_publication({k: v for k, v in r.items() if k not in {"pubtypes", "is_retracted"}}
                           ).publication_status in {"featured", "listed"} for r in pool)
    featured_with = sum(decide_publication(r).publication_status == "featured" for r in pool)
    check("published (featured+listed) count identical with and without the block", published_with == published_without == len(pool))
    check("only the clean records stay Featured", featured_with == 2)


# ---------------------------------------------------------------- B. appraisal text
def run_appraisal():
    ev = graded(human_rct())
    ev["abstract"] = "Adults were randomised to retatrutide or placebo (n=338)."
    paper_abs = {"abstract": ev["abstract"]}

    # full-text availability follows the actual fulltext fields
    a = appraise_evidence(ev, {**paper_abs, "fulltext_methods": "We enrolled 338 participants. " * 5})
    check("with OA full text: no 'no full text' limitation",
          "no full text" not in a.appraisal_limitations.lower()
          and "title/abstract only" not in a.appraisal_limitations.lower()
          and "no open-access full text" not in a.appraisal_limitations)
    a = appraise_evidence(ev, {**paper_abs, "fulltext_results": "Results text."})
    check("fulltext_results alone also counts as full text", "no open-access full text" not in a.appraisal_limitations)
    a = appraise_evidence(ev, paper_abs)
    check("abstract only: says no OA full text was available",
          "no open-access full text was available" in a.appraisal_limitations)
    check("old false wording is gone", "(no full text)" not in a.appraisal_limitations)
    a = appraise_evidence(ev, {})
    check("no paper passed: abstract on the row still counts", "title and abstract only" in a.appraisal_limitations)

    # source unavailable: no abstract, no full text -> one honest note, no 'not reported' claims
    bare = {**graded(human_rct()), "sample_size": "", "comparator_or_control": "", "dose_route": "", "duration": "",
            "outcome_direction": "", "abstract": ""}
    a = appraise_evidence(bare, {})
    check("no source text: says details could not be assessed", "could not be assessed" in a.appraisal_limitations)
    check("no source text: no per-item 'not found' claims", "not found in the" not in a.appraisal_limitations
          and "not reported" not in a.appraisal_limitations.lower())

    # missing items with a source are 'not found in the ...', never 'not reported' / 'no comparator'
    thin = {**graded(human_rct()), "sample_size": "", "comparator_or_control": "", "dose_route": "", "duration": ""}
    a = appraise_evidence(thin, paper_abs)
    check("sample size missing with abstract: 'not found in the title and abstract'",
          "sample size not found in the title and abstract" in a.appraisal_limitations)
    a = appraise_evidence(thin, {**paper_abs, "fulltext_methods": "Methods text."})
    check("sample size missing with full text: says full text was searched",
          "sample size not found in the abstract and open-access full text" in a.appraisal_limitations)
    check("no 'no comparator ... clearly reported' wording", "no comparator/control clearly reported" not in a.appraisal_limitations)

    # sample size: parsed/refined N suppresses the limitation and shows as a strength
    for field, val in (("refined_n", 338), ("refined_sample_size", "n=338"), ("sample_size", "n=338"),
                       ("abstract_sample_size", "338 patients")):
        r = {**thin, field: val}
        a = appraise_evidence(r, paper_abs)
        check(f"parsed N in {field}: no 'sample size not' limitation", "sample size not" not in a.appraisal_limitations)
        check(f"parsed N in {field}: listed as a strength", "sample size reported" in a.appraisal_strengths)
    r = {**thin, "sample_size": "multicentre"}
    check("a non-numeric sample_size does not count as a parsed N",
          "sample size not found" in appraise_evidence(r, paper_abs).appraisal_limitations)
    # same N source the rigor rubric uses
    from retarats_pipeline.curation.reliability import _sample_n
    r = {**thin, "refined_n": 338}
    check("appraisal agrees with the rubric's N", _sample_n(r) == 338
          and "sample size not" not in appraise_evidence(r, paper_abs).appraisal_limitations)

    # the class-driven 'reliability tier' wording is gone from the synopsis
    a = appraise_evidence({**ev, "reliability_tier": "high"}, paper_abs)
    check("appraisal_summary no longer states a reliability tier", "reliability" not in a.appraisal_summary.lower())
    check("missing info is not worded as poor quality",
          not re.search(r"poor|weak|low quality|unreliable", a.appraisal_limitations.lower()))


# ---------------------------------------------------------------- C. end-to-end build
def run_build():
    bcd = _load("build_curated_database", "scripts/build_curated_database.py")
    from retarats_pipeline.enrichment.common import save_payload_rows

    td = tempfile.mkdtemp(prefix="ws5a_")
    db = os.path.join(td, "c.sqlite")
    text = ("Background: obesity. Methods: adults were randomised to retatrutide or placebo in a double-blind, "
            "placebo-controlled trial of 338 participants. Results: body weight fell.")
    specs = {
        "1": ("clean RCT", ["Journal Article", "Randomized Controlled Trial"], True),
        "2": ("retracted RCT", ["Journal Article", "Randomized Controlled Trial", "Retracted Publication"], False),
        "3": ("PubMed preprint RCT", ["Preprint", "Randomized Controlled Trial"], False),
        "4": ("letter about a trial", ["Letter"], False),
        "5": ("erratum", ["Published Erratum"], False),
        "6": ("RCT with full text", ["Journal Article", "Randomized Controlled Trial"], True),
    }
    papers, evid = [], []
    for pmid, (label, pts, _) in specs.items():
        p = {"pmid": pmid, "title": f"Retatrutide randomized trial ({label})", "abstract": text, "pub_year": 2024,
             "pubtypes": pts, "mesh_terms": "Humans; Obesity", "journal": "Test J"}
        if pmid == "6":
            p["fulltext_methods"] = "We enrolled 338 participants at 12 sites. " * 4
        papers.append(p)
        evid.append({"evidence_id": f"{pmid}:retatrutide:r1", "pmid": pmid, "molecule_id": "retatrutide",
                     "molecule_name": "Retatrutide", "rule_id": "r1", "pub_year": 2024,
                     "primary_study_type": "RCT", "model_type": "human", "role_category": "direct_intervention",
                     "processing_lane": "human_intervention", "comparator_or_control": "placebo",
                     "dose_route": "subcutaneous", "duration": "48 weeks", "sample_size": "n=338",
                     "outcome_direction": "beneficial_or_desired_signal", "keep_for_final_database": True})
    conn = sqlite3.connect(db)
    save_payload_rows(conn, "papers", "pmid", papers)
    save_payload_rows(conn, "evidence", "evidence_id", evid)
    conn.close()
    out = os.path.join(td, "out")
    bcd.build(db, out)

    def read(name):
        with open(os.path.join(out, name), newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    cur = {r["pmid"]: r for r in read("curated_evidence.csv")}
    check("build keeps every record", set(cur) == set(specs))
    check("clean RCT is Featured", cur["1"]["publication_status"] == "featured")
    check("RCT with full text is Featured", cur["6"]["publication_status"] == "featured")
    for pmid in ("2", "3", "4", "5"):
        check(f"build: {specs[pmid][0]} is published but not Featured",
              cur[pmid]["publication_status"] == "listed" and cur[pmid]["auto_publish_eligible"] in ("False", "false", "0", ""))
    check("build: block recorded in publish_rule_id",
          [cur[p]["publish_rule_id"] for p in ("2", "3", "4", "5")]
          == ["ws5a:listed_retracted", "ws5a:listed_preprint", "ws5a:listed_non_research", "ws5a:listed_non_research"])
    pub = {r["pmid"] for r in read("public_records.csv")}
    check("published-record count unchanged by the Featured fix", pub == set(specs))
    feat = {r["pmid"] for r in read("featured_records.csv")}
    check("featured_records.csv holds only the clean records", feat == {"1", "6"})
    mi = {r["molecule_id"]: r for r in read("molecule_index.csv")}["retatrutide"]
    check("molecule index: total_records counts all published", mi["total_records"] == "6")
    check("molecule index: featured count excludes blocked records", mi["auto_published"] == "2")
    # appraisal text follows real full-text availability, end to end
    check("build: abstract-only record says OA full text was unavailable",
          "no open-access full text was available" in cur["1"]["appraisal_limitations"])
    check("build: record WITH full text does not claim there is none",
          "no open-access full text" not in cur["6"]["appraisal_limitations"]
          and "no full text" not in cur["6"]["appraisal_limitations"])
    check("build: parsed N -> no 'sample size not reported'", all(
        "sample size not" not in cur[p]["appraisal_limitations"] for p in cur))
    check("build: no reliability tier in the synopsis", all("reliability" not in cur[p]["appraisal_summary"].lower() for p in cur))
    # legacy fields are still stored (nothing deleted yet)
    check("legacy rigor/directness/rank fields still stored", all(
        cur["1"].get(k) not in (None, "") for k in ("reliability_score", "evidence_directness", "rank_score")))


# ---------------------------------------------------------------- D. public site
def run_site():
    site = _load("build_public_site", "scripts/build_public_site.py")
    html = site._render_html(site._safe_json_block({"records": []}), 0, 0, "2026-01-01T00:00:00Z", 0, 0, "inline")
    copy = json.dumps(site._load_copy(), ensure_ascii=False)
    low = html.lower()

    # removed controls
    for gone in ('value="rank_mixed"', 'value="reliability"', 'value="directness"', "Rank (mix all levels)",
                 "Automated rigor", ">Mechanism<", 'value="mechanism"'):
        check(f"removed from UI: {gone}", gone not in html)
    check("Reliability tier filter removed", ("reliability_tier", "Reliability tier") not in site.FILTER_FACETS
          and "Reliability tier" not in html)
    check("Directness tier filter removed", ("directness_tier", "Directness tier") not in site.FILTER_FACETS
          and "Directness tier" not in html)
    check("no filter facet is a legacy score tier", not any(f in ("reliability_tier", "directness_tier", "rank_tier")
                                                           for f, _ in site.FILTER_FACETS))
    check("molecule card has no 'max rigor' or 'spotlight' stat",
          'stat("max rigor"' not in html and 'stat("spotlight"' not in html and "Spotlight papers" not in html)
    check("no venue-quality tier badge (FLAGSHIP/TOP/...) in cards or modal",
          "journalTierBadge" not in html and "jtier" not in html)
    check("journal name is still shown", 'el("span", "pill", r.journal)' in html and 'kv(grid, "Journal", r.journal)' in html)
    check("copy: no venue tier is shown, venue not a quality signal",
          "no venue tier is displayed" in copy and "no venue tier is shown" in copy)
    check("Methods lede no longer promises 'every score'", "How every score is computed" not in html)
    check("overview intro no longer advertises 'strongest rigor'", "strongest rigor" not in html)
    check("kept sorts: default order, citations, year, percentile, APT, clinical influence",
          all(f'value="{v}"' in html for v in ("rank", "citations", "year", "percentile", "apt", "clinical_influence")))
    check("default order label is honest", "Default order (evidence level first)" in html)
    # default ordering itself is unchanged: level first, then rank_score, then feed index
    check("default sort is still level-first then rank_score",
          "levelRank(a[0]) - levelRank(b[0])" in html and "num(b[0].rank_score) - num(a[0].rank_score)" in html)
    check("removed sort modes fall back to the default order", 'if (!key) return sortRecords(list, "rank");' in html)
    check("presets never sort by the legacy rigor value", "byRel" not in html and "reliability_score) - pnum" not in html)

    # 0-100 rings: internal build only, never described as quality
    check("public cards do not render the rings", "if (INTERNAL) card.appendChild(scoreRings(r));" in html
          and "card.appendChild(scoreRings(r));" not in html.replace("if (INTERNAL) card.appendChild(scoreRings(r));", ""))
    check("modal rings only for the internal build", "if (INTERNAL) m.appendChild(scorePanel(r));" in html)
    check("legacy breakdowns labelled not validated", "internal, not validated" in html)
    check("rings are never described as validated quality / strength / confidence",
          not re.search(r"(validated (quality|strength|confidence))", low.replace("not validated", "")))

    # public claims
    banned = [
        "never scored on the same curve", "only ever compare like with like", "two independent axes",
        "can both score ~70", "weights live in an editable config", "strongest, most human-relevant",
        "spotlight papers", "never enough to leapfrog", "a strong recent paper isn't buried",
        "puts the strongest", "how well it was run for its type", "how directly it applies to humans",
        "best-first", "directly relevant to people with at least moderate rigor",
        "read from the **methods** section", "reliability tier", "max rigor",
    ]
    for b in banned:
        check(f"claim removed: {b!r}", b not in copy.lower() and b not in low)
    check("copy: honest about weights being hard-coded", "not in an editable config" in copy)
    check("copy: legacy 0-100 values are not validated, not comparable, not shown",
          "not validated" in copy and "not comparable across different kinds of study" in copy
          and "does not display them on cards" in copy)
    check("copy: Featured is not an endorsement and excludes retracted / preprint / non-research",
          "not an endorsement" in copy and "Retracted records, PubMed-indexed preprints" in copy)
    check("copy: missing information is not poor quality", "Missing information is not poor quality" in copy)
    check("copy: iCite lag for recent papers is disclosed", "last two years" in copy)
    check("copy: full-text is only used for extraction", "Open-access full text is used only to extract" in copy)
    # accurate disclaimers are preserved
    check("kept: not RoB 2 / ROBINS-I / GRADE", "RoB 2" in copy and "ROBINS-I" in copy and "GRADE" in copy
          and "a formal risk-of-bias assessment (Cochrane RoB 2, ROBINS-I)" in copy)
    check("kept: evidence level reflects design, not execution",
          "Evidence level reflects study design, not this study's execution" in copy)
    check("kept: rank formula disclosed as an ordering score", "ordering score = 0.30" in copy)
    check("kept: modal states risk of bias is not assessed", "Formal risk of bias" in html)

    # feedback form no longer offers to flag the hidden scores
    fb = json.load(open(os.path.join(ROOT, "config", "feedback.json"), encoding="utf-8"))
    keys = {a["key"] for a in fb["aspects"]}
    check("feedback aspects no longer include rigor / directness scores", not ({"rigor", "directness"} & keys))
    check("feedback keeps the evidence-level aspect", "evidence_level" in keys)


def run():
    run_featured_safety()
    run_appraisal()
    run_build()
    run_site()


if __name__ == "__main__":
    run()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
