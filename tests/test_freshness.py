#!/usr/bin/env python3
"""Tests for scripts/check_corpus_freshness.py (guard + freshness logic).

Plain script (no pytest): run `python3 tests/test_freshness.py`.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import check_corpus_freshness as f  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {name}")


UTC = timezone.utc


def _mk_corpus(n_rows):
    """Write a temp sqlite with a papers table holding n_rows, return its path."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("create table papers (pmid text, payload_json text)")
    conn.executemany("insert into papers values (?, ?)",
                     [(str(i), "{}") for i in range(n_rows)])
    conn.commit()
    conn.close()
    return path


def test_corpus_count():
    p = _mk_corpus(37)
    try:
        check("corpus_count reads row count", f.corpus_count(p) == 37)
    finally:
        os.remove(p)
    check("corpus_count None for missing file", f.corpus_count("/no/such.sqlite") is None)
    # a DB without a papers table -> None (unknown), not a crash
    fd, empty = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    sqlite3.connect(empty).close()
    try:
        check("corpus_count None when papers table absent", f.corpus_count(empty) is None)
    finally:
        os.remove(empty)


def test_guard():
    # No prior -> pass (bootstrapping)
    ok, _, _ = f.evaluate_guard(current=100, prev=None, min_ratio=0.9)
    check("guard passes with no prior count", ok is True)
    # Healthy: current above floor
    ok, floor, _ = f.evaluate_guard(current=1000, prev=1000, min_ratio=0.9)
    check("guard passes when count steady", ok is True and floor == 900)
    # Grew -> pass
    ok, _, _ = f.evaluate_guard(current=1200, prev=1000, min_ratio=0.9)
    check("guard passes when corpus grew", ok is True)
    # Collapsed cache -> FAIL
    ok, floor, _ = f.evaluate_guard(current=200, prev=1000, min_ratio=0.9)
    check("guard FAILS on collapsed/lost cache", ok is False and floor == 900)
    # Exactly at floor -> pass (>=)
    ok, _, _ = f.evaluate_guard(current=900, prev=1000, min_ratio=0.9)
    check("guard passes exactly at floor", ok is True)
    # Unknown current (no DB) -> pass (don't fail a bootstrap)
    ok, _, _ = f.evaluate_guard(current=None, prev=1000, min_ratio=0.9)
    check("guard passes when current unknown", ok is True)


def test_freshness_growth_resets_clock():
    now = datetime(2026, 8, 15, tzinfo=UTC)
    state = {"papers_count": 1000,
             "last_growth_utc": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    new_state, is_stale, days, _ = f.evaluate_freshness(1100, state, now, max_stale_days=14)
    check("growth is not stale", is_stale is False)
    check("growth records new count", new_state["papers_count"] == 1100)
    check("growth resets last_growth_utc to now",
          new_state["last_growth_utc"].startswith("2026-08-15"))


def test_freshness_stale_after_threshold():
    now = datetime(2026, 8, 15, tzinfo=UTC)
    state = {"papers_count": 1000,
             "last_growth_utc": (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    new_state, is_stale, days, _ = f.evaluate_freshness(1000, state, now, max_stale_days=14)
    check("no growth for 20d over 14d threshold is stale", is_stale is True and days == 20)
    check("stale run still refreshes updated_utc", new_state["updated_utc"].startswith("2026-08-15"))
    check("stale run keeps last_growth_utc pinned to the real last growth",
          new_state["last_growth_utc"].startswith((now - timedelta(days=20)).strftime("%Y-%m-%d")))


def test_freshness_within_threshold_not_stale():
    now = datetime(2026, 8, 15, tzinfo=UTC)
    state = {"papers_count": 1000,
             "last_growth_utc": (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    _, is_stale, days, _ = f.evaluate_freshness(1000, state, now, max_stale_days=14)
    check("no growth for 5d under 14d threshold is not stale", is_stale is False and days == 5)


def test_freshness_bootstrap_starts_clock():
    now = datetime(2026, 8, 15, tzinfo=UTC)
    new_state, is_stale, _, _ = f.evaluate_freshness(500, {}, now, max_stale_days=14)
    check("bootstrap is not stale", is_stale is False)
    check("bootstrap starts clock and records count",
          new_state["papers_count"] == 500 and new_state["last_growth_utc"].startswith("2026-08-15"))


def main():
    test_corpus_count()
    test_guard()
    test_freshness_growth_resets_clock()
    test_freshness_stale_after_threshold()
    test_freshness_within_threshold_not_stale()
    test_freshness_bootstrap_starts_clock()
    print(f"{_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
