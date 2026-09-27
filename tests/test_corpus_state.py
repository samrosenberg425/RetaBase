#!/usr/bin/env python3
"""Failure-simulation tests for durable corpus state (WS1) + release validation (WS3-core).

Plain script (no pytest, no network, no GitHub): run `python3 tests/test_corpus_state.py`.
The GitHub Release store is exercised against an in-process fake of the `gh` CLI.

Scenario letters (A-H) match the WS1 acceptance list:
  A cache unavailable -> restore from last-good        E mixed release_id -> rejected
  B no cache + no last-good -> explicit failure        F pruning never removes the pointer's asset
  C validation failure -> last-good untouched          G promotion advances pointer only at the end
  D checksum failure after upload -> pointer unchanged H two unchanged runs -> same fingerprint
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from retarats_pipeline import corpus_store as cs  # noqa: E402
from retarats_pipeline import source_policy as sp  # noqa: E402
from retarats_pipeline.release_validation import validate_release  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {name}")


def raises(exc_type, fn, kind=None):
    try:
        fn()
    except exc_type as exc:
        return kind is None or getattr(exc, "kind", None) == kind
    except Exception:  # noqa: BLE001
        return False
    return False


T0 = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)


def rid(n):
    """n-th release id (one minute apart, same sha)."""
    return cs.make_release_id(T0 + timedelta(minutes=n), "abc1234def")


# --- fixtures -------------------------------------------------------------------

def make_db(path, table, n, salt=""):
    conn = sqlite3.connect(path)
    conn.execute(f"create table {table} (id integer primary key, payload_json text)")
    conn.executemany(f"insert into {table} values (?,?)", [(i, f'{{"v":"{salt}{i}"}}') for i in range(n)])
    conn.commit()
    conn.close()


def make_corpus(d, papers=50, evidence=80, trials=20, preprints=10, salt=""):
    os.makedirs(d, exist_ok=True)
    make_db(os.path.join(d, "retarats_pubmed.sqlite"), "papers", papers, salt)
    conn = sqlite3.connect(os.path.join(d, "retarats_pubmed.sqlite"))
    conn.execute("create table evidence (id integer primary key, payload_json text)")
    conn.executemany("insert into evidence values (?,?)", [(i, f'{{"e":{i}}}') for i in range(evidence)])
    conn.commit()
    conn.close()
    if trials is not None:
        make_db(os.path.join(d, "retarats_trials.sqlite"), "trials", trials)
    if preprints is not None:
        make_db(os.path.join(d, "retarats_preprints.sqlite"), "preprints", preprints)
    return d


def make_site(d, release_id, records=30, trials=20, preprints=10, shards=2, status="current", **_):
    """A valid, fully stamped release directory."""
    os.makedirs(d, exist_ok=True)

    def w(name, obj):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
    first = max(records - 10 * shards, 0)
    per = (records - first) // shards if shards else 0
    names = ["site_records_%03d.json" % (i + 1) for i in range(shards)]
    used = first
    for i, n in enumerate(names):
        cnt = per if i < shards - 1 else records - used
        used += cnt
        w(n, {"release_id": release_id, "records": [{"pmid": str(j)} for j in range(cnt)]})
    w("site_data.json", {"release_id": release_id, "record_count": first, "total_records": records,
                         "shards": names, "records": [{"pmid": str(j)} for j in range(first)],
                         "corpus_stats": {"release_id": release_id, "total_papers": 50, "featured": 5,
                                          "listed": 20, "records_indexed": records,
                                          "molecules_with_data": 7, "total_evidence": 80}})
    w("site_detail.json", {"release_id": release_id, "detail": {}})
    w("trials_data.json", {"release_id": release_id, "count": trials, "trials": [{}] * trials})
    w("preprints_data.json", {"release_id": release_id, "count": preprints, "preprints": [{}] * preprints})
    w("status.json", {"release_id": release_id, "overall": status, "sources": {"pubmed_daily": {"status": status}}})
    with open(os.path.join(d, "index.html"), "w") as fh:
        fh.write("<!doctype html><html><body>x</body></html>")
    return d


class Env:
    """Temp workspace: store dir, cache dir, work dir, site dir."""

    def __init__(self, store=None):
        self.td = tempfile.mkdtemp(prefix="corpus_state_test_")
        self.store_dir = os.path.join(self.td, "store")
        self.store = store or cs.LocalStore(self.store_dir)
        self.cache = os.path.join(self.td, "cache")
        self.work = os.path.join(self.td, "work")
        self.site = os.path.join(self.td, "site")

    def close(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def seed(self, **kw):
        d = os.path.join(self.td, "seed")
        shutil.rmtree(d, ignore_errors=True)
        return make_corpus(d, **kw)

    def new_site(self, release_id, **kw):
        shutil.rmtree(self.site, ignore_errors=True)
        return make_site(self.site, release_id, **kw)

    def first_release(self, n=1, **kw):
        """Bootstrap-promote release n; returns its id."""
        cs.restore(self.store, self.work, cache_dir=self.cache, bootstrap=True, seed_dir=self.seed(**kw), log=quiet)
        r = rid(n)
        self.new_site(r)
        cs.promote(self.store, self.work, self.site, r, cache_dir=self.cache, now=T0 + timedelta(minutes=n), log=quiet)
        return r

    def next_release(self, n, mutate=None, site_kw=None, **kw):
        """restore -> (mutate) -> build fixture -> promote as release n."""
        cs.restore(self.store, self.work, cache_dir=self.cache, log=quiet)
        if mutate:
            mutate(self.work)
        r = rid(n)
        self.new_site(r, **(site_kw or {}))
        return cs.promote(self.store, self.work, self.site, r, cache_dir=self.cache,
                          now=T0 + timedelta(minutes=n), log=quiet, **kw).release_id

    def pointer_bytes(self):
        p = os.path.join(self.store_dir, cs.POINTER_NAME)
        return open(p, "rb").read() if os.path.exists(p) else None

    def listing(self):
        return sorted((n, os.path.getsize(os.path.join(self.store_dir, n))) for n in self.store.list_assets())


def quiet(_msg):
    pass


class RecordingStore(cs.LocalStore):
    def __init__(self, root):
        super().__init__(root)
        self.ops = []

    def put_file(self, name, path, overwrite=False):
        self.ops.append(("put", name, overwrite))
        super().put_file(name, path, overwrite)

    def get_file(self, name, dest):
        self.ops.append(("get", name))
        super().get_file(name, dest)

    def delete(self, name):
        self.ops.append(("delete", name))
        super().delete(name)


class CorruptingStore(RecordingStore):
    """Upload 'succeeds' but the downloaded candidate bytes differ (bit rot / truncated upload)."""

    def __init__(self, root, corrupt_name_prefix):
        super().__init__(root)
        self.prefix = corrupt_name_prefix

    def get_file(self, name, dest):
        super().get_file(name, dest)
        if name.startswith(self.prefix):
            with open(dest, "r+b") as fh:
                fh.seek(20)
                b = fh.read(1)
                fh.seek(20)
                fh.write(bytes([b[0] ^ 0xFF]))


class PointerFailStore(RecordingStore):
    def put_file(self, name, path, overwrite=False):
        if name == cs.POINTER_NAME:
            raise cs.StoreError("simulated: pointer upload failed")
        super().put_file(name, path, overwrite)


# --- fake gh CLI ------------------------------------------------------------------

class FakeGh:
    """In-process stand-in for `gh release ...` backed by a directory. Mirrors real gh:
    `upload` without --clobber refuses an existing asset name."""

    def __init__(self, root, release_exists=True):
        self.root, self.release_exists = root, release_exists
        os.makedirs(root, exist_ok=True)
        self.calls = []
        self.unreachable = False

    def __call__(self, args):
        self.calls.append(list(args))
        if self.unreachable:
            return 1, "", "HTTP 502: Bad Gateway"
        a = list(args)
        assert a[0] == "release"
        sub = a[1]
        if sub == "create":
            self.release_exists = True
            return 0, "", ""
        if not self.release_exists:
            return 1, "", "release not found"
        if sub == "view":
            if "assets" in a:
                return 0, json.dumps({"assets": [{"name": n} for n in sorted(os.listdir(self.root))]}), ""
            return 0, json.dumps({"tagName": "corpus-store"}), ""
        if sub == "upload":
            src = a[3]
            dst = os.path.join(self.root, os.path.basename(src))
            if os.path.exists(dst) and "--clobber" not in a:
                return 1, "", "a release asset with that name already exists"
            shutil.copyfile(src, dst)
            return 0, "", ""
        if sub == "download":
            pattern, d = a[a.index("--pattern") + 1], a[a.index("--dir") + 1]
            src = os.path.join(self.root, pattern)
            if not os.path.exists(src):
                return 1, "", "no assets match"
            shutil.copyfile(src, os.path.join(d, pattern))
            return 0, "", ""
        if sub == "delete-asset":
            p = os.path.join(self.root, a[3])
            os.remove(p) if os.path.exists(p) else None
            return 0, "", ""
        return 1, "", "unsupported"


# --- A: cache unavailable ----------------------------------------------------------

def test_A_cache_unavailable_recovers_from_last_good():
    e = Env()
    try:
        r1 = e.first_release()
        shutil.rmtree(e.cache)  # cache lost
        res = cs.restore(e.store, e.work, cache_dir=e.cache, log=quiet)
        m = e.store.read_json(cs.manifest_name(r1))
        check("A: restored from last-good archive", res.source == "last-good" and res.release_id == r1)
        check("A: restored files match manifest sha256",
              all(cs.sha256_file(os.path.join(e.work, n)) == v["sha256"] for n, v in m["files"].items()))
        # cache present + valid -> used, but only because it is byte-identical to the manifest
        cs.refresh_cache(e.cache, e.work, m["files"])
        res2 = cs.restore(e.store, e.work, cache_dir=e.cache, log=quiet)
        check("A: byte-identical cache is used as acceleration", res2.source == "cache")
        # cache tampered/stale -> ignored, archive used
        with open(os.path.join(e.cache, "retarats_pubmed.sqlite"), "ab") as fh:
            fh.write(b"junk")
        res3 = cs.restore(e.store, e.work, cache_dir=e.cache, log=quiet)
        check("A: corrupted cache rejected, falls back to last-good", res3.source == "last-good")
        check("A: staged copy is clean after cache rejection",
              cs.sha256_file(os.path.join(e.work, "retarats_pubmed.sqlite")) == m["files"]["retarats_pubmed.sqlite"]["sha256"])
    finally:
        e.close()


def test_A2_fallback_to_previous_good():
    e = Env()
    try:
        r1 = e.first_release(1)
        r2 = e.next_release(2)
        os.remove(os.path.join(e.store_dir, cs.archive_name(r2)))  # newest archive lost
        shutil.rmtree(e.cache, ignore_errors=True)
        res = cs.restore(e.store, e.work, cache_dir=e.cache, log=quiet)
        check("A2: missing pointer target -> previous verified release", res.release_id == r1 and res.source == "previous-good")
        # pointer file unreadable -> newest verified manifest wins
        e2 = Env()
        e2.first_release(1)
        r2b = e2.next_release(2)
        with open(os.path.join(e2.store_dir, cs.POINTER_NAME), "w") as fh:
            fh.write("{not json")
        res = cs.restore(e2.store, e2.work, log=quiet)
        check("A2: unreadable pointer -> newest verified release", res.release_id == r2b)
        e2.close()
        # corrupted archive bytes (sha mismatch) -> previous
        e3 = Env()
        e3.first_release(1)
        e3.next_release(2)
        r3 = e3.next_release(3)
        with open(os.path.join(e3.store_dir, cs.archive_name(r3)), "ab") as fh:
            fh.write(b"\0")
        res = cs.restore(e3.store, e3.work, log=quiet)
        check("A2: corrupted newest archive skipped", res.release_id == rid(2))
        e3.close()
    finally:
        e.close()


# --- B: no cache, no last-good ---------------------------------------------------------

def test_B_no_state_fails_explicitly():
    e = Env()
    try:
        check("B: empty store, no bootstrap -> RestoreError", raises(cs.RestoreError, lambda: cs.restore(e.store, e.work, cache_dir=e.cache, log=quiet)))
        check("B: nothing was staged", not os.path.exists(os.path.join(e.work, "retarats_pubmed.sqlite")))
        # a cache alone is NOT authoritative: without a store manifest it cannot be trusted
        make_corpus(e.cache)
        check("B: unverifiable cache alone is refused", raises(cs.RestoreError, lambda: cs.restore(e.store, e.work, cache_dir=e.cache, log=quiet)))
        # explicit bootstrap works on an empty store only
        res = cs.restore(e.store, e.work, bootstrap=True, seed_dir=e.seed(), log=quiet)
        check("B: explicit bootstrap (seeded) allowed on empty store", res.source == "bootstrap" and cs.read_stage_marker(e.work)["bootstrap"])
        check("B: unusable seed is refused", raises(cs.RestoreError, lambda: cs.restore(e.store, e.work, bootstrap=True, seed_dir=os.path.join(e.td, "nope"), log=quiet)))
        # a bootstrap candidate with an empty corpus can never be promoted (hollow)
        cs.restore(e.store, e.work, bootstrap=True, log=quiet)
        e.new_site(rid(1))
        check("B: hollow bootstrap corpus is not promotable", raises(cs.PromotionError, lambda: cs.promote(e.store, e.work, e.site, rid(1), log=quiet), "gate"))
        check("B: store still empty after refused promote", e.listing() == [])
        # store WITH state but nothing verifiable: refuse, even with --bootstrap
        e2 = Env()
        r1 = e2.first_release()
        os.remove(os.path.join(e2.store_dir, cs.archive_name(r1)))
        check("B: state exists but unrecoverable -> error even with bootstrap",
              raises(cs.RestoreError, lambda: cs.restore(e2.store, e2.work, bootstrap=True, seed_dir=e2.seed(), log=quiet)))
        e2.close()
        # stage guard: never wipe a non-empty directory we did not create
        os.makedirs(os.path.join(e.td, "precious"))
        open(os.path.join(e.td, "precious", "keep.txt"), "w").write("x")
        e3 = Env()
        e3.first_release()
        check("B: refuses to stage into a non-empty unmarked dir",
              raises(cs.CorpusStateError, lambda: cs.restore(e3.store, os.path.join(e.td, "precious"), log=quiet)))
        check("B: precious dir untouched", os.path.exists(os.path.join(e.td, "precious", "keep.txt")))
        e3.close()
    finally:
        e.close()


# --- C: candidate validation failure ----------------------------------------------------

def test_C_validation_failure_keeps_last_good():
    e = Env()
    try:
        r1 = e.first_release()
        ptr_before, list_before = e.pointer_bytes(), e.listing()

        def empty_trials(work):
            conn = sqlite3.connect(os.path.join(work, "retarats_trials.sqlite"))
            conn.execute("delete from trials")
            conn.commit()
            conn.close()
        check("C: emptied trials table -> gate failure", raises(cs.PromotionError, lambda: e.next_release(2, mutate=empty_trials), "gate"))
        check("C: pointer bytes unchanged", e.pointer_bytes() == ptr_before)
        check("C: no asset added/removed/changed", e.listing() == list_before)

        def drop_papers(work):
            conn = sqlite3.connect(os.path.join(work, "retarats_pubmed.sqlite"))
            conn.execute("delete from papers where id >= 10")
            conn.commit()
            conn.close()
        check("C: collapsed papers table -> gate failure", raises(cs.PromotionError, lambda: e.next_release(3, mutate=drop_papers), "gate"))
        check("C: dropped trials DB file -> gate failure",
              raises(cs.PromotionError, lambda: e.next_release(4, mutate=lambda w: os.remove(os.path.join(w, "retarats_trials.sqlite"))), "gate"))
        check("C: empty trials FEED -> gate failure (feed baseline)", raises(cs.PromotionError, lambda: e.next_release(5, site_kw={"trials": 0}), "gate"))
        check("C: collapsed record feed -> gate failure", raises(cs.PromotionError, lambda: e.next_release(6, site_kw={"records": 5, "shards": 1}), "gate"))
        check("C: still unchanged after several rejected candidates", e.pointer_bytes() == ptr_before and e.listing() == list_before)
        # small legitimate shrink within tolerance is fine; deliberate override works
        ok = e.next_release(7, mutate=lambda w: sqlite3.connect(os.path.join(w, "retarats_trials.sqlite")).execute("select 1"))
        check("C: an unchanged/healthy candidate still promotes afterwards", ok == rid(7))
        cs.restore(e.store, e.work, log=quiet)
        drop_papers(e.work)
        e.new_site(rid(8))
        res = cs.promote(e.store, e.work, e.site, rid(8), allow_shrink=True, now=T0 + timedelta(minutes=8), log=quiet)
        check("C: --allow-shrink is the explicit, manual override", res.release_id == rid(8))
        # promotion cannot bypass validation of a mixed-release site
        cs.restore(e.store, e.work, log=quiet)
        e.new_site(rid(9))
        d = json.load(open(os.path.join(e.site, "site_detail.json")))
        d["release_id"] = rid(1)
        json.dump(d, open(os.path.join(e.site, "site_detail.json"), "w"))
        before = e.pointer_bytes()
        check("C: promote() itself rejects a mixed-release site", raises(cs.PromotionError, lambda: cs.promote(e.store, e.work, e.site, rid(9), log=quiet), "gate"))
        check("C: pointer unchanged after mixed-release rejection", e.pointer_bytes() == before)
        check("C: promote refuses a work dir that was never staged", raises(cs.PromotionError, lambda: cs.promote(e.store, e.site, e.site, rid(9), log=quiet), "unstaged"))
        _ = r1
    finally:
        e.close()


# --- D: upload ok, checksum verification fails ----------------------------------------------

def test_D_checksum_failure_leaves_pointer():
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        store = CorruptingStore(os.path.join(td, "store"), corrupt_name_prefix="__none__")
        e = Env(store=store)
        e.td = td
        e.cache, e.work, e.site, e.store_dir = (os.path.join(td, "cache"), os.path.join(td, "work"),
                                                os.path.join(td, "site"), os.path.join(td, "store"))
        r1 = e.first_release()
        ptr_before = e.pointer_bytes()
        store.prefix = "corpus-"  # from now on downloads of candidates come back corrupted
        cs.restore(store, e.work, cache_dir=e.cache, log=quiet)  # restore of r1 would also be corrupted -> use cache
        # restore above used the byte-identical cache (acceleration), so it succeeded
        e.new_site(rid(2))
        check("D: checksum mismatch -> PromotionError(verification)",
              raises(cs.PromotionError, lambda: cs.promote(store, e.work, e.site, rid(2), log=quiet, now=T0 + timedelta(minutes=2)), "verification"))
        check("D: pointer unchanged", e.pointer_bytes() == ptr_before)
        check("D: unverified candidate asset removed", cs.archive_name(rid(2)) not in store.list_assets())
        check("D: no manifest published for the candidate", cs.manifest_name(rid(2)) not in store.list_assets())
        check("D: previous last-good still present", cs.archive_name(r1) in store.list_assets())
        store.prefix = "__none__"
        res = cs.restore(store, e.work, log=quiet)
        check("D: next run restores the previous asset successfully", res.release_id == r1)

        # failure AFTER upload+verify, at the pointer write
        ps = PointerFailStore(os.path.join(td, "store2"))
        e2 = Env(store=ps)
        e2.first_release_via = None
        try:
            cs.restore(ps, os.path.join(td, "w2"), bootstrap=True, seed_dir=make_corpus(os.path.join(td, "s2")), log=quiet)
        finally:
            pass
        # pointer store refuses even the first pointer: promotion must fail cleanly & leave nothing pointing anywhere
        make_site(os.path.join(td, "site2"), rid(1))
        check("D: pointer write failure -> PromotionError(store)",
              raises(cs.PromotionError, lambda: cs.promote(ps, os.path.join(td, "w2"), os.path.join(td, "site2"), rid(1), log=quiet, now=T0), "store"))
        check("D: no pointer exists after failed first promotion", cs.POINTER_NAME not in ps.list_assets())
        e2.close()
        e.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_D2_pointer_failure_keeps_previous_pointer():
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        root = os.path.join(td, "store")
        e = Env(store=cs.LocalStore(root))
        e.td, e.store_dir = td, root
        e.cache, e.work, e.site = (os.path.join(td, "cache"), os.path.join(td, "work"), os.path.join(td, "site"))
        r1 = e.first_release()
        ptr_before = e.pointer_bytes()
        ps = PointerFailStore(root)
        cs.restore(ps, e.work, cache_dir=e.cache, log=quiet)
        e.new_site(rid(2))
        check("D2: pointer upload failure -> PromotionError", raises(cs.PromotionError, lambda: cs.promote(ps, e.work, e.site, rid(2), log=quiet, now=T0 + timedelta(minutes=2)), "store"))
        check("D2: previous pointer bytes untouched", e.pointer_bytes() == ptr_before)
        res = cs.restore(cs.LocalStore(root), e.work, log=quiet)
        check("D2: next restore uses the previous last-good (pointer wins over orphan verified release)", res.release_id == r1)
        e.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)


# --- E: mixed release ---------------------------------------------------------------------------

def test_E_mixed_release_rejected():
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        d = make_site(os.path.join(td, "ok"), rid(2))
        check("E: consistent fixture passes", validate_release(d, rid(2)).ok)
        check("E: consistent fixture passes without pinning the id", validate_release(d).ok)

        def mutated(name, key, value):
            dd = make_site(os.path.join(td, "m_" + name.replace(".", "_") + key), rid(2))
            p = os.path.join(dd, name)
            doc = json.load(open(p))
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
            json.dump(doc, open(p, "w"))
            return dd
        r = validate_release(mutated("site_records_001.json", "release_id", rid(1)))
        check("E: one shard from an older release -> rejected", not r.ok and any("mixed release" in x for x in r.errors))
        check("E: error names the offending shard", any("site_records_001.json" in x for x in r.errors))
        check("E: stale detail file rejected", not validate_release(mutated("site_detail.json", "release_id", rid(1))).ok)
        check("E: stale trials feed rejected", not validate_release(mutated("trials_data.json", "release_id", rid(1))).ok)
        check("E: stale preprints feed rejected", not validate_release(mutated("preprints_data.json", "release_id", rid(1))).ok)
        check("E: stale status.json rejected", not validate_release(mutated("status.json", "release_id", rid(1))).ok)
        check("E: stale corpus_stats.release_id rejected",
              not validate_release(mutated("site_data.json", "corpus_stats", {"release_id": rid(1)})).ok)
        check("E: unstamped asset rejected", not validate_release(mutated("site_records_002.json", "release_id", None)).ok)
        check("E: whole release consistent but wrong id -> rejected when pinned", not validate_release(d, rid(3)).ok)
        # structure
        dd = make_site(os.path.join(td, "orphan"), rid(2))
        json.dump({"release_id": rid(1), "records": []}, open(os.path.join(dd, "site_records_009.json"), "w"))
        check("E: orphan shard left over from an earlier build rejected", any("orphan shard" in x for x in validate_release(dd).errors))
        dd = make_site(os.path.join(td, "missing"), rid(2))
        os.remove(os.path.join(dd, "site_records_002.json"))
        check("E: manifest-listed shard missing -> rejected", not validate_release(dd).ok)
        for name in ("index.html", "site_data.json", "site_detail.json", "trials_data.json", "preprints_data.json", "status.json"):
            dd = make_site(os.path.join(td, "no_" + name), rid(2))
            os.remove(os.path.join(dd, name))
            check(f"E: missing {name} -> rejected", not validate_release(dd).ok)
        dd = make_site(os.path.join(td, "empty"), rid(2))
        open(os.path.join(dd, "site_detail.json"), "w").close()
        check("E: empty asset -> rejected", not validate_release(dd).ok)
        dd = make_site(os.path.join(td, "bad"), rid(2))
        open(os.path.join(dd, "trials_data.json"), "w").write("{oops")
        check("E: unparseable asset -> rejected", not validate_release(dd).ok)
        dd = make_site(os.path.join(td, "count"), rid(2))
        doc = json.load(open(os.path.join(dd, "site_data.json")))
        doc["total_records"] += 1
        json.dump(doc, open(os.path.join(dd, "site_data.json"), "w"))
        check("E: total_records inconsistent with shards -> rejected", not validate_release(dd).ok)
        check("E: empty feed rejected", not validate_release(make_site(os.path.join(td, "z"), rid(2), records=0, shards=0)).ok)
        check("E: baseline collapse (feed) rejected", not validate_release(d, rid(2), baseline={"total_records": 100, "trials": 20, "preprints": 10}).ok)
        check("E: within tolerance vs baseline passes", validate_release(d, rid(2), baseline={"total_records": 31, "trials": 20, "preprints": 10}).ok)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# --- F: pruning -------------------------------------------------------------------------------------

def test_F_prune_never_removes_pointer_asset():
    e = Env()
    try:
        ids = [e.first_release(1)] + [e.next_release(n) for n in range(2, 7)]
        names = e.store.list_assets()
        check("F: promote() auto-pruned to the newest 3 releases",
              sorted(cs.manifest_release_ids(names)) == sorted(ids[-3:]))
        check("F: pointer + non-corpus assets preserved", cs.POINTER_NAME in names)
        # roll the pointer BACK to the oldest retained release; prune with keep=1 must protect it
        oldest = ids[-3]
        e.store.put_json(cs.POINTER_NAME, {"schema": 1, "release_id": oldest, "asset": cs.archive_name(oldest),
                                           "sha256": e.store.read_json(cs.manifest_name(oldest))["archive"]["sha256"],
                                           "manifest": cs.manifest_name(oldest)}, overwrite=True)
        e.store.put_file("snapshot-notes.txt", os.path.join(e.work, cs.STAGE_MARKER))
        cs.prune(e.store, keep=1, log=quiet)
        left = e.store.list_assets()
        check("F: pointer-referenced archive survives keep=1", cs.archive_name(oldest) in left and cs.manifest_name(oldest) in left)
        check("F: newest verified release also kept", cs.archive_name(ids[-1]) in left)
        check("F: middle release pruned", cs.archive_name(ids[-2]) not in left and cs.manifest_name(ids[-2]) not in left)
        check("F: unrelated assets are never touched", "snapshot-notes.txt" in left)
        res = cs.restore(e.store, e.work, log=quiet)
        check("F: pointer target still restorable after prune", res.release_id == oldest)
        # unreadable pointer while releases exist -> prune refuses to delete anything
        with open(os.path.join(e.store_dir, cs.POINTER_NAME), "w") as fh:
            fh.write("garbage")
        before = e.listing()
        check("F: unreadable pointer -> prune deletes nothing", cs.prune(e.store, keep=1, log=quiet) == [] and e.listing() == before)
        check("F: prune keep<1 refused", raises(cs.CorpusStateError, lambda: cs.prune(e.store, keep=0, log=quiet)))
        # orphan archive (failed candidate without manifest) is cleaned, pointer's never
        e2 = Env()
        r1 = e2.first_release(1)
        open(os.path.join(e2.store_dir, cs.archive_name(rid(50))), "wb").write(b"orphan")
        cs.prune(e2.store, keep=3, log=quiet)
        check("F: orphan archive without manifest pruned", cs.archive_name(rid(50)) not in e2.store.list_assets())
        check("F: pointer asset intact", cs.archive_name(r1) in e2.store.list_assets())
        e2.close()
    finally:
        e.close()


# --- G: successful promotion ordering ---------------------------------------------------------------

def test_G_promotion_order_and_consistency():
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        store = RecordingStore(os.path.join(td, "store"))
        e = Env(store=store)
        e.td, e.store_dir = td, os.path.join(td, "store")
        e.cache, e.work, e.site = (os.path.join(td, "cache"), os.path.join(td, "work"), os.path.join(td, "site"))
        cs.restore(store, e.work, bootstrap=True, seed_dir=e.seed(), log=quiet)
        store.ops.clear()
        e.new_site(rid(1))
        res = cs.promote(store, e.work, e.site, rid(1), cache_dir=e.cache, now=T0 + timedelta(minutes=1), log=quiet)
        writes = [op for op in store.ops if op[0] == "put"]
        check("G: write order = archive, manifest, pointer (pointer LAST)",
              [w[1] for w in writes] == [cs.archive_name(rid(1)), cs.manifest_name(rid(1)), cs.POINTER_NAME])
        gets = [i for i, op in enumerate(store.ops) if op[0] == "get" and op[1] == cs.archive_name(rid(1))]
        ptr_at = next(i for i, op in enumerate(store.ops) if op[0] == "put" and op[1] == cs.POINTER_NAME)
        check("G: candidate is downloaded back and verified BEFORE the pointer moves", gets and gets[0] < ptr_at)
        check("G: versioned assets never overwrite; only the pointer may", [w[2] for w in writes] == [False, False, True])
        m = store.read_json(cs.manifest_name(rid(1)))
        check("G: pointer names release, asset and the verified sha256",
              store.read_json(cs.POINTER_NAME)["sha256"] == m["archive"]["sha256"] == cs.sha256_file(os.path.join(td, "store", cs.archive_name(rid(1)))))
        check("G: release_id in manifest matches every public asset", m["release_id"] == rid(1) and validate_release(e.site, rid(1)).ok)
        check("G: manifest carries fingerprint, counts, site counts, stats baseline",
              m["corpus_fingerprint"] and m["counts"]["papers"] == 50 and m["site"]["total_records"] == 30 and m["stats_baseline"]["total_papers"] == 50)
        check("G: cache refreshed only after promotion (holds promoted bytes)",
              all(cs.sha256_file(os.path.join(e.cache, n)) == v["sha256"] for n, v in m["files"].items()))
        # release_id advances on the next successful run, in manifest and pointer
        r2 = e.next_release(2) if False else None
        cs.restore(store, e.work, cache_dir=e.cache, log=quiet)
        e.new_site(rid(2))
        cs.promote(store, e.work, e.site, rid(2), cache_dir=e.cache, now=T0 + timedelta(minutes=2), log=quiet)
        check("G: release_id advances (pointer + manifest)", store.read_json(cs.POINTER_NAME)["release_id"] == rid(2)
              and store.read_json(cs.manifest_name(rid(2)))["base_release_id"] == rid(1))
        check("G: duplicate release id refused (immutable versions)",
              raises(cs.PromotionError, lambda: (cs.restore(store, e.work, log=quiet), cs.promote(store, e.work, e.site, rid(2), log=quiet)), "gate"))
        _ = (res, r2)
        e.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_G2_stale_base_concurrent_writer():
    e = Env()
    try:
        e.first_release(1)
        cs.restore(e.store, e.work, log=quiet)
        e.new_site(rid(3))
        # another writer promotes while ours is mid-run
        w2, s2 = os.path.join(e.td, "w2"), os.path.join(e.td, "s2")
        cs.restore(e.store, w2, log=quiet)
        make_site(s2, rid(2))
        cs.promote(e.store, w2, s2, rid(2), now=T0 + timedelta(minutes=2), log=quiet)
        before = e.pointer_bytes()
        check("G2: stale base (last-good moved since restore) -> refused",
              raises(cs.PromotionError, lambda: cs.promote(e.store, e.work, e.site, rid(3), log=quiet), "stale_base"))
        check("G2: pointer untouched", e.pointer_bytes() == before)
    finally:
        e.close()


# --- H: idempotency -----------------------------------------------------------------------------------------

def test_H_unchanged_runs_same_fingerprint():
    e = Env()
    try:
        r1 = e.first_release(1)
        m1 = e.store.read_json(cs.manifest_name(r1))
        r2 = e.next_release(2)  # restore -> NO mutation -> build -> promote
        m2 = e.store.read_json(cs.manifest_name(r2))
        check("H: logical corpus fingerprint identical across unchanged runs", m1["corpus_fingerprint"] == m2["corpus_fingerprint"])
        check("H: identical row counts", m1["counts"] == m2["counts"])
        check("H: sqlite files byte-identical (no unintended mutation)",
              all(m1["files"][n]["sha256"] == m2["files"][n]["sha256"] for n in m1["files"] if n.endswith(".sqlite")))
        check("H: deterministic archive bytes for an unchanged corpus", m1["archive"]["sha256"] == m2["archive"]["sha256"])
        check("H: a new release_id is still issued", r1 != r2)
        # fingerprint is content-based, not layout-based
        cs.restore(e.store, e.work, log=quiet)
        f_before = cs.logical_fingerprint(e.work)
        c = sqlite3.connect(os.path.join(e.work, "retarats_pubmed.sqlite"), isolation_level=None)
        c.execute("delete from papers where id = 3")
        c.execute("vacuum")
        c.close()
        f_deleted = cs.logical_fingerprint(e.work)
        c = sqlite3.connect(os.path.join(e.work, "retarats_pubmed.sqlite"), isolation_level=None)
        c.execute("insert into papers values (3, '{\"v\":\"3\"}')")
        c.execute("vacuum")
        c.close()
        check("H: any row change alters the fingerprint", f_deleted != f_before)
        check("H: VACUUM/re-insert of identical rows restores the same fingerprint", cs.logical_fingerprint(e.work) == f_before)
        c = sqlite3.connect(os.path.join(e.work, "retarats_pubmed.sqlite"))
        c.execute("update papers set payload_json = '{\"v\":\"changed\"}' where id = 5")
        c.commit()
        c.close()
        check("H: an in-place row edit alters the fingerprint", cs.logical_fingerprint(e.work) != f_before)
    finally:
        e.close()


# --- GitHub store against the fake gh -------------------------------------------------------------------------

def test_github_store_via_fake_gh():
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        fake = FakeGh(os.path.join(td, "remote"), release_exists=False)
        store = cs.GitHubReleaseStore("owner/repo", runner=fake, retries=0)
        e = Env(store=store)
        e.td = td
        e.cache, e.work, e.site, e.store_dir = (os.path.join(td, "cache"), os.path.join(td, "work"),
                                                os.path.join(td, "site"), os.path.join(td, "remote"))
        check("GH: absent release == empty store, restore fails explicitly",
              raises(cs.RestoreError, lambda: cs.restore(store, e.work, log=quiet)))
        r1 = e.first_release(1)
        check("GH: release created on first write, assets uploaded", fake.release_exists and cs.archive_name(r1) in store.list_assets())
        r2 = e.next_release(2)
        uploads = [c for c in fake.calls if c[1] == "upload"]
        clobbered = [os.path.basename(c[3]) for c in uploads if "--clobber" in c]
        check("GH: only last-good.json is ever uploaded with --clobber", set(clobbered) == {cs.POINTER_NAME} and len(clobbered) == 2)
        check("GH: archives/manifests uploaded without --clobber",
              all("--clobber" not in c for c in uploads if os.path.basename(c[3]) != cs.POINTER_NAME))
        shutil.rmtree(e.cache, ignore_errors=True)
        res = cs.restore(store, e.work, cache_dir=e.cache, log=quiet)
        check("GH: cache miss restores the pointer-named asset (sha verified)", res.release_id == r2 and res.source == "last-good")
        check("GH: uploading an existing versioned name is refused", raises(cs.AssetExists, lambda: store.put_file(cs.archive_name(r1), os.path.join(e.work, "retarats_pubmed.sqlite"))))
        fake.unreachable = True
        check("GH: unreachable API is an error, NOT an empty store (no bootstrap over it)",
              raises(cs.StoreError, lambda: cs.restore(store, e.work, bootstrap=True, log=quiet)))
        check("GH: promote refuses when store unreachable", raises(cs.PromotionError, lambda: cs.promote(store, e.work, e.site, rid(9), log=quiet), "store"))
        fake.unreachable = False
        check("GH: delete-asset used for pruning only on corpus assets",
              all(c[3].startswith(("corpus-", "manifest-")) for c in fake.calls if c[1] == "delete-asset"))
        e.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)


# --- source policy + status ---------------------------------------------------------------------------------------

def test_source_policy_and_status():
    policy = sp.load_policy()
    check("policy: all seven sources present", set(policy) == {"pubmed_daily", "ctgov", "europepmc_preprints", "icite", "openalex",
                                                              "pubmed_resweep_retractions", "fulltext"})
    check("policy: thresholds from the approved plan", policy["pubmed_daily"]["degraded_after_days"] == 3
          and policy["pubmed_daily"]["failed_after_days"] == 10 and policy["europepmc_preprints"]["failed_after_days"] == 14
          and policy["icite"]["failed_after_days"] == 30 and policy["pubmed_resweep_retractions"]["failed_after_days"] == 21)
    check("policy: every source may deploy stale last-good", all(p["deploy_stale_last_good"] for p in policy.values()))
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        bad = os.path.join(td, "bad.csv")
        open(bad, "w").write("source,cadence,degraded_after_days,failed_after_days,deploy_stale_last_good,description\nx,daily,10,3,yes,d\n")
        check("policy: degraded>=failed rejected", raises(sp.PolicyError, lambda: sp.load_policy(bad)))
        open(bad, "w").write("source,cadence\nx,daily\n")
        check("policy: wrong columns rejected", raises(sp.PolicyError, lambda: sp.load_policy(bad)))
        st = os.path.join(td, "source_status.json")
        now = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
        sp.record_run(st, "pubmed_daily", True, now=now - timedelta(days=2), counts={"new": 5, "live": 5})
        sp.record_run(st, "ctgov", True, now=now - timedelta(days=4))
        sp.record_run(st, "icite", True, now=now - timedelta(days=40))
        sp.record_run(st, "fulltext", False, now=now, error="HTTP 503")
        s = sp.build_status(rid(1), st, now=now)
        S = s["sources"]
        check("status: 2d old daily source is current", S["pubmed_daily"]["status"] == "current")
        check("status: 4d old daily source is degraded", S["ctgov"]["status"] == "degraded")
        check("status: 40d old weekly source is failed", S["icite"]["status"] == "failed")
        check("status: never-succeeded source is unknown (and a failed attempt does not advance last_success)",
              S["fulltext"]["status"] == "unknown" and S["fulltext"]["last_attempt_ok"] is False and S["fulltext"]["error"] == "HTTP 503")
        check("status: overall = worst known state", s["overall"] == "failed")
        check("status: counts recorded", S["pubmed_daily"]["counts"] == {"new": 5, "live": 5})
        sp.record_run(st, "icite", False, now=now, error="boom")
        check("status: failure keeps last_success so staleness keeps growing",
              sp.build_status(rid(1), st, now=now)["sources"]["icite"]["age_days"] == 40)
        edge = {"cadence": "daily", "degraded_after_days": 3, "failed_after_days": 10, "deploy_stale_last_good": True, "description": ""}
        ent = lambda days: {"last_success_utc": sp._iso(now - timedelta(days=days))}  # noqa: E731
        check("status: boundary 3d -> degraded, 10d -> failed, 2d -> current",
              [sp.evaluate_source(edge, ent(d), now)["status"] for d in (2, 3, 9, 10)] == ["current", "degraded", "degraded", "failed"])
        check("status: unknown source name rejected", raises(sp.PolicyError, lambda: sp.record_run(st, "nope", True)))
        out = os.path.join(td, "status.json")
        sp.write_status(out, s)
        check("status: status.json is valid JSON stamped with the release_id", json.load(open(out))["release_id"] == rid(1))
        check("status: current-only release validates", validate_release(make_site(os.path.join(td, "sd"), rid(1), status="current")).ok)
        check("status: overall vocabulary enforced", not validate_release(make_site(os.path.join(td, "sd2"), rid(1), status="weird")).ok)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# --- CLI / misc ---------------------------------------------------------------------------------------------------------

def test_cli_and_release_id():
    from scripts import corpus_state as cli
    check("release_id format", cs.RELEASE_ID_RE.match(cs.make_release_id()) if hasattr(cs, "RELEASE_ID_RE") else True)
    check("release_id embeds minute + sha7", cs.make_release_id(T0, "0123456789abcdef") == "20260901T0600-0123456")
    check("release_id without sha is 'local'", cs.make_release_id(T0, "").endswith("-local"))
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        store, work = os.path.join(td, "store"), os.path.join(td, "work")
        seed = make_corpus(os.path.join(td, "seed"))
        rc = cli.main(["restore", "--local-store", store, "--dest", work])
        check("cli: restore on empty store exits 1", rc == 1)
        rc = cli.main(["restore", "--local-store", store, "--dest", work, "--bootstrap", "--seed-dir", seed])
        check("cli: bootstrap restore exits 0", rc == 0)
        make_site(os.path.join(td, "site"), rid(1))
        rc = cli.main(["promote", "--local-store", store, "--work", work, "--site-dir", os.path.join(td, "site"), "--release-id", rid(1),
                       "--cache-dir", os.path.join(td, "cache")])
        check("cli: promote exits 0 and advances pointer", rc == 0 and json.load(open(os.path.join(store, cs.POINTER_NAME)))["release_id"] == rid(1))
        rc = cli.main(["promote", "--work", work, "--release-id", rid(2), "--repo", "owner/repo"])
        check("cli: refuses GitHub-store writes outside Actions", rc == 1 and os.environ.get("GITHUB_ACTIONS") != "true")
        rc = cli.main(["simulate", "--kind", "empty_trials", "--work", work])
        conn = sqlite3.connect(os.path.join(work, "retarats_trials.sqlite"))
        check("cli: simulate empty_trials empties only the staged copy",
              rc == 0 and conn.execute("select count(*) from trials").fetchone()[0] == 0
              and cs.sqlite_table_counts(os.path.join(td, "seed", "retarats_trials.sqlite"))["trials"] == 20)
        conn.close()
        cli.main(["simulate", "--kind", "mixed_release", "--site-dir", os.path.join(td, "site"), "--work", work])
        check("cli: simulate mixed_release makes validation fail", not validate_release(os.path.join(td, "site"), rid(1)).ok)
        check("cli: unknown simulation is an error", cli.main(["simulate", "--kind", "x", "--work", work]) == 1)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_builders_stamp_release_id():
    """The real builders stamp release_id into every public JSON asset (and stay unstamped without one)."""
    import importlib.util

    def load(name):
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    bcd, btj, bpj = load("build_curated_database"), load("build_trials_json"), load("build_preprints_json")
    td = tempfile.mkdtemp(prefix="corpus_state_test_")
    try:
        recs = [{"pmid": str(i), "molecule_id": "m", "title": "t%d" % i} for i in range(3)]
        for stamped in (True, False):
            out = os.path.join(td, "s" if stamped else "u")
            os.makedirs(out)
            bcd._write_site_json(os.path.join(out, "site_data.json"), recs, [{"molecule_id": "m"}], {},
                                 release_id=rid(4) if stamped else "")
            docs = [json.load(open(os.path.join(out, f))) for f in ("site_data.json", "site_detail.json")]
            got = [d.get("release_id") for d in docs]
            check(f"builders: site_data + site_detail stamped={stamped}", got == ([rid(4)] * 2 if stamped else [None] * 2))
        # trials / preprints builders (empty DBs -> empty valid feeds)
        for mod, key in ((btj, "trials"), (bpj, "preprints")):
            p = os.path.join(td, key + ".json")
            mod.build(os.path.join(td, "absent.sqlite"), p, release_id=rid(4))
            check(f"builders: {key} feed stamped", json.load(open(p))["release_id"] == rid(4))
            mod.build(os.path.join(td, "absent.sqlite"), p)
            check(f"builders: {key} feed unstamped without an id", "release_id" not in json.load(open(p)))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} raised {exc!r}", False)
    print(f"{_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
