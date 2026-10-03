#!/usr/bin/env python3
"""Retrieval benchmark tool (WS4): makes retrieval-rule changes scientifically
testable instead of subjective.

Two independent modes:

  OFFLINE  -- check the CURRENT BUILT CORPUS against approved benchmark rows.
             Read-only against the sqlite payload tables; no network.

    python3 scripts/benchmark_retrieval.py offline \
        --pubmed-db data/retarats_pubmed.sqlite \
        --trials-db data/retarats_trials.sqlite \
        --preprints-db data/retarats_preprints.sqlite \
        --benchmark config/retrieval_benchmark.csv

  DIFF     -- compare OLD vs PROPOSED retrieval rules LIVE (real PubMed/CT.gov
             queries), without writing anything to the corpus. Use this to
             evaluate a query-string edit, a datetype change (pdat vs edat), or
             a CT.gov query.term vs query.intr choice before committing to it.

    python3 scripts/benchmark_retrieval.py diff-pubmed --molecule mots_c \
        --query-a '"MOTS-c"[tiab]' --query-b '"MOTS-c"[tiab] OR "MOTSc"[tiab]'

    python3 scripts/benchmark_retrieval.py diff-ctgov --molecule mots_c \
        --query MOTS-c --field-a term --field-b intr

Both modes are read-only: OFFLINE only ever SELECTs from local sqlite files;
DIFF only ever performs GET requests against public PubMed/CT.gov endpoints. No
corpus writer, no promotion, no state mutation.

ONLY rows with status == "approved" in the benchmark CSV are ever checked or
allowed to affect a pass/fail verdict -- "proposed" (model-suggested, awaiting
human review) and "rejected" rows are reported separately for visibility but
never gate anything (see ``load_benchmark_rows``/``BenchmarkReport.ok``).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.enrichment.common import load_payload_table  # noqa: E402

BENCHMARK_COLUMNS = ["molecule_id", "id_type", "id", "expected", "reason",
                     "status", "reviewed_by", "reviewed_on"]
VALID_ID_TYPES = {"pmid", "nct_id", "doi"}
VALID_EXPECTED = {"include", "exclude"}
VALID_STATUS = {"approved", "proposed", "rejected"}

DEFAULT_PUBMED_DB = "data/retarats_pubmed.sqlite"
DEFAULT_TRIALS_DB = "data/retarats_trials.sqlite"
DEFAULT_PREPRINTS_DB = "data/retarats_preprints.sqlite"
DEFAULT_BENCHMARK = "config/retrieval_benchmark.csv"
DEFAULT_MANUAL_EXCLUSIONS = "config/manual_exclusions.csv"


# --------------------------------------------------------------------------
# Benchmark CSV loading
# --------------------------------------------------------------------------

@dataclass
class BenchmarkRow:
    molecule_id: str
    id_type: str
    id: str
    expected: str
    reason: str
    status: str
    reviewed_by: str
    reviewed_on: str

    @property
    def key(self) -> Tuple[str, str]:
        norm_id = self.id.upper() if self.id_type == "nct_id" else self.id.lower()
        return (self.molecule_id, norm_id)


def load_benchmark_rows(path: str) -> List[BenchmarkRow]:
    """Load every row (any status). Callers that gate on approval MUST filter
    with ``approved_only`` themselves -- this function never does it silently,
    so "did I forget to filter" is never a silent bug."""
    if not os.path.exists(path):
        return []
    rows: List[BenchmarkRow] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, r in enumerate(reader, start=2):
            molecule_id = (r.get("molecule_id") or "").strip()
            if not molecule_id:
                continue
            id_type = (r.get("id_type") or "").strip().lower()
            expected = (r.get("expected") or "").strip().lower()
            status = (r.get("status") or "").strip().lower()
            if id_type not in VALID_ID_TYPES:
                raise ValueError(f"{path}:{i}: unknown id_type {id_type!r}; must be one of {sorted(VALID_ID_TYPES)}")
            if expected not in VALID_EXPECTED:
                raise ValueError(f"{path}:{i}: unknown expected {expected!r}; must be one of {sorted(VALID_EXPECTED)}")
            if status not in VALID_STATUS:
                raise ValueError(f"{path}:{i}: unknown status {status!r}; must be one of {sorted(VALID_STATUS)}")
            rows.append(BenchmarkRow(
                molecule_id=molecule_id, id_type=id_type, id=(r.get("id") or "").strip(),
                expected=expected, reason=(r.get("reason") or "").strip(), status=status,
                reviewed_by=(r.get("reviewed_by") or "").strip(), reviewed_on=(r.get("reviewed_on") or "").strip(),
            ))
    return rows


def approved_only(rows: Iterable[BenchmarkRow]) -> List[BenchmarkRow]:
    """The ONLY rows allowed to gate anything (CI, a pass/fail verdict). A
    "proposed" or "rejected" row is informational only -- see module docstring."""
    return [r for r in rows if r.status == "approved"]


def append_candidate(path: str, row: BenchmarkRow) -> None:
    """Append a model-PROPOSED row to a candidates file. Always writes
    status="proposed" regardless of what the caller passes -- a model must
    never mark its own proposal approved (see module docstring)."""
    row = BenchmarkRow(row.molecule_id, row.id_type, row.id, row.expected, row.reason,
                       "proposed", "", "")
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(BENCHMARK_COLUMNS)
        writer.writerow([row.molecule_id, row.id_type, row.id, row.expected, row.reason,
                         row.status, row.reviewed_by, row.reviewed_on])


# --------------------------------------------------------------------------
# OFFLINE mode: check approved rows against the built corpus
# --------------------------------------------------------------------------

def _index_by_molecule_id(rows: Sequence[Mapping], id_field: str, upper: bool = False) -> Set[Tuple[str, str]]:
    out = set()
    for r in rows:
        mid = str(r.get("molecule_id", "") or "").strip()
        rid = str(r.get(id_field, "") or "").strip()
        if not mid or not rid:
            continue
        out.add((mid, rid.upper() if upper else rid.lower()))
    return out


def build_corpus_index(pubmed_db: str = DEFAULT_PUBMED_DB, trials_db: str = DEFAULT_TRIALS_DB,
                       preprints_db: str = DEFAULT_PREPRINTS_DB) -> Dict[str, Set[Tuple[str, str]]]:
    """(molecule_id, id) sets actually present in the built corpus, one per
    id_type. A benchmark row's presence is ALWAYS checked scoped to its own
    molecule_id (an id retrieved under the wrong molecule is not a pass)."""
    import sqlite3

    index: Dict[str, Set[Tuple[str, str]]] = {"pmid": set(), "nct_id": set(), "doi": set()}
    if os.path.exists(pubmed_db):
        conn = sqlite3.connect(pubmed_db)
        try:
            index["pmid"] = _index_by_molecule_id(load_payload_table(conn, "evidence"), "pmid")
        finally:
            conn.close()
    if os.path.exists(trials_db):
        conn = sqlite3.connect(trials_db)
        try:
            from retarats_pipeline import identity
            index["nct_id"] = _index_by_molecule_id(
                identity.apply_identity_gate(
                    "ctgov", [r for r in load_payload_table(conn, "trials") if not r.get("stale_query")]),
                "nct_id", upper=True)
        finally:
            conn.close()
    if os.path.exists(preprints_db):
        conn = sqlite3.connect(preprints_db)
        try:
            from retarats_pipeline import identity
            index["doi"] = _index_by_molecule_id(
                identity.apply_identity_gate(
                    "preprints", [r for r in load_payload_table(conn, "preprints") if not r.get("stale_query")]),
                "id")
        finally:
            conn.close()
    return index


def manually_held_keys(pubmed_db: str = DEFAULT_PUBMED_DB,
                       exclusions_path: str = DEFAULT_MANUAL_EXCLUSIONS) -> Set[Tuple[str, str]]:
    """(molecule_id, pmid) pairs the curated build holds off the public site via
    config/manual_exclusions.csv. They are still in the corpus, but the site does not show
    them, so for "should this be absent?" purposes they count as absent -- reported
    separately by the CLI so a hold can never silently mask a retrieval-rule failure."""
    import sqlite3

    from retarats_pipeline.manual_exclusions import load_exclusions, plan_holds

    if not exclusions_path or not os.path.exists(exclusions_path) or not os.path.exists(pubmed_db):
        return set()
    excl = load_exclusions(exclusions_path)
    if not excl.rules:
        return set()
    conn = sqlite3.connect(pubmed_db)
    try:
        papers = {str(p.get("pmid", "")): p for p in load_payload_table(conn, "papers")}
        evidence = load_payload_table(conn, "evidence")
    finally:
        conn.close()
    plan_rows = []
    for ev in evidence:
        paper = papers.get(str(ev.get("pmid", "") or ""), {})
        plan_rows.append({"molecule_id": ev.get("molecule_id", ""), "pmid": ev.get("pmid", ""),
                          "title": ev.get("title") or paper.get("title", ""),
                          "abstract": ev.get("abstract") or paper.get("abstract", ""),
                          "keywords": ev.get("keywords") or paper.get("keywords", "")})
    holds, _ = plan_holds(excl, plan_rows)
    return {(str(plan_rows[i]["molecule_id"]), str(plan_rows[i]["pmid"]).lower()) for i in holds}


def identity_held_keys(pubmed_db: str = DEFAULT_PUBMED_DB) -> Set[Tuple[str, str]]:
    """(molecule_id, pmid) pairs the identity gate holds off the public site (config/MOLECULE_IDENTITY.csv):
    still in the corpus, not published for that molecule, so they count as absent here (reported separately).
    Trials and preprints are already filtered in ``build_corpus_index``."""
    import sqlite3

    from retarats_pipeline import identity

    if not os.path.exists(pubmed_db) or not identity.enforced("pubmed"):
        return set()
    import run_identity_reeval as rr

    conn = sqlite3.connect(pubmed_db)
    try:
        cfg = identity.get_config()
        return {(mid, str(rec.get("pmid", "")).lower())
                for _, _, mid, v, rec in rr.evaluate_source("pubmed", conn, cfg) if not v.published}
    finally:
        conn.close()


@dataclass
class RowResult:
    row: BenchmarkRow
    present: bool
    passed: bool


@dataclass
class BenchmarkReport:
    results: List[RowResult] = field(default_factory=list)
    skipped_unapproved: int = 0

    @property
    def ok(self) -> bool:
        """True iff every APPROVED row passed. Unapproved rows never affect this."""
        return all(r.passed for r in self.results)

    @property
    def include_rows(self) -> List[RowResult]:
        return [r for r in self.results if r.row.expected == "include"]

    @property
    def exclude_rows(self) -> List[RowResult]:
        return [r for r in self.results if r.row.expected == "exclude"]

    @property
    def known_positive_recall(self) -> Optional[float]:
        rows = self.include_rows
        return (sum(1 for r in rows if r.passed) / len(rows)) if rows else None

    @property
    def known_negative_correct_rate(self) -> Optional[float]:
        rows = self.exclude_rows
        return (sum(1 for r in rows if r.passed) / len(rows)) if rows else None

    @property
    def unexpected_losses(self) -> List[RowResult]:
        """Approved "include" rows that are NOT currently present -- a retrieval
        regression relative to the human-curated source of truth."""
        return [r for r in self.include_rows if not r.passed]

    @property
    def new_false_positives(self) -> List[RowResult]:
        """Approved "exclude" rows that ARE currently present -- the corpus
        wrongly includes something a human explicitly marked out of scope."""
        return [r for r in self.exclude_rows if not r.passed]

    def render(self) -> str:
        lines = [f"Retrieval benchmark: {len(self.results)} approved row(s) checked "
                 f"({self.skipped_unapproved} unapproved row(s) skipped, never gating)."]
        pr = self.known_positive_recall
        nr = self.known_negative_correct_rate
        lines.append(f"  known-positive recall:        {pr:.1%}" if pr is not None else
                     "  known-positive recall:        n/a (no approved include rows)")
        lines.append(f"  known-negative correct rate:  {nr:.1%}" if nr is not None else
                     "  known-negative correct rate:  n/a (no approved exclude rows)")
        if self.unexpected_losses:
            lines.append(f"  UNEXPECTED LOSSES ({len(self.unexpected_losses)}):")
            for r in self.unexpected_losses:
                lines.append(f"    - {r.row.molecule_id}/{r.row.id_type}/{r.row.id}: {r.row.reason}")
        if self.new_false_positives:
            lines.append(f"  NEW FALSE POSITIVES ({len(self.new_false_positives)}):")
            for r in self.new_false_positives:
                lines.append(f"    - {r.row.molecule_id}/{r.row.id_type}/{r.row.id}: {r.row.reason}")
        lines.append("PASS" if self.ok else "FAIL")
        return "\n".join(lines)


def check_offline(rows: Sequence[BenchmarkRow], index: Dict[str, Set[Tuple[str, str]]]) -> BenchmarkReport:
    all_rows = list(rows)
    approved = approved_only(all_rows)
    results = []
    for row in approved:
        present = row.key in index.get(row.id_type, set())
        expected_present = row.expected == "include"
        results.append(RowResult(row=row, present=present, passed=(present == expected_present)))
    return BenchmarkReport(results=results, skipped_unapproved=len(all_rows) - len(approved))


def duplicate_evidence(pubmed_db: str = DEFAULT_PUBMED_DB) -> Dict[Tuple[str, str], List[str]]:
    """(molecule_id, pmid) pairs matched by MORE THAN ONE rule_id -- not
    necessarily a bug, but worth surfacing (duplicate-behavior report item)."""
    import sqlite3

    if not os.path.exists(pubmed_db):
        return {}
    conn = sqlite3.connect(pubmed_db)
    try:
        rows = load_payload_table(conn, "evidence")
    finally:
        conn.close()
    by_pair: Dict[Tuple[str, str], Set[str]] = {}
    for r in rows:
        mid, pmid, rule_id = r.get("molecule_id", ""), r.get("pmid", ""), r.get("rule_id", "")
        if not mid or not pmid:
            continue
        by_pair.setdefault((mid, pmid), set()).add(rule_id)
    return {k: sorted(v) for k, v in by_pair.items() if len(v) > 1}


def corpus_counts_by_molecule(pubmed_db: str = DEFAULT_PUBMED_DB, trials_db: str = DEFAULT_TRIALS_DB,
                              preprints_db: str = DEFAULT_PREPRINTS_DB) -> Dict[str, Dict[str, int]]:
    """Per-molecule evidence/trial/preprint counts -- a snapshot to diff against
    a previously written baseline for retrieval-count-drift detection."""
    import sqlite3

    counts: Dict[str, Dict[str, int]] = {}

    def _bump(table_rows, key):
        for r in table_rows:
            mid = str(r.get("molecule_id", "") or "").strip()
            if not mid:
                continue
            counts.setdefault(mid, {"evidence": 0, "trials": 0, "preprints": 0})[key] += 1

    if os.path.exists(pubmed_db):
        conn = sqlite3.connect(pubmed_db)
        try:
            _bump(load_payload_table(conn, "evidence"), "evidence")
        finally:
            conn.close()
    if os.path.exists(trials_db):
        conn = sqlite3.connect(trials_db)
        try:
            _bump(load_payload_table(conn, "trials"), "trials")
        finally:
            conn.close()
    if os.path.exists(preprints_db):
        conn = sqlite3.connect(preprints_db)
        try:
            _bump(load_payload_table(conn, "preprints"), "preprints")
        finally:
            conn.close()
    return counts


def diff_counts(old: Dict[str, Dict[str, int]], new: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
    """{molecule_id: {evidence: delta, trials: delta, preprints: delta}} for
    every molecule present in either snapshot."""
    out: Dict[str, Dict[str, int]] = {}
    for mid in sorted(set(old) | set(new)):
        o, n = old.get(mid, {}), new.get(mid, {})
        out[mid] = {k: n.get(k, 0) - o.get(k, 0) for k in ("evidence", "trials", "preprints")}
    return {mid: d for mid, d in out.items() if any(v != 0 for v in d.values())}


# --------------------------------------------------------------------------
# DIFF mode: old vs proposed rules, LIVE, no corpus writes
# --------------------------------------------------------------------------

@dataclass
class RuleDiff:
    label_a: str
    label_b: str
    count_a: int
    count_b: int
    ids_a: Set[str]
    ids_b: Set[str]

    @property
    def gained(self) -> Set[str]:
        """In B (proposed) but not A (old) -- candidate NEW positives to review."""
        return self.ids_b - self.ids_a

    @property
    def lost(self) -> Set[str]:
        """In A (old) but not B (proposed) -- candidate regressions to review."""
        return self.ids_a - self.ids_b

    @property
    def overlap(self) -> Set[str]:
        return self.ids_a & self.ids_b

    def render(self) -> str:
        lines = [
            f"{self.label_a}: {self.count_a} id(s)",
            f"{self.label_b}: {self.count_b} id(s)",
            f"overlap: {len(self.overlap)}",
            f"gained (in B, not A): {len(self.gained)}" + (f" -> {sorted(self.gained)[:20]}" if self.gained else ""),
            f"lost (in A, not B): {len(self.lost)}" + (f" -> {sorted(self.lost)[:20]}" if self.lost else ""),
        ]
        return "\n".join(lines)


def diff_id_sets(label_a: str, ids_a: Iterable[str], label_b: str, ids_b: Iterable[str]) -> RuleDiff:
    a, b = set(ids_a), set(ids_b)
    return RuleDiff(label_a=label_a, label_b=label_b, count_a=len(a), count_b=len(b), ids_a=a, ids_b=b)


def live_pubmed_ids(query: str, datetype: str = "pdat", reldate: Optional[int] = None,
                    mindate: Optional[str] = None, maxdate: Optional[str] = None) -> List[str]:
    """Read-only esearch (no history server writes, nothing saved locally)."""
    from retarats_pipeline.pubmed import PubMedClient

    client = PubMedClient(email=os.getenv("NCBI_EMAIL", "diff-mode@example.org"),
                          api_key=os.getenv("NCBI_API_KEY", ""))
    search = client.esearch(term=query, datetype=datetype, reldate=reldate, mindate=mindate, maxdate=maxdate,
                            retmax=9999, usehistory=False)
    return list(search.ids)


def live_ctgov_ids(query: str, field: str = "term", page_size: int = 100, max_pages: int = 20) -> List[str]:
    """Read-only CT.gov v2 search (ClinicalTrialsClient.search_all already never
    writes anywhere)."""
    from retarats_pipeline.enrichment.clients import ClinicalTrialsClient
    from retarats_pipeline.enrichment.common import APIConfig, CachedHTTPClient

    if field not in ("term", "intr"):
        raise ValueError(f"field must be 'term' or 'intr', got {field!r}")
    config = APIConfig.from_env(api_enabled=True)
    client = ClinicalTrialsClient(CachedHTTPClient(config))
    if field == "term":
        result = client.search_all(query, page_size=page_size, max_pages=max_pages)
    else:
        # query.intr isn't exposed by ClinicalTrialsClient.search_all (which always
        # sends query.term); build the same nextPageToken walk directly for the
        # query.intr comparison arm.
        result = _ctgov_search_all_by_field(client, query, "query.intr", page_size, max_pages)
    return [ClinicalTrialsClient.parse_study(s).get("nct_id", "") for s in result["items"]]


def _ctgov_search_all_by_field(client, query: str, field_param: str, page_size: int, max_pages: int) -> dict:
    """Same pagination loop as ClinicalTrialsClient.search_all, parameterized by
    which CT.gov query field to search (query.term vs query.intr) -- kept local to
    the diff tool since production retrieval only ever uses query.term."""
    from retarats_pipeline.enrichment.clients import CTG_BASE

    result = {"items": [], "pages": 0, "exhausted": False, "partial": False, "ok": True, "error": ""}
    token = None
    for i in range(max(1, max_pages)):
        params = {"format": "json", "pageSize": page_size, field_param: query}
        if token:
            params["pageToken"] = token
        data, _source = client.http.get_json("clinicaltrials_diff", f"{field_param}_{query[:100]}_{token or 'first'}",
                                             CTG_BASE, params=params)
        if not data or data.get("error"):
            result["ok"] = result["pages"] > 0
            result["partial"] = result["pages"] > 0
            result["error"] = (data or {}).get("error", "no_data")
            break
        studies = data.get("studies") or []
        result["items"].extend(studies)
        result["pages"] += 1
        token = data.get("nextPageToken")
        if not token:
            result["exhausted"] = True
            break
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_offline(args: argparse.Namespace) -> int:
    rows = load_benchmark_rows(args.benchmark)
    index = build_corpus_index(args.pubmed_db, args.trials_db, args.preprints_db)
    held = manually_held_keys(args.pubmed_db, args.manual_exclusions) | identity_held_keys(args.pubmed_db)
    in_corpus_but_held = [r for r in approved_only(rows) if r.id_type == "pmid" and r.key in held and r.key in index["pmid"]]
    index["pmid"] = index["pmid"] - held
    report = check_offline(rows, index)
    print(report.render())
    if held:
        print(f"\nHolds: {len(held)} (molecule, pmid) pair(s) are in the corpus but held off the site by "
              f"{args.manual_exclusions} or the identity gate (config/MOLECULE_IDENTITY.csv); treated as absent above.")
        for r in in_corpus_but_held:
            print(f"  approved row {r.molecule_id}/{r.id} ({r.expected}) is currently held -- "
                  + ("OK (expected exclude)" if r.expected == "exclude" else "WARNING: an approved INCLUDE is held"))

    dups = duplicate_evidence(args.pubmed_db)
    if dups:
        print(f"\nDuplicate behavior: {len(dups)} (molecule_id, pmid) pair(s) matched by multiple rule_ids:")
        for (mid, pmid), rule_ids in sorted(dups.items())[:20]:
            print(f"  {mid}/{pmid}: {', '.join(rule_ids)}")

    counts = corpus_counts_by_molecule(args.pubmed_db, args.trials_db, args.preprints_db)
    if args.write_baseline:
        os.makedirs(os.path.dirname(args.write_baseline) or ".", exist_ok=True)
        with open(args.write_baseline, "w", encoding="utf-8") as fh:
            json.dump(counts, fh, indent=2, sort_keys=True)
        print(f"\nWrote per-molecule count baseline -> {args.write_baseline}")
    if args.baseline_counts and os.path.exists(args.baseline_counts):
        with open(args.baseline_counts, encoding="utf-8") as fh:
            old = json.load(fh)
        drift = diff_counts(old, counts)
        if drift:
            print(f"\nRetrieval-count drift vs {args.baseline_counts}:")
            for mid, d in drift.items():
                print(f"  {mid}: {d}")
        else:
            print(f"\nNo retrieval-count drift vs {args.baseline_counts}.")

    return 0 if report.ok else 1


def _cmd_propose(args: argparse.Namespace) -> int:
    row = BenchmarkRow(args.molecule, args.id_type, args.id, args.expected, args.reason,
                       "proposed", "", "")
    append_candidate(args.candidates, row)
    print(f"Proposed (status=proposed, NOT approved): {row.molecule_id}/{row.id_type}/{row.id} -> {row.expected}")
    print(f"A human must review and move this into {args.approved!r} with status=approved before it can gate anything.")
    return 0


def _cmd_diff_pubmed(args: argparse.Namespace) -> int:
    ids_a = live_pubmed_ids(args.query_a, datetype=args.datetype_a, reldate=args.reldate,
                            mindate=args.mindate, maxdate=args.maxdate)
    ids_b = live_pubmed_ids(args.query_b, datetype=args.datetype_b, reldate=args.reldate,
                            mindate=args.mindate, maxdate=args.maxdate)
    diff = diff_id_sets(f"A ({args.datetype_a}): {args.query_a!r}", ids_a,
                        f"B ({args.datetype_b}): {args.query_b!r}", ids_b)
    print(f"PubMed rule diff for molecule={args.molecule!r} (live, read-only, nothing written)\n")
    print(diff.render())
    return 0


def _cmd_diff_ctgov(args: argparse.Namespace) -> int:
    ids_a = live_ctgov_ids(args.query, field=args.field_a, page_size=args.page_size, max_pages=args.max_pages)
    ids_b = live_ctgov_ids(args.query, field=args.field_b, page_size=args.page_size, max_pages=args.max_pages)
    diff = diff_id_sets(f"A (query.{args.field_a}): {args.query!r}", ids_a,
                        f"B (query.{args.field_b}): {args.query!r}", ids_b)
    print(f"CT.gov rule diff for molecule={args.molecule!r} (live, read-only, nothing written)\n")
    print(diff.render())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("offline", help="Check approved benchmark rows against the built corpus.")
    p.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    p.add_argument("--pubmed-db", default=DEFAULT_PUBMED_DB)
    p.add_argument("--trials-db", default=DEFAULT_TRIALS_DB)
    p.add_argument("--preprints-db", default=DEFAULT_PREPRINTS_DB)
    p.add_argument("--write-baseline", default="", help="Write a per-molecule count snapshot here.")
    p.add_argument("--baseline-counts", default="", help="Diff current counts against a prior snapshot.")
    p.add_argument("--manual-exclusions", default=DEFAULT_MANUAL_EXCLUSIONS,
                   help="Records held by this file count as absent ('' disables).")
    p.set_defaults(func=_cmd_offline)

    p = sub.add_parser("propose", help="Append a model-PROPOSED row to a candidates file (never approved).")
    p.add_argument("--molecule", required=True, dest="molecule")
    p.add_argument("--id-type", required=True, choices=sorted(VALID_ID_TYPES))
    p.add_argument("--id", required=True)
    p.add_argument("--expected", required=True, choices=sorted(VALID_EXPECTED))
    p.add_argument("--reason", required=True)
    p.add_argument("--candidates", default="config/retrieval_benchmark_candidates.csv")
    p.add_argument("--approved", default=DEFAULT_BENCHMARK)
    p.set_defaults(func=_cmd_propose)

    p = sub.add_parser("diff-pubmed", help="Live PubMed old-vs-proposed query diff (no writes).")
    p.add_argument("--molecule", required=True)
    p.add_argument("--query-a", required=True)
    p.add_argument("--query-b", required=True)
    p.add_argument("--datetype-a", default="pdat")
    p.add_argument("--datetype-b", default="pdat")
    p.add_argument("--reldate", type=int, default=None)
    p.add_argument("--mindate", default=None)
    p.add_argument("--maxdate", default=None)
    p.set_defaults(func=_cmd_diff_pubmed)

    p = sub.add_parser("diff-ctgov", help="Live CT.gov query.term-vs-query.intr diff (no writes).")
    p.add_argument("--molecule", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--field-a", default="term", choices=["term", "intr"])
    p.add_argument("--field-b", default="intr", choices=["term", "intr"])
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=20)
    p.set_defaults(func=_cmd_diff_ctgov)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
