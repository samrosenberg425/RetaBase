#!/usr/bin/env python3
"""Offline unit tests for WS4: the retrieval benchmark tool, PubMed edat/re-sweep
window policy, retraction/erratum refresh, citation-freshness rolling refresh,
and the rules_version fingerprint.

No network: PubMed/OpenAlex clients are monkeypatched at the class level (same
pattern as tests/test_ws2_retrieval.py). Run:

    python3 tests/test_ws4_retrieval.py
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def _write_csv(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for r in rows:
            w.writerow(r)


def run():
    # =========================================================================
    # scripts/benchmark_retrieval.py: benchmark include/exclude behavior,
    # unapproved rows never gate, diff-mode output
    # =========================================================================
    import benchmark_retrieval as br  # noqa: E402

    tmpdir = tempfile.mkdtemp(prefix="ws4_benchmark_")
    bench_path = os.path.join(tmpdir, "benchmark.csv")
    _write_csv(bench_path, [
        ["retatrutide", "pmid", "111", "include", "true positive", "approved", "sam", "2026-09-27"],
        ["retatrutide", "pmid", "222", "exclude", "known confound", "approved", "sam", "2026-09-27"],
        ["retatrutide", "pmid", "333", "include", "unreviewed candidate", "proposed", "", ""],
        ["retatrutide", "pmid", "444", "exclude", "rejected candidate", "rejected", "sam", "2026-09-27"],
    ], br.BENCHMARK_COLUMNS)

    rows = br.load_benchmark_rows(bench_path)
    check("load_benchmark_rows reads all statuses", len(rows) == 4)
    approved = br.approved_only(rows)
    check("approved_only keeps only status=approved", len(approved) == 2)
    check("approved_only excludes proposed/rejected", all(r.status == "approved" for r in approved))

    # Corpus has 111 (correctly present) and does NOT have 222 (correctly absent):
    # both approved rows should PASS. 333/444 are unapproved and must never be
    # evaluated at all, let alone gate the result.
    index = {"pmid": {("retatrutide", "111")}, "nct_id": set(), "doi": set()}
    report = br.check_offline(rows, index)
    check("offline: only approved rows are evaluated", len(report.results) == 2)
    check("offline: unapproved count reported separately", report.skipped_unapproved == 2)
    check("offline: known-positive recall 100%", report.known_positive_recall == 1.0)
    check("offline: known-negative correct rate 100%", report.known_negative_correct_rate == 1.0)
    check("offline: PASS", report.ok is True)
    check("offline: no unexpected losses", report.unexpected_losses == [])
    check("offline: no false positives", report.new_false_positives == [])

    # Now corrupt the corpus: 111 disappears (a loss) and 222 wrongly appears (a
    # false positive). Both are APPROVED rows, so this must flip the gate to FAIL,
    # and the unapproved 333/444 must still play no role in that verdict.
    bad_index = {"pmid": {("retatrutide", "222")}, "nct_id": set(), "doi": set()}
    bad_report = br.check_offline(rows, bad_index)
    check("offline: unexpected loss detected", len(bad_report.unexpected_losses) == 1
          and bad_report.unexpected_losses[0].row.id == "111")
    check("offline: new false positive detected", len(bad_report.new_false_positives) == 1
          and bad_report.new_false_positives[0].row.id == "222")
    check("offline: FAIL on approved-row regression", bad_report.ok is False)

    # Unapproved rows cannot gate CI: an "approved" benchmark with ZERO approved
    # rows always passes, no matter how badly the (irrelevant) proposed/rejected
    # rows would have failed if they'd been checked.
    only_unapproved_path = os.path.join(tmpdir, "only_unapproved.csv")
    _write_csv(only_unapproved_path, [
        ["retatrutide", "pmid", "999", "include", "proposed, would fail if checked", "proposed", "", ""],
    ], br.BENCHMARK_COLUMNS)
    empty_index = {"pmid": set(), "nct_id": set(), "doi": set()}
    unapproved_report = br.check_offline(br.load_benchmark_rows(only_unapproved_path), empty_index)
    check("unapproved-only benchmark always PASSes (nothing to gate on)", unapproved_report.ok is True)
    check("unapproved-only benchmark evaluates zero rows", len(unapproved_report.results) == 0)

    # propose() always writes status=proposed regardless of what's passed in.
    candidates_path = os.path.join(tmpdir, "candidates.csv")
    sneaky = br.BenchmarkRow("retatrutide", "pmid", "555", "include", "sneaky self-approval attempt",
                             "approved", "not-a-human", "2026-09-27")
    br.append_candidate(candidates_path, sneaky)
    proposed_rows = br.load_benchmark_rows(candidates_path)
    check("append_candidate always forces status=proposed", proposed_rows[0].status == "proposed")
    check("append_candidate clears reviewed_by/reviewed_on", proposed_rows[0].reviewed_by == ""
          and proposed_rows[0].reviewed_on == "")

    # Malformed CSV values are rejected loudly rather than silently miscounted.
    bad_path = os.path.join(tmpdir, "bad.csv")
    _write_csv(bad_path, [["retatrutide", "pmid", "1", "maybe", "", "approved", "", ""]], br.BENCHMARK_COLUMNS)
    try:
        br.load_benchmark_rows(bad_path)
        check("bad 'expected' value raises", False)
    except ValueError:
        check("bad 'expected' value raises", True)

    # --- diff-mode pure logic ---
    diff = br.diff_id_sets("old", ["1", "2", "3"], "new", ["2", "3", "4"])
    check("diff_id_sets overlap", diff.overlap == {"2", "3"})
    check("diff_id_sets gained (in new, not old)", diff.gained == {"4"})
    check("diff_id_sets lost (in old, not new)", diff.lost == {"1"})
    check("diff_id_sets counts", diff.count_a == 3 and diff.count_b == 3)
    rendered = diff.render()
    check("diff render mentions gained/lost", "gained" in rendered and "lost" in rendered)

    # --- retrieval-count drift ---
    old_counts = {"retatrutide": {"evidence": 10, "trials": 2, "preprints": 1}}
    new_counts = {"retatrutide": {"evidence": 12, "trials": 2, "preprints": 1},
                 "bpc_157": {"evidence": 3, "trials": 0, "preprints": 0}}
    drift = br.diff_counts(old_counts, new_counts)
    check("diff_counts flags changed molecule", drift["retatrutide"]["evidence"] == 2)
    check("diff_counts flags brand-new molecule", drift["bpc_157"]["evidence"] == 3)
    check("diff_counts omits unchanged molecules", "unchanged" not in drift)

    # --- duplicate behavior + corpus index, against a real temp sqlite ---
    pubmed_db = os.path.join(tmpdir, "pubmed.sqlite")
    conn = sqlite3.connect(pubmed_db)
    conn.execute("create table evidence (evidence_id text primary key, payload_json text, updated_at_utc text)")
    ev_rows = [
        {"evidence_id": "111:retatrutide:r1", "pmid": "111", "molecule_id": "retatrutide", "rule_id": "r1"},
        {"evidence_id": "111:retatrutide:r2", "pmid": "111", "molecule_id": "retatrutide", "rule_id": "r2"},
        {"evidence_id": "222:retatrutide:r1", "pmid": "222", "molecule_id": "retatrutide", "rule_id": "r1"},
    ]
    for r in ev_rows:
        conn.execute("insert into evidence values (?, ?, ?)", (r["evidence_id"], json.dumps(r), ""))
    conn.commit()
    conn.close()
    dups = br.duplicate_evidence(pubmed_db)
    check("duplicate_evidence finds the multi-rule pair", ("retatrutide", "111") in dups)
    check("duplicate_evidence excludes the single-rule pair", ("retatrutide", "222") not in dups)
    idx = br.build_corpus_index(pubmed_db=pubmed_db, trials_db="/nonexistent.sqlite",
                                preprints_db="/nonexistent.sqlite")
    check("build_corpus_index scopes by molecule_id", ("retatrutide", "111") in idx["pmid"])
    check("build_corpus_index is case-insensitive for pmid text", ("retatrutide", "111") in idx["pmid"])

    # =========================================================================
    # PubMed discovery-window policy + re-sweep window construction
    # =========================================================================
    from retarats_pipeline.rules_version import load_pubmed_discovery_policy

    policy_path = os.path.join(tmpdir, "pubmed_discovery.json")
    with open(policy_path, "w") as fh:
        json.dump({"schema": 1, "datetype": "edat", "daily_days": 14}, fh)
    policy = load_pubmed_discovery_policy(policy_path)
    check("discovery policy loads datetype", policy["datetype"] == "edat")
    check("discovery policy loads daily_days", policy["daily_days"] == 14)
    missing_policy = load_pubmed_discovery_policy("/nonexistent/path.json")
    check("missing policy file falls back to a sane default", missing_policy["datetype"] in ("pdat", "edat"))

    import argparse
    import retarats_v2 as v2

    daily_args = argparse.Namespace(mode="daily", daily_days=14, datetype="edat")
    windows = list(v2.iter_search_windows(daily_args))
    check("daily window: exactly one window", len(windows) == 1)
    check("daily window: reldate matches daily_days", windows[0][1]["reldate"] == 14)
    check("daily window: datetype threaded through", windows[0][1]["datetype"] == "edat")

    this_year = datetime.now().year
    resweep_args = argparse.Namespace(mode="backfill", start_year=this_year - 1, end_year=0, datetype="edat")
    resweep_windows = list(v2.iter_search_windows(resweep_args))
    check("re-sweep window: covers previous year + current year",
          {w[0] for w in resweep_windows} == {str(this_year - 1), str(this_year)})
    check("re-sweep window: previous year has a full-year range",
          dict(resweep_windows)[str(this_year - 1)]["mindate"] == f"{this_year - 1}/01/01")
    check("re-sweep window: datetype threaded through", all(w[1]["datetype"] == "edat" for w in resweep_windows))

    # --- dedup is untouched by the datetype/window change: PipelineState still
    # skips an evidence_id already marked seen, regardless of which window/datetype
    # rediscovered it (this is what makes the wider re-sweep window "free"). ---
    from retarats_pipeline.sinks import PipelineState

    state_db = os.path.join(tmpdir, "state.sqlite")
    state = PipelineState(state_db)
    check("dedup: not seen initially", state.seen("111:retatrutide:r1") is False)
    state.mark_seen("111:retatrutide:r1", "2026-01-01T00:00:00Z")
    check("dedup: seen after mark_seen", state.seen("111:retatrutide:r1") is True)
    # A second "run" (e.g. the weekly re-sweep re-discovering the same PMID via a
    # wider window) must not re-mark or duplicate anything -- insert-or-ignore.
    state.mark_seen("111:retatrutide:r1", "2026-02-01T00:00:00Z")
    check("dedup: re-sweep re-discovery still reads as seen", state.seen("111:retatrutide:r1") is True)

    # =========================================================================
    # scripts/run_retraction_refresh.py: pubtype detection, query construction,
    # intersect-with-corpus, refresh-in-place without deleting history
    # =========================================================================
    import run_retraction_refresh as rr  # noqa: E402

    check("is_retraction_flagged: true for Retracted Publication",
          rr.is_retraction_flagged(["Journal Article", "Retracted Publication"]))
    check("is_retraction_flagged: true for Published Erratum",
          rr.is_retraction_flagged(["Published Erratum"]))
    check("is_retraction_flagged: false for ordinary pubtypes",
          not rr.is_retraction_flagged(["Journal Article", "Review"]))
    check("is_retraction_flagged: false/blank-safe on empty", not rr.is_retraction_flagged([]))

    q = rr.retraction_query({"display_name": "BPC-157", "synonyms_csv": "BPC157"})
    check("retraction_query includes molecule terms", "BPC-157" in q and "BPC157" in q)
    check("retraction_query includes the pubtype filter", "retracted publication" in q.lower())

    def _pubmed_article_xml(pmid, title, pubtypes):
        pt_xml = "".join(f"<PublicationType>{p}</PublicationType>" for p in pubtypes)
        return f"""
        <PubmedArticle>
          <MedlineCitation>
            <PMID>{pmid}</PMID>
            <Article>
              <ArticleTitle>{title}</ArticleTitle>
              <Journal><Title>Test Journal</Title></Journal>
              <PublicationTypeList>{pt_xml}</PublicationTypeList>
            </Article>
          </MedlineCitation>
          <PubmedData><ArticleIdList></ArticleIdList></PubmedData>
        </PubmedArticle>
        """

    def _pubmed_set_xml(*articles):
        return "<PubmedArticleSet>" + "".join(articles) + "</PubmedArticleSet>"

    from retarats_pipeline.pubmed import PubMedClient, PubMedSearch

    # Seed a corpus with two already-known papers, EACH carrying enrichment
    # fields from prior pipeline runs (iCite, OpenAlex/citations, Semantic
    # Scholar) that this script knows nothing about. One will be flagged
    # retracted by PubMed, one stays clean. run() must find/refresh only the
    # flagged one, never delete either row, and -- the regression this guards
    # against -- never erase the pre-existing enrichment fields while doing so.
    def _seeded_paper(pmid, title, pubtypes):
        return {
            "pmid": pmid, "title": title, "pubtypes": pubtypes,
            "first_seen_utc": "2019-01-01T00:00:00Z",
            "icite_rcr": 1.23, "icite_apt": 0.45, "icite_schema": 2,
            "icite_updated_utc": "2020-06-01T00:00:00Z",
            "citation_count": 17, "citation_source": "openalex_doi:api",
            "citation_updated_utc": "2020-06-01T00:00:00Z",
            "influential_citation_count": 3,
            "s2_authors": json.dumps([{"name": "A B", "authorId": "1", "url": "https://x"}]),
        }

    retraction_db = os.path.join(tmpdir, "retraction.sqlite")
    conn = sqlite3.connect(retraction_db)
    conn.execute("create table papers (pmid text primary key, payload_json text, updated_at_utc text)")
    conn.execute("insert into papers values (?, ?, ?)",
                ("111", json.dumps(_seeded_paper("111", "Old title", ["Journal Article"])), ""))
    conn.execute("insert into papers values (?, ?, ?)",
                ("222", json.dumps(_seeded_paper("222", "Unrelated paper", ["Journal Article"])), ""))
    conn.commit()
    conn.close()

    retraction_mol_csv = os.path.join(tmpdir, "molecules.csv")
    _write_csv(retraction_mol_csv, [["bpc_157", "BPC-157", "", "true"]],
              ["molecule_id", "display_name", "synonyms_csv", "active"])

    orig_esearch = PubMedClient.esearch
    orig_efetch = PubMedClient.efetch_xml

    def fake_esearch(self, **kwargs):
        # The retraction-scan query always matches PMID 111 upstream (its pubtype
        # was just flagged); PMID 222 is never returned by this query.
        return PubMedSearch(query=kwargs.get("term", ""), count=1, webenv="WE", query_key="1", ids=["111"])

    def fake_efetch_xml(self, **kwargs):
        return _pubmed_set_xml(_pubmed_article_xml("111", "Old title [RETRACTED]",
                                                    ["Journal Article", "Retracted Publication"]))

    PubMedClient.esearch = fake_esearch
    PubMedClient.efetch_xml = fake_efetch_xml
    try:
        result = rr.run(db_path=retraction_db, molecules_csv=retraction_mol_csv)
    finally:
        PubMedClient.esearch = orig_esearch
        PubMedClient.efetch_xml = orig_efetch

    check("retraction refresh: ok", result["ok"] is True)
    check("retraction refresh: flagged PMID found via corpus intersection", result["flagged_pmids_in_corpus"] == ["111"])
    check("retraction refresh: exactly one record refreshed", result["refreshed"] == ["111"])
    check("retraction refresh: newly-flagged reported", result["newly_flagged"] == ["111"])

    conn = sqlite3.connect(retraction_db)
    row111 = json.loads(conn.execute("select payload_json from papers where pmid='111'").fetchone()[0])
    row222 = json.loads(conn.execute("select payload_json from papers where pmid='222'").fetchone()[0])
    conn.close()
    check("retraction refresh: flagged row updated in place (title refreshed)",
          row111["title"] == "Old title [RETRACTED]")
    check("retraction refresh: retraction_status stamped", row111["retraction_status"] == "retracted_or_corrected")
    check("retraction refresh: retraction_checked_utc stamped", bool(row111.get("retraction_checked_utc")))
    check("retraction refresh: untouched paper NOT deleted (history preserved)", row222["title"] == "Unrelated paper")
    check("retraction refresh: untouched paper NOT flagged", row222.get("retraction_status", "") != "retracted_or_corrected")

    # REGRESSION: the refreshed row's pre-existing enrichment fields (from
    # iCite/OpenAlex/Semantic Scholar/discovery) must survive a retraction
    # refresh untouched -- a merge, never a wholesale replace with a bare
    # PubMedRecord.to_dict(). This is the exact bug the independent review
    # caught: record.to_dict() only carries PubMed's own fields, so assigning
    # it directly as the new payload silently erased everything else.
    check("retraction refresh preserves first_seen_utc", row111["first_seen_utc"] == "2019-01-01T00:00:00Z")
    check("retraction refresh preserves icite_rcr", row111["icite_rcr"] == 1.23)
    check("retraction refresh preserves icite_apt", row111["icite_apt"] == 0.45)
    check("retraction refresh preserves icite_schema", row111["icite_schema"] == 2)
    check("retraction refresh preserves icite_updated_utc", row111["icite_updated_utc"] == "2020-06-01T00:00:00Z")
    check("retraction refresh preserves citation_count", row111["citation_count"] == 17)
    check("retraction refresh preserves citation_source", row111["citation_source"] == "openalex_doi:api")
    check("retraction refresh preserves citation_updated_utc", row111["citation_updated_utc"] == "2020-06-01T00:00:00Z")
    check("retraction refresh preserves influential_citation_count", row111["influential_citation_count"] == 3)
    check("retraction refresh preserves s2_authors",
          json.loads(row111["s2_authors"]) == [{"name": "A B", "authorId": "1", "url": "https://x"}])
    # The untouched paper's enrichment fields must also be exactly as seeded
    # (it was never even re-fetched, but this pins the fixture as a control).
    check("retraction refresh: untouched paper's enrichment unaffected", row222["icite_rcr"] == 1.23
          and row222["citation_count"] == 17)

    # A total upstream failure (esearch raises for every molecule) must be
    # visible, not swallowed as "0 retractions, all clear".
    def failing_esearch(self, **kwargs):
        raise RuntimeError("simulated upstream outage")

    PubMedClient.esearch = failing_esearch
    try:
        fail_result = rr.run(db_path=retraction_db, molecules_csv=retraction_mol_csv)
    finally:
        PubMedClient.esearch = orig_esearch
    check("retraction refresh: total upstream failure -> ok False", fail_result["ok"] is False)
    check("retraction refresh: total upstream failure -> outcome failed", fail_result["outcome"] == "failed")

    # =========================================================================
    # scripts/run_icite_backfill.py: staleness selection + preserve-on-failure
    # =========================================================================
    import run_icite_backfill as icb  # noqa: E402

    now = datetime.now(timezone.utc)
    fresh_iso = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    stale_iso = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    fresh_paper = {"pmid": "1", "icite_rcr": 1.0, "icite_apt": 1.0, "icite_schema": icb.ICITE_SCHEMA,
                  "icite_updated_utc": fresh_iso}
    stale_paper = {"pmid": "2", "icite_rcr": 1.0, "icite_apt": 1.0, "icite_schema": icb.ICITE_SCHEMA,
                  "icite_updated_utc": stale_iso}
    missing_paper = {"pmid": "3"}
    check("icite: fresh paper not stale at 6-day cutoff", icb._stale(fresh_paper, 6, now) is False)
    check("icite: stale paper IS stale at 6-day cutoff", icb._stale(stale_paper, 6, now) is True)
    check("icite: a genuinely missing paper needs enrichment (not the staleness path)", icb._needs(missing_paper))
    check("icite: an up-to-date paper does not need re-enrichment", not icb._needs(fresh_paper))

    icite_db = os.path.join(tmpdir, "icite.sqlite")
    conn = sqlite3.connect(icite_db)
    conn.execute("create table papers (pmid text primary key, payload_json text, updated_at_utc text)")
    conn.execute("insert into papers values (?, ?, ?)", ("42", json.dumps(dict(stale_paper, pmid="42")), stale_iso))
    conn.commit()
    conn.close()

    import argparse as _argparse

    orig_fetch_icite = icb.fetch_icite
    # Simulate a total iCite outage (got_any False): the existing icite_rcr must
    # survive untouched, not be cleared or overwritten with nothing.
    icb.fetch_icite = lambda pmids, batch_size=200: {}
    try:
        old_argv = sys.argv
        sys.argv = ["run_icite_backfill.py", "--db", icite_db, "--refresh-older-than-days", "6", "--all"]
        try:
            icb.main()
        finally:
            sys.argv = old_argv
    finally:
        icb.fetch_icite = orig_fetch_icite

    conn = sqlite3.connect(icite_db)
    row42 = json.loads(conn.execute("select payload_json from papers where pmid='42'").fetchone()[0])
    conn.close()
    check("icite: total outage preserves the existing rcr value", row42["icite_rcr"] == 1.0)
    check("icite: total outage does not advance icite_updated_utc (still stale)",
          row42["icite_updated_utc"] == stale_iso)

    # =========================================================================
    # scripts/run_impact_backfill.py: rolling-refresh selection + preserve-on-failure
    # =========================================================================
    import run_impact_backfill as rib  # noqa: E402

    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    new_ts = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    papers = [
        {"pmid": "1", "citation_count": 5, "citation_source": "openalex_doi:api", "citation_updated_utc": old_ts},
        {"pmid": "2", "citation_count": 9, "citation_source": "openalex_pmid:api", "citation_updated_utc": new_ts},
        {"pmid": "3", "citation_count": 2, "citation_source": "semanticscholar_doi:api", "citation_updated_utc": old_ts},
    ]
    candidates = rib.openalex_refresh_candidates(papers, 5)
    check("rolling refresh: only OpenAlex-sourced papers are candidates", {p["pmid"] for p in candidates} == {"1", "2"})
    check("rolling refresh: oldest first", candidates[0]["pmid"] == "1")
    check("rolling refresh: never selects a Semantic Scholar fill (fill-only)",
          all(p["pmid"] != "3" for p in candidates))

    impact_db = os.path.join(tmpdir, "impact.sqlite")
    conn = sqlite3.connect(impact_db)
    conn.execute("create table papers (pmid text primary key, payload_json text, updated_at_utc text)")
    conn.execute("insert into papers values (?, ?, ?)",
                ("1", json.dumps({"pmid": "1", "citation_count": 5, "citation_source": "openalex_doi:api",
                                 "citation_updated_utc": old_ts}), old_ts))
    conn.commit()
    conn.close()

    from retarats_pipeline.enrichment.clients import IdentifierMetadataClient

    orig_openalex = IdentifierMetadataClient.openalex_cited_by
    # Upstream OpenAlex has nothing this time (simulated outage/miss): the
    # existing citation_count must survive untouched.
    IdentifierMetadataClient.openalex_cited_by = lambda self, doi="", pmid="", force_refresh=False: (None, "openalex_not_found")
    try:
        old_argv = sys.argv
        sys.argv = ["run_impact_backfill.py", "--db", impact_db, "--max-records", "0", "--rolling-refresh", "1"]
        try:
            rib.main()
        finally:
            sys.argv = old_argv
    finally:
        IdentifierMetadataClient.openalex_cited_by = orig_openalex

    conn = sqlite3.connect(impact_db)
    row1 = json.loads(conn.execute("select payload_json from papers where pmid='1'").fetchone()[0])
    conn.close()
    check("rolling refresh: upstream miss preserves the existing citation_count", row1["citation_count"] == 5)
    check("rolling refresh: upstream miss does not advance citation_updated_utc", row1["citation_updated_utc"] == old_ts)

    # Rolling refresh is bounded: (a) a persistent upstream stall aborts after N
    # consecutive misses instead of grinding through every candidate, (b) a time budget
    # stops it with work-so-far saved, (c) lookups bypass the by-id forever-cache.
    many = [{"pmid": str(1000 + i), "citation_count": 1, "citation_source": "openalex_doi:api",
             "citation_updated_utc": old_ts} for i in range(rib.ROLLING_MAX_CONSECUTIVE_MISSES + 30)]
    calls = {"n": 0, "force": []}

    def _always_miss(self, doi="", pmid="", force_refresh=False):
        calls["n"] += 1
        calls["force"].append(force_refresh)
        return (None, "openalex_not_found")

    def _run_refresh(rows, budget="900"):
        db = os.path.join(tmpdir, f"impact_{len(rows)}_{budget}_{calls['n']}.sqlite")
        c = sqlite3.connect(db)
        c.execute("create table papers (pmid text primary key, payload_json text, updated_at_utc text)")
        for r in rows:
            c.execute("insert into papers values (?, ?, ?)", (r["pmid"], json.dumps(r), old_ts))
        c.commit(); c.close()
        old_argv = sys.argv
        sys.argv = ["run_impact_backfill.py", "--db", db, "--max-records", "0",
                    "--rolling-refresh", str(len(rows)), "--rolling-refresh-budget-sec", budget]
        try:
            rib.main()
        finally:
            sys.argv = old_argv
        c = sqlite3.connect(db)
        out = {pm: json.loads(pl) for pm, pl in c.execute("select pmid, payload_json from papers")}
        c.close()
        return out

    IdentifierMetadataClient.openalex_cited_by = _always_miss
    try:
        _run_refresh(many)
    finally:
        IdentifierMetadataClient.openalex_cited_by = orig_openalex
    check("rolling refresh: persistent misses abort early (not every candidate queried)",
          calls["n"] == rib.ROLLING_MAX_CONSECUTIVE_MISSES)
    check("rolling refresh: lookups bypass the forever-cache (force_refresh=True)", all(calls["force"]))

    def _hit(self, doi="", pmid="", force_refresh=False):
        return (42, "openalex_doi:api")

    IdentifierMetadataClient.openalex_cited_by = _hit
    try:
        saved = _run_refresh(many[:5])
        check("rolling refresh: successful lookups are written", all(r["citation_count"] == 42 for r in saved.values()))
        cleared = _run_refresh(many[:5], budget="0.000001")
    finally:
        IdentifierMetadataClient.openalex_cited_by = orig_openalex
    check("rolling refresh: time budget stops the loop (values left untouched)",
          sum(1 for r in cleared.values() if r["citation_count"] == 42) < 5)

    # =========================================================================
    # retarats_pipeline/rules_version.py: stability + change detection
    # =========================================================================
    from retarats_pipeline.rules_version import compute_rules_version

    rv_dir = tempfile.mkdtemp(prefix="ws4_rules_version_")
    _write_csv(os.path.join(rv_dir, "MOLECULES.csv"),
              [["retatrutide", "Retatrutide", "peptide", "incretin", "clinical", "Retatrutide,LY-3437943", "", "True", ""]],
              ["molecule_id", "display_name", "type", "mechanism_class", "status", "synonyms_csv", "exclusions_csv", "active", "notes"])
    _write_csv(os.path.join(rv_dir, "SEARCH_RULES.csv"),
              [["r1", "retatrutide", "strict", '"Retatrutide"[tiab]', "True", ""]],
              ["rule_id", "molecule_id", "match_strength", "query_string", "active", "notes"])
    with open(os.path.join(rv_dir, "pubmed_discovery.json"), "w") as fh:
        json.dump({"schema": 1, "datetype": "edat", "daily_days": 14}, fh)

    v1 = compute_rules_version(rv_dir)
    v2_ = compute_rules_version(rv_dir)
    check("rules_version: stable across repeated calls with unchanged inputs", v1 == v2_)
    check("rules_version: has the expected rv<schema>- prefix", v1.startswith("rv1-"))

    # Editing MOLECULES.csv (adding a synonym -> changes the rendered CT.gov/EuropePMC
    # query) must change the version.
    _write_csv(os.path.join(rv_dir, "MOLECULES.csv"),
              [["retatrutide", "Retatrutide", "peptide", "incretin", "clinical",
               "Retatrutide,LY-3437943,LY3437943", "", "True", ""]],
              ["molecule_id", "display_name", "type", "mechanism_class", "status", "synonyms_csv", "exclusions_csv", "active", "notes"])
    v3 = compute_rules_version(rv_dir)
    check("rules_version: changes when MOLECULES.csv content changes", v3 != v1)

    # Reverting the edit reproduces the original hash exactly (fully deterministic).
    _write_csv(os.path.join(rv_dir, "MOLECULES.csv"),
              [["retatrutide", "Retatrutide", "peptide", "incretin", "clinical", "Retatrutide,LY-3437943", "", "True", ""]],
              ["molecule_id", "display_name", "type", "mechanism_class", "status", "synonyms_csv", "exclusions_csv", "active", "notes"])
    v4 = compute_rules_version(rv_dir)
    check("rules_version: reverting the edit reproduces the original hash", v4 == v1)

    # Changing the discovery-window policy (a scientific retrieval input) must
    # also change the version, even with molecule/rule files untouched.
    with open(os.path.join(rv_dir, "pubmed_discovery.json"), "w") as fh:
        json.dump({"schema": 1, "datetype": "pdat", "daily_days": 8}, fh)
    v5 = compute_rules_version(rv_dir)
    check("rules_version: changes when the discovery-window policy changes", v5 != v1)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
