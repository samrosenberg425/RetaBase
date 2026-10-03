#!/usr/bin/env python3
"""Offline tests for molecule identity (retarats_pipeline/identity.py, config/MOLECULE_IDENTITY.csv),
the offline re-evaluation of stored records, and the search-cache key fix.

    python3 tests/test_identity.py        (use .venv/bin/python if system python lacks `requests`)
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
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from retarats_pipeline import identity as idn  # noqa: E402
from retarats_pipeline.enrichment import registry as reg  # noqa: E402
from retarats_pipeline.enrichment.common import save_payload_rows, search_cache_key  # noqa: E402

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


OVERLAY_HEADER = ["molecule_id", "term", "role", "context_any", "exclude_any", "zones", "discovery", "applies_to",
                  "case_sensitive", "evidence", "reviewed_on"]


def mols():
    return [
        {"molecule_id": "vip", "display_name": "VIP (Vasoactive Intestinal Peptide)",
         "synonyms_csv": "Vasoactive Intestinal Peptide, VIP", "exclusions_csv": "VIP alone broad"},
        {"molecule_id": "ldn", "display_name": "Low-Dose Naltrexone (LDN)", "synonyms_csv": "Low dose naltrexone, LDN",
         "exclusions_csv": "LDN alone ambiguous"},
        {"molecule_id": "tp", "display_name": "Teriparatide", "synonyms_csv": "Teriparatide, PTH 1-34, Forteo",
         "exclusions_csv": ""},
        {"molecule_id": "mt", "display_name": "Melanotan II", "synonyms_csv": "Melanotan II, MT-II", "exclusions_csv": "MT2"},
        {"molecule_id": "nr", "display_name": "Nicotinamide Riboside (NR)", "synonyms_csv": "Nicotinamide riboside, NR",
         "exclusions_csv": "NR (alone)"},
    ]


def run():
    # ------------------------------------------------------------------ term matching
    c = idn.compile_term
    check("term: hyphen/space/none are interchangeable", all(c("MT-II").search(t) for t in ("MT-II", "MT II", "MTII")))
    check("term: but a comma is not a separator", not c("TB-4").search("TB, 4 each"))
    check("term: parentheses spelled in the term are honoured", c("PTH(1-34)").search("PTH(1-34)")
          and c("PTH(1-34)").search("PTH 1-34") and c("PTH(1-34)").search("PTH(1–34)"))
    check("term: never starts or ends inside a longer word", not c("metallothionein").search("metallothioneinase")
          and not c("PTH(1-34)").search("hPTH(1-34)") and not c("MT-II").search("MT-III"))
    check("term: plain-word names also match their plural; codes and acronyms do not",
          c("Kisspeptin").search("Kisspeptins") and not c("TB4").search("TB4s") and not c("MT-II").search("MT-IIs"))
    check("term: slash/bracket variants ('ActRIIB/Fc', 'PTH [1-34]')", c("ActRIIB-Fc").search("ActRIIB/Fc") and c("PTH(1-34)").search("PTH [1-34]"))
    check("term: adjacent digit runs need a separator ('22-2' is not '222'; '1-34' is not '134')",
          not c("P-22-2").search("P222") and c("P-22-2").search("P22-2") and not c("PTH(1-34)").search("PTH134")
          and c("PTH(1-34)").search("PTH 1-34"))
    check("preprint markup is stripped before matching ('VPAC<sub>1</sub>' reads VPAC1, entities decoded)",
          idn.plain_text("VPAC <sub>1</sub> and <i>TB</i><sub>4</sub> &gt; 1 <h4>ABSTRACT</h4>x").replace("  ", " ").startswith("VPAC 1 and TB4 > 1")
          and idn.plain_text("VPAC<sub>1</sub>") == "VPAC1")
    check("term: Greek letters are tokens", c("Tα1").search("the Tα1 peptide"))
    check("acronym detection", idn.is_acronym("VIP") and idn.is_acronym("TB-4") and not idn.is_acronym("Melanotan")
          and not idn.is_acronym("MOTS-c") and not idn.is_acronym("Thymosin alpha 1"))
    ctx = idn.compile_context
    check("context words only need a word start", ctx("thymopoietin").search("thymopoietin32-36")
          and ctx("interneuron").search("VIP interneurons") and not ctx("MT-I").search("MT-II"))

    # ------------------------------------------------------------------ config + defaults + overlay
    td = tempfile.mkdtemp(prefix="idn_")
    ov = os.path.join(td, "ov.csv")
    write_csv(ov, OVERLAY_HEADER, [
        ["vip", "VIP", "contextual_alias", "vasoactive intestinal|PACAP|VPAC1", "interneuron|variable importance", "", "", "", "", "t", ""],
        ["vip", "Aviptadil", "specific_alias", "", "", "", "ctgov;preprints", "", "", "INN", ""],
        ["ldn", "LDN", "contextual_alias", "naltrexone", "low-density neutrophil|LDN-193189", "", "", "", "", "t", ""],
        ["tp", "PTH(1-34)|hPTH(1-34)", "specific_alias", "", "", "", "ctgov", "", "", "MeSH", ""],
        ["mt", "MT-II", "contextual_alias", "melanotan|melanocortin|MSH", "metallothionein|metatarsal", "", "", "", "", "t", ""],
        ["mt", "Melanotan 2", "specific_alias", "", "", "", "", "", "", "t", ""],
        ["nr", "NR", "contextual_alias", "nicotinamide", "", "", "", "", "", "t", ""],
        ["ghost", "Nothing", "specific_alias", "", "", "", "", "", "", "unknown molecule", ""],
        ["tp", "Bad", "weird_role", "", "", "", "", "", "", "bad role", ""],
        ["tp", "PubMedOnly", "specific_alias", "", "", "", "pubmed", "", "", "pubmed discovery is not the overlay's job", ""],
    ])
    keep = {("ldn", "9999"), ("vip", "NCT00000099")}
    cfg = idn.load_identity_config(mols(), ov, keep)
    check("overlay: unknown molecule / bad role rows are ignored with a warning, never crash",
          any("unknown molecule_id 'ghost'" in w for w in cfg.warnings) and any("bad role" in w for w in cfg.warnings))
    check("overlay: a PubMed discovery flag is rejected (PubMed discovery is SEARCH_RULES.csv only)",
          any("PubMed discovery" in w for w in cfg.warnings) and cfg.discovery_terms("tp", "ctgov") == ["PTH(1-34)", "hPTH(1-34)"]
          and "pubmed" not in "".join(r.term for r in cfg.by_molecule["tp"].specific if "pubmed" in r.discovery))
    check("defaults: untouched molecules behave like the old name guard (display + synonyms identify)",
          [r.term for r in cfg.by_molecule["tp"].canonical] == ["Teriparatide"]
          and {r.term for r in cfg.by_molecule["tp"].specific} >= {"PTH 1-34", "Forteo"})
    check("defaults: a term MOLECULES.csv says is ambiguous ('NR (alone)') is contextual without any overlay row",
          idn.ambiguous_tokens(mols()[4]) == {"nr"})
    v1 = cfg.version
    cfg_b = idn.load_identity_config(mols(), ov, keep)
    check("rules_version is deterministic", v1 == cfg_b.version and v1.startswith("id1-"))
    write_csv(ov, OVERLAY_HEADER, [["vip", "VIP", "contextual_alias", "vasoactive intestinal|PACAP", "", "", "", "", "", "t", ""]])
    check("rules_version changes when the rules change", idn.load_identity_config(mols(), ov, keep).version != v1)
    write_csv(ov, OVERLAY_HEADER, [
        ["vip", "VIP", "contextual_alias", "vasoactive intestinal|PACAP|VPAC1", "interneuron|variable importance", "", "", "", "", "t", ""],
        ["vip", "Aviptadil", "specific_alias", "", "", "", "ctgov;preprints", "", "", "INN", ""],
        ["ldn", "LDN", "contextual_alias", "naltrexone", "low-density neutrophil|LDN-193189", "", "", "", "", "t", ""],
        ["tp", "PTH(1-34)|hPTH(1-34)", "specific_alias", "", "", "", "ctgov", "", "", "MeSH", ""],
        ["mt", "MT-II", "contextual_alias", "melanotan|melanocortin|MSH", "metallothionein|metatarsal", "", "", "", "", "t", ""],
        ["nr", "NR", "contextual_alias", "nicotinamide", "", "", "", "", "", "t", ""],
    ])
    cfg = idn.load_identity_config(mols(), ov, keep)

    def ev(source, mid, key="", **zones):
        return idn.evaluate(cfg, source, mid, zones, key)

    # ------------------------------------------------------------------ evaluation
    v = ev("preprints", "vip", title="Vasoactive intestinal peptide in asthma", abstract="VIP was infused.")
    check("canonical name -> pass", v.outcome == idn.PASS and v.match_type == idn.M_CANONICAL)
    v = ev("preprints", "vip", title="Variable importance in projection", abstract="Metabolites with VIP > 1 were selected.")
    check("alias alone next to a vetoing context (VIP = variable importance) -> exclude", v.outcome == idn.EXCLUDE)
    v = ev("preprints", "vip", title="A VIP database search", abstract="we searched CNKI and VIP")
    check("alias alone with no context and no veto -> hold", v.outcome == idn.HOLD)
    v = ev("preprints", "vip", title="VIP interneurons gate cortex", abstract="VIP interneurons ... vasoactive intestinal peptide neurons")
    check("a vasoactive-intestinal-peptide name still passes even next to 'interneuron' (specific alias wins)", v.outcome == idn.PASS)
    v = ev("preprints", "vip", title="PACAP and VIP signalling in the gut", abstract="VIP and PACAP act on VPAC1.")
    check("contextual alias + positive context -> pass (contextual_alias)", v.outcome == idn.PASS and v.match_type == idn.M_CONTEXTUAL)
    v = ev("preprints", "vip", title="VIP interneurons and PACAP", abstract="VIP interneurons express PACAP receptors.")
    check("contextual alias + a vetoing context (interneuron) -> exclude, with the reason", v.outcome == idn.EXCLUDE and "interneuron" in v.reason)
    v = ev("ctgov", "vip", brief_title="Aviptadil for COVID-19", interventions="DRUG | Aviptadil | inhaled")
    check("specific alias (aviptadil) identifies VIP in a trial", v.outcome == idn.PASS and v.matched_term == "Aviptadil")
    # acronyms are case-sensitive
    v = ev("preprints", "ldn", title="Low-density neutrophils", abstract="ldn expression")
    check("contextual acronym is case-sensitive (lowercase 'ldn' is not LDN)", v.outcome == idn.HOLD)
    v = ev("preprints", "ldn", title="LDN for fibromyalgia", abstract="Naltrexone 4.5 mg (LDN) reduced pain.")
    check("LDN next to naltrexone passes", v.outcome == idn.PASS)
    v = ev("preprints", "ldn", title="LDN-193189 blocks BMP", abstract="naltrexone was not used; LDN-193189 inhibits ALK2")
    check("LDN-193189 (BMP inhibitor) never counts as low-dose naltrexone", v.outcome != idn.PASS)
    v = ev("preprints", "ldn", title="Land degradation neutrality (LDN) assessment", abstract="grassland")
    check("LDN = land degradation neutrality -> hold", v.outcome == idn.HOLD)
    # exclusion terms on a canonical-less alias (MT-II vs metallothionein)
    v = ev("pubmed", "mt", title="Metallothionein in brain disorders", abstract="MT-I and MT-II have been localized...")
    check("MT-II next to metallothionein -> exclude (approved benchmark regression 29085556)", v.outcome == idn.EXCLUDE)
    v = ev("pubmed", "mt", title="Cyclic alpha-MSH analogues", abstract="potent cyclic alpha-MSH analogues, such as MT-II and SHU-9119")
    check("MT-II next to melanocortin context -> pass", v.outcome == idn.PASS)
    v = ev("pubmed", "mt", title="Pressure below the metatarsals", abstract="MT-II>MT-III>MT-I peak pressures")
    check("MT-II = second metatarsal -> not pass", v.outcome != idn.PASS)
    v = ev("pubmed", "mt", title="Melanotan II renal infarction", abstract="case report")
    check("canonical 'Melanotan II' passes regardless of other words", v.outcome == idn.PASS and v.match_type == idn.M_CANONICAL)
    v = ev("pubmed", "mt", title="Melanotan-2 tanning", abstract="")
    check("overlay alias 'Melanotan 2' matches 'Melanotan-2'", v.outcome == idn.PASS)
    # default-contextual NR
    v = ev("preprints", "nr", title="NR in plants", abstract="NR reductase")
    check("NR alone (default-ambiguous) -> hold", v.outcome == idn.HOLD)
    v = ev("preprints", "nr", title="Nicotinamide riboside supplementation", abstract="")
    check("NR full name -> pass", v.outcome == idn.PASS)
    # aliases with parentheses
    v = ev("ctgov", "tp", brief_title="Bone study", interventions="DRUG | PTH(1-34) | daily injection")
    check("PTH(1-34) identifies teriparatide in a trial", v.outcome == idn.PASS)
    v = ev("ctgov", "tp", brief_title="Bone study", interventions="DRUG | hPTH(1-34) | daily")
    check("hPTH(1-34) identifies teriparatide", v.outcome == idn.PASS)
    # zones: a trial is judged on titles/interventions/arms, not on outcomes/eligibility we do not store
    v = ev("ctgov", "tp", brief_title="Osteoporosis cohort", conditions="Osteoporosis", interventions="OTHER | exercise")
    check("trial with no name in its stored zones -> hold", v.outcome == idn.HOLD and v.match_type == idn.M_NONE)
    v = ev("ctgov", "tp", brief_title="Osteoporosis cohort", other_names="Forteo")
    check("intervention other names count", v.outcome == idn.PASS)
    # indexing zones prove identity for names, not for contextual aliases
    v = ev("pubmed", "tp", title="Bone formation", abstract="anabolic effect", mesh_terms="Teriparatide: therapeutic use")
    check("a MeSH heading naming the molecule is indexing only (held) unless the text names it too", v.outcome == idn.HOLD and v.match_type == "indexing_only")
    # fail-open
    check("fail-open: no text at all", ev("preprints", "tp").outcome == idn.PASS)
    check("fail-open: no abstract and a title that does not name it",
          ev("preprints", "tp", title="Something else").outcome == idn.PASS and ev("preprints", "tp", title="Something else").match_type == idn.M_INSUFFICIENT)
    v = ev("pubmed", "tp", title="Old paper", abstract="", mesh_terms="Spermine: pharmacology")
    check("PubMed record without an abstract but WITH MeSH that does not name the molecule -> hold (it has something to judge)", v.outcome == idn.HOLD)
    check("fail-open: unknown molecule", idn.evaluate(cfg, "ctgov", "no_such_mol", {"brief_title": "x"}).outcome == idn.PASS)
    # manual keep overrides every automated outcome
    v = ev("pubmed", "ldn", key="9999", title="Land degradation neutrality", abstract="x")
    check("manual keep overrides an automated hold", v.outcome == idn.PASS and v.match_type == idn.M_KEEP)
    v = ev("ctgov", "vip", key="nct00000099", brief_title="Unrelated")
    check("manual keep keys are case-insensitive (NCT ids)", v.outcome == idn.PASS and v.match_type == idn.M_KEEP)
    check("verdicts are deterministic", ev("preprints", "vip", title="VIP score", abstract="x") == ev("preprints", "vip", title="VIP score", abstract="x"))

    # ------------------------------------------------------------------ discovery vs identity separation
    check("discovery terms are explicit and per source", cfg.discovery_terms("vip", "ctgov") == ["Aviptadil"]
          and cfg.discovery_terms("vip", "preprints") == ["Aviptadil"] and cfg.discovery_terms("ldn", "ctgov") == [])
    old_cwd = os.getcwd()
    cdir = os.path.join(td, "cwd")
    os.makedirs(os.path.join(cdir, "config"))
    os.chdir(cdir)
    try:
        write_csv("config/MOLECULES.csv", ["molecule_id", "display_name", "synonyms_csv", "exclusions_csv", "active"],
                  [[m["molecule_id"], m["display_name"], m["synonyms_csv"], m["exclusions_csv"], "True"] for m in mols()])
        write_csv("config/MOLECULE_IDENTITY.csv", OVERLAY_HEADER, [
            ["vip", "VIP", "contextual_alias", "vasoactive intestinal", "", "", "", "", "", "t", ""],
            ["vip", "Aviptadil", "specific_alias", "", "", "", "ctgov;preprints", "", "", "INN", ""]])
        m_vip = next(m for m in reg.load_active_molecules() if m["molecule_id"] == "vip")
        check("registry query: a contextual alias is NOT a search term; an overlay discovery term is",
              reg.registry_terms(m_vip, "ctgov") == ["VIP (Vasoactive Intestinal Peptide)", "Vasoactive Intestinal Peptide", "Aviptadil"])
        check("registry query without a source is unchanged (backwards compatible)",
              "VIP" in reg.registry_terms(m_vip))
        check("trials_query/preprints_query quote every term", reg.trials_query(m_vip).count('"') == 6
              and reg.preprints_query(m_vip).endswith(") AND SRC:PPR"))
        # a molecule whose contextual alias is explicitly a discovery term for a source keeps it there
        write_csv("config/MOLECULE_IDENTITY.csv", OVERLAY_HEADER, [
            ["ldn", "LDN", "contextual_alias", "naltrexone", "", "", "preprints", "", "", "t", ""]])
        m_ldn = next(m for m in reg.load_active_molecules() if m["molecule_id"] == "ldn")
        check("explicit discovery overrides the contextual-alias skip, per source",
              "LDN" in reg.registry_terms(m_ldn, "preprints") and "LDN" not in reg.registry_terms(m_ldn, "ctgov"))
        # ---------------------------------------------------------------- gate application + report + policy
        write_csv("config/MOLECULE_IDENTITY.csv", OVERLAY_HEADER, [
            ["vip", "VIP", "contextual_alias", "vasoactive intestinal", "interneuron", "", "", "", "", "t", ""]])
        rows = [{"id": "p1", "molecule_id": "vip", "title": "Vasoactive intestinal peptide in asthma", "abstract": "VIP infusion"},
                {"id": "p2", "molecule_id": "vip", "title": "Variable importance", "abstract": "VIP > 1"},
                {"id": "p3", "molecule_id": "vip", "title": "VIP interneurons", "abstract": "vasoactive intestinal peptide?? no: interneurons"}]
        rep = os.path.join(cdir, "rep.csv")
        kept = idn.apply_identity_gate("preprints", rows, rep)
        check("gate keeps what names the molecule and drops the rest", [r["id"] for r in kept] == ["p1", "p3"] or [r["id"] for r in kept] == ["p1"])
        with open(rep, newline="", encoding="utf-8") as fh:
            held = list(csv.DictReader(fh))
        check("gate report lists every held record with outcome, reason and rules_version",
              held and held[0]["record_key"] == "p2" and held[0]["outcome"] == "hold" and held[0]["rules_version"].startswith("id1-"))
        write_csv("config/identity_policy.csv", ["source", "enforce", "notes"], [["preprints", "false", "off"]])
        check("policy file can switch a source off without code", idn.enforced("preprints") is False
              and idn.enforced("ctgov") is True and len(idn.apply_identity_gate("preprints", rows)) == 3)
        os.remove("config/identity_policy.csv")
        with open("config/MOLECULE_IDENTITY.csv", "w") as fh:
            fh.write("this is not, a valid\x00 overlay")
        check("a broken overlay never breaks the gate (fail-open)", len(idn.apply_identity_gate("preprints", rows)) >= 1)
        write_csv("config/MOLECULE_IDENTITY.csv", OVERLAY_HEADER, [])

        # ---------------------------------------------------------------- offline re-evaluation of stored rows
        import run_identity_reeval as rr  # noqa: E402

        os.makedirs("work")
        tdb = os.path.join("work", "retarats_trials.sqlite")
        conn = sqlite3.connect(tdb)
        trials = [{"nct_id": "NCT00000001", "molecule_id": "tp", "brief_title": "PTH trial", "interventions": "DRUG | Teriparatide | sc",
                   "identity_fields_v": "1"},
                  {"nct_id": "NCT00000002", "molecule_id": "tp", "brief_title": "Unrelated cohort", "interventions": "OTHER | diet",
                   "stale_query": True, "identity_fields_v": "1"},
                  {"nct_id": "NCT00000003", "molecule_id": "tp", "brief_title": "Unrelated cohort 2", "interventions": "OTHER | diet",
                   "identity_fields_v": "1"},
                  {"nct_id": "NCT00000004", "molecule_id": "tp", "brief_title": "Legacy cohort", "interventions": "OTHER | diet"}]
        save_payload_rows(conn, "trials", "nct_id", trials)
        before = {r[0]: r[1] for r in conn.execute("select nct_id, payload_json from trials")}
        conn.close()
        summ = rr.run("work", ["ctgov", "preprints", "pubmed"])
        check("reeval: only existing sources are processed; missing DBs are skipped, never an error",
              set(summ["sources"]) == {"ctgov"})
        conn = sqlite3.connect(tdb)
        after = {r[0]: r[1] for r in conn.execute("select nct_id, payload_json from trials")}
        check("reeval NEVER touches the stored records (payloads byte-identical)", before == after)
        ver = idn.load_verdicts(conn, "ctgov")
        check("reeval writes one provenance row per stored record (including stale ones)", len(ver) == 4)
        check("provenance answers WHY: outcome, match type, term, zone, rules_version",
              ver[("NCT00000001", "tp")]["outcome"] == "pass" and ver[("NCT00000001", "tp")]["match_type"] == "canonical_name"
              and ver[("NCT00000001", "tp")]["matched_term"] == "Teriparatide" and ver[("NCT00000001", "tp")]["zone"] == "interventions"
              and ver[("NCT00000003", "tp")]["outcome"] == "hold" and ver[("NCT00000003", "tp")]["rules_version"].startswith("id1-")
              and ver[("NCT00000003", "tp")]["role"] == "unrelated" and ver[("NCT00000001", "tp")]["role"] == "exposure")
        check("a LEGACY trial row (no identity fields yet) is never held on incomplete evidence",
              ver[("NCT00000004", "tp")]["outcome"] == "pass" and ver[("NCT00000004", "tp")]["match_type"] == "legacy_unverified"
              and ver[("NCT00000004", "tp")]["role"] == "unverified")
        conn.close()
        n1 = summ["sources"]["ctgov"]["rows_written"]
        rr.run("work", ["ctgov"])
        conn = sqlite3.connect(tdb)
        check("reeval is idempotent (full rebuild of the table, same row count)",
              conn.execute("select count(*) from record_identity").fetchone()[0] == n1)
        conn.close()
        # a rules change is picked up by simply running again; manual keep overrides
        write_csv("config/registry_keep.csv", ["molecule_id", "id", "reason"], [["tp", "nct00000003", "reviewed"]])
        rr.run("work", ["ctgov"])
        conn = sqlite3.connect(tdb)
        ver = idn.load_verdicts(conn, "ctgov")
        check("after adding a manual keep, re-running flips the hold to pass(manual_keep)",
              ver[("NCT00000003", "tp")]["outcome"] == "pass" and ver[("NCT00000003", "tp")]["match_type"] == "manual_keep")
        conn.close()
        os.remove("config/registry_keep.csv")
        # ---- the feeds apply the same function
        import build_trials_json as bt  # noqa: E402

        pub = bt._load_trials(tdb, os.path.join(cdir, "held.csv"))
        check("trials feed = non-stale AND identity-pass (legacy rows stay published until backfilled)",
              [r["nct_id"] for r in pub] == ["NCT00000001", "NCT00000004"])
        with open(os.path.join(cdir, "held.csv"), newline="", encoding="utf-8") as fh:
            check("trials feed writes the held report", [r["record_key"] for r in csv.DictReader(fh)] == ["NCT00000003"])
        conn = sqlite3.connect(tdb)
        check("feeds never delete: all four trials are still stored", conn.execute("select count(*) from trials").fetchone()[0] == 4)
        conn.close()
    finally:
        os.chdir(old_cwd)



    # ------------------------------------------------------------------ roles: how the molecule appears in a trial
    shipped = idn.load_identity_config()

    def tv(**z):
        z.setdefault("identity_fields_v", "1")
        flds = {k: v for k, v in z.items() if k != "identity_fields_v"}
        if z["identity_fields_v"]:
            flds["identity_fields_v"] = "1"
        return idn.evaluate(shipped, "ctgov", "gdf15", idn.zones_for_trial(flds), "")

    v = tv(brief_title="Weight loss study", interventions="DRUG | GDF15 antibody | iv")
    check("trial role: intervention -> exposure, published", v.outcome == idn.PASS and v.role == "exposure")
    v = tv(brief_title="GDF15 in pregnancy", conditions="Hyperemesis")
    check("trial role: in the title -> subject, published", v.outcome == idn.PASS and v.role == "subject")
    v = tv(brief_title="Metabolic phenotyping", outcome_measures="Serum GDF15 level; body weight")
    check("trial role: an outcome MEASURE (biomarker study) is kept as measured_outcome, not rejected",
          v.outcome == idn.PASS and v.role == "measured_outcome" and v.zone == "outcome_measures")
    v = tv(brief_title="Metabolic phenotyping", outcome_measures="Myokines", outcome_text="Myokines | time frame: 12 weeks | GDF15 and irisin levels")
    check("trial role: named in an outcome DESCRIPTION is also a measured outcome (kept)", v.outcome == idn.PASS and v.role == "measured_outcome")
    v = tv(brief_title="Cohort", summary_text="Markers such as GDF15 will be explored.")
    check("trial role: only in the summary -> background mention", v.outcome == idn.HOLD and v.role == "background")
    v = tv(brief_title="Cohort", eligibility_text="Exclusion: prior GDF15 therapy")
    check("trial role: only in eligibility -> background mention", v.outcome == idn.HOLD and v.role == "background")
    v = tv(brief_title="Cohort", interventions="DRUG | placebo")
    check("trial role: nothing -> unrelated", v.outcome == idn.HOLD and v.role == "unrelated")
    v = idn.evaluate(shipped, "ctgov", "vip", idn.zones_for_trial({"identity_fields_v": "1", "brief_title": "HD-VIP chemotherapy regimen",
                                                                   "interventions": "DRUG | VIP regimen | etoposide"}), "")
    check("trial role: an ambiguous acronym without its context -> ambiguous_acronym", v.outcome != idn.PASS and v.role == "ambiguous_acronym")
    v = tv(brief_title="Cohort", interventions="OTHER | diet", identity_fields_v="")
    check("legacy trial row (no identity_fields_v) that WOULD be held is published as legacy_unverified (fail-open)",
          v.outcome == idn.PASS and v.match_type == "legacy_unverified")
    check("an excluded-by-veto trial stays excluded even when legacy",
          idn.evaluate(shipped, "ctgov", "vip", idn.zones_for_trial({"brief_title": "HD-VIP", "interventions": "DRUG | VIP regimen"}), "").outcome != idn.PASS)
    # the establishing roles are policy, not code
    pol_td = tempfile.mkdtemp(prefix="idn_pol_")
    pol = os.path.join(pol_td, "pol.csv")
    write_csv(pol, ["source", "enforce", "publish_roles", "notes"], [["ctgov", "true", "exposure;subject;measured_outcome;background", ""],
                                                                       ["preprints", "true", "bogus", ""]])
    w = []
    roles = idn.load_establishing_roles(w, pol)
    check("policy: publish_roles is configurable per source; an unknown role is rejected with a warning (default kept)",
          roles["ctgov"] == frozenset({"exposure", "subject", "measured_outcome", "background"})
          and roles["preprints"] == idn.DEFAULT_ESTABLISHING["preprints"] and w)
    check("shipped policy: trials publish exposure, subject, measured_outcome only",
          shipped.roles_ok["ctgov"] == frozenset({"exposure", "subject", "measured_outcome"}))

    # ------------------------------------------------------------------ MeSH: exact heading only
    def mv(mid, **z):
        return idn.evaluate(shipped, "pubmed", mid, z, "")

    v = mv("taurine", title="Acamprosate in alcohol dependence", abstract="relapse prevention", mesh_terms="Taurine: pharmacology; Humans")
    check("MeSH: even an EXACT heading (Taurine) does not establish identity alone -> held as indexing_only (derivative papers carry it)",
          v.outcome == idn.HOLD and v.match_type == "indexing_only" and v.role == "indexing" and "Taurine" in v.reason + v.matched_term)
    check("MeSH: an exact heading supports a record that also names the molecule in its text",
          mv("taurine", title="Taurine and bone", abstract="x", mesh_terms="Taurine: pharmacology").outcome == idn.PASS)
    check("MeSH: a SIBLING heading (Taurocholic Acid) does not establish taurine",
          mv("taurine", title="Bile", abstract="x", mesh_terms="Taurocholic Acid: metabolism", chemicals="Taurocholic Acid").outcome != idn.PASS)
    check("MeSH: spermine does not establish spermidine; everolimus does not establish rapamycin",
          mv("spermidine", title="DNA", abstract="x", mesh_terms="Spermine: pharmacology", chemicals="Spermine").outcome != idn.PASS
          and mv("rapamycin", title="Stent", abstract="x", mesh_terms="Everolimus: administration & dosage", chemicals="Everolimus").outcome != idn.PASS)
    check("MeSH: a heading that merely CONTAINS the name (a derivative) does not count",
          mv("quercetin", title="Flavonoids", abstract="x", mesh_terms="Quercetin-3-glucoside: analogs", chemicals="quercetin 3-O-glucoside").outcome != idn.PASS)
    check("MeSH: a substance-list entry that equals the name is indexing only too", mv("quercetin", title="Flavonoids", abstract="x", chemicals="Quercetin").outcome == idn.HOLD)
    check("MeSH: a contextual alias can never be established through indexing text",
          mv("ldn", title="Pharmacology", abstract="x", mesh_terms="LDN: pharmacology").outcome != idn.PASS)

    # ------------------------------------------------------------------ CT.gov identity-field backfill (one-time, bounded)
    import run_trials_identity_backfill as bf  # noqa: E402

    bdb = os.path.join(td, "bf.sqlite")
    conn = sqlite3.connect(bdb)
    legacy = {"nct_id": "NCT00000101", "molecule_id": "tirzepatide", "brief_title": "Weight study", "interventions": "OTHER | diet",
              "conditions": "Obesity", "first_seen_utc": "2026-01-01T00:00:00Z", "enriched_at_utc": "2026-01-02T00:00:00Z",
              "overall_status": "COMPLETED"}
    done = {"nct_id": "NCT00000102", "molecule_id": "tirzepatide", "brief_title": "Done", "identity_fields_v": "1", "arms": "keep me"}
    stale = {"nct_id": "NCT00000103", "molecule_id": "tirzepatide", "brief_title": "Stale", "stale_query": True}
    missing = {"nct_id": "NCT00000104", "molecule_id": "tirzepatide", "brief_title": "Not returned by CT.gov"}
    save_payload_rows(conn, "trials", "nct_id", [legacy, done, stale, missing])
    conn.close()

    class FakeClient:
        calls = 0
        fail = False

        def studies_by_ids(self, ids):
            FakeClient.calls += 1
            if FakeClient.fail:
                return [], "http_error", "boom"
            return [{"protocolSection": {
                "identificationModule": {"nctId": "NCT00000101", "briefTitle": "Weight study", "officialTitle": "A Study of Tirzepatide"},
                "conditionsModule": {"conditions": ["Obesity"], "keywords": ["incretin"]},
                "armsInterventionsModule": {"armGroups": [{"label": "Arm A", "type": "EXPERIMENTAL", "description": "Weekly dosing"}],
                                            "interventions": [{"type": "DRUG", "name": "LY3298176", "otherNames": ["Mounjaro"]}]},
                "outcomesModule": {"primaryOutcomes": [{"measure": "Change in GDF15", "timeFrame": "12 weeks", "description": "level"}]},
                "descriptionModule": {"briefSummary": "A summary.", "detailedDescription": "Details."},
                "eligibilityModule": {"eligibilityCriteria": "Adults"}}}], "api", ""

    FakeClient.fail = True
    r0 = bf.run(bdb, client=FakeClient())
    conn = sqlite3.connect(bdb)
    rows0 = {d["nct_id"]: d for d in (json.loads(p) for (p,) in conn.execute("select payload_json from trials"))}
    conn.close()
    check("backfill: a failed batch leaves every row exactly as it was (still legacy, nothing lost)",
          r0["filled"] == 0 and r0["failed_batches"] >= 1 and "identity_fields_v" not in rows0["NCT00000101"])
    FakeClient.fail = False
    r1 = bf.run(bdb, client=FakeClient())
    conn = sqlite3.connect(bdb)
    rows1 = {d["nct_id"]: d for d in (json.loads(p) for (p,) in conn.execute("select payload_json from trials"))}
    conn.close()
    new = rows1["NCT00000101"]
    check("backfill: selects only non-stale rows without the marker", r0["pending"] == 2 and r1["filled"] == 1 and r1["not_returned"] == 1)
    check("backfill: ADDS the identity fields (official title, keywords, arms, other names, outcome measures, summary, eligibility)",
          new["official_title"] == "A Study of Tirzepatide" and "incretin" in new["keywords"] and "Arm A" in new["arms"]
          and "Mounjaro" in new["other_names"] and "GDF15" in new["outcome_measures"] and "A summary" in new["summary_text"]
          and "Adults" in new["eligibility_text"] and new["identity_fields_v"] == "1")
    check("backfill: every pre-existing field is left exactly as stored (no source metadata rewritten)",
          all(new[k] == legacy[k] for k in legacy))
    check("backfill: completed / stale rows are untouched; a row CT.gov does not return stays legacy",
          rows1["NCT00000102"] == done and rows1["NCT00000103"] == stale and "identity_fields_v" not in rows1["NCT00000104"])
    calls = FakeClient.calls
    r2 = bf.run(bdb, client=FakeClient())
    check("backfill: idempotent (re-running only retries the row CT.gov did not return)", r2["pending"] == 1 and r2["filled"] == 0
          and FakeClient.calls == calls + 1)
    check("backfill: --max-rows bounds one run", bf.run(bdb, max_rows=0, dry_run=True)["pending"] == 1)
    check("a still-legacy trial is published, never held on incomplete evidence",
          idn.evaluate(shipped, "ctgov", "tirzepatide", idn.zones_for_trial(rows1["NCT00000104"]), "").match_type == "legacy_unverified")
    check("after the backfill the same trial is judged on its full record (exposure via LY3298176)",
          idn.evaluate(shipped, "ctgov", "tirzepatide", idn.zones_for_trial(new), "").role == "exposure")

    # ------------------------------------------------------------------ PubMed: the curated build holds in place
    spec = importlib.util.spec_from_file_location("build_curated_database", os.path.join(ROOT, "scripts", "build_curated_database.py"))
    bcd = importlib.util.module_from_spec(spec)
    sys.modules["build_curated_database"] = bcd
    spec.loader.exec_module(bcd)
    db = os.path.join(td, "pm.sqlite")
    conn = sqlite3.connect(db)
    papers = [{"pmid": "11", "title": "Recalcitrant Hailey-Hailey disease treated with low-dose naltrexone", "abstract": "Naltrexone 3 mg daily.", "pub_year": 2021},
              {"pmid": "12", "title": "Authors and affiliations", "abstract": "Jane Doe, LDN, FAND, Academy of Nutrition and Dietetics.", "pub_year": 2021},
              {"pmid": "13", "title": "Land degradation neutrality in China", "abstract": "LDN assessment of grassland", "pub_year": 2021}]
    evid = [{"evidence_id": f"{p}:ldn:r1", "pmid": p, "molecule_id": "ldn", "molecule_name": "Low-Dose Naltrexone (LDN)", "rule_id": "r1",
             "pub_year": 2021, "evidence_class": "clinical_case"} for p in ("11", "12", "13")]
    save_payload_rows(conn, "papers", "pmid", papers)
    save_payload_rows(conn, "evidence", "evidence_id", evid)
    conn.close()
    out = os.path.join(td, "out_pm")
    res = bcd.build(db, out)

    def rd(name):
        with open(os.path.join(out, name), newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    cur = {r["pmid"]: r for r in rd("curated_evidence.csv")}
    check("PubMed: identity-held records stay in curated_evidence.csv", set(cur) == {"11", "12", "13"})
    check("PubMed: record naming the molecule is untouched", cur["11"]["publication_status"] != "excluded_noise")
    check("PubMed: LDN credential and land-degradation records are held (excluded_noise, identity:*)",
          all(cur[p]["publication_status"] == "excluded_noise" and cur[p]["publish_rule_id"].startswith("identity:") for p in ("12", "13")))
    check("PubMed: held record carries the reason", cur["13"]["review_reason"].startswith("identity "))
    check("PubMed: held records are absent from public_records.csv", {"12", "13"}.isdisjoint({r["pmid"] for r in rd("public_records.csv")}))
    check("PubMed: identity report lists them with outcome + matched term",
          {r["pmid"] for r in rd("identity_report_pubmed.csv")} == {"12", "13"})
    check("PubMed: build stats count identity holds", res["stats"].get("identity_holds") == 2)

    # ------------------------------------------------------------------ cache key: quoted != unquoted search
    q1 = '("Thymosin Beta-4" OR "TB4") AND SRC:PPR'
    q2 = '(Thymosin Beta-4 OR TB4) AND SRC:PPR'
    check("search cache keys differ for quoted vs unquoted queries (they used to collide after punctuation collapsing)",
          search_cache_key(q1, 100, "core", "*") != search_cache_key(q2, 100, "core", "*"))
    check("search cache key is stable for the same query/paging", search_cache_key(q1, 100, "core", "*") == search_cache_key(q1, 100, "core", "*"))
    check("search cache key differs per page cursor", search_cache_key(q1, 100, "core", "*") != search_cache_key(q1, 100, "core", "AoE="))
    from retarats_pipeline.enrichment.common import APIConfig, CachedHTTPClient

    try:
        http = CachedHTTPClient(APIConfig(cache_dir=os.path.join(td, "cache"), api_enabled=False))
    except Exception:  # requests missing
        http = None
    if http is not None:
        p1 = http._cache_path("europepmc_search", search_cache_key(q1, 100, "core", "*"))
        p2 = http._cache_path("europepmc_search", search_cache_key(q2, 100, "core", "*"))
        check("cache FILE paths differ for quoted vs unquoted queries", p1 != p2)

    # ------------------------------------------------------------------ the shipped config
    shipped = idn.load_identity_config()
    check("shipped identity overlay loads without warnings", shipped.warnings == [])
    check("shipped config covers all active molecules", len(shipped.by_molecule) == len(reg.load_active_molecules()))
    check("shipped overlay never lists a PubMed discovery term (retrieval for PubMed stays in SEARCH_RULES.csv)",
          all(r.get("discovery", "").strip() == "" or set(r["discovery"].replace(";", "|").split("|")) <= {"ctgov", "preprints"}
              for r in csv.DictReader(open(os.path.join(ROOT, "config", "MOLECULE_IDENTITY.csv"), encoding="utf-8"))))
    # every short, code-like name that is still a default specific alias has been reviewed on purpose
    unreviewed = [(mid, r.term) for mid, mi in shipped.by_molecule.items() for r in mi.specific
                  if r.origin == "default" and len("".join(idn._alnum_tokens(r.term))) <= 3]
    check("no 3-character name is an UNREVIEWED specific alias (add it to the overlay as specific or contextual): %s" % unreviewed,
          unreviewed == [])
    # the shipped overlay reproduces the approved-benchmark ambiguity cases
    def sv(source, mid, **z):
        return idn.evaluate(shipped, source, mid, z, "")

    check("shipped: benchmark 29085556 (metallothionein MT-II) is not melanotan II",
          sv("pubmed", "melanotan_ii", title="Metallothionein in Brain Disorders.", abstract="MT-I and MT-II have been localized...").outcome != idn.PASS)
    check("shipped: benchmark 35146537 (Very Important Pharmacogene) is not VIP",
          sv("pubmed", "vip", title="Pharmacogenomic VIP variants", abstract="Very Important Pharmacogene (VIP) variants of drug metabolism").outcome != idn.PASS)
    check("shipped: benchmark 35180395 (LDN 193189) is not low-dose naltrexone",
          sv("pubmed", "ldn", title="Effect of LDN 193189 on BMP signaling", abstract="LDN-193189 was used").outcome != idn.PASS)
    check("shipped: benchmark 37210207 (LDN, FAND credential) is not low-dose naltrexone",
          sv("pubmed", "ldn", title="Authors", abstract="Jane Doe, LDN, FAND").outcome != idn.PASS)
    check("shipped: benchmark NCT06133946 (no MOTS in the record) is not MOTS-c",
          sv("ctgov", "mots_c", brief_title="Cohort Of DEafness-gene Screening", conditions="Deafness", interventions="").outcome != idn.PASS)
    check("shipped: benchmark include - Melanotan-II memory paper passes",
          sv("pubmed", "melanotan_ii", title="Melanotan-II reverses memory impairment", abstract="").outcome == idn.PASS)
    check("shipped: thymalfasin / Zadaxin identify thymosin alpha 1",
          sv("preprints", "thymosin_alpha_1", title="Thymalfasin in sepsis", abstract="x").outcome == idn.PASS
          and sv("ctgov", "thymosin_alpha_1", brief_title="Zadaxin trial").outcome == idn.PASS)
    check("shipped: TA-1 (tranexamic acid) is not thymosin alpha 1",
          sv("pubmed", "thymosin_alpha_1", title="Oral TA 1 g after knee surgery", abstract="tranexamic acid TA-1").outcome != idn.PASS)
    check("shipped: TB-4 in tuberculosis papers is not thymosin beta-4",
          sv("pubmed", "thymosin_beta_4", title="MDR TB 4%", abstract="the TB 4 patients").outcome != idn.PASS
          and sv("pubmed", "thymosin_beta_4", title="Tb4 and actin", abstract="TB4 sequesters G-actin").outcome == idn.PASS)
    check("shipped: PTH(1-34) identifies teriparatide", sv("preprints", "teriparatide", title="Daily PTH(1-34) treatment", abstract="x").outcome == idn.PASS)
    check("shipped: Geref identifies sermorelin; TH9507 identifies tesamorelin",
          sv("ctgov", "sermorelin", brief_title="Geref in GH deficiency").outcome == idn.PASS
          and sv("ctgov", "tesamorelin", brief_title="TH9507 in HIV lipodystrophy").outcome == idn.PASS)
    check("shipped: bare GHRH is NOT an alias of sermorelin",
          sv("pubmed", "sermorelin", title="GHRH neurons", abstract="growth hormone releasing hormone (GHRH) secretion").outcome != idn.PASS)
    check("shipped: everolimus is not rapamycin", sv("ctgov", "rapamycin", brief_title="Everolimus in breast cancer",
                                                       interventions="DRUG | Everolimus | 10 mg").outcome != idn.PASS)
    check("shipped: NADH keeps the pre-WS4.5 standard (name in the text) -- the title+use-context restriction is NOT applied (unresolved scope)",
          sv("pubmed", "nadh", title="NADH oxidation by complex I", abstract="x").outcome == idn.PASS
          and sv("pubmed", "nadh", title="Oral NADH supplementation in chronic fatigue", abstract="").outcome == idn.PASS)
    check("shipped: foreign / brand metformin spellings identify metformin",
          sv("ctgov", "metformin", brief_title="Metformina en diabetes").outcome == idn.PASS
          and sv("preprints", "metformin", title="メトホルミン", abstract="x").outcome == idn.PASS)
    check("shipped: ALCAR spellings", sv("preprints", "alcar", title="L-acetylcarnitine in neuropathy", abstract="x").outcome == idn.PASS)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
