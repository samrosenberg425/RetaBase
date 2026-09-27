"""Per-source freshness policy + status.json foundation (WS3-core).

``config/source_policy.csv`` declares, per upstream source, how long its data may go
without a successful refresh before it is labelled *degraded* and then *failed*, and
whether stale last-good data may still be deployed. Fetch steps record what happened
in ``source_status.json`` (durable state, carried inside the corpus archive) via
``record_run``; ``build_status`` turns that record + the policy into the public
``status.json`` shipped with every release.

Stale data is never discarded: a failed source still ships its last-good data with a
degraded/failed label. This module only *labels*; it never blocks a deploy (that
decision lives in the release gates).

Pure stdlib, no network, deterministic given an explicit ``now``.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_PATH = os.path.join(ROOT, "config", "source_policy.csv")
POLICY_COLUMNS = ["source", "cadence", "degraded_after_days", "failed_after_days",
                  "deploy_stale_last_good", "description"]

CURRENT, DEGRADED, FAILED, UNKNOWN = "current", "degraded", "failed", "unknown"
COUNT_KEYS = ("new", "updated", "unchanged", "live", "cache", "error")
STATUS_SCHEMA = 1


class PolicyError(ValueError):
    pass


def _utc(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text) -> Optional[datetime]:
    if not text or not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_policy(path: str = POLICY_PATH) -> Dict[str, dict]:
    """Load + validate the policy. Raises PolicyError on any malformed row."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != POLICY_COLUMNS:
                raise PolicyError(f"{path}: columns {reader.fieldnames} != {POLICY_COLUMNS}")
            rows = list(reader)
    except OSError as exc:
        raise PolicyError(f"cannot read source policy {path}: {exc}") from exc
    policy: Dict[str, dict] = {}
    for i, r in enumerate(rows, start=2):
        name = (r["source"] or "").strip()
        if not name or name in policy:
            raise PolicyError(f"{path}:{i}: blank or duplicate source {name!r}")
        try:
            deg, fail = int(r["degraded_after_days"]), int(r["failed_after_days"])
        except (TypeError, ValueError) as exc:
            raise PolicyError(f"{path}:{i}: non-integer threshold for {name}") from exc
        if not (0 < deg < fail):
            raise PolicyError(f"{path}:{i}: need 0 < degraded ({deg}) < failed ({fail}) for {name}")
        stale_ok = (r["deploy_stale_last_good"] or "").strip().lower()
        if stale_ok not in ("yes", "no"):
            raise PolicyError(f"{path}:{i}: deploy_stale_last_good must be yes/no for {name}")
        policy[name] = {"cadence": r["cadence"].strip(), "degraded_after_days": deg,
                        "failed_after_days": fail, "deploy_stale_last_good": stale_ok == "yes",
                        "description": r["description"].strip()}
    if not policy:
        raise PolicyError(f"{path}: no sources defined")
    return policy


# --- durable per-source run record ------------------------------------------

def load_runs(path: Optional[str]) -> dict:
    if not path or not os.path.exists(path):
        return {"schema": STATUS_SCHEMA, "sources": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"schema": STATUS_SCHEMA, "sources": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return {"schema": STATUS_SCHEMA, "sources": {}}
    return data


def record_run(path: str, source: str, ok: bool, now: Optional[datetime] = None,
               counts: Optional[dict] = None, error: str = "",
               policy: Optional[Dict[str, dict]] = None) -> dict:
    """Record one attempt of ``source``. A success advances last_success; a failure
    only advances last_attempted (and stores the error) so staleness keeps growing."""
    policy = policy if policy is not None else load_policy()
    if source not in policy:
        raise PolicyError(f"unknown source {source!r}; known: {sorted(policy)}")
    stamp = _iso(_utc(now))
    data = load_runs(path)
    entry = dict(data["sources"].get(source) or {})
    entry["last_attempted_utc"] = stamp
    entry["last_attempt_ok"] = bool(ok)
    if ok:
        entry["last_success_utc"] = stamp
        entry["error"] = ""
    else:
        entry["error"] = (error or "failed")[:500]
    if counts:
        entry["counts"] = {k: int(counts[k]) for k in COUNT_KEYS if k in counts}
    data["sources"][source] = entry
    data["schema"] = STATUS_SCHEMA
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return entry


# --- evaluation ---------------------------------------------------------------

def evaluate_source(rule: dict, entry: Optional[dict], now: datetime) -> dict:
    """Status of one source from its policy rule + recorded run entry."""
    entry = entry or {}
    last_ok = _parse(entry.get("last_success_utc"))
    if last_ok is None:
        status, age = UNKNOWN, None
    else:
        age = max(0, (now - last_ok).days)
        status = (FAILED if age >= rule["failed_after_days"]
                  else DEGRADED if age >= rule["degraded_after_days"] else CURRENT)
    return {
        "cadence": rule["cadence"],
        "status": status,
        "as_of_utc": entry.get("last_success_utc", ""),
        "last_attempted_utc": entry.get("last_attempted_utc", ""),
        "last_success_utc": entry.get("last_success_utc", ""),
        "last_attempt_ok": entry.get("last_attempt_ok"),
        "age_days": age,
        "degraded_after_days": rule["degraded_after_days"],
        "failed_after_days": rule["failed_after_days"],
        "deploy_stale_last_good": rule["deploy_stale_last_good"],
        "counts": entry.get("counts", {}),
        "error": entry.get("error", ""),
    }


def overall_status(sources: Dict[str, dict]) -> str:
    states = {s["status"] for s in sources.values()}
    return FAILED if FAILED in states else DEGRADED if DEGRADED in states else CURRENT


def build_status(release_id: str, runs_path: Optional[str] = None,
                 policy: Optional[Dict[str, dict]] = None, now: Optional[datetime] = None,
                 build_sha: str = "", corpus_fingerprint: str = "") -> dict:
    policy = policy if policy is not None else load_policy()
    now = _utc(now)
    runs = load_runs(runs_path)["sources"]
    sources = {name: evaluate_source(rule, runs.get(name), now) for name, rule in sorted(policy.items())}
    return {
        "schema": STATUS_SCHEMA,
        "release_id": release_id,
        "generated_utc": _iso(now),
        "build_sha": build_sha,
        "corpus_fingerprint": corpus_fingerprint,
        "overall": overall_status(sources),
        "unknown_sources": sorted(n for n, s in sources.items() if s["status"] == UNKNOWN),
        "sources": sources,
    }


def write_status(path: str, status: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(status, fh, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def summary_lines(status: dict) -> List[str]:
    """Human-readable per-source lines (job summary / logs)."""
    out = [f"Data sources (release {status.get('release_id')}): overall {status.get('overall')}"]
    for name, s in status["sources"].items():
        age = "never" if s["age_days"] is None else f"{s['age_days']}d ago"
        out.append(f"  {name:28s} {s['status']:9s} last success {age}"
                   + (f"  error: {s['error']}" if s.get("error") else ""))
    return out
