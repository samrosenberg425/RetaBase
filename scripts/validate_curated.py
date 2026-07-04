#!/usr/bin/env python3
"""QA / validation pass over the curated evidence CSV.

Runs a set of cheap, offline invariants over
``exports/curated/curated_evidence.csv`` and reports, per check, a count plus a
PASS/FAIL verdict. The process exits non-zero if any check FAILs so this can gate
a build in CI.

Checks:

* ``reliability_score`` is an integer in ``[0, 100]`` and ``reliability_tier`` is
  in the known vocabulary.
* ``publication_status`` is in the known set; every ``auto_published`` row has
  ``required_fields_present == True`` and a non-empty ``website_section``.
* ``evidence_id`` values are unique (no duplicates).
* Facet sanity: every record whose ``facet_species`` mentions
  ``nonhuman_primate`` has a primate term in its title+abstract (when a papers
  table is available to join); otherwise every ``facet_species`` value is drawn
  from the known species vocabulary.
* ``auto_publish_eligible`` row count is ``> 0`` and ``< total`` (sanity band).

A short report is written to stdout and to
``<curated-dir>/validation_report.txt``.

Pure stdlib (csv/sqlite3). No network.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

KNOWN_TIERS = {"high", "moderate", "limited", "low", "not_applicable"}
KNOWN_STATUSES = {"featured", "listed", "review", "excluded_noise"}
FEATURED_STATUS = "featured"
# Species vocabulary from config/FACETS.csv (species facet group).
KNOWN_SPECIES = {
    "human",
    "mouse",
    "rat",
    "nonhuman_primate",
    "pig",
    "dog",
    "rabbit",
    "zebrafish",
    "drosophila",
    "c_elegans",
    "cell_line",
}
PRIMATE_TERMS = re.compile(
    r"\b(?:monkey|macaque|primate|cynomolgus|rhesus|marmoset|baboon)\b", re.IGNORECASE
)


class Report:
    """Accumulates per-check results and an overall pass/fail verdict."""

    def __init__(self) -> None:
        self.lines: List[str] = []
        self.all_pass = True

    def check(self, name: str, ok: bool, detail: str) -> None:
        verdict = "PASS" if ok else "FAIL"
        if not ok:
            self.all_pass = False
        self.lines.append(f"[{verdict}] {name}: {detail}")

    def note(self, text: str) -> None:
        self.lines.append(text)

    def render(self) -> str:
        header = "Curated dataset validation report"
        footer = "OVERALL: PASS" if self.all_pass else "OVERALL: FAIL"
        return "\n".join([header, "=" * len(header), *self.lines, "", footer])


def _to_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _load_rows(path: str) -> List[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _load_paper_text(db_path: str) -> Dict[str, str]:
    """Return {pmid: title+abstract lowercased} from a papers payload table.

    Best-effort: returns ``{}`` if the DB or table is unavailable.
    """
    out: Dict[str, str] = {}
    if not db_path or not os.path.exists(db_path):
        return out
    try:
        import json

        conn = sqlite3.connect(db_path)
        cur = conn.execute("select payload_json from papers")
        for (payload,) in cur:
            try:
                p = json.loads(payload)
            except (TypeError, ValueError):
                continue
            pmid = str(p.get("pmid", "") or "")
            if not pmid:
                continue
            out[pmid] = f"{p.get('title', '') or ''} {p.get('abstract', '') or ''}".lower()
        conn.close()
    except sqlite3.OperationalError:
        return out
    return out


def _split_species(value: str) -> List[str]:
    return [v.strip() for v in str(value or "").split(";") if v.strip()]


def validate(curated_dir: str, db_path: Optional[str] = None) -> Tuple[Report, int]:
    path = os.path.join(curated_dir, "curated_evidence.csv")
    rep = Report()
    if not os.path.exists(path):
        rep.check("input", False, f"{path} not found")
        return rep, 1

    rows = _load_rows(path)
    total = len(rows)
    rep.note(f"Rows: {total}")

    # 1) reliability_score int in [0,100]; reliability_tier known.
    bad_score = 0
    bad_tier = 0
    for r in rows:
        n = _to_int(r.get("reliability_score"))
        if n is None or not (0 <= n <= 100):
            bad_score += 1
        if str(r.get("reliability_tier", "")).strip() not in KNOWN_TIERS:
            bad_tier += 1
    rep.check("reliability_score in [0,100]", bad_score == 0, f"{bad_score} out-of-range")
    rep.check("reliability_tier in vocabulary", bad_tier == 0, f"{bad_tier} unknown tiers")

    # 2) publication_status known; auto_published rows well-formed.
    bad_status = 0
    bad_auto = 0
    auto_count = 0
    for r in rows:
        if str(r.get("publication_status", "")).strip() not in KNOWN_STATUSES:
            bad_status += 1
        if str(r.get("publication_status", "")).strip() == FEATURED_STATUS:
            auto_count += 1
            if not _truthy(r.get("required_fields_present")) or not str(r.get("website_section", "")).strip():
                bad_auto += 1
    rep.check("publication_status in vocabulary", bad_status == 0, f"{bad_status} unknown statuses")
    rep.check(
        "featured rows have required fields + section",
        bad_auto == 0,
        f"{bad_auto} malformed of {auto_count} featured",
    )

    # 3) no duplicate evidence_id.
    seen: Dict[str, int] = {}
    for r in rows:
        eid = str(r.get("evidence_id", ""))
        seen[eid] = seen.get(eid, 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    rep.check("evidence_id unique", len(dupes) == 0, f"{len(dupes)} duplicate ids")

    # 4) facet_species sanity.
    unknown_species = 0
    for r in rows:
        for sp in _split_species(r.get("facet_species", "")):
            if sp not in KNOWN_SPECIES:
                unknown_species += 1
    rep.check("facet_species values in vocabulary", unknown_species == 0, f"{unknown_species} unknown species values")

    paper_text = _load_paper_text(db_path) if db_path else {}
    nhp_rows = [r for r in rows if "nonhuman_primate" in _split_species(r.get("facet_species", ""))]
    if paper_text:
        nhp_bad = 0
        nhp_checked = 0
        for r in nhp_rows:
            pmid = str(r.get("pmid", "") or "")
            text = paper_text.get(pmid)
            if text is None:
                continue  # no joinable paper; skip rather than false-fail
            nhp_checked += 1
            if not PRIMATE_TERMS.search(text):
                nhp_bad += 1
        rep.check(
            "nonhuman_primate facets backed by primate term in text",
            nhp_bad == 0,
            f"{nhp_bad} of {nhp_checked} checked ({len(nhp_rows)} NHP rows) lack a primate term",
        )
    else:
        rep.note(
            f"[INFO] nonhuman_primate rows: {len(nhp_rows)} "
            "(no papers DB provided; verified species vocabulary only)"
        )

    # 5) auto_publish_eligible band: >0 and <total.
    eligible = sum(1 for r in rows if _truthy(r.get("auto_publish_eligible")))
    rep.check(
        "auto_publish_eligible count sane (0 < n < total)",
        0 < eligible < total,
        f"{eligible} eligible of {total}",
    )

    # Informational: disambiguation-impact count (model_primary != model_type).
    changed = 0
    for r in rows:
        mp = str(r.get("model_primary", "") or "")
        mt = str(r.get("model_type", "") or "").lower().replace(" ", "_")
        if mp and mp != mt:
            changed += 1
    rep.note(f"[INFO] model_primary != model_type (disambiguation impact): {changed} of {total}")

    return rep, (0 if rep.all_pass else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the curated evidence CSV.")
    ap.add_argument("--curated-dir", default="exports/curated")
    ap.add_argument(
        "--db",
        default="",
        help="Optional SQLite DB to join papers for the NHP facet text check.",
    )
    args = ap.parse_args()

    rep, code = validate(args.curated_dir, args.db or None)
    text = rep.render()
    print(text)

    try:
        os.makedirs(args.curated_dir, exist_ok=True)
        with open(os.path.join(args.curated_dir, "validation_report.txt"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass

    sys.exit(code)


if __name__ == "__main__":
    main()
