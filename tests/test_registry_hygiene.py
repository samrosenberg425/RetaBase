#!/usr/bin/env python3
"""Offline tests for registry/preprint search hygiene: quoted terms, the term blocklist, and
marking stored rows stale when a molecule's (corrected) search no longer returns them.

    python3 tests/test_registry_hygiene.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from retarats_pipeline.enrichment import registry as reg  # noqa: E402
from retarats_pipeline.enrichment.registry_stale import ANOMALY_MIN_STORED, drop_stale, is_stale, mark_stale_rows  # noqa: E402
from retarats_pipeline.enrichment.clients import ClinicalTrialsClient, IdentifierMetadataClient  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def make_table(path, table, key, rows):
    conn = sqlite3.connect(path)
    conn.execute(f"create table if not exists {table} ({key} text primary key, payload_json text, updated_at_utc text)")
    for r in rows:
        conn.execute(f"insert or replace into {table} values (?, ?, ?)", (r[key], json.dumps(r), "2020-01-01T00:00:00Z"))
    conn.commit()
    conn.close()


def read_table(path, table):
    conn = sqlite3.connect(path)
    out = {}
    for (pl,) in conn.execute(f"select payload_json from {table}"):
        d = json.loads(pl)
        out[d.get("nct_id") or d.get("id")] = d
    conn.close()
    return out


def run():
    td = tempfile.mkdtemp(prefix="hyg_")
    cwd = os.getcwd()
    os.chdir(td)
    os.makedirs("config")
    try:
        # ---- query building: every term quoted; blocklist; display name protected ----
        mol = {"molecule_id": "m1", "display_name": "Foo Peptide", "synonyms_csv": "FP-1, NR, Foo-2"}
        tq = reg.trials_query(mol)
        check("trials query quotes every term, including single tokens and hyphenated codes",
              tq == '"Foo Peptide" OR "FP-1" OR "NR" OR "Foo-2"')
        pq = reg.preprints_query(mol)
        check("preprint query quotes every term", pq == '("Foo Peptide" OR "FP-1" OR "NR" OR "Foo-2") AND SRC:PPR')

        with open("config/registry_term_blocklist.csv", "w") as fh:
            fh.write("molecule_id,term,reason\nm1,NR,bare abbreviation matches unrelated records\nother,FP-1,not this molecule\n")
        check("blocklisted term is dropped for that molecule only", reg.trials_query(mol) == '"Foo Peptide" OR "FP-1" OR "Foo-2"')
        check("blocklist applies to preprint queries too", '"NR"' not in reg.preprints_query(mol))
        with open("config/registry_term_blocklist.csv", "w") as fh:
            fh.write("molecule_id,term,reason\nm1,Foo Peptide,x\nm1,FP-1,x\nm1,NR,x\nm1,Foo-2,x\n")
        check("display name can never be blocked: molecule always keeps a query", reg.trials_query(mol) == '"Foo Peptide"')
        os.remove("config/registry_term_blocklist.csv")
        check("no blocklist file is a no-op", reg.trials_query(mol).count("OR") == 3)

        # ---- mark_stale_rows (unit) ----
        db = os.path.join(td, "u.sqlite")
        rows = [{"nct_id": f"NCT{i:08d}", "molecule_id": "A"} for i in range(3)]
        rows += [{"nct_id": f"NCT9{i:07d}", "molecule_id": "B"} for i in range(3)]
        make_table(db, "trials", "nct_id", rows)
        conn = sqlite3.connect(db)
        res = mark_stale_rows(conn, "trials", "nct_id", {"NCT00000000"}, {"A"}, {"A": 1}, "2026-10-02T00:00:00Z",
                              key_norm=lambda k: str(k).upper())
        conn.close()
        t = read_table(db, "trials")
        check("rows of a completed molecule not returned this run are marked stale", res["marked"] == 2
              and is_stale(t["NCT00000001"]) and is_stale(t["NCT00000002"]))
        check("the returned row is not marked", not is_stale(t["NCT00000000"]))
        check("a molecule whose retrieval did not complete is left untouched", not any(is_stale(t[k]) for k in t if k.startswith("NCT9")))
        check("nothing is deleted", len(t) == 6)
        check("drop_stale hides marked rows from feeds", len(drop_stale(list(t.values()))) == 4)
        conn = sqlite3.connect(db)
        res2 = mark_stale_rows(conn, "trials", "nct_id", {"NCT00000000"}, {"A"}, {"A": 1}, "2026-10-09T00:00:00Z",
                               key_norm=lambda k: str(k).upper())
        conn.close()
        check("re-marking is idempotent and keeps the original stale_since", res2["marked"] == 0
              and read_table(db, "trials")["NCT00000001"]["stale_since_utc"] == "2026-10-02T00:00:00Z")

        # anomaly: an EMPTY result for a molecule with many stored rows is an API glitch, not "all stale"
        db2 = os.path.join(td, "anom.sqlite")
        make_table(db2, "trials", "nct_id", [{"nct_id": f"NCT{i:08d}", "molecule_id": "A"} for i in range(ANOMALY_MIN_STORED)])
        conn = sqlite3.connect(db2)
        res3 = mark_stale_rows(conn, "trials", "nct_id", set(), {"A"}, {"A": 0}, "now", key_norm=str)
        conn.close()
        check("empty retrieval for a molecule with many stored rows is skipped (fail-open)",
              res3["marked"] == 0 and res3["skipped_anomalies"] == ["A"])
        db3 = os.path.join(td, "small.sqlite")
        make_table(db3, "trials", "nct_id", [{"nct_id": "NCT00000001", "molecule_id": "A"}])
        conn = sqlite3.connect(db3)
        res4 = mark_stale_rows(conn, "trials", "nct_id", set(), {"A"}, {"A": 0}, "now", key_norm=str)
        conn.close()
        check("a small molecule with a genuinely empty result is reconciled", res4["marked"] == 1)

        # ---- run_trials_fetch end to end (patched client, no network) ----
        import run_trials_fetch  # noqa: E402
        import build_trials_json  # noqa: E402

        mol_csv = os.path.join(td, "mols.csv")
        with open(mol_csv, "w") as fh:
            fh.write("molecule_id,display_name,synonyms_csv,active\nretatrutide,Retatrutide,,true\n")

        def study(nct):
            return {"protocolSection": {"identificationModule": {"nctId": nct, "briefTitle": f"Retatrutide trial {nct}"},
                                        "statusModule": {"overallStatus": "RECRUITING"}}}

        def page(studies, **kw):
            r = {"items": studies, "pages": 1, "total_reported": len(studies), "rows_retrieved": len(studies),
                 "exhausted": True, "partial": False, "ok": True, "error": "", "source": "api"}
            r.update(kw)
            return r

        def patched(result):
            orig = ClinicalTrialsClient.search_all
            ClinicalTrialsClient.search_all = lambda self, q, page_size=100, max_pages=20: result
            return orig

        tdb = os.path.join(td, "trials.sqlite")
        make_table(tdb, "trials", "nct_id", [
            {"nct_id": "NCT00000001", "molecule_id": "retatrutide", "first_seen_utc": "2020-01-01T00:00:00Z"},   # junk from an old query
            {"nct_id": "NCT00000002", "molecule_id": "retatrutide", "first_seen_utc": "2020-01-01T00:00:00Z"}])  # still returned
        orig = patched(page([study("NCT00000002")]))
        try:
            run_trials_fetch.run(db_path=tdb, molecules_csv=mol_csv)
            t = read_table(tdb, "trials")
            check("fetch: a stored trial the corrected search no longer returns is marked stale", is_stale(t["NCT00000001"]))
            check("fetch: a trial the search still returns is live", not is_stale(t["NCT00000002"]))
            check("fetch: the stale trial is hidden from the published feed",
                  [r["nct_id"] for r in build_trials_json._load_trials(tdb)] == ["NCT00000002"])

            ClinicalTrialsClient.search_all = lambda self, q, page_size=100, max_pages=20: page([study("NCT00000001"), study("NCT00000002")])
            run_trials_fetch.run(db_path=tdb, molecules_csv=mol_csv)
            t = read_table(tdb, "trials")
            check("fetch: a trial the search returns again is un-marked automatically", not is_stale(t["NCT00000001"]))

            # partial / failed retrieval must NOT mark anything
            make_table(tdb, "trials", "nct_id", [{"nct_id": "NCT00000003", "molecule_id": "retatrutide"}])
            ClinicalTrialsClient.search_all = lambda self, q, page_size=100, max_pages=20: page([study("NCT00000002")], partial=True, exhausted=False)
            run_trials_fetch.run(db_path=tdb, molecules_csv=mol_csv)
            check("fetch: a PARTIAL retrieval never marks rows stale", not is_stale(read_table(tdb, "trials")["NCT00000003"]))
            ClinicalTrialsClient.search_all = lambda self, q, page_size=100, max_pages=20: {
                "items": [], "pages": 0, "total_reported": None, "rows_retrieved": 0, "exhausted": False,
                "partial": False, "ok": False, "error": "boom", "source": "http_error"}
            try:
                run_trials_fetch.run(db_path=tdb, molecules_csv=mol_csv)
            except SystemExit:
                pass
            check("fetch: a FAILED retrieval never marks rows stale", not is_stale(read_table(tdb, "trials")["NCT00000003"]))
        finally:
            ClinicalTrialsClient.search_all = orig

        # ---- precision guard: keep a hit only if its own record names the molecule ----
        names = reg.molecule_all_names({"display_name": "MOTS-c", "synonyms_csv": "MOTSc, MOTS C, XY"})
        check("guard names: display + synonyms, 2-letter codes ignored", names == ["MOTS-c", "MOTSc", "MOTS C"])
        check("guard matches hyphen/space/case variants", reg.names_molecule("a trial of mots c in adults", names)
              and reg.names_molecule("MOTS-c", names))
        check("guard rejects records that do not name it", not reg.names_molecule("Cohort of deafness-gene screening", names))
        check("guard does not match inside a longer word", not reg.names_molecule("MOTSCX", ["MOTSc"]))
        check("guard is fail-open when the molecule has no usable names", reg.names_molecule("anything", []))
        check("guard: LL-37 / LL 37 / LL37 are the same name", all(reg.names_molecule(t, ["LL-37"]) for t in
              ("Intratumoral injections of LL37", "the LL 37 peptide", "ll-37")))
        check("guard: MT-II also finds MTII but not MT-III", reg.names_molecule("MTII dosing", ["MT-II"])
              and not reg.names_molecule("MT-III", ["MT-II"]))

        # reviewed always-keep list overrides the guard (brand name missing from synonyms)
        with open("config/registry_keep.csv", "w") as fh:
            fh.write("molecule_id,id,reason\nretatrutide,nct00000020,brand-name-only trial\n")
        check("keep list loads (ids upper-cased)", reg.load_registry_keep() == {("retatrutide", "NCT00000020")})
        kdb = os.path.join(td, "keep.sqlite")
        brand = {"protocolSection": {"identificationModule": {"nctId": "NCT00000020", "briefTitle": "Post-marketing surveillance for BrandX"},
                                     "statusModule": {"overallStatus": "COMPLETED"}}}
        stranger = {"protocolSection": {"identificationModule": {"nctId": "NCT00000021", "briefTitle": "Unrelated"},
                                        "statusModule": {"overallStatus": "COMPLETED"}}}
        orig = patched(page([brand, stranger]))
        try:
            run_trials_fetch.run(db_path=kdb, molecules_csv=mol_csv)
        finally:
            ClinicalTrialsClient.search_all = orig
        kt = read_table(kdb, "trials")
        check("keep list: a reviewed trial that does not name the molecule is kept", "NCT00000020" in kt)
        check("keep list: other non-naming trials are still skipped", "NCT00000021" not in kt)
        os.remove("config/registry_keep.csv")

        # expected-empty list: a reviewed-empty molecule IS reconciled even with many stored rows
        edb = os.path.join(td, "empty.sqlite")
        make_table(edb, "trials", "nct_id", [{"nct_id": f"NCT{i:08d}", "molecule_id": "A"} for i in range(ANOMALY_MIN_STORED + 2)])
        conn = sqlite3.connect(edb)
        r_exp = mark_stale_rows(conn, "trials", "nct_id", set(), {"A"}, {"A": 0}, "now", key_norm=str, expected_empty={"A"})
        conn.close()
        check("expected-empty molecule bypasses the anomaly guard and is reconciled", r_exp["marked"] == ANOMALY_MIN_STORED + 2)
        with open("config/registry_expected_empty.csv", "w") as fh:
            fh.write("molecule_id,reason\nA,reviewed empty\n")
        check("expected-empty list loads", reg.load_registry_expected_empty() == {"A"})
        os.remove("config/registry_expected_empty.csv")

        gdb = os.path.join(td, "guard.sqlite")
        make_table(gdb, "trials", "nct_id", [
            {"nct_id": "NCT00000010", "molecule_id": "retatrutide"}])   # previously stored junk
        junk = {"protocolSection": {"identificationModule": {"nctId": "NCT00000010", "briefTitle": "Deafness gene screening cohort"},
                                    "statusModule": {"overallStatus": "RECRUITING"}}}
        buried = {"protocolSection": {"identificationModule": {"nctId": "NCT00000011", "briefTitle": "Obesity study"},
                                      "descriptionModule": {"detailedDescription": "Participants receive retatrutide weekly."},
                                      "statusModule": {"overallStatus": "RECRUITING"}}}
        orig = patched(page([junk, buried, study("NCT00000012")]))
        try:
            run_trials_fetch.run(db_path=gdb, molecules_csv=mol_csv)
        finally:
            ClinicalTrialsClient.search_all = orig
        g = read_table(gdb, "trials")
        check("guard: a hit that does not name the molecule is not stored", "NCT00000010" in g and is_stale(g["NCT00000010"]))
        check("guard: a record naming the molecule only in its description is kept", "NCT00000011" in g and not is_stale(g["NCT00000011"]))
        check("guard: a record naming it in the title is kept", "NCT00000012" in g and not is_stale(g["NCT00000012"]))
        check("guard: the junk is hidden from the published feed",
              {r["nct_id"] for r in build_trials_json._load_trials(gdb)} == {"NCT00000011", "NCT00000012"})

        # ---- run_preprints_fetch end to end ----
        import run_preprints_fetch  # noqa: E402
        import build_preprints_json  # noqa: E402

        pdb = os.path.join(td, "pp.sqlite")
        make_table(pdb, "preprints", "id", [
            {"id": "10.1101/ppr1", "molecule_id": "retatrutide"}, {"id": "10.1101/ppr2", "molecule_id": "retatrutide"}])

        def pp(i):
            return {"id": i, "source": "PPR", "title": f"Preprint {i}", "abstractText": "A retatrutide study.", "doi": f"10.1101/{i.lower()}",
                    "firstPublicationDate": "2026-01-01"}

        orig_pp = IdentifierMetadataClient.europepmc_search_all
        IdentifierMetadataClient.europepmc_search_all = lambda self, q, page_size=100, result_type="core", max_pages=20: page([pp("PPR2")])
        try:
            run_preprints_fetch.run(db_path=pdb, molecules_csv=mol_csv)
        finally:
            IdentifierMetadataClient.europepmc_search_all = orig_pp
        gp = os.path.join(td, "ppguard.sqlite")
        off = {"id": "PPROFF", "source": "PPR", "title": "Unrelated mitochondria paper", "abstractText": "Nothing relevant here.",
               "doi": "10.1101/off", "firstPublicationDate": "2026-01-01"}
        noabs = {"id": "PPRNOABS", "source": "PPR", "title": "Unrelated title", "doi": "10.1101/noabs",
                 "firstPublicationDate": "2026-01-01"}
        IdentifierMetadataClient.europepmc_search_all = lambda self, q, page_size=100, result_type="core", max_pages=20: page([off, noabs, pp("PPR3")])
        try:
            run_preprints_fetch.run(db_path=gp, molecules_csv=mol_csv)
        finally:
            IdentifierMetadataClient.europepmc_search_all = orig_pp
        gpr = read_table(gp, "preprints")
        check("preprint guard: an abstract that does not name the molecule is skipped", "10.1101/off" not in gpr)
        check("preprint guard: a preprint with no abstract is kept (cannot be judged)", "10.1101/noabs" in gpr)
        check("preprint guard: a preprint naming the molecule is kept", "10.1101/ppr3" in gpr)
        p = read_table(pdb, "preprints")
        check("preprints: stored rows the corrected search no longer returns are marked stale", is_stale(p["10.1101/ppr1"]))
        check("preprints: a row the search still returns is live", not is_stale(p["10.1101/ppr2"]))
        check("preprints: stale rows are hidden from the published feed",
              [r["id"] for r in build_preprints_json._load_preprints(pdb)] == ["10.1101/ppr2"])
    finally:
        os.chdir(cwd)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
