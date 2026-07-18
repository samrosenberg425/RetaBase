#!/usr/bin/env python3
"""Build a small, self-contained inline fixture.html for the Playwright E2E test.

Inline mode embeds the records directly in the HTML, so the fixture opens via
file:// with no server and no network -- the browser test just drives the real UI
(search, card -> modal, tab switching). This lets CI actually EXECUTE the site's
JavaScript, closing the "JS is only substring-tested" gap the audit flagged.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "build_public_site", os.path.join(ROOT, "scripts", "build_public_site.py"))
_bps = importlib.util.module_from_spec(_spec)
sys.modules["build_public_site"] = _bps  # needed so @dataclass can resolve __module__
_spec.loader.exec_module(_bps)


def _records():
    mols = [("retatrutide", "Retatrutide"), ("semaglutide", "Semaglutide"),
            ("tirzepatide", "Tirzepatide")]
    recs = []
    for i, (mid, name) in enumerate(mols):
        for j in range(3):
            recs.append({
                "molecule_id": mid, "molecule_name": name, "pmid": str(1000 + i * 10 + j),
                "title": f"{name} randomized trial {j} in obesity",
                "facet_all": f"{name} obesity weight glp1",
                "pub_year": str(2020 + j), "website_section": "Human evidence",
                "evidence_class_label": "Human — controlled trial",
                "reliability_score": str(70 + j), "evidence_directness": str(80 + j),
                "rank_score": str(90 - j), "refined_dose": "5 mg",
                "refined_route": "subcutaneous", "refined_extraction_scope": "document",
            })
    return recs, mols


def main() -> None:
    recs, mols = _records()
    feed = {
        "records": recs,
        "molecules": [{"molecule_id": m, "molecule_name": n, "auto_published": "3",
                       "total_records": "3", "record_count": "3", "human_count": "3",
                       "density_tier": "sparse"} for m, n in mols],
        "corpus_stats": {"total_papers": 9, "total_evidence": 9, "molecules_with_data": 3,
                         "generated_utc": "2026-01-01T00:00:00Z",
                         "corpus_fingerprint": "testfixture01", "build_sha": "local",
                         "zenodo_doi": "10.5281/zenodo.21207064"},
    }
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "site_data.json"), "w", encoding="utf-8") as fh:
            json.dump(feed, fh)
        _bps.build_site(src, out, mode="inline")
        shutil.copy(os.path.join(out, "index.html"), os.path.join(HERE, "fixture.html"))
    print("wrote", os.path.join(HERE, "fixture.html"))


if __name__ == "__main__":
    main()
