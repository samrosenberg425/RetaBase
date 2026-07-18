#!/usr/bin/env python3
"""Security + correctness tests for the public site generator.

Focus: the security-critical control is that hostile CSV content cannot break
out of the inlined JSON data block or produce a dangerous link. No network,
no DB needed. Run:

    python3 tests/test_site.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the generator module by path (scripts/ is not a package).
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "build_public_site",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "build_public_site.py"),
)
site = importlib.util.module_from_spec(_SPEC)
# Register before exec so @dataclass can resolve the module via sys.modules.
sys.modules["build_public_site"] = site
_SPEC.loader.exec_module(site)  # type: ignore

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
    # 1) _safe_json_block neutralizes the </script breakout sequence.
    hostile = {"x": "</script><script>alert(1)</script>"}
    block = site._safe_json_block(hostile)
    check("no raw </script in data block", "</script" not in block)
    check("</ neutralized to <\\/", "<\\/script" in block)
    # still valid JSON
    import json
    round_trip = json.loads(block.replace("<\\/", "</"))
    check("block is valid JSON", round_trip["x"] == hostile["x"])

    # 2) A hostile record round-trips into the HTML with no breakout / no js: href.
    json_block = site._safe_json_block(
        {
            "records": [
                {
                    "molecule_name": "Evil",
                    "title": "</script><img src=x onerror=alert(1)>",
                    "pmid": "javascript:alert(2)",
                    "doi": '"><script>alert(3)</script>',
                    "reliability_tier": "high\"><b>",
                    "facet_all": "x",
                }
            ],
            "molecules": [],
            "filters": [{"field": "molecule_name", "label": "Molecule"}],
            "multi": ["facet_all"],
        }
    )
    html_text = site._render_html(json_block, 1, 0, "2026-01-01T00:00:00Z", 1, 0, "inline")
    # The only real </script> tags are the two template ones (data block + logic).
    # Hostile markup may still appear as INERT TEXT inside the JSON data block; that
    # is safe. The security invariant is that it cannot BREAK OUT of the block, i.e.
    # no raw "</script>" precedes the injected markup.
    check("exactly 2 literal </script> tags", html_text.count("</script>") == 2)
    check("no raw </script> breakout before injected markup", "</script><img" not in html_text)
    check("breakout sequence is neutralized", "<\\/script><img" in html_text)
    check("no javascript: href scheme emitted", "href=\"javascript:" not in html_text.lower())

    # Evidence-density badge: the molecule card renders a density tier + counts.
    check("density badge markup present", "mol-density" in html_text and "tier-" in html_text)
    check("density badge reads the tier field", "density_tier" in html_text)
    check("density badge labels literature volume honestly", "Evidence density:" in html_text)
    check("density fields survive the molecule allowlist",
          all(f in site.MOLECULE_FIELDS for f in ("density_tier", "record_count", "human_count")))

    # 3) Missing columns / empty inputs don't crash.
    empty = site.SiteData(records=[], molecules=[])
    check("empty site data renders", isinstance(site._safe_json_block({"records": []}), str))
    check("SiteData default ok", empty.records == [] and empty.molecules == [])

    # 4) Molecules with zero public records are dropped from the index.
    #    (load_site_data filters; simulate via the same logic path.)
    import tempfile, csv
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "public_records.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["molecule_id", "molecule_name", "title"])
            w.writeheader()
            w.writerow({"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "t"})
        with open(os.path.join(d, "molecule_index.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["molecule_id", "molecule_name", "auto_published"])
            w.writeheader()
            w.writerow({"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "auto_published": "1"})
            w.writerow({"molecule_id": "noise", "molecule_name": "Noise", "auto_published": "0"})
        sd = site.load_site_data(d)
        names = {m.get("molecule_name") for m in sd.molecules}
        check("published molecule kept", "Retatrutide" in names)
        check("zero-published molecule dropped", "Noise" not in names)

    # 5) site_data.json is preferred over the CSV when present, and carries the
    #    extra axes (rank/directness/component breakdowns) verbatim.
    with tempfile.TemporaryDirectory() as d:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T1",
                 "facet_species": "human", "facet_indication": "obesity_weight; diabetes_glycemic",
                 "reliability_score": 90, "reliability_tier": "high",
                 "reliability_components": '{"design": 60}', "rank_score": 88},
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T2",
                 "facet_species": "mouse", "facet_indication": "obesity_weight",
                 "reliability_score": 40, "reliability_tier": "limited"},
            ],
            "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "auto_published": "2"}],
        }
        with open(os.path.join(d, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        sd = site.load_site_data(d)
        check("feed generated_utc loaded", sd.generated_utc == "2026-07-04T00:00:00Z")
        check("feed record count", len(sd.records) == 2)
        check("feed carries component breakdown", sd.records[0]["reliability_components"] == '{"design": 60}')

    # 6) Cross-filter facet counting (include/exclude): selecting Species include
    #    =human must shrink the Indication counts to only human records, while the
    #    Species facet does NOT constrain its own option list.
    recs = [
        {"facet_species": "human", "facet_indication": "obesity_weight; diabetes_glycemic"},
        {"facet_species": "human", "facet_indication": "obesity_weight"},
        {"facet_species": "mouse", "facet_indication": "obesity_weight"},
    ]
    multi = {"facet_species", "facet_indication"}
    inc_human = {"facet_species": {"inc": ["human"], "exc": []},
                 "facet_indication": {"inc": [], "exc": []}}
    counts = site._cross_filter_counts(recs, inc_human, multi)
    # Indication counts respect the human include filter: obesity 2 (both human), diabetes 1.
    check("cross-filter indication respects species include", counts["facet_indication"].get("obesity_weight") == 2)
    check("cross-filter indication excludes mouse-only", counts["facet_indication"].get("diabetes_glycemic") == 1)
    # Species facet ignores its OWN selection, so mouse is still counted (=1).
    counts_sp = site._cross_filter_counts(recs, {"facet_species": {"inc": ["human"], "exc": []}}, multi)
    check("facet does not constrain itself", counts_sp["facet_species"].get("mouse") == 1)
    check("facet self count includes selected", counts_sp["facet_species"].get("human") == 2)

    # 6b) INCLUDE is OR-within-domain; multiple includes match records with ANY of them.
    multi_inc = {"facet_indication": {"inc": ["obesity_weight", "diabetes_glycemic"], "exc": []}}
    check("include OR: obesity+diabetes matches all obesity-or-diabetes records",
          len([r for r in recs if site._record_passes(r, multi_inc, multi)]) == 3)
    only_diab = {"facet_indication": {"inc": ["diabetes_glycemic"], "exc": []}}
    check("include single: only diabetes record passes",
          len([r for r in recs if site._record_passes(r, only_diab, multi)]) == 1)

    # 6c) EXCLUDE drops any record carrying an excluded value, and wins over include.
    excl_mouse = {"facet_species": {"inc": [], "exc": ["mouse"]}}
    check("exclude mouse drops the mouse record",
          len([r for r in recs if site._record_passes(r, excl_mouse, multi)]) == 2)
    # exclude beats include on the same domain: include human but exclude the human
    # record that also carries diabetes -> that record is dropped.
    inc_and_exc = {"facet_indication": {"inc": ["obesity_weight"], "exc": ["diabetes_glycemic"]}}
    passed = [r for r in recs if site._record_passes(r, inc_and_exc, multi)]
    check("exclude wins over include on same domain",
          all("diabetes_glycemic" not in r["facet_indication"] for r in passed) and len(passed) == 2)

    # 6d) Year filter: before / after / range on pub_year.
    yrecs = [{"pub_year": "2018"}, {"pub_year": "2021"}, {"pub_year": "2024"}, {"pub_year": ""}]
    def ypass(yf):
        return [r for r in yrecs if site._year_passes(r, yf)]
    check("year after 2021 keeps 2021 & 2024", len(ypass({"mode": "after", "a": 2021})) == 2)
    check("year before 2021 keeps 2018 & 2021", len(ypass({"mode": "before", "a": 2021})) == 2)
    check("year range 2020-2023 keeps only 2021", len(ypass({"mode": "range", "a": 2020, "b": 2023})) == 1)
    check("blank year passes when a bound applies is False",
          {"pub_year": ""} not in ypass({"mode": "after", "a": 2000}))
    check("no year mode passes everything", len(ypass(None)) == 4)

    # 6e) Journal substring (case-insensitive) + min-citations via _record_passes.
    jrecs = [{"journal": "Nature Medicine", "citation_count": "50"},
             {"journal": "Cell Metabolism", "citation_count": "3"},
             {"journal": "Diabetes Care", "citation_count": ""}]
    check("journal contains 'nature' matches Nature Medicine",
          len([r for r in jrecs if site._record_passes(r, {}, multi, journal_sub="nature")]) == 1)
    check("min citations 10 keeps only the 50-cited paper",
          len([r for r in jrecs if site._record_passes(r, {}, multi, min_citations=10)]) == 1)
    check("blank citation_count fails a min-citations floor",
          not site._record_passes(jrecs[2], {}, multi, min_citations=1))

    # 7) fetch mode inlines config but no record bodies.
    fetch_html = site.build_site  # sanity: callable exists
    check("build_site callable", callable(fetch_html))

    # 8) The 5 new facets are wired into filters, aspects, and multi-value set so
    #    they participate in cross-filter counting exactly like existing facets.
    new_facets = [
        "facet_drug_class", "facet_population", "facet_sex",
        "facet_formulation", "facet_evidence_direction",
    ]
    filter_fields = {f for f, _ in site.FILTER_FACETS}
    aspect_fields = {f for f, _, _ in site.ASPECT_TAGS}
    for nf in new_facets:
        check(nf + " is a filter facet", nf in filter_fields)
        check(nf + " is an aspect tag", nf in aspect_fields)
        check(nf + " is multi-valued", nf in site.MULTI_VALUE_FIELDS)
        check(nf + " is a record field", nf in site.RECORD_FIELDS)
    # New author/journal fields carried through the record schema.
    for extra in ("authors_short", "first_author", "author_count", "journal_tier"):
        check(extra + " in RECORD_FIELDS", extra in site.RECORD_FIELDS)
    # Cross-filter counting includes a new facet just like the old ones: pick
    # Drug class as the exemplar and confirm selecting Species=human reshrinks it.
    recs2 = [
        {"facet_species": "human", "facet_drug_class": "glp1_agonist; gip_agonist"},
        {"facet_species": "human", "facet_drug_class": "glp1_agonist"},
        {"facet_species": "mouse", "facet_drug_class": "gip_agonist"},
    ]
    multi2 = {"facet_species", "facet_drug_class"}
    c2 = site._cross_filter_counts(
        recs2, {"facet_species": {"inc": ["human"], "exc": []},
                "facet_drug_class": {"inc": [], "exc": []}}, multi2)
    check("new facet respects cross-filter (glp1 human=2)",
          c2["facet_drug_class"].get("glp1_agonist") == 2)
    check("new facet excludes mouse-only value (gip human=1)",
          c2["facet_drug_class"].get("gip_agonist") == 1)

    # 9) Rendered HTML carries the new UI features and keeps them injection-safe.
    feed_html = site._render_html(
        site._safe_json_block({
            "records": [{
                "molecule_name": "Retatrutide",
                "title": "A trial",
                "authors_short": "Giblin K; Kaplan LM; Somers VK et al.",
                "journal": "Nature Medicine", "journal_tier": "top_tier",
                "facet_drug_class": "glp1_agonist",
                "facet_all": "x",
            }],
            "molecules": [],
            "filters": [{"field": "facet_drug_class", "label": "Drug class"}],
            "multi": ["facet_all", "facet_drug_class"],
            "aspects": [{"field": "facet_drug_class", "cls": "dc", "label": "drug class"}],
        }),
        1, 0, "2026-07-04T00:00:00Z", 1, 0, "inline",
    )
    # (a) Author names produce an escaped Google Scholar href, no javascript:.
    check("Scholar search base present",
          "https://scholar.google.com/scholar?q=" in feed_html)
    check("author links use encodeURIComponent(name)",
          "SCHOLAR + encodeURIComponent(name)" in feed_html)
    check("no javascript: href in author/feature HTML",
          "href=\"javascript:" not in feed_html.lower())
    check("authors rendered via el() textContent (authorsLine present)",
          "function authorsLine" in feed_html)
    # (b) The explainer text is present (reliability + directness lines).
    check("explainer: How to read this", "How to read this" in feed_html)
    check("explainer: directness line",
          "how directly the evidence applies to humans" in feed_html)
    # public build (default internal=False) must NOT emit the export-decisions
    # button markup; the curator notes/approval code remains in the JS but is
    # gated behind the runtime INTERNAL flag (see the dedicated --internal test).
    check("public build omits export-decisions button",
          "Export decisions" not in feed_html)
    check("curator notes text is gated behind INTERNAL",
          "if (INTERNAL) {" in feed_html and "Notes are for curators" in feed_html)
    # (c) The new facet filters appear as configured labels.
    check("new filter label Drug class rendered", "Drug class" in feed_html)
    # journal-tier badge helper wired in.
    check("journal tier badge helper present",
          "function journalTierBadge" in feed_html)
    # friendly-label helper preserves raw values (only display prettified).
    check("pretty() helper present", "function pretty" in feed_html)

    # 10) authorsLine emitting nothing for empty authors_short is enforced by the
    #     JS guard; assert the guard text exists so a refactor can't drop it.
    check("empty authors short-circuits", "if (!raw) return null" in feed_html)

    # 10b) Render cap: the DOM is limited to RENDER_LIMIT cards with a "Load more"
    #      control, while the count line reports the FULL filtered total (not the
    #      rendered count) and facet counts stay independent of the cap.
    check("RENDER_LIMIT constant present", "var RENDER_LIMIT" in feed_html)
    check("visibleCount state present", "var visibleCount" in feed_html)
    check("Load more control present",
          'id="load-more"' in feed_html and "function loadMore" in feed_html)
    check("count line uses base total (Showing X of Y evidence records)",
          'Showing " + fmtInt(x) + " of " + fmtInt(y) + " evidence records"' in feed_html
          and "filtered out" in feed_html)
    # Count-label honesty (task 4): the feed-length label must NOT say "papers"
    # (it counts evidence records, unlike the corpus strip's distinct-paper number).
    check("count label no longer says 'papers' for the feed length",
          '+ fmtInt(y) + " papers"' not in feed_html)
    check("empty-feed message relabeled to evidence records",
          "No evidence records match these filters." in feed_html
          and "No papers match these filters." not in feed_html)
    check("render window mounts only first visibleCount",
          "Math.min(visibleCount, total)" in feed_html)
    check("visibleCount resets on filter/search/sort change",
          "visibleCount = RENDER_LIMIT;" in feed_html)
    # Cross-filter counts must be computed over the active base set (all or human),
    # so they are driven by that base, never lastVisible / the render window.
    check("cross-filter counts independent of render cap",
          "crossFilterCounts(base, filters, extra, q)" in feed_html
          and "lastVisible" not in feed_html.split("function crossFilterCounts")[1].split("}")[0])

    # 11) Experimental candidates load from the feed and drive the tab's visibility.
    with tempfile.TemporaryDirectory() as d:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T"}],
            "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "auto_published": "1"}],
            "experimental": [
                {"molecule_id": "survodutide", "display_name": "Survodutide",
                 "class": "glucagon_glp1_dual_agonist", "rationale": "dual agonist in phase 3",
                 "status": "experimental", "example_search_terms": "Survodutide; BI 456906"},
                {"molecule_id": "mazdutide", "display_name": "Mazdutide",
                 "class": "glucagon_glp1_dual_agonist", "rationale": "obesity + T2D trials",
                 "status": "experimental", "example_search_terms": "Mazdutide; IBI362"},
            ],
        }
        with open(os.path.join(d, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        sd = site.load_site_data(d)
        check("experimental items load from feed", len(sd.experimental) == 2)
        names = {e.get("display_name") for e in sd.experimental}
        check("experimental carries display_name", "Survodutide" in names and "Mazdutide" in names)
        check("experimental carries rationale + terms",
              sd.experimental[0]["rationale"] == "dual agonist in phase 3"
              and "BI 456906" in sd.experimental[0]["example_search_terms"])
    # Missing experimental key defaults to an empty list.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [], "molecules": []}, fh)
        sd_noexp = site.load_site_data(d)
        check("experimental defaults to empty list when absent", sd_noexp.experimental == [])

    # 12) The Experimental tab renders when candidates are present and stays hidden
    #     (JS toggles display) when the list is empty; banner text is present and
    #     candidate names are rendered via textContent (renderExperimental helper).
    # render with candidates present:
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T",
                         "facet_all": "x"}],
            "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "auto_published": "1"}],
            "experimental": [
                {"molecule_id": "survodutide", "display_name": "Survodutide", "class": "c",
                 "rationale": "r", "status": "experimental", "example_search_terms": "Survodutide"},
                {"molecule_id": "mazdutide", "display_name": "Mazdutide", "class": "c",
                 "rationale": "r", "status": "experimental", "example_search_terms": "Mazdutide"},
            ],
        }
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            html_present = fh.read()
        check("Experimental tab button present", 'id="tab-experimental"' in html_present)
        check("renderExperimental helper present", "function renderExperimental" in html_present)
        check("tab reveal gated on candidate count",
              'EXPERIMENTAL.length ? "" : "none"' in html_present)
        check("banner text present",
              "proposed molecules not yet in" in html_present)
        check("candidate names inlined (Survodutide/Mazdutide)",
              "Survodutide" in html_present and "Mazdutide" in html_present)
        # Security invariants hold with the experimental section added.
        check("exp build: exactly 2 </script> tags", html_present.count("</script>") == 2)
        check("exp build: no </script> breakout", "</script><" not in html_present)
        check("exp build: no javascript: href", 'href="javascript:' not in html_present.lower())

    # render with NO candidates: tab button still emitted but starts hidden and the
    # JS toggle keeps it hidden (EXPERIMENTAL length 0).
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {
            "records": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T",
                         "facet_all": "x"}],
            "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "auto_published": "1"}],
        }
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            html_empty = fh.read()
        check("tab hidden by default (style display:none)",
              'id="tab-experimental" style="display:none"' in html_empty)
        # No candidate payload -> empty experimental array inlined.
        check("empty experimental array inlined", '"experimental":[]' in html_empty)

    # 13) Brand: user-facing text is "RetaBase", not "Retarats".
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {"records": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                             "title": "T", "facet_all": "x"}],
                "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                               "auto_published": "1"}]}
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            brand_html = fh.read()
        check("brand: RetaBase in title", "RetaBase" in brand_html)
        check("brand: no user-facing 'Retarats' text", "Retarats" not in brand_html)
        check("Bioactives tab present (renamed Molecules)",
              ">Bioactives<" in brand_html)
        check("Clinical evidence tab present", "Clinical evidence" in brand_html)
        check("About / Methods tab present", "About / Methods" in brand_html)
        # About page carries the actual rank formula.
        check("About page carries rank formula (0.33 directness)",
              "0.33" in brand_html and "directness" in brand_html)

    # 14) --internal toggles the curator approval UI. Public build (default) omits
    #     the approve/reject/notes + export-decisions markup entirely; the internal
    #     build keeps them. Both remain injection-safe.
    with tempfile.TemporaryDirectory() as src, \
         tempfile.TemporaryDirectory() as pub, tempfile.TemporaryDirectory() as intr:
        feed = {"records": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                             "title": "T", "evidence_class": "human_clinical_controlled",
                             "website_section": "Human evidence", "facet_all": "x"}],
                "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                               "auto_published": "1"}]}
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        rp = site.build_site(src, pub, mode="inline")            # default public
        ri = site.build_site(src, intr, mode="inline", internal=True)  # internal
        check("build_site reports public by default", rp["internal"] is False)
        check("build_site reports internal when flagged", ri["internal"] is True)
        with open(os.path.join(pub, "index.html"), encoding="utf-8") as fh:
            pub_html = fh.read()
        with open(os.path.join(intr, "index.html"), encoding="utf-8") as fh:
            int_html = fh.read()
        # public: no export button, approval row gated off (INTERNAL flag false).
        check("public build has NO export-decisions button",
              "Export decisions" not in pub_html)
        check("public build sets internal flag false in payload",
              '"internal":false' in pub_html)
        check("public build gates approval row on INTERNAL",
              "if (INTERNAL) card.appendChild(approvalRow" in pub_html)
        # internal: export button present, internal flag true.
        check("internal build HAS export-decisions button",
              "Export decisions" in int_html)
        check("internal build sets internal flag true in payload",
              '"internal":true' in int_html)
        # both keep the security invariants.
        for name, txt in (("public", pub_html), ("internal", int_html)):
            check(name + ": exactly 2 </script> tags", txt.count("</script>") == 2)
            check(name + ": no </script> breakout", "</script><" not in txt)
            check(name + ": no javascript: href", 'href="javascript:' not in txt.lower())

    # 15) Clinical-evidence human-only filter: the JS isHuman() definition must
    #     cover exactly the human evidence classes / sections, and the browser must
    #     switch its base set on the Clinical tab. Assert the definition + wiring
    #     are present so the human-only view can't silently drift.
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {"records": [
                    {"molecule_id": "retatrutide", "molecule_name": "R", "title": "RCT",
                     "evidence_class": "human_clinical_controlled", "website_section": "Human evidence",
                     "facet_all": "x"},
                    {"molecule_id": "retatrutide", "molecule_name": "R", "title": "mouse",
                     "evidence_class": "preclinical_invivo", "website_section": "Preclinical evidence",
                     "facet_all": "x"},
                ],
                "molecules": [{"molecule_id": "retatrutide", "molecule_name": "R", "auto_published": "1"}]}
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            clin_html = fh.read()
        for cls in ("human_clinical_controlled", "human_clinical",
                    "human_observational", "evidence_synthesis"):
            check("isHuman covers class " + cls, '"' + cls + '"' in clin_html)
        check("isHuman covers Human evidence section", '"Human evidence"' in clin_html)
        check("isHuman covers Reviews and overviews section",
              '"Reviews and overviews"' in clin_html)
        check("clinical view switches base to human-only",
              'currentView === "clinical" ? RECORDS.filter(isHuman) : RECORDS' in clin_html)
        check("Clinical tab wires to clinical view",
              "showTab('clinical')" in clin_html)

    # 16) Trials + preprints feeds load from their sibling JSON, are empty-safe,
    #     thread corpus_stats through, and keep the security invariants with
    #     hostile trial/preprint fields.
    # 16a) Feeds load from curated_dir when present; ongoing kept as a real bool.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "records": [{"molecule_id": "retatrutide", "molecule_name": "R", "title": "T"}],
                "molecules": [{"molecule_id": "retatrutide", "molecule_name": "R", "auto_published": "1"}],
                "corpus_stats": {"generated_utc": "2026-07-04T00:00:00Z", "total_papers": 36371,
                                 "molecules_with_data": 29, "year_min": 2015, "year_max": 2026,
                                 "pct_citations_filled": 42, "featured": 22, "listed": 173},
            }, fh)
        with open(os.path.join(d, "trials_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_utc": "2026-07-04T01:00:00Z", "count": 2, "ongoing_count": 1,
                       "trials": [
                           {"nct_id": "NCT01", "molecule_name": "R", "brief_title": "Ongoing trial",
                            "overall_status": "RECRUITING", "phases": "PHASE2",
                            "conditions": "Obesity", "lead_sponsor": "Acme", "url": "https://clinicaltrials.gov/study/NCT01",
                            "start_date": "2025-01-01", "completion_date": "2027-01-01", "ongoing": True},
                           {"nct_id": "NCT02", "molecule_name": "R", "brief_title": "Done trial",
                            "overall_status": "COMPLETED", "conditions": "Diabetes",
                            "url": "https://clinicaltrials.gov/study/NCT02", "start_date": "2020-01-01",
                            "ongoing": False},
                       ]}, fh)
        with open(os.path.join(d, "preprints_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_utc": "2026-07-04T02:00:00Z", "count": 1, "preprints": [
                {"id": "PP1", "molecule_name": "R", "title": "A preprint",
                 "authors_short": "Doe J; Roe K", "server": "bioRxiv", "date": "2026-05-01",
                 "doi": "10.1101/xyz", "url": "https://www.biorxiv.org/content/xyz"},
            ]}, fh)
        sd = site.load_site_data(d)
        check("trials feed loads", len(sd.trials) == 2)
        check("preprints feed loads", len(sd.preprints) == 1)
        check("trial ongoing is a real bool True", sd.trials[0]["ongoing"] is True)
        check("trial ongoing is a real bool False", sd.trials[1]["ongoing"] is False)
        check("corpus_stats threaded through", sd.corpus_stats.get("total_papers") == 36371)
        check("trials generated_utc captured", sd.trials_generated_utc == "2026-07-04T01:00:00Z")
        check("preprints generated_utc captured", sd.preprints_generated_utc == "2026-07-04T02:00:00Z")

    # 16b) Absent feeds -> empty lists, absent corpus_stats -> empty dict (no crash).
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [], "molecules": []}, fh)
        sd = site.load_site_data(d)
        check("absent trials feed -> empty list", sd.trials == [])
        check("absent preprints feed -> empty list", sd.preprints == [])
        check("absent corpus_stats -> empty dict", sd.corpus_stats == {})

    # 16c) Empty (present-but-no-rows) feeds are safe too.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [], "molecules": []}, fh)
        with open(os.path.join(d, "trials_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_utc": "z", "count": 0, "ongoing_count": 0, "trials": []}, fh)
        with open(os.path.join(d, "preprints_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_utc": "z", "count": 0, "preprints": []}, fh)
        sd = site.load_site_data(d)
        check("empty trials feed -> empty list", sd.trials == [])
        check("empty preprints feed -> empty list", sd.preprints == [])

    # 16d) Corrupt feed JSON degrades to empty rather than raising.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [], "molecules": []}, fh)
        with open(os.path.join(d, "trials_data.json"), "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        sd = site.load_site_data(d)
        check("corrupt trials feed -> empty list (no raise)", sd.trials == [])

    # 16e) The built page carries the new tabs, subtitles, and empty-placeholder
    #      text, and renders the corpus-stats strip with formatted numbers.
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "records": [{"molecule_id": "retatrutide", "molecule_name": "R", "title": "T",
                             "facet_all": "x"}],
                "molecules": [{"molecule_id": "retatrutide", "molecule_name": "R", "auto_published": "1"}],
                "corpus_stats": {"generated_utc": "2026-07-04T00:00:00Z", "total_papers": 36371,
                                 "molecules_with_data": 29, "year_min": 2015, "year_max": 2026,
                                 "pct_citations_filled": 42},
            }, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            feed3 = fh.read()
        check("Trials registry tab present", "Trials registry" in feed3)
        check("Preprints tab present", ">Preprints<" in feed3)
        check("trials tab wires to trials view", "showTab('trials')" in feed3)
        check("preprints tab wires to preprints view", "showTab('preprints')" in feed3)
        check("trials subtitle mentions ClinicalTrials.gov, not results",
              "ClinicalTrials.gov" in feed3 and "not published results" in feed3)
        check("preprints subtitle: NOT peer-reviewed",
              "not peer-reviewed" in feed3.lower())
        check("trials empty placeholder text present",
              "populates after the" in feed3 and "trials fetch runs" in feed3)
        check("preprints empty placeholder text present",
              "preprints fetch runs" in feed3)
        check("ongoing-only toggle present",
              'id="trials-ongoing"' in feed3 and "ongoingOnly" in feed3)
        check("ongoing filter drops non-ongoing when checked",
              "if (ongoingOnly && !t.ongoing) return false" in feed3)
        # corpus_stats: numbers inlined AND formatted with thousands separators in JS.
        check("corpus_stats total_papers inlined", "36371" in feed3)
        check("corpus strip renderer present", "function renderCorpusStrip" in feed3)
        check("corpus strip uses thousands-sep formatter",
              "fmtInt(CORPUS.total_papers)" in feed3)
        check("corpus strip hidden when stats absent",
              "if (!CORPUS || !CORPUS.total_papers)" in feed3)
        # trial link safety: only http(s) allowed, encoded, no javascript:.
        check("safeLink rejects non-http(s) schemes",
              "/^https?:\\/\\//i.test(u)" in feed3)
        check("trial/preprint links via safeLink helper", "function safeLink" in feed3)

    # 16f) Corpus strip hides gracefully when corpus_stats is absent from the feed.
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [{"molecule_id": "r", "molecule_name": "R", "title": "T",
                                    "facet_all": "x"}],
                       "molecules": [{"molecule_id": "r", "molecule_name": "R", "auto_published": "1"}]}, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            nostats = fh.read()
        check("empty corpus_stats inlined as {}", '"corpus_stats":{}' in nostats)

    # 16g) Security: hostile trial + preprint fields render inert (inside the JSON
    #      data block, </ neutralized) and produce no breakout / no js: href. A
    #      hostile url must NOT become a link (safeLink rejects non-http(s)).
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [], "molecules": []}, fh)
        with open(os.path.join(src, "trials_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"trials": [{
                "nct_id": "NCT99",
                "brief_title": "</script><img src=x onerror=alert(1)>",
                "lead_sponsor": '"><script>alert(2)</script>',
                "overall_status": "RECRUITING", "conditions": "x",
                "url": "javascript:alert(3)", "ongoing": True,
            }]}, fh)
        with open(os.path.join(src, "preprints_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"preprints": [{
                "id": "P99", "title": "</script><b>evil</b>",
                "authors_short": "</script>x", "server": "bioRxiv",
                "date": "2026-01-01", "url": "data:text/html,evil", "doi": "10.1/x",
            }]}, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            hostile_html = fh.read()
        # Data goes inside the SAME single application/json block, so still exactly
        # two literal </script> tags (data block close + logic script close).
        check("hostile feeds: exactly 2 </script> tags", hostile_html.count("</script>") == 2)
        check("hostile feeds: no </script> breakout", "</script><" not in hostile_html)
        check("hostile feeds: breakout neutralized in block", "<\\/script><img" in hostile_html)
        check("hostile feeds: no javascript: href", 'href="javascript:' not in hostile_html.lower())
        check("hostile feeds: no data: href", 'href="data:' not in hostile_html.lower())
        # hostile url stored inert in the block but rejected by safeLink at runtime.
        check("hostile trial url present only as inert data",
              "javascript:alert(3)" in hostile_html)

    # 16h) fetch mode blanks trials/preprints (fetched at runtime) but keeps
    #      corpus_stats inlined, and wires the runtime side-feed fetch + 404 tolerance.
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [{"molecule_id": "r", "molecule_name": "R", "title": "T",
                                    "facet_all": "x"}],
                       "molecules": [{"molecule_id": "r", "molecule_name": "R", "auto_published": "1"}],
                       "corpus_stats": {"total_papers": 100, "generated_utc": "2026-07-04T00:00:00Z"}}, fh)
        with open(os.path.join(src, "trials_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"trials": [{"nct_id": "NCT1", "brief_title": "t", "ongoing": True,
                                   "url": "https://clinicaltrials.gov/study/NCT1"}]}, fh)
        with open(os.path.join(src, "preprints_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"preprints": [{"id": "p", "title": "t", "url": "https://x/y"}]}, fh)
        site.build_site(src, out, mode="fetch")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            fetch_html = fh.read()
        check("fetch mode blanks inlined trials", '"trials":[]' in fetch_html)
        check("fetch mode blanks inlined preprints", '"preprints":[]' in fetch_html)
        check("fetch mode keeps corpus_stats inlined", '"total_papers":100' in fetch_html)
        check("fetch mode fetches trials_data.json at runtime",
              'fetchSideFeed("trials_data.json"' in fetch_html)
        check("fetch mode fetches preprints_data.json at runtime",
              'fetchSideFeed("preprints_data.json"' in fetch_html)
        check("fetch mode tolerates 404 on side feeds", "if (!r.ok) return null" in fetch_html)
        check("fetch mode: still exactly 2 </script> tags", fetch_html.count("</script>") == 2)

    # 17) PubChem "learn more" link on the Bioactives cards: a molecule that
    #     resolved to a CID carries it through MOLECULE_FIELDS and renders a
    #     pubchem.ncbi.nlm.nih.gov/compound link (built with the safe pattern);
    #     a molecule without a CID keeps an empty string and renders nothing.
    check("pubchem_cid is a molecule field", "pubchem_cid" in site.MOLECULE_FIELDS)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T1",
                 "facet_all": "x"},
                {"molecule_id": "kisspeptin", "molecule_name": "Kisspeptin", "title": "T2",
                 "facet_all": "x"},
            ],
            "molecules": [
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                 "auto_published": "1", "pubchem_cid": "2244"},
                {"molecule_id": "kisspeptin", "molecule_name": "Kisspeptin",
                 "auto_published": "1", "pubchem_cid": ""},
            ],
        }
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        # data-level: the CID threads through for the resolved molecule, and the
        # unresolved molecule degrades to an empty string (never None / KeyError).
        sd = site.load_site_data(src)
        by_id = {m["molecule_id"]: m for m in sd.molecules}
        check("resolved molecule carries its CID", by_id["retatrutide"]["pubchem_cid"] == "2244")
        check("unresolved molecule has empty CID", by_id["kisspeptin"]["pubchem_cid"] == "")
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            pc_html = fh.read()
        # The compound link base + safe href construction are present, and the
        # render is guarded so an empty CID produces no link.
        check("pubchem compound base present",
              "https://pubchem.ncbi.nlm.nih.gov/compound/" in pc_html)
        check("pubchem href uses encodeURIComponent(cid)",
              "PUBCHEM + encodeURIComponent(m.pubchem_cid)" in pc_html)
        check("pubchem link guarded on non-empty cid", "if (m.pubchem_cid) {" in pc_html)
        check("View on PubChem label present", "View on PubChem" in pc_html)
        # resolved CID is inlined for the molecule that has one; no javascript: href.
        check("resolved CID inlined in data block", '"pubchem_cid":"2244"' in pc_html)
        check("unresolved CID inlined as empty string", '"pubchem_cid":""' in pc_html)
        check("pubchem link keeps security invariant (no javascript:)",
              'href="javascript:' not in pc_html.lower())

    # 18) NIH iCite-derived facets (impact tier + clinical article) are wired as
    #     sidebar filters end-to-end: present in the field allowlist, offered as
    #     filter facets, threaded through load_site_data, and rendered as filter
    #     labels in the built page so a user can filter by impact tier / clinical
    #     status. (Regression guard: these were previously dropped by the site
    #     normalizer + filter allowlists.)
    icite_facets = ["facet_evidence_impact", "facet_clinical_article"]
    filter_fields2 = {f for f, _ in site.FILTER_FACETS}
    for nf in icite_facets:
        check(nf + " is a filter facet", nf in filter_fields2)
        check(nf + " is a record field", nf in site.RECORD_FIELDS)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T1",
                 "facet_evidence_impact": "top_decile", "facet_clinical_article": "yes",
                 "facet_all": "x"},
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T2",
                 "facet_evidence_impact": "typical", "facet_clinical_article": "no",
                 "facet_all": "x"},
            ],
            "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                           "auto_published": "2"}],
        }
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        # data-level: the facet values survive the site normalizer (were dropped before).
        sd = site.load_site_data(src)
        vals_impact = {r.get("facet_evidence_impact") for r in sd.records}
        vals_clin = {r.get("facet_clinical_article") for r in sd.records}
        check("evidence_impact value survives normalizer", "top_decile" in vals_impact)
        check("clinical_article value survives normalizer",
              "yes" in vals_clin and "no" in vals_clin)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            imp_html = fh.read()
        # UI-level: both filters are offered by their configured labels, and the
        # record values are inlined so cross-filter counting can bucket them.
        check("Evidence impact filter label rendered", "Evidence impact" in imp_html)
        check("Clinical article filter label rendered", "Clinical article" in imp_html)
        check("evidence_impact filter field wired in payload",
              '"field":"facet_evidence_impact"' in imp_html)
        check("clinical_article filter field wired in payload",
              '"field":"facet_clinical_article"' in imp_html)
        check("impact tier value inlined for filtering", "top_decile" in imp_html)
        # security invariant unaffected.
        check("icite-facet build: exactly 2 </script> tags",
              imp_html.count("</script>") == 2)

    # Translational triangle view: the toggle control, SVG host, render helpers,
    # and the three labeled corners must all be emitted by the template. Corner
    # labels are set via textContent in JS, so they appear as JS string literals.
    tri_html = site._render_html(
        site._safe_json_block({"records": []}),
        0, 0, "2026-01-01T00:00:00Z", 0, 0, "inline",
    )
    check("triangle toggle button rendered", 'id="triangle-toggle"' in tri_html)
    check("triangle toggle wired to handler", "toggleTriangle()" in tri_html)
    check("triangle SVG host rendered", 'id="triangle-svg"' in tri_html)
    check("triangle render function defined", "function renderTriangle()" in tri_html)
    check("triangle corner label Human", '"Human"' in tri_html)
    check("triangle corner label Animal", '"Animal"' in tri_html)
    check("triangle corner label Molecular/Cellular", '"Molecular/Cellular"' in tri_html)
    check("triangle plots from icite composition (barycentric)",
          "icite_human" in tri_html and "icite_animal" in tri_html and "icite_molecular" in tri_html)
    check("triangle uses barycentric placement of the 3 corners",
          "top[0]" in tri_html and "bl[0]" in tri_html and "br[0]" in tri_html)
    check("triangle no longer min-max fits into a rectangle",
          "function sx(v)" not in tri_html and "function sy(v)" not in tri_html)
    # Injection-safe invariant still holds for the triangle-bearing page.
    check("triangle build: exactly 2 </script> tags", tri_html.count("</script>") == 2)

    # Triangle dot -> paper interaction: each dot keeps a reference to its record,
    # opens the detail modal on click/keyboard, and shows a custom cursor-following
    # tooltip box (the flaky native <title> + hover-reparent was replaced).
    check("triangle dot keeps its record for interaction",
          "pts.push([px, py, r])" in tri_html)
    check("triangle dot opens the modal on click (openModal hook)",
          "openModal(rec)" in tri_html and 'd.addEventListener("click"' in tri_html)
    check("triangle dot keyboard-operable (Enter/Space -> openModal)",
          '"Enter"' in tri_html and "openModal(rec)" in tri_html)
    check("triangle dot shows a custom tooltip box on hover",
          'tip.id = "tri-tip"' in tri_html and "tip.style.display" in tri_html
          and 'd.addEventListener("mouseenter"' in tri_html)
    check("triangle tooltip built via textContent (injection-safe)",
          "tip.textContent = text" in tri_html)
    check("triangle no longer re-parents dots on hover (was breaking the tooltip)",
          'd.addEventListener("mouseover"' not in tri_html)
    check("triangle tooltip styled (tri-tip class)", ".tri-tip {" in tri_html)
    check("triangle dot styled clickable (tri-dot class + hover)",
          ".tri-dot {" in tri_html and ".tri-dot:hover" in tri_html)
    # Dot interaction must not smuggle a javascript: scheme.
    check("triangle build: no javascript: href", 'href="javascript:' not in tri_html.lower())

    # Search debounce (task 2): the q input's oninput is routed through a ~150ms
    # debounce wrapper, and it no longer calls applyFilters() inline. Other triggers
    # (facet selects) stay immediate.
    check("debounce helper present", "function debounce(fn, wait)" in tri_html)
    check("q input routes through qDebounced (150ms)",
          'oninput="qDebounced()"' in tri_html
          and "debounce(function() {" in tri_html and ", 150)" in tri_html)
    check("q input no longer calls applyFilters() inline",
          'id="q"' in tri_html and 'id="q" type="search" placeholder="title, bioactive, facets, summary..." oninput="applyFilters()"' not in tri_html)
    check("facet selects stay immediate (year-mode still calls applyFilters)",
          'id="year-mode" onchange="applyFilters()"' in tri_html)

    # A11y (task 3): cards are keyboard-operable (tabindex/role/aria-label + keydown),
    # the modal is a real dialog, focus is managed, the count region is a live region,
    # and there is a visible focus outline.
    check("card is keyboard-operable (tabindex + role button)",
          'card.setAttribute("tabindex", "0")' in tri_html
          and 'card.setAttribute("role", "button")' in tri_html)
    check("card carries aria-label with the title",
          'card.setAttribute("aria-label", r.title' in tri_html)
    check("card keydown fires openModal on Enter/Space",
          'card.addEventListener("keydown"' in tri_html and "openModal(r)" in tri_html)
    check("modal is a dialog (role=dialog aria-modal)",
          'role="dialog"' in tri_html and 'aria-modal="true"' in tri_html)
    check("modal moves focus in on open, restores on close",
          "modalOpener = document.activeElement" in tri_html
          and "close.focus()" in tri_html and "modalOpener.focus()" in tri_html)
    check("count region is an aria-live polite region",
          'id="showing" aria-live="polite"' in tri_html)
    check("visible focus outline in CSS for cards/buttons/links",
          ".card:focus-visible" in tri_html and "outline:" in tri_html.replace(" ", ""))

    # Honest rigor labeling + ranking presets. The user-facing "Reliability"
    # label is relabeled to "Automated rigor" (the underlying data fields such as
    # reliability_score are unchanged), the detail view carries an explicit
    # "Formal risk of bias: not assessed" row, and the Evidence browser exposes a
    # rank-preset ("View") control with the documented preset options.
    rig_html = site._render_html(
        site._safe_json_block({"records": []}),
        0, 0, "2026-01-01T00:00:00Z", 0, 0, "inline",
    )
    check("Automated rigor label rendered", "Automated rigor" in rig_html)
    # The standalone "Reliability" meter/sort label is gone (capitalized, as a
    # user-facing label; lowercase field names in JS are unaffected).
    check("no standalone <b>Reliability</b> meter label", "<b>Reliability</b>" not in rig_html)
    check("no Reliability sort-option label", ">Reliability<" not in rig_html)
    check("no capitalized Reliability label anywhere", "Reliability" not in rig_html)
    # Data field names must stay intact (display-only change).
    check("reliability_score field preserved", "reliability_score" in rig_html)
    # Detail-view formal risk-of-bias disclosure row.
    check("Formal risk of bias detail row present", "Formal risk of bias" in rig_html)
    check("risk of bias marked not assessed",
          "not assessed (automated rigor signals only)" in rig_html)
    # About / Methods honesty text: rule-based signals, explicitly NOT RoB 2 /
    # ROBINS-I / GRADE.
    check("about clarifies not a formal risk-of-bias assessment",
          "not a formal risk-of-bias assessment" in rig_html.lower())
    check("about names RoB 2 / ROBINS-I", "RoB 2" in rig_html and "ROBINS-I" in rig_html)
    check("about names GRADE certainty", "GRADE" in rig_html)
    # Ranking presets: the control and all option labels.
    check("rank-preset control present", 'id="rank-preset"' in rig_html)
    check("preset Default (blended rank)", "Default (blended rank)" in rig_html)
    check("preset Clinical answer", "Clinical answer" in rig_html)
    check("preset Best synthesis", "Best synthesis" in rig_html)
    check("preset Landmark", ">Landmark<" in rig_html)
    check("preset Latest", ">Latest<" in rig_html)
    check("preset Mechanism", ">Mechanism<" in rig_html)
    check("presetSort comparator defined", "function presetSort" in rig_html)
    # Preset override wiring in applyFilters (predictable: preset overrides Sort).
    check("preset overrides sort when not default",
          'presetSort(visible, preset)' in rig_html)
    # Injection-safe invariant survives the new markup.
    check("rigor build: exactly 2 </script> tags", rig_html.count("</script>") == 2)

    # Single-molecule Evidence map: a use-case x evidence-class COUNT matrix (a
    # map, NOT an efficacy verdict). facet_indication + evidence_class must reach
    # the browser (be in RECORD_FIELDS + survive the normalizer), and the built
    # page must expose the render function, its caption, and the single-molecule
    # detection helper it reuses. Injection-safe invariant preserved.
    check("facet_indication in RECORD_FIELDS", "facet_indication" in site.RECORD_FIELDS)
    check("evidence_class in RECORD_FIELDS", "evidence_class" in site.RECORD_FIELDS)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T1",
                 "facet_indication": "obesity; NAFLD",
                 "evidence_class": "human_clinical_controlled", "facet_all": "x"},
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "T2",
                 "facet_indication": "obesity",
                 "evidence_class": "preclinical_invivo", "facet_all": "x"},
            ],
            "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                           "auto_published": "2"}],
        }
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        # data-level: both fields survive the site normalizer into the browser payload.
        sd = site.load_site_data(src)
        inds = {r.get("facet_indication") for r in sd.records}
        classes = {r.get("evidence_class") for r in sd.records}
        check("facet_indication value survives normalizer", "obesity; NAFLD" in inds)
        check("evidence_class values survive normalizer",
              "human_clinical_controlled" in classes and "preclinical_invivo" in classes)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            evmap_html = fh.read()
        # UI-level: render function, caption, single-molecule detection, host div.
        check("evidence-map host div rendered", 'id="evidence-map"' in evmap_html)
        check("renderEvidenceMap function defined",
              "function renderEvidenceMap" in evmap_html)
        check("single-molecule detection helper defined",
              "function singleMoleculeId" in evmap_html)
        check("evidence-map caption present",
              "Counts of retrieved papers by use case and evidence class "
              "(not an efficacy assessment)." in evmap_html)
        check("evidence-map column group Human controlled", '"Human controlled"' in evmap_html)
        check("evidence-map column group Reviews", '"Reviews"' in evmap_html)
        # Record values reach the browser so the matrix can be built client-side.
        check("facet_indication value inlined for the map", "obesity; NAFLD" in evmap_html)
        check("evidence_class value inlined for the map",
              "human_clinical_controlled" in evmap_html)
        # Injection-safe invariant unaffected by the evidence-map markup.
        check("evmap build: exactly 2 </script> tags",
              evmap_html.count("</script>") == 2)
        check("evmap build: no </script> breakout", "</script><" not in evmap_html)

    # 17) Data-health coverage line: the new corpus_stats keys survive the site
    #     normalizer, are wired into CORPUS_STATS_FIELDS, and the corpus strip
    #     renders a compact "Data health" line referencing each metric.
    for k in ("pct_with_abstract", "pct_with_doi", "pct_with_icite"):
        check(k + " in CORPUS_STATS_FIELDS", k in site.CORPUS_STATS_FIELDS)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "records": [{"molecule_id": "r", "molecule_name": "R", "title": "T", "facet_all": "x"}],
                "molecules": [{"molecule_id": "r", "molecule_name": "R", "auto_published": "1"}],
                "corpus_stats": {"generated_utc": "2026-07-04T00:00:00Z", "total_papers": 36371,
                                 "pct_with_abstract": 98, "pct_with_doi": 91,
                                 "pct_citations_filled": 55, "pct_with_icite": 40},
            }, fh)
        sd = site.load_site_data(src)
        check("pct_with_abstract survives normalizer", sd.corpus_stats.get("pct_with_abstract") == 98)
        check("pct_with_icite survives normalizer", sd.corpus_stats.get("pct_with_icite") == 40)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            dh_html = fh.read()
        check("Data health line rendered", "Data health" in dh_html)
        check("data-health references pct_with_abstract", "CORPUS.pct_with_abstract" in dh_html)
        check("data-health references pct_with_doi", "CORPUS.pct_with_doi" in dh_html)
        check("data-health references pct_with_icite", "CORPUS.pct_with_icite" in dh_html)
        check("data-health uses abstracts/DOIs/iCite labels",
              '"abstracts"' in dh_html and '"DOIs"' in dh_html and '"iCite"' in dh_html)
        check("coverage values inlined", '"pct_with_abstract":98' in dh_html and '"pct_with_icite":40' in dh_html)
        check("data-health build: exactly 2 </script> tags", dh_html.count("</script>") == 2)

    # 18) Retraction badge: is_retracted / facet_publication_flag reach the browser,
    #     the CSS + badge machinery are emitted, and only the retracted record
    #     carries the flag (the non-retracted one does not).
    check("is_retracted in RECORD_FIELDS", "is_retracted" in site.RECORD_FIELDS)
    check("facet_publication_flag in RECORD_FIELDS", "facet_publication_flag" in site.RECORD_FIELDS)
    check("facet_publication_flag is a filter facet",
          "facet_publication_flag" in {f for f, _ in site.FILTER_FACETS})
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "records": [
                    {"molecule_id": "r", "molecule_name": "R", "title": "Retracted paper",
                     "is_retracted": True, "facet_publication_flag": "retracted", "facet_all": "x"},
                    {"molecule_id": "r", "molecule_name": "R", "title": "Clean paper",
                     "facet_all": "x"},
                ],
                "molecules": [{"molecule_id": "r", "molecule_name": "R", "auto_published": "2"}],
            }, fh)
        sd = site.load_site_data(src)
        flags = [rr.get("is_retracted") for rr in sd.records]
        check("retracted record flagged True through normalizer", "True" in flags)
        check("non-retracted record blank/not-True", flags.count("True") == 1)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            rt_html = fh.read()
        check("retracted CSS pill class present", ".pill.retracted" in rt_html)
        check("isRetracted helper present", "function isRetracted" in rt_html)
        check("RETRACTED badge text present", "\\u26a0 RETRACTED" in rt_html)
        check("retracted flag inlined for exactly one record",
              rt_html.count('"is_retracted":"True"') == 1)
        check("publication_flag filter label rendered", "Publication flag" in rt_html)
        check("retraction build: exactly 2 </script> tags", rt_html.count("</script>") == 2)
        check("retraction build: no </script> breakout", "</script><" not in rt_html)

    # 19) Single-molecule Safety & evidence status panel: a caution panel computed
    #     from the visible records for one molecule (NOT a safety/efficacy verdict).
    #     facet_route must reach the browser; the built page must expose the render
    #     function, the persistent caution strings, the "Human efficacy data" line,
    #     and a studied route. The yes/none branch is driven by whether any visible
    #     record is human evidence; we verify both cases via what is inlined for the
    #     browser to compute (a molecule WITH vs WITHOUT human evidence). Injection-
    #     safe invariant preserved.
    check("facet_route in RECORD_FIELDS", "facet_route" in site.RECORD_FIELDS)
    # 19a) A single molecule WITH human evidence and a studied route.
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "RCT",
                 "evidence_class": "human_clinical_controlled", "website_section": "Human evidence",
                 "facet_route": "subcutaneous", "facet_all": "x"},
                {"molecule_id": "retatrutide", "molecule_name": "Retatrutide", "title": "mouse",
                 "evidence_class": "preclinical_invivo", "website_section": "Preclinical evidence",
                 "facet_route": "oral", "facet_all": "x"},
            ],
            "molecules": [{"molecule_id": "retatrutide", "molecule_name": "Retatrutide",
                           "auto_published": "2"}],
        }
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        # data-level: facet_route survives the site normalizer into the browser payload.
        sd = site.load_site_data(src)
        routes = {r.get("facet_route") for r in sd.records}
        check("facet_route value survives normalizer",
              "subcutaneous" in routes and "oral" in routes)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            safe_html = fh.read()
        # UI-level: render function, host div, single-molecule detection reuse.
        check("safety-panel host div rendered", 'id="safety-panel"' in safe_html)
        check("renderSafetyPanel function defined",
              "function renderSafetyPanel" in safe_html)
        check("safety panel reuses singleMoleculeId detection",
              "function singleMoleculeId" in safe_html)
        check("safety CSS class rule present", ".safety {" in safe_html)
        # Human-efficacy line + both yes/none branch literals present.
        check("Human efficacy data line present", "Human efficacy data" in safe_html)
        check("safety yes-branch literal present", '"yes ("' in safe_html)
        check("safety none-branch literal present", '"none found"' in safe_html)
        check("Controlled human trials line present", "Controlled human trials" in safe_html)
        check("Routes studied line present", "Routes studied" in safe_html)
        check("route not-reported fallback present", '"not clearly reported"' in safe_html)
        # Persistent static caution strings (the two the task names explicitly).
        check("caution: absence of harms is not safety",
              "Absence of reported harms is not evidence of safety." in safe_html)
        check("caution: not medical advice",
              "not medical advice" in safe_html)
        check("caution: experimental/investigational framing",
              "experimental, investigational, or not approved" in safe_html)
        check("caution: purity/dose/formulation differ",
              "purity, dose, and formulation" in safe_html)
        # WITH-human case: a human evidence_class + section are inlined so the
        # browser computes "yes"; a studied route reaches the browser too.
        check("human evidence_class inlined (drives yes)",
              '"evidence_class":"human_clinical_controlled"' in safe_html)
        check("human website_section inlined (drives yes)",
              '"website_section":"Human evidence"' in safe_html)
        check("studied route inlined for the panel",
              '"facet_route":"subcutaneous"' in safe_html)
        # Injection-safe invariant unaffected by the safety panel markup.
        check("safety build: exactly 2 </script> tags",
              safe_html.count("</script>") == 2)
        check("safety build: no </script> breakout", "</script><" not in safe_html)
    # 19b) A single molecule WITHOUT any human evidence: nothing human is inlined,
    #      so the browser computes "none found" for Human efficacy data; a route is
    #      still reported.
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        feed = {
            "generated_utc": "2026-07-04T00:00:00Z",
            "records": [
                {"molecule_id": "kisspeptin", "molecule_name": "Kisspeptin", "title": "mouse",
                 "evidence_class": "preclinical_invivo", "website_section": "Preclinical evidence",
                 "facet_route": "intranasal", "facet_all": "x"},
            ],
            "molecules": [{"molecule_id": "kisspeptin", "molecule_name": "Kisspeptin",
                           "auto_published": "1"}],
        }
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            nohuman_html = fh.read()
        # No human evidence_class / section is inlined -> the panel would render
        # "none found"; the render function + none-branch literal are still present.
        check("no-human page: renderSafetyPanel present",
              "function renderSafetyPanel" in nohuman_html)
        check("no-human page: no human evidence_class inlined",
              '"evidence_class":"human_clinical_controlled"' not in nohuman_html)
        check("no-human page: no Human evidence section inlined",
              '"website_section":"Human evidence"' not in nohuman_html)
        check("no-human page: route still inlined for panel",
              '"facet_route":"intranasal"' in nohuman_html)
        check("no-human build: exactly 2 </script> tags",
              nohuman_html.count("</script>") == 2)

    # 20) Trial -> publication linkage: a trial carrying RESULT/DERIVED PMIDs
    #     (result_pmids) renders a "Published results:" line with pubmed.ncbi.nlm.nih.gov
    #     links via the safe helper; a trial without carries an empty string and
    #     renders no such link. The field must be in the trial allowlist or it is
    #     stripped by _norm_trial before it ever reaches the UI.
    check("result_pmids is a trial field", "result_pmids" in site.TRIAL_FIELDS)
    check("reference_pmids is a trial field", "reference_pmids" in site.TRIAL_FIELDS)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [], "molecules": []}, fh)
        with open(os.path.join(src, "trials_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_utc": "2026-07-04T00:00:00Z", "count": 2,
                       "ongoing_count": 0, "trials": [
                           {"nct_id": "NCT77", "molecule_name": "R", "brief_title": "With results",
                            "overall_status": "COMPLETED", "ongoing": False,
                            "result_pmids": "37345678", "reference_pmids": "37345678; 36000000",
                            "url": "https://clinicaltrials.gov/study/NCT77"},
                           {"nct_id": "NCT88", "molecule_name": "R", "brief_title": "No results",
                            "overall_status": "RECRUITING", "ongoing": True,
                            "result_pmids": "", "reference_pmids": "",
                            "url": "https://clinicaltrials.gov/study/NCT88"},
                       ]}, fh)
        # data-level: the allowlist threads result_pmids through untouched.
        sd = site.load_site_data(src)
        by_nct = {t["nct_id"]: t for t in sd.trials}
        check("trial with results keeps result_pmids", by_nct["NCT77"]["result_pmids"] == "37345678")
        check("trial without results has empty result_pmids", by_nct["NCT88"]["result_pmids"] == "")
        site.build_site(src, out, mode="inline")
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            pmid_html = fh.read()
        # PubMed base + safe href construction + guarded "Published results:" line.
        check("trials pubmed base present", "https://pubmed.ncbi.nlm.nih.gov/" in pmid_html)
        check("trial pmid href uses encodeURIComponent(pmid)",
              "PUBMED + encodeURIComponent(pmid)" in pmid_html)
        check("Published results label present", "Published results:" in pmid_html)
        check("trial pubs guarded on non-empty result_pmids", "if (resultPmids.length) {" in pmid_html)
        # result pmid inlined for the trial that has one; empty for the one without.
        check("result pmid inlined in data block", '"result_pmids":"37345678"' in pmid_html)
        check("no-result trial inlined as empty string", '"result_pmids":""' in pmid_html)
        check("trial pmid link keeps security invariant (no javascript:)",
              'href="javascript:' not in pmid_html.lower())
        # _TEMPLATE.format() still renders (page built without KeyError/IndexError).
        check("trials page rendered via _TEMPLATE.format()", "<!DOCTYPE html>" in pmid_html)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
