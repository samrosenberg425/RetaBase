#!/usr/bin/env python3
"""Re-evaluate the molecule identity of every ALREADY-STORED record. Offline, deterministic, reversible.

    python3 scripts/run_identity_reeval.py --work work [--sources ctgov,preprints,pubmed] [--summary-out FILE]

Why this exists: stale-marking (registry_stale) only runs for a molecule whose live retrieval COMPLETED, so a
partial/failed/cached retrieval left known junk published forever. Retrieval completeness and molecule
identity are different questions; this answers the second from the stored text alone -- no network, no API
page limits, nothing to wait for.

What it does (per source: trials -> ``ctgov``, preprints -> ``preprints``, PubMed evidence -> ``pubmed``):

* judges every stored record against the molecule it is filed under with ``retarats_pipeline.identity``
  (canonical name / specific alias / contextual alias + context / manual keep -> pass; otherwise hold/exclude);
* (re)writes the provenance table ``record_identity`` in that source's SQLite DB: molecule, outcome, match type,
  matched term, zone, reason, rules version, timestamp. The table is rebuilt in full each run, so a changed alias
  or rule is picked up simply by running again;
* NEVER touches the stored records themselves (no delete, no payload rewrite, no change to source metadata).

The feed builders apply the same function in memory (``identity.apply_identity_gate`` / the curated build's
identity gate), so what is published always equals what this table says. Fail-open: a source whose DB is
missing/unreadable is skipped with a warning and nothing is held.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline import identity as idn  # noqa: E402
from retarats_pipeline.enrichment.common import utc_now_iso  # noqa: E402

DBS = {"ctgov": ("retarats_trials.sqlite", "trials", "nct_id"),
       "preprints": ("retarats_preprints.sqlite", "preprints", "id"),
       "pubmed": ("retarats_pubmed.sqlite", "evidence", "evidence_id")}


def _iter_payloads(conn: sqlite3.Connection, table: str) -> Iterator[dict]:
    for (payload,) in conn.execute(f"select payload_json from {table}"):
        try:
            yield json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue


def _paper_text(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    """pmid -> only the stored text fields identity needs (kept small: ~150k papers)."""
    out: Dict[str, Dict[str, str]] = {}
    for p in _iter_payloads(conn, "papers"):
        pmid = str(p.get("pmid", "") or "")
        if pmid:
            out[pmid] = {k: idn._flat(p.get(k)) for k in idn.SOURCE_ZONES["pubmed"]}
    return out


def evaluate_source(source: str, conn: sqlite3.Connection, cfg: idn.IdentityConfig
                    ) -> Iterator[Tuple[str, str, str, idn.Verdict, dict]]:
    """Yield (source, record_key, molecule_id, verdict, record_summary) for every stored record."""
    if source == "ctgov":
        for r in _iter_payloads(conn, "trials"):
            key = str(r.get("nct_id", "") or "").upper()
            mid = str(r.get("molecule_id", "") or "")
            yield source, key, mid, idn.evaluate(cfg, source, mid, idn.zones_for_trial(r), key), r
    elif source == "preprints":
        for r in _iter_payloads(conn, "preprints"):
            key = str(r.get("id", "") or "")
            mid = str(r.get("molecule_id", "") or "")
            yield source, key, mid, idn.evaluate(cfg, source, mid, idn.zones_for_preprint(r), key), r
    else:
        papers = _paper_text(conn)
        for ev in _iter_payloads(conn, "evidence"):
            pmid = str(ev.get("pmid", "") or "")
            mid = str(ev.get("molecule_id", "") or "")
            z = {k: idn._flat(ev.get(k)) or papers.get(pmid, {}).get(k, "") for k in idn.SOURCE_ZONES["pubmed"]}
            yield source, str(ev.get("evidence_id", "") or f"{pmid}:{mid}"), mid, idn.evaluate(cfg, source, mid, z, pmid), ev


def run(work: str, sources: Iterable[str] = ("ctgov", "preprints", "pubmed"), write: bool = True,
        persist_pass: bool = True) -> dict:
    cfg = idn.get_config()
    now = utc_now_iso()
    summary: dict = {"rules_version": cfg.version, "evaluated_utc": now, "warnings": list(cfg.warnings), "sources": {}}
    for source in sources:
        fname, table, _ = DBS[source]
        path = os.path.join(work, fname)
        if not os.path.exists(path):
            print(f"[{source}] {path} missing -- skipped (nothing held)")
            continue
        conn = sqlite3.connect(path)
        try:
            outcomes: Counter = Counter()
            by_mol: Dict[str, Counter] = defaultdict(Counter)
            rows: List[Tuple[str, str, str, idn.Verdict]] = []
            for src, key, mid, v, _rec in evaluate_source(source, conn, cfg):
                outcomes[v.outcome] += 1
                by_mol[mid][v.outcome] += 1
                if persist_pass or not v.published:
                    rows.append((src, key, mid, v))
            if write:
                n = idn.write_verdicts(conn, rows, cfg.version, now, source)
            else:
                n = 0
        finally:
            conn.close()
        total = sum(outcomes.values())
        zero = sorted(m for m, c in by_mol.items() if c[idn.PASS] == 0 and sum(c.values()) >= 5)
        top = sorted(((m, c[idn.HOLD] + c[idn.EXCLUDE], sum(c.values())) for m, c in by_mol.items()),
                     key=lambda t: -t[1])[:10]
        summary["sources"][source] = {"stored": total, "outcomes": dict(outcomes), "rows_written": n,
                                      "molecules_with_no_passing_record": zero,
                                      "top_held": [{"molecule_id": m, "held": h, "stored": t} for m, h, t in top]}
        print(f"[{source}] {total} stored record(s): " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
              + (f"; wrote {n} provenance row(s) to {fname}:{idn.TABLE}" if write else " (dry run)"))
        for m, h, t in top[:5]:
            print(f"    most held: {m} {h}/{t}")
        if zero:
            print(f"    molecules with NO passing record (>=5 stored): {', '.join(zero)}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="work", help="staged corpus dir holding the *.sqlite files")
    ap.add_argument("--sources", default="ctgov,preprints,pubmed")
    ap.add_argument("--dry-run", action="store_true", help="evaluate and report, write no provenance table")
    ap.add_argument("--summary-out", default="", help="write the summary as JSON here")
    args = ap.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    bad = [s for s in sources if s not in DBS]
    if bad:
        ap.error(f"unknown source(s): {bad}")
    summary = run(args.work, sources, write=not args.dry_run)
    if args.summary_out:
        os.makedirs(os.path.dirname(args.summary_out) or ".", exist_ok=True)
        with open(args.summary_out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
