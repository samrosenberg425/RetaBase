"""Release validation: required assets, one-release consistency, count-collapse gate.

A *release* is the set of public data assets deployed together:

    index.html, site_data.json, site_records_NNN.json (every shard listed in
    site_data.json), site_detail.json, trials_data.json, preprints_data.json,
    status.json

Every JSON asset carries the same ``release_id`` (stamped by the builders). This
module rejects a directory whose assets are missing, empty, unparseable, listed
inconsistently, or drawn from more than one release (e.g. a stale shard left over
from an earlier build). Given a baseline (the last-good manifest's ``site`` counts)
it also refuses a release whose feeds collapsed.

Pure stdlib; no network. Used by scripts/validate_release.py and, unconditionally,
by corpus_store.promote (promotion cannot skip validation).
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Dict, List, Optional

REQUIRED_HTML = ("index.html",)
REQUIRED_JSON = ("site_data.json", "site_detail.json", "trials_data.json",
                 "preprints_data.json", "status.json")
SHARD_RE = re.compile(r"^site_records_\d{3}\.json$")
RELEASE_ID_RE = re.compile(r"^\d{8}T\d{4}-[0-9A-Za-z]{3,40}$")
STATUS_VOCAB = {"current", "degraded", "failed"}
DEFAULT_MAX_DROP = 0.10  # feed counts may shrink by at most 10% vs last-good


class ValidationResult:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: Dict[str, object] = {}

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def render(self) -> str:
        lines = ["Release validation: " + ("PASS" if self.ok else "FAIL")]
        lines += [f"  [FAIL] {e}" for e in self.errors]
        lines += [f"  [WARN] {w}" for w in self.warnings]
        lines += [f"  [INFO] {k}: {v}" for k, v in sorted(self.info.items())]
        return "\n".join(lines)


def _load(path: str, res: ValidationResult, name: str) -> Optional[dict]:
    if not os.path.isfile(path):
        res.error(f"required asset missing: {name}")
        return None
    if os.path.getsize(path) == 0:
        res.error(f"required asset is empty: {name}")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        res.error(f"{name} is not parseable JSON: {exc}")
        return None
    if not isinstance(data, dict):
        res.error(f"{name} top-level value is not an object")
        return None
    return data


def _check_drop(res: ValidationResult, label: str, current: int, baseline, max_drop: float) -> None:
    try:
        base = int(baseline)
    except (TypeError, ValueError):
        return
    if base <= 0:
        return
    floor = int(base * (1 - max_drop))
    if current < floor:
        res.error(f"{label} collapsed: {current} < {floor} (last-good {base}, max drop {max_drop:.0%})")


def validate_release(site_dir: str, release_id: Optional[str] = None,
                     baseline: Optional[dict] = None,
                     max_drop: float = DEFAULT_MAX_DROP) -> ValidationResult:
    """Validate the release in ``site_dir``.

    ``release_id`` (optional) pins the expected id; otherwise every asset must merely
    agree with each other. ``baseline`` is a manifest ``site`` dict
    (``{"total_records", "trials", "preprints"}``) from the last-good release.
    """
    res = ValidationResult()
    if release_id is not None and not RELEASE_ID_RE.match(release_id):
        res.error(f"expected release_id {release_id!r} is malformed")

    for name in REQUIRED_HTML:
        p = os.path.join(site_dir, name)
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            res.error(f"required asset missing or empty: {name}")
        else:
            with open(p, encoding="utf-8", errors="replace") as fh:
                if "<html" not in fh.read(4096).lower():
                    res.error(f"{name} does not look like an HTML document")

    docs: Dict[str, dict] = {}
    for name in REQUIRED_JSON:
        d = _load(os.path.join(site_dir, name), res, name)
        if d is not None:
            docs[name] = d

    # Shards: exactly the ones site_data.json lists, no more, no fewer.
    shard_records = 0
    listed: List[str] = []
    sd = docs.get("site_data.json")
    if sd is not None:
        shards = sd.get("shards", [])
        if not isinstance(shards, list) or not all(isinstance(s, str) for s in shards):
            res.error("site_data.json 'shards' is not a list of file names")
            shards = []
        for name in shards:
            if not SHARD_RE.match(name):
                res.error(f"site_data.json lists an invalid shard name: {name!r}")
                continue
            listed.append(name)
            d = _load(os.path.join(site_dir, name), res, name)
            if d is None:
                continue
            docs[name] = d
            recs = d.get("records")
            if not isinstance(recs, list):
                res.error(f"{name} has no 'records' list")
            else:
                shard_records += len(recs)
        on_disk = sorted(os.path.basename(p) for p in glob.glob(os.path.join(site_dir, "site_records_*.json")))
        for name in on_disk:
            if name not in listed:
                res.error(f"orphan shard not listed in site_data.json (leftover from another build?): {name}")
        first = sd.get("records")
        if not isinstance(first, list):
            res.error("site_data.json has no 'records' list")
            first = []
        if sd.get("record_count") != len(first):
            res.error(f"site_data.json record_count {sd.get('record_count')} != {len(first)} inlined records")
        total = len(first) + shard_records
        if sd.get("total_records") != total:
            res.error(f"site_data.json total_records {sd.get('total_records')} != {total} "
                      f"(inlined {len(first)} + shards {shard_records})")
        if total == 0:
            res.error("feed is empty (0 records)")
        res.info["total_records"] = total
        _check_drop(res, "feed total_records", total, (baseline or {}).get("total_records"), max_drop)

    td, pd_ = docs.get("trials_data.json"), docs.get("preprints_data.json")
    for label, doc, key in (("trials", td, "trials"), ("preprints", pd_, "preprints")):
        if doc is None:
            continue
        rows = doc.get(key)
        if not isinstance(rows, list):
            res.error(f"{label}_data.json has no '{key}' list")
            continue
        if doc.get("count") != len(rows):
            res.error(f"{label}_data.json count {doc.get('count')} != {len(rows)} rows")
        res.info[label] = len(rows)
        _check_drop(res, f"{label} feed", len(rows), (baseline or {}).get(label), max_drop)

    st = docs.get("status.json")
    if st is not None:
        if st.get("overall") not in STATUS_VOCAB:
            res.error(f"status.json overall {st.get('overall')!r} not in {sorted(STATUS_VOCAB)}")
        if not isinstance(st.get("sources"), dict) or not st["sources"]:
            res.error("status.json has no 'sources'")

    # One release_id across every JSON asset (and corpus_stats inside site_data).
    stamps: Dict[str, str] = {}
    for name, d in docs.items():
        rid = d.get("release_id")
        if not rid:
            res.error(f"{name} carries no release_id (unstamped asset)")
        else:
            stamps[name] = str(rid)
    cs = (sd or {}).get("corpus_stats") if isinstance((sd or {}).get("corpus_stats"), dict) else {}
    if cs.get("release_id"):
        stamps["site_data.json:corpus_stats"] = str(cs["release_id"])
    distinct = sorted(set(stamps.values()))
    if len(distinct) > 1:
        by_id: Dict[str, List[str]] = {}
        for name, rid in stamps.items():
            by_id.setdefault(rid, []).append(name)
        detail = "; ".join(f"{rid}: {', '.join(sorted(n))}" for rid, n in sorted(by_id.items()))
        res.error(f"mixed release: assets carry {len(distinct)} different release_ids ({detail})")
    if release_id is not None and distinct:
        wrong = sorted(n for n, r in stamps.items() if r != release_id)
        if wrong and len(distinct) == 1:
            res.error(f"release_id mismatch: expected {release_id}, assets carry {distinct[0]}")
    if len(distinct) == 1:
        res.info["release_id"] = distinct[0]
    return res


def site_counts(res: ValidationResult) -> dict:
    """Feed counts for the manifest ``site`` block (call after a passing validation)."""
    return {k: int(res.info[k]) for k in ("total_records", "trials", "preprints") if k in res.info}
