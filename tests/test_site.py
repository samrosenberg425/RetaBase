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
    check("count line uses base total (Showing X of Y papers)",
          'Showing " + fmtInt(x) + " of " + fmtInt(y) + " papers"' in feed_html
          and "filtered out" in feed_html)
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

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
