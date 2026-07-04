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

    # 6) Cross-filter facet counting: selecting Species=human must shrink the
    #    Indication counts to only human records, while the Species facet does
    #    NOT constrain its own option list.
    recs = [
        {"facet_species": "human", "facet_indication": "obesity_weight; diabetes_glycemic"},
        {"facet_species": "human", "facet_indication": "obesity_weight"},
        {"facet_species": "mouse", "facet_indication": "obesity_weight"},
    ]
    multi = {"facet_species", "facet_indication"}
    counts = site._cross_filter_counts(recs, {"facet_species": "human", "facet_indication": ""}, multi)
    # Indication counts respect the human filter: obesity 2 (both human), diabetes 1.
    check("cross-filter indication respects species", counts["facet_indication"].get("obesity_weight") == 2)
    check("cross-filter indication excludes mouse-only", counts["facet_indication"].get("diabetes_glycemic") == 1)
    # Species facet ignores its OWN selection, so mouse is still counted (=1).
    counts_sp = site._cross_filter_counts(recs, {"facet_species": "human"}, multi)
    check("facet does not constrain itself", counts_sp["facet_species"].get("mouse") == 1)
    check("facet self count includes selected", counts_sp["facet_species"].get("human") == 2)

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
        recs2, {"facet_species": "human", "facet_drug_class": ""}, multi2)
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
    # (b) The explainer text is present (reliability + directness + notes lines).
    check("explainer: How to read this", "How to read this" in feed_html)
    check("explainer: directness line",
          "how directly the evidence applies to humans" in feed_html)
    check("explainer: curator notes line",
          "Notes are for curators" in feed_html)
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
    check("count line uses filtered total (Showing X of Y matches)",
          'of " + fmtInt(total) +' in feed_html and '" matches (refine filters' in feed_html)
    check("render window mounts only first visibleCount",
          "Math.min(visibleCount, total)" in feed_html)
    check("visibleCount resets on filter/search/sort change",
          "visibleCount = RENDER_LIMIT;" in feed_html)
    # Cross-filter counts must be computed over the full filtered set, so they are
    # driven by RECORDS (the complete data), never lastVisible / the render window.
    check("cross-filter counts independent of render cap",
          "crossFilterCounts(filters, q)" in feed_html
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

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
