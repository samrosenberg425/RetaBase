#!/usr/bin/env python3
"""Offline unit tests for WS2: HTTP retry/backoff + search-cache TTL (common.py),
CT.gov/EuropePMC pagination (clients.py), preprint version/withdrawal fields
(registry.py), and safe upsert + failure semantics in the registry fetch scripts.

No network: a fake ``requests`` module is swapped in for CachedHTTPClient, and the
fetch scripts are driven against temp SQLite DBs with a monkeypatched client.

Run:

    python3 tests/test_ws2_retrieval.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from retarats_pipeline.enrichment import common as common_mod  # noqa: E402
from retarats_pipeline.enrichment.common import APIConfig, CachedHTTPClient  # noqa: E402
from retarats_pipeline.enrichment.clients import ClinicalTrialsClient, IdentifierMetadataClient  # noqa: E402
from retarats_pipeline.enrichment.registry import normalize_preprint  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


# --- fake requests -------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeRequests:
    """Drop-in for the ``requests`` module: a queue of canned responses/exceptions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if not self.responses:
            raise AssertionError("fake requests queue exhausted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _NoSleep:
    """Context manager: no-op time.sleep in common.py so retry/backoff tests are fast."""

    def __enter__(self):
        self._orig = common_mod.time.sleep
        common_mod.time.sleep = lambda _s: None
        return self

    def __exit__(self, *exc):
        common_mod.time.sleep = self._orig


def _client(responses, **cfg_kwargs):
    tmpdir = tempfile.mkdtemp(prefix="ws2_cache_")
    cfg = APIConfig(cache_dir=tmpdir, api_enabled=True, min_interval_sec=0, max_retries=3,
                     retry_base_delay_sec=0.001, retry_max_delay_sec=0.01, **cfg_kwargs)
    http = CachedHTTPClient(cfg)
    fake = _FakeRequests(responses)
    http_orig = common_mod.requests
    common_mod.requests = fake
    return http, fake, http_orig


def run():
    # =========================================================================
    # common.py: retry/backoff + search-cache TTL/bypass + stale-cache fallback
    # =========================================================================
    with _NoSleep():
        # --- 429 retry: two 429s then a 200 succeeds ---
        http, fake, orig = _client([_FakeResp(429), _FakeResp(429), _FakeResp(200, {"ok": True})])
        try:
            data, source = http.get_json("ns_429", "k1", "https://x/1")
            check("429 retry: eventually succeeds", data == {"ok": True} and source == "api")
            check("429 retry: took 3 attempts", len(fake.calls) == 3)
        finally:
            common_mod.requests = orig

        # --- 5xx retry: 503, 502, then 200 ---
        http, fake, orig = _client([_FakeResp(503), _FakeResp(502), _FakeResp(200, {"ok": True})])
        try:
            data, source = http.get_json("ns_5xx", "k1", "https://x/2")
            check("5xx retry: eventually succeeds", data == {"ok": True} and source == "api")
            check("5xx retry: took 3 attempts", len(fake.calls) == 3)
        finally:
            common_mod.requests = orig

        # --- retry exhaustion: persistent 503, no cache to fall back on ---
        http, fake, orig = _client([_FakeResp(503)] * 10)  # more than enough to exhaust
        try:
            data, source = http.get_json("ns_exhaust", "k1", "https://x/3")
            check("retry exhaustion: tag is retry_exhausted", source == "retry_exhausted")
            check("retry exhaustion: attempts capped at max_retries+1",
                  len(fake.calls) == http.config.max_retries + 1)
            check("retry exhaustion: error surfaced, not swallowed", isinstance(data, dict) and data.get("error"))
        finally:
            common_mod.requests = orig

        # --- non-retryable 4xx: fails immediately, no retries burned ---
        http, fake, orig = _client([_FakeResp(404)])
        try:
            data, source = http.get_json("ns_404", "k1", "https://x/4")
            check("404 is non-retryable", source == "http_error" and len(fake.calls) == 1)
        finally:
            common_mod.requests = orig

        # --- stale cache fallback: a live refresh fails but a cached response exists ---
        http, fake, orig = _client([_FakeResp(200, {"v": 1}), _FakeResp(503)] * 1)
        try:
            data1, source1 = http.get_json("ns_stale", "k1", "https://x/5", ttl_sec=0)
            check("stale-fallback: first live fetch is api", data1 == {"v": 1} and source1 == "api")
            # ttl_sec=0 means the just-written cache is immediately "stale"; the retry
            # queue only has one more response (a 503), so refresh fails but the
            # previously cached {"v": 1} should be served instead of nothing.
            common_mod.requests = _FakeRequests([_FakeResp(503)] * 10)
            data2, source2 = http.get_json("ns_stale", "k1", "https://x/5", ttl_sec=0)
            check("stale-fallback: serves old cache on failed refresh",
                  data2 == {"v": 1} and source2 == "stale_cache")
        finally:
            common_mod.requests = orig

        # --- search TTL: cache honored within TTL, bypassed once stale ---
        http, fake, orig = _client([_FakeResp(200, {"n": 1}), _FakeResp(200, {"n": 2})])
        try:
            d1, s1 = http.get_json("ns_ttl", "k1", "https://x/6", ttl_sec=3600)
            check("ttl: first call live", d1 == {"n": 1} and s1 == "api")
            d2, s2 = http.get_json("ns_ttl", "k1", "https://x/6", ttl_sec=3600)
            check("ttl: second call within TTL hits cache (no 2nd network call)",
                  d2 == {"n": 1} and s2 == "cache" and len(fake.calls) == 1)
            # Force the cache entry to look old by rewinding its mtime past the TTL.
            cache_path = http._cache_path("ns_ttl", "k1")
            old = os.path.getmtime(cache_path) - 7200
            os.utime(cache_path, (old, old))
            d3, s3 = http.get_json("ns_ttl", "k1", "https://x/6", ttl_sec=3600)
            check("ttl: stale entry triggers a fresh live fetch", d3 == {"n": 2} and s3 == "api")
        finally:
            common_mod.requests = orig

        # --- force_refresh bypasses a perfectly fresh cache-forever entry ---
        http, fake, orig = _client([_FakeResp(200, {"n": 1}), _FakeResp(200, {"n": 2})])
        try:
            http.get_json("ns_force", "k1", "https://x/7")  # ttl_sec=None: cache forever
            d, s = http.get_json("ns_force", "k1", "https://x/7", force_refresh=True)
            check("force_refresh bypasses cache-forever entry", d == {"n": 2} and s == "api")
        finally:
            common_mod.requests = orig

    # =========================================================================
    # clients.py: CT.gov nextPageToken + EuropePMC cursorMark pagination
    # =========================================================================
    with _NoSleep():
        # --- CT.gov multi-page retrieval, clean exhaustion ---
        pages = [
            _FakeResp(200, {"studies": [{"id": 1}, {"id": 2}], "totalCount": 5, "nextPageToken": "p2"}),
            _FakeResp(200, {"studies": [{"id": 3}, {"id": 4}], "nextPageToken": "p3"}),
            _FakeResp(200, {"studies": [{"id": 5}]}),  # no nextPageToken -> exhausted
        ]
        http, fake, orig = _client(pages)
        try:
            client = ClinicalTrialsClient(http)
            result = client.search_all("retatrutide", page_size=2, max_pages=10)
            check("ctgov pagination: 3 pages fetched", result["pages"] == 3)
            check("ctgov pagination: all rows retrieved", result["rows_retrieved"] == 5)
            check("ctgov pagination: total_reported from first page", result["total_reported"] == 5)
            check("ctgov pagination: exhausted, not partial", result["exhausted"] and not result["partial"])
            check("ctgov pagination: ok", result["ok"])
            check("ctgov pagination: items accumulated in order",
                  [s["id"] for s in result["items"]] == [1, 2, 3, 4, 5])
            # countTotal should only be requested on the first page.
            check("ctgov pagination: countTotal only on page 1",
                  fake.calls[0]["params"].get("countTotal") == "true"
                  and "countTotal" not in fake.calls[1]["params"])
        finally:
            common_mod.requests = orig

        # --- CT.gov: partial retrieval when an error interrupts pagination ---
        pages = [
            _FakeResp(200, {"studies": [{"id": 1}], "nextPageToken": "p2"}),
            _FakeResp(404),  # non-retryable, aborts the walk
        ]
        http, fake, orig = _client(pages)
        try:
            client = ClinicalTrialsClient(http)
            result = client.search_all("x", page_size=1, max_pages=10)
            check("ctgov partial: ok True (got page 1)", result["ok"] is True)
            check("ctgov partial: partial True", result["partial"] is True)
            check("ctgov partial: not exhausted", result["exhausted"] is False)
            check("ctgov partial: only page-1 rows retrieved", result["rows_retrieved"] == 1)
        finally:
            common_mod.requests = orig

        # --- CT.gov: total failure on page 1 -> ok False, not a fake empty success ---
        http, fake, orig = _client([_FakeResp(404)])
        try:
            client = ClinicalTrialsClient(http)
            result = client.search_all("x", page_size=1, max_pages=10)
            check("ctgov total failure: ok is False", result["ok"] is False)
            check("ctgov total failure: error surfaced", bool(result["error"]))
        finally:
            common_mod.requests = orig

        # --- EuropePMC multi-page retrieval, clean exhaustion ---
        pages = [
            _FakeResp(200, {"hitCount": 3, "nextCursorMark": "C2",
                             "resultList": {"result": [{"id": "PPR1"}, {"id": "PPR2"}]}}),
            # EuropePMC signals "last page" by returning the SAME cursorMark back.
            _FakeResp(200, {"hitCount": 3, "nextCursorMark": "C2",
                             "resultList": {"result": [{"id": "PPR3"}]}}),
        ]
        http, fake, orig = _client(pages)
        try:
            client = IdentifierMetadataClient(http, APIConfig(cache_dir=http.config.cache_dir))
            result = client.europepmc_search_all("retatrutide AND SRC:PPR", page_size=2, max_pages=10)
            check("europepmc pagination: 2 pages fetched", result["pages"] == 2)
            check("europepmc pagination: all rows retrieved", result["rows_retrieved"] == 3)
            check("europepmc pagination: total_reported from hitCount", result["total_reported"] == 3)
            # cursor exhaustion: nextCursorMark == request cursorMark on the 2nd page.
            check("europepmc cursor exhaustion detected", result["exhausted"] is True)
            check("europepmc pagination: ok, not partial", result["ok"] and not result["partial"])
        finally:
            common_mod.requests = orig

    # =========================================================================
    # registry.py: preprint version / withdrawal normalization
    # =========================================================================
    base = {
        "id": "PPR100", "source": "PPR", "doi": "10.1101/foo.v2",
        "title": "Some preprint", "authorString": "A B.",
        "firstPublicationDate": "2026-01-01",
        "versionNumber": 2,
        "pubTypeList": {"pubType": ["preprint"]},
    }
    pp = normalize_preprint(base)
    check("preprint version_number captured", pp["version_number"] == 2)
    check("preprint not withdrawn", pp["withdrawn"] is False)
    check("preprint pub_types captured", pp["pub_types"] == "preprint")

    withdrawn = dict(base, versionNumber=3, pubTypeList={"pubType": ["preprint", "preprint-withdrawal"]})
    ppw = normalize_preprint(withdrawn)
    check("preprint version bump reflected", ppw["version_number"] == 3)
    check("preprint withdrawal detected", ppw["withdrawn"] is True)
    check("preprint pub_types include withdrawal", "withdrawal" in ppw["pub_types"].lower())

    no_version = normalize_preprint({"id": "PPR200", "title": "No version info"})
    check("preprint blank-safe version_number", no_version["version_number"] == "")
    check("preprint blank-safe withdrawn", no_version["withdrawn"] is False)

    # =========================================================================
    # run_trials_fetch.py / run_preprints_fetch.py: upsert, freshness fields,
    # failure preservation, partial status, and "no fake empty success"
    # =========================================================================
    import run_trials_fetch  # noqa: E402
    import run_preprints_fetch  # noqa: E402

    def _molecules_csv(rows):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as fh:
            fh.write("molecule_id,display_name,synonyms_csv,active\n")
            for r in rows:
                fh.write(f"{r},{r.title()},,true\n")
        return path

    def _ctgov_study(nct, status):
        return {
            "protocolSection": {
                "identificationModule": {"nctId": nct, "briefTitle": f"Retatrutide trial {nct}"},
                "statusModule": {"overallStatus": status},
            }
        }

    def _patched_search_all(plan):
        """Context manager: replace ClinicalTrialsClient.search_all on the real
        class (parse_study etc. stay real/offline) with a canned-results-by-call
        sequence, no network involved."""
        state = {"n": 0}

        def fake(self, query, page_size=100, max_pages=20):
            i = state["n"]
            state["n"] += 1
            return plan[i] if i < len(plan) else plan[-1]

        class _Patch:
            def __enter__(self_):
                self_.orig = ClinicalTrialsClient.search_all
                ClinicalTrialsClient.search_all = fake
                return self_

            def __exit__(self_, *exc):
                ClinicalTrialsClient.search_all = self_.orig

        return _Patch()

    def _patched_epmc_search_all(plan):
        state = {"n": 0}

        def fake(self, query, page_size=100, result_type="core", max_pages=20):
            i = state["n"]
            state["n"] += 1
            return plan[i] if i < len(plan) else plan[-1]

        class _Patch:
            def __enter__(self_):
                self_.orig = IdentifierMetadataClient.europepmc_search_all
                IdentifierMetadataClient.europepmc_search_all = fake
                return self_

            def __exit__(self_, *exc):
                IdentifierMetadataClient.europepmc_search_all = self_.orig

        return _Patch()

    def _ok_page(studies, **overrides):
        r = {"items": studies, "pages": 1, "total_reported": len(studies), "rows_retrieved": len(studies),
             "exhausted": True, "partial": False, "ok": True, "error": "", "source": "api"}
        r.update(overrides)
        return r

    def _failed_page():
        return {"items": [], "pages": 0, "total_reported": None, "rows_retrieved": 0,
                "exhausted": False, "partial": False, "ok": False, "error": "boom", "source": "http_error"}

    # --- upsert of an existing trial: status change is captured, first_seen_utc kept ---
    db_fd, db_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(db_fd)
    os.remove(db_path)  # run_trials_fetch creates it fresh
    mol_csv = _molecules_csv(["retatrutide"])
    conn = sqlite3.connect(db_path)
    conn.execute("create table trials (nct_id text primary key, payload_json text, updated_at_utc text)")
    conn.execute(
        "insert into trials values (?, ?, ?)",
        ("NCT00000001", json.dumps({"nct_id": "NCT00000001", "overall_status": "RECRUITING",
                                     "first_seen_utc": "2020-01-01T00:00:00Z"}), "2020-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    with _patched_search_all([_ok_page([_ctgov_study("NCT00000001", "COMPLETED")])]):
        result = run_trials_fetch.run(db_path=db_path, molecules_csv=mol_csv)
    check("trials upsert: 0 new, 1 updated", result["stored_new"] == 0 and result["stored_updated"] == 1)
    check("trials upsert: run ok", result["ok"] is True)
    conn = sqlite3.connect(db_path)
    row = json.loads(conn.execute("select payload_json from trials where nct_id='NCT00000001'").fetchone()[0])
    conn.close()
    check("trials upsert: status refreshed (no longer skipped forever)", row["overall_status"] == "COMPLETED")
    check("trials upsert: first_seen_utc preserved", row["first_seen_utc"] == "2020-01-01T00:00:00Z")
    check("trials upsert: last_seen_utc advanced", row["last_seen_utc"] != "2020-01-01T00:00:00Z")
    check("trials upsert: fetched_at_utc set", bool(row.get("fetched_at_utc")))

    # --- existing records preserved after upstream failure; failure not "0 results ok" ---
    with _patched_search_all([_failed_page()]):
        result2 = run_trials_fetch.run(db_path=db_path, molecules_csv=mol_csv)
    check("trials total failure: ok is False", result2["ok"] is False)
    check("trials total failure: outcome is failed", result2["outcome"] == "failed")
    conn = sqlite3.connect(db_path)
    row2 = json.loads(conn.execute("select payload_json from trials where nct_id='NCT00000001'").fetchone()[0])
    conn.close()
    check("trials total failure: existing record untouched",
          row2["overall_status"] == "COMPLETED" and row2["first_seen_utc"] == "2020-01-01T00:00:00Z")

    # --- partial retrieval status: one molecule ok, one molecule fails ---
    mol_csv2 = _molecules_csv(["retatrutide", "tirzepatide"])
    db_fd2, db_path2 = tempfile.mkstemp(suffix=".sqlite")
    os.close(db_fd2)
    os.remove(db_path2)
    with _patched_search_all([_ok_page([_ctgov_study("NCT00000002", "RECRUITING")]), _failed_page()]):
        result3 = run_trials_fetch.run(db_path=db_path2, molecules_csv=mol_csv2)
    check("trials partial: overall ok True (not all molecules failed)", result3["ok"] is True)
    check("trials partial: outcome is partial", result3["outcome"] == "partial")
    check("trials partial: retrieval lists the failed molecule",
          result3["retrieval"]["failed_molecule_ids"] == ["tirzepatide"])
    check("trials partial: new trial from the healthy molecule still stored", result3["stored_new"] == 1)

    # --- preprints: upsert (version bump captured), mirroring the trials case ---
    db_fd3, db_path3 = tempfile.mkstemp(suffix=".sqlite")
    os.close(db_fd3)
    os.remove(db_path3)
    conn = sqlite3.connect(db_path3)
    conn.execute("create table preprints (id text primary key, payload_json text, updated_at_utc text)")
    conn.execute(
        "insert into preprints values (?, ?, ?)",
        ("10.1101/foo.v1", json.dumps({"id": "10.1101/foo.v1", "version_number": 1,
                                        "withdrawn": False, "first_seen_utc": "2020-01-01T00:00:00Z"}),
         "2020-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    updated_result = {
        "id": "10.1101/foo.v1", "doi": "10.1101/foo.v1", "source": "PPR", "title": "Foo",
        "authorString": "A B.", "versionNumber": 2,
        "pubTypeList": {"pubType": ["preprint", "preprint-withdrawal"]},
    }
    with _patched_epmc_search_all([_ok_page([updated_result])]):
        result4 = run_preprints_fetch.run(db_path=db_path3, molecules_csv=mol_csv)
    check("preprints upsert: 0 new, 1 updated", result4["stored_new"] == 0 and result4["stored_updated"] == 1)
    conn = sqlite3.connect(db_path3)
    prow = json.loads(conn.execute("select payload_json from preprints where id=?",
                                    ("10.1101/foo.v1",)).fetchone()[0])
    conn.close()
    check("preprints upsert: version bump captured", prow["version_number"] == 2)
    check("preprints upsert: withdrawal captured", prow["withdrawn"] is True)
    check("preprints upsert: first_seen_utc preserved", prow["first_seen_utc"] == "2020-01-01T00:00:00Z")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
