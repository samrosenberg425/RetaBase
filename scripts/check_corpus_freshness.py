#!/usr/bin/env python3
"""Corpus freshness + cache-integrity guard for the daily update pipeline.

Two responsibilities, selected by ``--mode``:

* ``guard`` (run right AFTER the cache restore, BEFORE the fetch): assert the
  restored SQLite corpus still holds roughly as many papers as last run recorded.
  A lost or truncated Actions cache would otherwise let the pipeline "succeed"
  from an almost-empty corpus and republish a hollowed-out site. If the restored
  count has collapsed below ``--min-ratio`` of the last recorded count this exits
  NON-ZERO so the build job fails and deploy (needs: build) is skipped — the last
  good site stays up. First run / no state / no baseline -> passes (bootstrapping).

* ``freshness`` (run AFTER a good rebuild): compare the current corpus size to the
  last recorded size. Growth updates the "last grew" timestamp. If the corpus has
  NOT grown for ``--max-stale-days`` days it emits a GitHub ``::warning::`` and a
  job-summary line so a silently-dead fetch (expired API key, upstream change)
  surfaces instead of failing quietly. This mode is a SOFT alert: it always exits
  zero and always persists updated state.

State lives in a small JSON file kept in the same ``data/`` dir as the corpus (so
it rides along in the Actions cache):

    {"papers_count": N, "total_papers_stat": M,
     "last_growth_utc": "2026-08-15T06:00:00Z", "updated_utc": "..."}

``papers_count`` (raw ``papers`` rows) is what both modes compare, so the numbers
are always apples-to-apples across runs. Pure stdlib; no network.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional, Tuple


# --- pure helpers (unit-tested in tests/test_freshness.py) -------------------

def corpus_count(db_path: str) -> Optional[int]:
    """Raw row count of the ``papers`` table, or None if unavailable.

    None (not 0) when the DB or table is missing, so a missing file is treated as
    "unknown / bootstrapping" rather than "collapsed to zero" (which would nuke a
    guard). A genuinely empty table returns 0.
    """
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("select count(*) from papers").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


def read_stat(stats_path: Optional[str], key: str) -> Optional[int]:
    """Read an integer metric from a corpus_stats.json, or None."""
    if not stats_path or not os.path.exists(stats_path):
        return None
    try:
        with open(stats_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    val = data.get(key)
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def load_state(state_path: Optional[str]) -> dict:
    if not state_path or not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _parse_utc(text) -> Optional[datetime]:
    if not text or not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate_guard(current: Optional[int], prev: Optional[int],
                   min_ratio: float) -> Tuple[bool, Optional[int], str]:
    """Guard verdict: (ok, floor, detail).

    Passes (ok=True) when there is nothing to compare against — no previous count
    recorded, or the current count is unknown (missing DB on a bootstrap run).
    Fails only when we HAVE a prior count and the current count has dropped below
    ``min_ratio * prev``.
    """
    if prev is None or prev <= 0:
        return True, None, f"no prior papers_count to compare (prev={prev!r}); bootstrapping"
    if current is None:
        return True, None, "current corpus count unknown (no DB yet); skipping guard"
    floor = int(min_ratio * prev)
    ok = current >= floor
    detail = f"restored papers={current} vs prev={prev} (floor={floor}, ratio={min_ratio:g})"
    return ok, floor, detail


def evaluate_freshness(current: Optional[int], state: dict, now: datetime,
                       max_stale_days: int) -> Tuple[dict, bool, Optional[int], str]:
    """Freshness verdict: (new_state, is_stale, days_stale, detail).

    Growth (current > last recorded, or no prior record) resets the staleness
    clock. Otherwise staleness is measured from the last growth timestamp.
    ``new_state`` always reflects ``current`` and a refreshed ``updated_utc``.
    """
    now_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = state.get("papers_count")
    prev = int(prev) if isinstance(prev, (int, float)) else None
    last_growth = _parse_utc(state.get("last_growth_utc"))

    new_state = dict(state)
    new_state["updated_utc"] = now_iso
    if current is not None:
        new_state["papers_count"] = int(current)

    grew = current is not None and (prev is None or current > prev)
    if grew or last_growth is None:
        new_state["last_growth_utc"] = now_iso
        detail = (f"corpus grew ({prev} -> {current})" if (grew and prev is not None)
                  else f"freshness clock started (count={current})")
        return new_state, False, 0, detail

    days_stale = (now - last_growth).days
    is_stale = days_stale >= max_stale_days
    detail = (f"no growth for {days_stale}d (count steady at {current}, "
              f"threshold {max_stale_days}d)")
    return new_state, is_stale, days_stale, detail


def write_state(state_path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(state_path)) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def _emit_summary(text: str) -> None:
    """Append a line to the GitHub Actions job summary, if running in CI."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


# --- CLI ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("guard", "freshness"), required=True)
    ap.add_argument("--db", default="data/retarats_pubmed.sqlite",
                    help="SQLite corpus (papers table) to count")
    ap.add_argument("--state", default="data/freshness_state.json")
    ap.add_argument("--stats", default=None,
                    help="optional corpus_stats.json to also record total_papers")
    ap.add_argument("--min-ratio", type=float, default=0.9,
                    help="guard: restored count must be >= ratio * last recorded")
    ap.add_argument("--max-stale-days", type=int, default=14,
                    help="freshness: warn if corpus hasn't grown in this many days")
    args = ap.parse_args()

    current = corpus_count(args.db)
    state = load_state(args.state)

    if args.mode == "guard":
        prev = state.get("papers_count")
        prev = int(prev) if isinstance(prev, (int, float)) else None
        ok, floor, detail = evaluate_guard(current, prev, args.min_ratio)
        verdict = "PASS" if ok else "FAIL"
        print(f"[freshness guard] {verdict}: {detail}")
        if not ok:
            msg = ("::error::Restored corpus cache looks truncated/lost "
                   f"({detail}). Refusing to rebuild from a shrunken corpus; "
                   "the last good site stays published.")
            print(msg)
            _emit_summary(f"❌ **Corpus guard FAILED** — {detail}")
            return 1
        return 0

    # freshness
    now = datetime.now(timezone.utc)
    new_state, is_stale, days_stale, detail = evaluate_freshness(
        current, state, now, args.max_stale_days)
    if args.stats:
        tp = read_stat(args.stats, "total_papers")
        if tp is not None:
            new_state["total_papers_stat"] = tp
    write_state(args.state, new_state)
    print(f"[freshness] {detail}")
    if is_stale:
        warn = (f"::warning::Corpus has not grown in {days_stale} days "
                f"(>= {args.max_stale_days}). The daily fetch may be silently "
                "failing — check the NCBI API key/quota and the fetch step logs.")
        print(warn)
        _emit_summary(f"⚠️ **Corpus freshness** — no new papers in {days_stale} days "
                      f"(threshold {args.max_stale_days}d).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
