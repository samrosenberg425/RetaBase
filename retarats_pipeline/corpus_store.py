"""Durable corpus state: versioned archives, last-good pointer, staging, promotion.

Authority for the corpus is a versioned store (a GitHub Release named ``corpus-store`` in
production; a plain directory locally/in tests), NOT the Actions cache:

    corpus-<release_id>.tar.gz     the corpus files (sqlite DBs + small state files)
    manifest-<release_id>.json     release_id, per-file sha256/table counts, corpus
                                   fingerprint, feed counts, stats baseline, sources
    last-good.json                 tiny pointer -> release_id / asset / sha256

Every corpus writer follows ONE path: restore -> stage -> mutate -> build -> validate
-> promote.

* ``restore`` prefers a cache copy that is byte-identical (sha256) to the last-good
  manifest, else downloads + verifies the last-good archive (falling back to the
  newest older release whose manifest+archive verify), else FAILS explicitly. It only
  bootstraps an empty/seeded corpus when asked (``bootstrap=True``) AND the store holds
  no state at all.
* ``promote`` validates the candidate (release validation + count gates against the
  last-good manifest), uploads it as a NEW versioned asset (never overwriting), pulls
  it back and verifies its sha256, publishes its manifest, and only then replaces the
  pointer. Any earlier failure leaves the pointer, the previous asset and everything
  else untouched. The cache is refreshed only after the pointer has advanced.
* ``prune`` keeps the newest N verified releases and never removes the release the
  pointer references.

Pure stdlib. GitHub access goes through an injectable ``gh`` runner so it can be
exercised offline against a fake.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from retarats_pipeline.release_validation import DEFAULT_MAX_DROP, RELEASE_ID_RE, site_counts, validate_release

MANIFEST_SCHEMA = 1
STORE_TAG = "corpus-store"
POINTER_NAME = "last-good.json"
DEFAULT_KEEP = 3

# Files that make up the durable corpus state. Only sqlite files feed the logical
# fingerprint; the small JSON files ride along so resumable state survives.
SQLITE_FILES = ("retarats_pubmed.sqlite", "retarats_state.sqlite",
                "retarats_trials.sqlite", "retarats_preprints.sqlite")
JSON_FILES = ("backfill_checkpoint.json", "freshness_state.json", "source_status.json")
PAYLOAD_FILES = SQLITE_FILES + JSON_FILES
REQUIRED_FILES = ("retarats_pubmed.sqlite",)
# Feed/table counts gated against the last-good manifest: name -> (file, table)
COUNT_TABLES = {
    "papers": ("retarats_pubmed.sqlite", "papers"),
    "evidence": ("retarats_pubmed.sqlite", "evidence"),
    "trials": ("retarats_trials.sqlite", "trials"),
    "preprints": ("retarats_preprints.sqlite", "preprints"),
}

STAGE_MARKER = ".staged.json"
BASELINE_STATS = ".baseline_stats.json"  # last-good corpus_stats subset, for validate_curated (not archived)
_ID = r"(\d{8}T\d{4}-[0-9A-Za-z]{3,40})"
ARCHIVE_RE = re.compile(rf"^corpus-{_ID}\.tar\.gz$")
MANIFEST_RE = re.compile(rf"^manifest-{_ID}\.json$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class CorpusStateError(Exception):
    """Base class for all explicit, expected failures."""


class StoreError(CorpusStateError):
    pass


class AssetExists(StoreError):
    pass


class ManifestError(CorpusStateError):
    pass


class RestoreError(CorpusStateError):
    pass


class PromotionError(CorpusStateError):
    """Promotion refused/failed. ``kind`` is one of gate|verification|stale_base|unstaged|store."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def archive_name(release_id: str) -> str:
    return f"corpus-{release_id}.tar.gz"


def manifest_name(release_id: str) -> str:
    return f"manifest-{release_id}.json"


def make_release_id(now: Optional[datetime] = None, sha: str = "") -> str:
    """``YYYYMMDDTHHMM-<sha7>`` (sha7 from the commit; ``local`` when unknown)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tag = re.sub(r"[^0-9A-Za-z]", "", sha or "")[:7] or "local"
    return f"{now:%Y%m%dT%H%M}-{tag}"


def _iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- hashing / sqlite ---------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _tables(conn: sqlite3.Connection) -> List[str]:
    return sorted(r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"))


def sqlite_table_counts(path: str) -> Dict[str, int]:
    conn = _ro(path)
    try:
        return {t: conn.execute(f'select count(*) from "{t}"').fetchone()[0] for t in _tables(conn)}
    finally:
        conn.close()


def sqlite_quick_check(path: str) -> bool:
    try:
        conn = _ro(path)
        try:
            return conn.execute("pragma quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def sqlite_fingerprint(path: str) -> str:
    """Order-independent content hash of every table (schema + rows).

    Independent of file layout (VACUUM, page order, WAL state), so two byte-different
    files holding the same rows fingerprint identically; any row change alters it."""
    conn = _ro(path)
    try:
        outer = hashlib.sha256()
        for t in _tables(conn):
            cols = [r[1] for r in conn.execute(f'pragma table_info("{t}")')]
            acc, n = 0, 0
            for row in conn.execute(f'select * from "{t}"'):
                acc = (acc + int.from_bytes(hashlib.sha256(repr(row).encode("utf-8", "surrogatepass")).digest(),
                                            "big")) % (1 << 256)
                n += 1
            outer.update(f"{t}|{','.join(cols)}|{n}|{acc:064x}\n".encode())
        return outer.hexdigest()
    finally:
        conn.close()


def logical_fingerprint(work_dir: str) -> str:
    """Fingerprint of the logical corpus (all sqlite payloads) — 16 hex chars."""
    outer = hashlib.sha256()
    for name in SQLITE_FILES:
        p = os.path.join(work_dir, name)
        outer.update(f"{name}:{sqlite_fingerprint(p) if os.path.isfile(p) else 'absent'}\n".encode())
    return outer.hexdigest()[:16]


def file_entries(work_dir: str) -> Dict[str, dict]:
    entries: Dict[str, dict] = {}
    for name in PAYLOAD_FILES:
        p = os.path.join(work_dir, name)
        if not os.path.isfile(p):
            continue
        e = {"sha256": sha256_file(p), "bytes": os.path.getsize(p)}
        if name.endswith(".sqlite"):
            e["tables"] = sqlite_table_counts(p)
        entries[name] = e
    return entries


def counts_from_entries(entries: Dict[str, dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for key, (fname, table) in COUNT_TABLES.items():
        n = (entries.get(fname) or {}).get("tables", {}).get(table)
        if n is not None:
            out[key] = int(n)
    return out


# --- stores -------------------------------------------------------------------

class Store:
    """Minimal asset store. put() never overwrites unless ``overwrite=True``."""

    def list_assets(self) -> List[str]:
        raise NotImplementedError

    def put_file(self, name: str, path: str, overwrite: bool = False) -> None:
        raise NotImplementedError

    def get_file(self, name: str, dest: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    def read_json(self, name: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, name)
            self.get_file(name, p)
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        if not isinstance(data, dict):
            raise StoreError(f"{name} is not a JSON object")
        return data

    def put_json(self, name: str, obj: dict, overwrite: bool = False) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, name)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, indent=2, sort_keys=True)
            self.put_file(name, p, overwrite=overwrite)


class LocalStore(Store):
    """Directory-backed store (local runs, tests). Writes are tmp + atomic rename."""

    def __init__(self, root: str) -> None:
        self.root = root

    def list_assets(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(n for n in os.listdir(self.root)
                      if os.path.isfile(os.path.join(self.root, n)) and not n.endswith(".partial"))

    def put_file(self, name: str, path: str, overwrite: bool = False) -> None:
        os.makedirs(self.root, exist_ok=True)
        final = os.path.join(self.root, name)
        if os.path.exists(final) and not overwrite:
            raise AssetExists(f"{name} already exists in the store (versioned assets are immutable)")
        tmp = final + ".partial"
        shutil.copyfile(path, tmp)
        os.replace(tmp, final)

    def get_file(self, name: str, dest: str) -> None:
        src = os.path.join(self.root, name)
        if not os.path.isfile(src):
            raise StoreError(f"{name} not found in the store")
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        shutil.copyfile(src, dest)

    def delete(self, name: str) -> None:
        p = os.path.join(self.root, name)
        if os.path.exists(p):
            os.remove(p)


Runner = Callable[[List[str]], Tuple[int, str, str]]


def _gh_runner(args: List[str]) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return p.returncode, p.stdout, p.stderr


class GitHubReleaseStore(Store):
    """Assets of one GitHub Release, via the ``gh`` CLI.

    Versioned assets are uploaded WITHOUT ``--clobber`` (gh refuses to replace them);
    only the tiny pointer is ever uploaded with ``overwrite=True``. An unreachable
    release/API raises StoreError — it is never mistaken for an empty store."""

    NOTES = ("Durable corpus store for the RetaBase pipeline (versioned corpus archives, manifests, "
             "last-good.json pointer). Managed by scripts/corpus_state.py — do not edit by hand.")

    def __init__(self, repo: str, tag: str = STORE_TAG, runner: Optional[Runner] = None,
                 retries: int = 2, sleep: Callable[[float], None] = time.sleep) -> None:
        self.repo, self.tag = repo, tag
        self.runner = runner or _gh_runner
        self.retries, self.sleep = retries, sleep

    def _run(self, args: List[str], ok_if: Optional[str] = None) -> Tuple[int, str, str]:
        rc, out, err = 1, "", ""
        for attempt in range(self.retries + 1):
            rc, out, err = self.runner(args)
            if rc == 0 or (ok_if and ok_if in err.lower()):
                return rc, out, err
            if attempt < self.retries:
                self.sleep(2 ** attempt * 2)
        return rc, out, err

    def list_assets(self) -> List[str]:
        rc, out, err = self._run(["release", "view", self.tag, "--repo", self.repo, "--json", "assets"],
                                 ok_if="release not found")
        if rc != 0:
            if "release not found" in err.lower():
                return []
            raise StoreError(f"cannot list release {self.tag}: {err.strip()[:300]}")
        try:
            return sorted(a["name"] for a in json.loads(out).get("assets", []))
        except (ValueError, KeyError, TypeError) as exc:
            raise StoreError(f"unparseable release listing: {exc}") from exc

    def _ensure_release(self) -> None:
        rc, _, err = self._run(["release", "view", self.tag, "--repo", self.repo, "--json", "tagName"],
                               ok_if="release not found")
        if rc == 0:
            return
        if "release not found" not in err.lower():
            raise StoreError(f"cannot reach release {self.tag}: {err.strip()[:300]}")
        rc, _, err = self._run(["release", "create", self.tag, "--repo", self.repo, "--title",
                                "RetaBase corpus store", "--notes", self.NOTES, "--latest=false"])
        if rc != 0:
            raise StoreError(f"cannot create release {self.tag}: {err.strip()[:300]}")

    def put_file(self, name: str, path: str, overwrite: bool = False) -> None:
        self._ensure_release()
        if not overwrite and name in self.list_assets():
            raise AssetExists(f"{name} already exists in release {self.tag}")
        with tempfile.TemporaryDirectory() as td:
            staged = os.path.join(td, name)
            shutil.copyfile(path, staged)
            args = ["release", "upload", self.tag, staged, "--repo", self.repo]
            if overwrite:
                args.append("--clobber")
            rc, _, err = self._run(args)
        if rc != 0:
            raise StoreError(f"upload of {name} failed: {err.strip()[:300]}")

    def get_file(self, name: str, dest: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc, _, err = self._run(["release", "download", self.tag, "--repo", self.repo,
                                    "--pattern", name, "--dir", td, "--clobber"])
            src = os.path.join(td, name)
            if rc != 0 or not os.path.isfile(src):
                raise StoreError(f"download of {name} failed: {err.strip()[:300]}")
            os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
            shutil.move(src, dest)

    def delete(self, name: str) -> None:
        rc, _, err = self._run(["release", "delete-asset", self.tag, name, "--repo", self.repo, "--yes"])
        if rc != 0:
            raise StoreError(f"delete of {name} failed: {err.strip()[:300]}")


# --- manifest / pointer ---------------------------------------------------------

def validate_manifest(m: dict, expect_id: Optional[str] = None) -> dict:
    try:
        if m.get("schema") != MANIFEST_SCHEMA:
            raise ManifestError(f"unsupported manifest schema {m.get('schema')!r}")
        rid = m.get("release_id", "")
        if not RELEASE_ID_RE.match(str(rid)):
            raise ManifestError(f"bad release_id {rid!r}")
        if expect_id and rid != expect_id:
            raise ManifestError(f"manifest names release {rid}, expected {expect_id}")
        arch = m.get("archive") or {}
        if arch.get("name") != archive_name(rid) or not SHA_RE.match(str(arch.get("sha256", ""))):
            raise ManifestError("archive name/sha256 missing or inconsistent")
        files = m.get("files")
        if not isinstance(files, dict) or not set(REQUIRED_FILES) <= set(files):
            raise ManifestError("manifest lacks required corpus files")
        for name, e in files.items():
            if name not in PAYLOAD_FILES or not SHA_RE.match(str(e.get("sha256", ""))):
                raise ManifestError(f"bad file entry {name!r}")
    except AttributeError as exc:
        raise ManifestError(f"malformed manifest: {exc}") from exc
    return m


def load_manifest(store: Store, release_id: str) -> dict:
    try:
        m = store.read_json(manifest_name(release_id))
    except (StoreError, ValueError, OSError) as exc:
        raise ManifestError(f"cannot read manifest for {release_id}: {exc}") from exc
    return validate_manifest(m, release_id)


def read_pointer(store: Store, assets: Optional[List[str]] = None) -> Optional[dict]:
    """Current pointer, or None if absent/unparseable. StoreError propagates (unreachable)."""
    assets = store.list_assets() if assets is None else assets
    if POINTER_NAME not in assets:
        return None
    try:
        p = store.read_json(POINTER_NAME)
    except (StoreError, ValueError, OSError) as exc:
        _log(f"WARN: pointer {POINTER_NAME} unreadable ({exc}); will fall back to manifest scan")
        return None
    ok = (RELEASE_ID_RE.match(str(p.get("release_id", ""))) and ARCHIVE_RE.match(str(p.get("asset", "")))
          and SHA_RE.match(str(p.get("sha256", ""))))
    if not ok:
        _log("WARN: pointer malformed; will fall back to manifest scan")
        return None
    return p


def manifest_release_ids(assets: Iterable[str]) -> List[str]:
    """Release ids that have a published manifest, newest first."""
    ids = [m.group(1) for m in (MANIFEST_RE.match(a) for a in assets) if m]
    return sorted(ids, reverse=True)


# --- staging --------------------------------------------------------------------

def _prepare_stage_dir(work_dir: str) -> None:
    """Empty (or create) the staging dir. Refuses a non-empty dir we did not create,
    so a mistyped --dest can never wipe real data."""
    if os.path.isdir(work_dir) and os.listdir(work_dir):
        if not os.path.isfile(os.path.join(work_dir, STAGE_MARKER)):
            raise CorpusStateError(f"refusing to stage into non-empty directory {work_dir} "
                                   f"(no {STAGE_MARKER}); pick an empty or previously staged dir")
        shutil.rmtree(work_dir)
    os.makedirs(work_dir, exist_ok=True)


def _write_stage_marker(work_dir: str, info: dict) -> None:
    with open(os.path.join(work_dir, STAGE_MARKER), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=2, sort_keys=True)


def read_stage_marker(work_dir: str) -> Optional[dict]:
    p = os.path.join(work_dir, STAGE_MARKER)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _copy_verified(src: str, dest: str, want_sha: str) -> None:
    shutil.copyfile(src, dest)
    if sha256_file(dest) != want_sha:
        os.remove(dest)
        raise RestoreError(f"checksum mismatch after copying {os.path.basename(dest)}")


def _cache_matches(cache_dir: Optional[str], manifest: dict) -> bool:
    if not cache_dir or not os.path.isdir(cache_dir):
        return False
    for name, e in manifest["files"].items():
        p = os.path.join(cache_dir, name)
        if not os.path.isfile(p) or os.path.getsize(p) != e.get("bytes", -1) or sha256_file(p) != e["sha256"]:
            return False
    return True


def _extract_verified(archive_path: str, manifest: dict, dest_dir: str) -> None:
    """Extract only manifest-listed flat regular files; verify each sha256."""
    want = manifest["files"]
    seen = set()
    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf:
            if not member.isreg() or member.name not in want or member.name in seen:
                raise RestoreError(f"unexpected archive member {member.name!r}")
            out = os.path.join(dest_dir, member.name)
            h = hashlib.sha256()
            src = tf.extractfile(member)
            with open(out, "wb") as fh:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    h.update(chunk)
                    fh.write(chunk)
            if h.hexdigest() != want[member.name]["sha256"]:
                raise RestoreError(f"checksum mismatch for {member.name} inside archive")
            seen.add(member.name)
    if seen != set(want):
        raise RestoreError(f"archive is missing {sorted(set(want) - seen)}")


class RestoreResult:
    def __init__(self, source: str, release_id: Optional[str], manifest: Optional[dict],
                 pointer_release_id: Optional[str]) -> None:
        self.source, self.release_id, self.manifest = source, release_id, manifest
        self.pointer_release_id = pointer_release_id

    def __repr__(self) -> str:
        return f"RestoreResult(source={self.source!r}, release_id={self.release_id!r})"


def restore(store: Store, work_dir: str, cache_dir: Optional[str] = None, bootstrap: bool = False,
            seed_dir: Optional[str] = None, log: Callable[[str], None] = _log) -> RestoreResult:
    """Stage the last-good corpus into ``work_dir``. See module docstring for order.

    Raises RestoreError when nothing valid can be restored. Never fabricates state
    unless ``bootstrap`` is set and the store holds no corpus state at all."""
    assets = store.list_assets()  # StoreError (unreachable) propagates: explicit failure
    pointer = read_pointer(store, assets)
    order: List[str] = []
    if pointer:
        order.append(pointer["release_id"])
    order += [r for r in manifest_release_ids(assets) if r not in order]
    holds_state = bool(order) or POINTER_NAME in assets or any(ARCHIVE_RE.match(a) for a in assets)
    parent = os.path.dirname(os.path.abspath(work_dir)) or "."
    os.makedirs(parent, exist_ok=True)

    for rid in order:
        try:
            manifest = load_manifest(store, rid)
        except ManifestError as exc:
            log(f"restore: skipping {rid}: {exc}")
            continue
        if pointer and rid == pointer["release_id"] and pointer["sha256"] != manifest["archive"]["sha256"]:
            log(f"restore: skipping {rid}: pointer sha256 disagrees with its manifest")
            continue
        tmp = tempfile.mkdtemp(prefix=".restore-", dir=parent)
        try:
            source = "last-good" if pointer and rid == pointer["release_id"] else "previous-good"
            if _cache_matches(cache_dir, manifest):
                log(f"restore: cache matches manifest {rid}; using cache (acceleration only)")
                for name, e in manifest["files"].items():
                    _copy_verified(os.path.join(cache_dir, name), os.path.join(tmp, name), e["sha256"])
                source = "cache"
            else:
                arch = manifest["archive"]["name"]
                if arch not in assets:
                    raise RestoreError(f"archive {arch} missing from store")
                ap = os.path.join(tmp, "_archive.tar.gz")
                store.get_file(arch, ap)
                if sha256_file(ap) != manifest["archive"]["sha256"]:
                    raise RestoreError(f"archive {arch} sha256 does not match its manifest")
                _extract_verified(ap, manifest, tmp)
                os.remove(ap)
        except (RestoreError, StoreError, tarfile.TarError, OSError, EOFError) as exc:
            log(f"restore: {rid} unusable ({exc}); trying an older release")
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        _prepare_stage_dir(work_dir)
        for name in manifest["files"]:
            os.replace(os.path.join(tmp, name), os.path.join(work_dir, name))
        shutil.rmtree(tmp, ignore_errors=True)
        _write_stage_marker(work_dir, {"base_release_id": rid, "restored_from": source, "bootstrap": False,
                                       "pointer_release_id_at_restore": pointer["release_id"] if pointer else None})
        with open(os.path.join(work_dir, BASELINE_STATS), "w", encoding="utf-8") as fh:
            json.dump(manifest.get("stats_baseline") or {}, fh, indent=2, sort_keys=True)
        log(f"restore: staged release {rid} from {source} -> {work_dir}")
        return RestoreResult(source, rid, manifest, pointer["release_id"] if pointer else None)

    if holds_state:
        raise RestoreError("the corpus store holds state but no release could be verified and restored; "
                           "refusing to fabricate a corpus (bootstrap is only allowed on an EMPTY store). "
                           "Inspect the store; remove the bad pointer/assets manually if intended.")
    if not bootstrap:
        raise RestoreError("no valid cache and no last-good corpus in the store. Refusing to start from a "
                           "hollow corpus. If this is the first run, re-run in manual bootstrap mode "
                           "(--bootstrap [--seed-dir DIR]).")
    _prepare_stage_dir(work_dir)
    if seed_dir:
        seeded = [n for n in PAYLOAD_FILES if os.path.isfile(os.path.join(seed_dir, n))]
        main_db = os.path.join(seed_dir, REQUIRED_FILES[0])
        if REQUIRED_FILES[0] not in seeded or not sqlite_quick_check(main_db) \
                or sqlite_table_counts(main_db).get("papers", 0) <= 0:
            raise RestoreError(f"seed dir {seed_dir} has no usable {REQUIRED_FILES[0]} (need intact papers table)")
        for name in seeded:
            shutil.copyfile(os.path.join(seed_dir, name), os.path.join(work_dir, name))
        log(f"restore: BOOTSTRAP seeded from {seed_dir}: {seeded}")
    else:
        log("restore: BOOTSTRAP with an EMPTY corpus (manual bootstrap requested)")
    _write_stage_marker(work_dir, {"base_release_id": None, "restored_from": "bootstrap", "bootstrap": True,
                                   "pointer_release_id_at_restore": None})
    return RestoreResult("bootstrap", None, None, None)


# --- promotion -------------------------------------------------------------------

def make_archive(work_dir: str, entries: Dict[str, dict], out_path: str) -> None:
    """Deterministic gzip'd tar (fixed mtimes/owners, sorted members)."""
    with open(out_path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
                for name in sorted(entries):
                    p = os.path.join(work_dir, name)
                    ti = tarfile.TarInfo(name)
                    ti.size, ti.mtime, ti.mode = os.path.getsize(p), 0, 0o644
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    with open(p, "rb") as fh:
                        tf.addfile(ti, fh)


def _stats_baseline(site_dir: str) -> dict:
    """Compact corpus_stats subset kept as the durable validation baseline."""
    keep = ("total_papers", "total_evidence", "molecules_with_data", "featured", "listed",
            "records_indexed", "corpus_fingerprint")
    try:
        with open(os.path.join(site_dir, "site_data.json"), encoding="utf-8") as fh:
            cs = json.load(fh).get("corpus_stats") or {}
    except (OSError, ValueError):
        return {}
    return {k: cs[k] for k in keep if k in cs}


def _sources(work_dir: str) -> dict:
    try:
        with open(os.path.join(work_dir, "source_status.json"), encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("sources", {})
    except (OSError, ValueError):
        return {}


def gate_candidate(entries: Dict[str, dict], work_dir: str, base: Optional[dict],
                   max_drop: float = DEFAULT_MAX_DROP, allow_shrink: bool = False) -> List[str]:
    """Corpus-level gates. Returns a list of failure messages (empty = pass)."""
    errs: List[str] = []
    for name in REQUIRED_FILES:
        if name not in entries:
            errs.append(f"required corpus file missing: {name}")
    for name in entries:
        if name.endswith(".sqlite") and not sqlite_quick_check(os.path.join(work_dir, name)):
            errs.append(f"{name} failed sqlite integrity check")
    counts = counts_from_entries(entries)
    if counts.get("papers", 0) <= 0:
        errs.append("candidate corpus has 0 papers (hollow corpus)")
    if base is not None and not allow_shrink:
        for name in base["files"]:
            if name.endswith(".sqlite") and name not in entries:
                errs.append(f"{name} was in last-good {base['release_id']} but is missing from the candidate")
        bcounts = base.get("counts", {})
        for key, bval in bcounts.items():
            if not isinstance(bval, int) or bval <= 0:
                continue
            cur = counts.get(key, 0)
            floor = int(bval * (1 - max_drop))
            if cur < floor:
                errs.append(f"{key} collapsed: {cur} < {floor} (last-good {bval}, max drop {max_drop:.0%})")
    return errs


class PromoteResult:
    def __init__(self, release_id: str, manifest: dict, pruned: List[str]) -> None:
        self.release_id, self.manifest, self.pruned = release_id, manifest, pruned


def promote(store: Store, work_dir: str, site_dir: str, release_id: str, cache_dir: Optional[str] = None,
            keep: int = DEFAULT_KEEP, build_sha: str = "", now: Optional[datetime] = None,
            max_drop: float = DEFAULT_MAX_DROP, allow_shrink: bool = False,
            log: Callable[[str], None] = _log) -> PromoteResult:
    """Validate + verify a staged candidate, then (only then) advance the pointer."""
    if not RELEASE_ID_RE.match(release_id):
        raise PromotionError("gate", f"malformed release_id {release_id!r}")
    marker = read_stage_marker(work_dir)
    if marker is None:
        raise PromotionError("unstaged", f"{work_dir} was not staged by restore(); every writer must go through "
                                         "restore -> stage -> mutate -> build -> validate -> promote")
    try:
        assets = store.list_assets()
        pointer = read_pointer(store, assets)
    except StoreError as exc:
        raise PromotionError("store", f"cannot read the corpus store: {exc}") from exc
    now_pointer = pointer["release_id"] if pointer else None
    if now_pointer != marker.get("pointer_release_id_at_restore"):
        raise PromotionError("stale_base", f"last-good moved from {marker.get('pointer_release_id_at_restore')} "
                                           f"to {now_pointer} since this run restored (concurrent writer?); "
                                           "refusing to overwrite it")
    if manifest_name(release_id) in assets or archive_name(release_id) in assets:
        raise PromotionError("gate", f"release {release_id} already exists in the store")

    base: Optional[dict] = None
    if marker.get("base_release_id"):
        try:
            base = load_manifest(store, marker["base_release_id"])
        except ManifestError as exc:
            raise PromotionError("gate", f"cannot load baseline manifest {marker['base_release_id']}: {exc}") from exc
    elif not marker.get("bootstrap"):
        raise PromotionError("unstaged", "stage marker has neither a base release nor bootstrap")
    elif now_pointer is not None:
        raise PromotionError("stale_base", "bootstrap candidate but the store already has a last-good")

    # 1) validate ----------------------------------------------------------------
    entries = file_entries(work_dir)
    errs = gate_candidate(entries, work_dir, base, max_drop, allow_shrink)
    vres = validate_release(site_dir, release_id, baseline=(base or {}).get("site") if not allow_shrink else None,
                            max_drop=max_drop)
    errs += vres.errors
    if errs:
        raise PromotionError("gate", "candidate rejected; last-good untouched:\n  - " + "\n  - ".join(errs))
    log("promote: candidate passed release validation and corpus gates")

    # 2) build the candidate manifest + archive -----------------------------------
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "release_id": release_id,
        "created_utc": _iso(now),
        "build_sha": build_sha or "local",
        "base_release_id": marker.get("base_release_id"),
        "bootstrap": bool(marker.get("bootstrap")),
        "corpus_fingerprint": logical_fingerprint(work_dir),
        "files": entries,
        "counts": counts_from_entries(entries),
        "site": site_counts(vres),
        "stats_baseline": _stats_baseline(site_dir),
        "sources": _sources(work_dir),
    }
    tmpdir = tempfile.mkdtemp(prefix=".promote-", dir=os.path.dirname(os.path.abspath(work_dir)) or ".")
    try:
        apath = os.path.join(tmpdir, archive_name(release_id))
        make_archive(work_dir, entries, apath)
        manifest["archive"] = {"name": archive_name(release_id), "sha256": sha256_file(apath),
                               "bytes": os.path.getsize(apath)}
        validate_manifest(manifest, release_id)

        # 3) upload as a NEW versioned asset, pull back, verify ----------------------
        try:
            store.put_file(archive_name(release_id), apath, overwrite=False)
        except StoreError as exc:
            raise PromotionError("store", f"candidate upload failed; last-good untouched: {exc}") from exc
        back = os.path.join(tmpdir, "verify.tar.gz")
        try:
            store.get_file(archive_name(release_id), back)
            got = sha256_file(back)
        except (StoreError, OSError) as exc:
            _drop_candidate(store, release_id, log)
            raise PromotionError("verification", f"could not download the candidate back: {exc}") from exc
        if got != manifest["archive"]["sha256"]:
            _drop_candidate(store, release_id, log)
            raise PromotionError("verification", f"uploaded archive checksum mismatch (expected "
                                                 f"{manifest['archive']['sha256']}, got {got}); last-good untouched")
        log("promote: candidate archive uploaded and checksum-verified")

        # 4) publish the manifest (marks the asset as a verified, restorable release)
        try:
            store.put_json(manifest_name(release_id), manifest, overwrite=False)
            # 5) ONLY NOW advance the pointer -------------------------------------------
            store.put_json(POINTER_NAME, {
                "schema": MANIFEST_SCHEMA, "release_id": release_id, "asset": archive_name(release_id),
                "sha256": manifest["archive"]["sha256"], "manifest": manifest_name(release_id),
                "promoted_utc": _iso(now)}, overwrite=True)
        except StoreError as exc:
            raise PromotionError("store", f"could not publish manifest/pointer ({exc}); the previous pointer "
                                          "remains in effect") from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    try:
        confirmed = read_pointer(store)
    except StoreError as exc:
        raise PromotionError("store", f"could not read the pointer back ({exc}); treat as NOT promoted") from exc
    if not confirmed or confirmed["release_id"] != release_id:
        raise PromotionError("store", "pointer read-back does not name the new release; treat as NOT promoted")
    log(f"promote: last-good pointer advanced -> {release_id}")

    # 6) acceleration + housekeeping (failures here never un-promote) ---------------
    if cache_dir:
        try:
            refresh_cache(cache_dir, work_dir, entries)
        except OSError as exc:
            log(f"WARN: cache refresh failed (non-fatal): {exc}")
    pruned: List[str] = []
    try:
        pruned = prune(store, keep=keep, log=log)
    except CorpusStateError as exc:
        log(f"WARN: prune failed (non-fatal): {exc}")
    return PromoteResult(release_id, manifest, pruned)


def _drop_candidate(store: Store, release_id: str, log: Callable[[str], None]) -> None:
    try:
        store.delete(archive_name(release_id))
        log(f"promote: removed unverified candidate {archive_name(release_id)}")
    except CorpusStateError as exc:
        log(f"WARN: could not remove unverified candidate asset ({exc}); it has no manifest so it can never be restored")


def refresh_cache(cache_dir: str, work_dir: str, entries: Dict[str, dict]) -> None:
    """Copy the promoted payload into the (acceleration-only) cache dir, atomically per file."""
    os.makedirs(cache_dir, exist_ok=True)
    for name in entries:
        tmp = os.path.join(cache_dir, name + ".tmp")
        shutil.copyfile(os.path.join(work_dir, name), tmp)
        os.replace(tmp, os.path.join(cache_dir, name))


def prune(store: Store, keep: int = DEFAULT_KEEP, protect: Iterable[str] = (),
          log: Callable[[str], None] = _log) -> List[str]:
    """Delete corpus assets of releases older than the newest ``keep`` (plus orphans).

    NEVER deletes the release the pointer references, ``last-good.json``, or anything
    that is not a corpus-<id>.tar.gz / manifest-<id>.json asset. If the pointer cannot
    be identified while the store holds releases, nothing is deleted."""
    if keep < 1:
        raise CorpusStateError("prune keep must be >= 1")
    assets = store.list_assets()
    pointer = read_pointer(store, assets)
    ids = manifest_release_ids(assets)
    archive_ids = [m.group(1) for m in (ARCHIVE_RE.match(a) for a in assets) if m]
    if (ids or archive_ids) and pointer is None:
        log("prune: no readable pointer; skipping prune (cannot tell which release is protected)")
        return []
    keep_set = set(ids[:keep]) | set(protect)
    if pointer:
        keep_set.add(pointer["release_id"])
    deleted: List[str] = []
    for rid in sorted(set(ids) | set(archive_ids)):
        if rid in keep_set:
            continue
        for name in (manifest_name(rid), archive_name(rid)):  # manifest first: never leave a dangling manifest
            if name in assets:
                store.delete(name)
                deleted.append(name)
    if deleted:
        log(f"prune: removed {deleted}")
    return deleted


# --- CI fault simulation (acts on the staged/built copies only) ---------------------

def simulate_fault(kind: str, work_dir: str, site_dir: str) -> str:
    """Inject a fault into the STAGED work dir / built site so a real run can prove that
    promotion is refused and last-good is untouched. Never touches the store."""
    if kind == "empty_trials":
        p = os.path.join(work_dir, "retarats_trials.sqlite")
        conn = sqlite3.connect(p)
        try:
            if "trials" not in _tables(conn):
                conn.execute("create table trials (nct_id text primary key, payload_json text)")
            conn.execute("delete from trials")
            conn.commit()
        finally:
            conn.close()
        return "emptied the staged trials table"
    if kind == "mixed_release":
        shards = sorted(f for f in os.listdir(site_dir) if re.match(r"^site_records_\d{3}\.json$", f))
        target = os.path.join(site_dir, shards[0] if shards else "site_detail.json")
        with open(target, encoding="utf-8") as fh:
            d = json.load(fh)
        d["release_id"] = "19700101T0000-stale00"
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(d, fh, separators=(",", ":"))
        return f"re-stamped {os.path.basename(target)} with a stale release_id"
    raise CorpusStateError(f"unknown simulation {kind!r} (empty_trials|mixed_release)")
