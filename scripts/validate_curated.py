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
import json
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

# --- corpus-collapse anomaly gates ------------------------------------------
# When a known-good baseline corpus_stats.json is supplied, FAIL the build if a
# core corpus metric has collapsed to below this fraction of the baseline value
# (a catastrophic data loss). GROWTH never fails -- only drops below the floor
# do. If no valid baseline is available (first run / bootstrapping) the gates are
# skipped and the build passes.
COLLAPSE_RATIO = 0.5  # current must be >= 50% of baseline
# Metrics read straight off corpus_stats.json for the drop check.
COLLAPSE_METRICS = ("total_papers", "molecules_with_data")


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


def _to_num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_stats(path: Optional[str]) -> Optional[dict]:
    """Load a corpus_stats.json file. Returns ``None`` if absent/blank/invalid."""
    if not path or not str(path).strip() or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _published_count(stats: dict) -> Optional[float]:
    """Published/public record count from stats = featured + listed.

    ``public_records.csv`` is exactly the ``featured`` + ``listed`` rows, and both
    counts are recorded in corpus_stats.json, so this is comparable across runs.
    Returns ``None`` if neither key is present/numeric.
    """
    featured = _to_num(stats.get("featured"))
    listed = _to_num(stats.get("listed"))
    if featured is None and listed is None:
        return None
    return (featured or 0.0) + (listed or 0.0)


def _anomaly_check(rep: "Report", label: str, baseline_value, current_value) -> None:
    """Add a collapse-detection check row: FAIL if current < COLLAPSE_RATIO*baseline.

    Growth (current >= baseline) always PASSes. Skipped (as an INFO note, no
    fail) when either value is missing/non-numeric or the baseline is <= 0.
    """
    b = _to_num(baseline_value)
    c = _to_num(current_value)
    if b is None or c is None or b <= 0:
        rep.note(
            f"[INFO] anomaly gate '{label}' skipped "
            f"(baseline={baseline_value!r}, current={current_value!r})"
        )
        return
    floor = COLLAPSE_RATIO * b
    rep.check(
        f"{label} not collapsed (>= {int(COLLAPSE_RATIO * 100)}% of baseline)",
        c >= floor,
        f"current={c:g} baseline={b:g} (floor={floor:g})",
    )


def validate(
    curated_dir: str,
    db_path: Optional[str] = None,
    baseline_path: Optional[str] = None,
    stats_path: Optional[str] = None,
) -> Tuple[Report, int]:
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

    # 3a) rank_score is an int in [0,100] (it drives the primary sort). Hard gate.
    bad_rank = 0
    for r in rows:
        n = _to_int(r.get("rank_score"))
        if n is None or not (0 <= n <= 100):
            bad_rank += 1
    rep.check("rank_score in [0,100]", bad_rank == 0, f"{bad_rank} out-of-range")

    # 3b) duplicate (pmid, molecule) pairs -- a paper matched via multiple rules.
    # INFORMATIONAL (the curated CSV intentionally keeps the per-rule audit trail;
    # the site feed and molecule counts dedup these), so we report, not fail.
    pair_seen: Dict[tuple, int] = {}
    for r in rows:
        pmid = str(r.get("pmid", "") or "")
        if pmid:
            k = (pmid, str(r.get("molecule_id", "") or ""))
            pair_seen[k] = pair_seen.get(k, 0) + 1
    pair_dupes = sum(v - 1 for v in pair_seen.values() if v > 1)
    rep.note(f"[INFO] duplicate (pmid,molecule) rows in curated export: {pair_dupes} "
             f"(deduped in the site feed + molecule counts)")

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

    # 5b) schema-drift canary: rows dropped at load because their JSON payload didn't
    # parse. The builder records this in corpus_stats.json; enforce it here. FAIL only
    # on meaningful drift (>1% of evidence, min 20 rows) so a single stray row warns
    # rather than blocking a deploy.
    canary_path = stats_path or os.path.join(curated_dir, "corpus_stats.json")
    canary_stats = _load_stats(canary_path) or {}
    dropped = _to_int(canary_stats.get("dropped_payload_rows")) or 0
    total_ev = _to_int(canary_stats.get("total_evidence")) or 0
    if dropped and total_ev and dropped > max(20, int(0.01 * total_ev)):
        rep.check("payload schema drift", False,
                  f"{dropped} rows dropped at load (>1% of {total_ev} -- likely schema change)")
    elif dropped:
        rep.note(f"[INFO] {dropped} payload row(s) dropped at load (schema-drift canary; within tolerance)")
    else:
        rep.note("[INFO] no payload rows dropped at load")

    # 6) corpus-collapse anomaly gates vs a known-good baseline corpus_stats.json.
    # corpus_stats.json is written by build_curated_database.py into the curated
    # out-dir, so the CURRENT run's stats live at <curated-dir>/corpus_stats.json
    # (overridable via --stats). When a valid --baseline is provided, FAIL if any
    # core metric has collapsed to < COLLAPSE_RATIO of the baseline. Missing/blank/
    # invalid baseline -> skip (bootstrapping first run) and pass.
    if baseline_path and str(baseline_path).strip():
        baseline_stats = _load_stats(baseline_path)
        current_path = stats_path or os.path.join(curated_dir, "corpus_stats.json")
        current_stats = _load_stats(current_path)
        if baseline_stats is None:
            rep.note(
                f"[INFO] corpus-collapse gates skipped: no valid baseline at "
                f"{baseline_path} (bootstrapping / first run)."
            )
        elif current_stats is None:
            rep.note(
                f"[INFO] corpus-collapse gates skipped: current corpus_stats.json "
                f"missing/invalid at {current_path}."
            )
        else:
            for key in COLLAPSE_METRICS:
                _anomaly_check(rep, key, baseline_stats.get(key), current_stats.get(key))
            _anomaly_check(
                rep,
                "published_records",
                _published_count(baseline_stats),
                _published_count(current_stats),
            )
    else:
        rep.note("[INFO] no --baseline provided; corpus-collapse anomaly gates skipped.")

    return rep, (0 if rep.all_pass else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the curated evidence CSV.")
    ap.add_argument("--curated-dir", default="exports/curated")
    ap.add_argument(
        "--db",
        default="",
        help="Optional SQLite DB to join papers for the NHP facet text check.",
    )
    ap.add_argument(
        "--baseline",
        default="",
        help=(
            "Optional previous (known-good) corpus_stats.json. When present and "
            "valid, gates the build against a catastrophic corpus collapse "
            "(a core metric dropping below 50%% of this baseline). Absent/blank/"
            "invalid -> gates skipped (bootstrapping)."
        ),
    )
    ap.add_argument(
        "--stats",
        default="",
        help=(
            "Path to the CURRENT run's corpus_stats.json for the anomaly gates. "
            "Defaults to <curated-dir>/corpus_stats.json."
        ),
    )
    args = ap.parse_args()

    rep, code = validate(
        args.curated_dir,
        args.db or None,
        baseline_path=args.baseline or None,
        stats_path=args.stats or None,
    )
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
