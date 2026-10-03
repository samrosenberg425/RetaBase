#!/usr/bin/env python3
"""Offline tests for config/manual_exclusions.csv (hold-in-place record exclusions).

    python3 tests/test_manual_exclusions.py
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from retarats_pipeline import manual_exclusions as mx  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def rec(mol, pmid, title="", abstract="", keywords=""):
    return {"molecule_id": mol, "pmid": pmid, "title": title, "abstract": abstract, "keywords": keywords}


def run():
    td = tempfile.mkdtemp(prefix="mx_")
    path = os.path.join(td, "ex.csv")
    none = set()

    # ---- loading / validation: anything doubtful is IGNORED (kept), never applied ----
    write_csv(path, mx.COLUMNS, [
        ["m1", "111", "", "", "paper row ok", "2026-10-02"],
        ["m1", "", "foo|bar", "keepme", "pattern ok", ""],
        ["m1", "222", "", "", "", ""],                    # no reason
        ["m1", "333", "foo", "x", "both set", ""],         # pmid AND pattern
        ["m1", "", "foo", "", "no protect list", ""],      # pattern without protect terms
        ["m1", "abc", "", "", "non numeric pmid", ""],
        ["m1", "", "", "", "needs pmid or pattern", ""],
        ["ghost", "444", "", "", "unknown molecule", ""],
        ["m1", "555", "", "", "protected pmid", ""],
    ])
    ex = mx.load_exclusions(path, known_molecule_ids={"m1"}, protected={("m1", "555")})
    check("loader keeps exactly the 2 valid rows", len(ex.rules) == 2)
    check("loader reports each ignored row", len(ex.warnings) == 7)
    check("paper row parsed", ex.rules[0].is_paper_row and ex.rules[0].pmid == "111")
    check("pattern row parsed with terms", ex.rules[1].hold_terms == ("foo", "bar") and ex.rules[1].protect_terms == ("keepme",))
    check("missing file is a harmless no-op", not mx.load_exclusions(os.path.join(td, "nope.csv"), protected=none))

    # ---- paper rows: exact (molecule, pmid) only ----
    write_csv(path, mx.COLUMNS, [["m1", "111", "", "", "bad paper", ""], ["m1", "999", "", "", "stale row", ""]])
    ex = mx.load_exclusions(path, protected=none)
    rows = [rec("m1", "111"), rec("m2", "111"), rec("m1", "112")]
    holds, msgs = mx.plan_holds(ex, rows)
    check("paper row holds that molecule's record", 0 in holds)
    check("same PMID under ANOTHER molecule stays live", 1 not in holds)
    check("a different PMID in the same molecule stays live", 2 not in holds)
    check("stale paper row is reported (matches nothing)", any("999" in m and "stale" in m for m in msgs))

    # ---- pattern rows ----
    write_csv(path, mx.COLUMNS, [["mt", "", "metallothionein|metallothioneins", "melanotan|MSH|alpha-MSH", "MT-II clash", ""]])
    ex = mx.load_exclusions(path, protected={("mt", "9")})
    rows = [
        rec("mt", "1", "Metallothionein in brain disorders", "MT-I and MT-II are proteins"),
        rec("mt", "2", "Melanotan II and metallothionein", "melanotan II tested"),              # protected by text
        rec("mt", "3", "Zinc proteins", "keywords only", keywords="['Metallothionein']"),        # keyword hit
        rec("mt", "4", "Study of metallothioneins", "plural form"),
        rec("mt", "5", "Skin tanning", "alpha-MSH analog, no mention of the other protein"),     # no hold term
        rec("mt", "6", "Metallothionein and MSH signalling", "x"),                               # protected by MSH token
        rec("mt", "7", "Metallothionein and MSH2 repair", "MSH2 is a different gene"),           # MSH2 != MSH -> not protected
        rec("mt", "8", "Antimetallothioneinase", "substring must not match"),                    # token boundary
        rec("other", "1", "Metallothionein", "different molecule"),
        rec("mt", "9", "Metallothionein", "a human-approved include"),                           # protected pair
    ]
    holds, msgs = mx.plan_holds(ex, rows)
    held = sorted(rows[i]["pmid"] + ("*" if rows[i]["molecule_id"] != "mt" else "") for i in holds)
    check("pattern holds metallothionein-only records (incl. keyword + plural)", held == ["1", "3", "4", "7"])
    check("protect terms keep melanotan/MSH papers", all(i not in holds for i in (1, 5)))
    check("pattern never reaches another molecule", all(rows[i]["molecule_id"] == "mt" for i in holds))
    check("protected (approved/manual/gold) PMID is never held", 9 not in holds)
    check("substring inside a longer word does not match", 7 not in holds)
    check("hyphen/space variants match", mx._term_regex("MT-II").search("the MT II protein") is not None
          and mx._term_regex("MT-II").search("MT-III") is None)
    h2, _ = mx.plan_holds(ex, rows)
    check("planning is deterministic", sorted(h2) == sorted(holds))
    check("no rules -> nothing held", mx.plan_holds(mx.Exclusions(), rows)[0] == {})

    # ---- safety cap: a runaway pattern is switched OFF, not applied ----
    big = [rec("mt", str(i), "Metallothionein", "x") for i in range(30)] + [rec("mt", "k1", "ok"), rec("mt", "k2", "ok")]
    holds, msgs = mx.plan_holds(ex, big)
    check("pattern above the safety cap is NOT applied", holds == {})
    check("cap trip is reported loudly", any("safety cap" in m for m in msgs))
    small = [rec("mt", "1", "Metallothionein", "x"), rec("mt", "2", "ok")]
    check("tiny molecules are not subject to the fraction cap", len(mx.plan_holds(ex, small)[0]) == 1)

    # ---- protected set is built from approved includes / manual / gold only ----
    bench, manual, gold = (os.path.join(td, n) for n in ("b.csv", "m.csv", "g.csv"))
    write_csv(bench, ["molecule_id", "id_type", "id", "expected", "reason", "status", "reviewed_by", "reviewed_on"], [
        ["a", "pmid", "1", "include", "", "approved", "", ""],
        ["a", "pmid", "2", "include", "", "proposed", "", ""],     # not approved -> not protected
        ["a", "pmid", "3", "exclude", "", "approved", "", ""],     # an exclude is not protected
        ["a", "nct_id", "NCT1", "include", "", "approved", "", ""],  # not a pmid
    ])
    write_csv(manual, ["molecule_id", "pmid", "note"], [["b", "10", ""]])
    write_csv(gold, ["molecule_id", "must_retrieve_pmids", "must_not_retrieve_pmids", "notes"], [["c", "20; 21", "", ""]])
    prot = mx.load_protected(bench, manual, gold)
    check("protected = approved pmid includes + manual_pmids + gold must-retrieve",
          prot == {("a", "1"), ("b", "10"), ("c", "20"), ("c", "21")})

    # ---- the shipped config is valid and protects the shipped benchmark positives ----
    shipped = mx.load_exclusions(os.path.join(ROOT, "config", "manual_exclusions.csv"),
                                 protected=mx.load_protected(*(os.path.join(ROOT, p) for p in (mx.BENCHMARK_PATH, mx.MANUAL_PMIDS_PATH, mx.GOLD_PATH))))
    check("shipped manual_exclusions.csv has no warnings", shipped.warnings == [])
    pos = [rec("melanotan_ii", "31953620", "Melanotan II: a possible cause of renal infarction", "Melanotan II is..."),
           rec("melanotan_ii", "37478579", "Melanotan-II reverses memory impairment", "Melanotan-II (MT-II) ...")]
    check("shipped rules never hold the approved Melanotan II positives", mx.plan_holds(shipped, pos)[0] == {})
    neg = [rec("melanotan_ii", "29085556", "Metallothionein in Brain Disorders.", "MT-I and MT-II have been localized...")]
    check("shipped rules hold the known metallothionein false positive", len(mx.plan_holds(shipped, neg)[0]) == 1)

    # ---- end to end: build keeps the record, marks it excluded_noise, drops it from public_records.csv ----
    spec = importlib.util.spec_from_file_location("build_curated_database", os.path.join(ROOT, "scripts", "build_curated_database.py"))
    bcd = importlib.util.module_from_spec(spec)
    sys.modules["build_curated_database"] = bcd
    spec.loader.exec_module(bcd)
    from retarats_pipeline.enrichment.common import save_payload_rows

    db = os.path.join(td, "c.sqlite")
    conn = sqlite3.connect(db)
    papers = [{"pmid": "1", "title": "Metallothionein in brain disorders", "abstract": "MT-II protein. " * 5, "pub_year": 2017},
              {"pmid": "2", "title": "Melanotan II reduces appetite in rats", "abstract": "Melanotan II (MT-II) given i.p.", "pub_year": 2020}]
    evid = [{"evidence_id": f"{p}:mt:r1", "pmid": p, "molecule_id": "mt", "molecule_name": "mt", "rule_id": "r1",
             "pub_year": 2020, "evidence_class": "preclinical_invivo"} for p in ("1", "2")]
    save_payload_rows(conn, "papers", "pmid", papers)
    save_payload_rows(conn, "evidence", "evidence_id", evid)
    conn.close()
    ex2 = mx.Exclusions(rules=[mx.Exclusion("mt", "", ("metallothionein",), ("melanotan",), "test", "", 2)])
    orig = bcd.load_exclusions
    bcd.load_exclusions = lambda *a, **k: ex2
    try:
        out = os.path.join(td, "out")
        res = bcd.build(db, out)
    finally:
        bcd.load_exclusions = orig

    def read(name):
        with open(os.path.join(out, name), newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    cur = {r["pmid"]: r for r in read("curated_evidence.csv")}
    check("held record is KEPT in curated_evidence.csv", set(cur) == {"1", "2"})
    check("held record is marked excluded_noise / manual:exclude",
          cur["1"]["publication_status"] == "excluded_noise" and cur["1"]["publish_rule_id"] == "manual:exclude")
    check("held record carries the reason", "manual exclusion: test" in cur["1"]["review_reason"])
    check("unheld record is untouched", cur["2"]["publication_status"] != "excluded_noise")
    check("held record is absent from public_records.csv", "1" not in {r["pmid"] for r in read("public_records.csv")})
    rep = read("manual_exclusions_report.csv")
    check("audit report lists exactly the held record", [r["pmid"] for r in rep] == ["1"])
    check("build stats count the holds", res["stats"].get("manual_holds") == 1)
    mi = {r["molecule_id"]: r for r in read("molecule_index.csv")}
    check("molecule card counts only published records (not held ones)",
          mi["mt"]["total_records"] == "1" and mi["mt"]["record_count"] == "1")
    check("molecule card still reports the held count", mi["mt"]["held"] == "1")

    # ---- benchmark: held records count as absent, and are reported separately ----
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import benchmark_retrieval as br  # noqa: E402

    held = br.manually_held_keys(db, os.path.join(td, "bench_ex.csv"))
    check("benchmark: missing exclusions file -> nothing held", held == set())
    write_csv(os.path.join(td, "bench_ex.csv"), mx.COLUMNS,
              [["mt", "", "metallothionein", "melanotan", "test", ""]])
    held = br.manually_held_keys(db, os.path.join(td, "bench_ex.csv"))
    check("benchmark: held (molecule, pmid) pairs are computed from the corpus", held == {("mt", "1")})

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
