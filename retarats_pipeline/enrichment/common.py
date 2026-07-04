from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

MISSING_VALUES = {
    "", "na", "n/a", "none", "null", "nan", "not reported", "not clearly reported",
    "unclear", "unknown", "not applicable", "not_applicable", "nr",
}

NCT_RE = re.compile(r"\bNCT\d{8}\b", re.I)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_dotenv_minimal(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class APIConfig:
    contact_email: str = "sr2007@rwjms.rutgers.edu"
    ncbi_email: str = "samrosenberg425@gmail.com"
    ncbi_api_key: str = ""
    tool_name: str = "retarats-enrichment"
    api_enabled: bool = True
    timeout_sec: int = 25
    min_interval_sec: float = 0.12
    cache_dir: str = "data/api_cache"
    user_agent: str = ""
    max_trial_search_results: int = 5
    max_pubtator_batch: int = 50

    @classmethod
    def from_env(cls, api_enabled: bool = True, cache_dir: str = "data/api_cache") -> "APIConfig":
        load_dotenv_minimal()
        contact = os.getenv("API_CONTACT_EMAIL") or os.getenv("UNPAYWALL_EMAIL") or "sr2007@rwjms.rutgers.edu"
        ncbi_email = os.getenv("NCBI_EMAIL") or "samrosenberg425@gmail.com"
        cfg = cls(
            contact_email=contact,
            ncbi_email=ncbi_email,
            ncbi_api_key=os.getenv("NCBI_API_KEY", ""),
            tool_name=os.getenv("NCBI_TOOL", "retarats-enrichment"),
            api_enabled=api_enabled,
            timeout_sec=int(os.getenv("API_TIMEOUT_SEC", "25")),
            min_interval_sec=float(os.getenv("API_MIN_INTERVAL_SEC", "0.12")),
            cache_dir=cache_dir,
        )
        cfg.user_agent = f"{cfg.tool_name}/0.1 (mailto:{cfg.contact_email})"
        return cfg


def ensure_dir(path: str | Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def is_blankish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        try:
            if value != value:  # NaN
                return True
        except Exception:
            pass
    text = str(value).strip()
    return text.lower() in MISSING_VALUES


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def split_semicolon(value: Any) -> List[str]:
    if is_blankish(value):
        return []
    parts = re.split(r"[;|]", str(value))
    return [p.strip() for p in parts if p.strip()]


def semicolon_join(values: Iterable[Any]) -> str:
    seen = set()
    out = []
    for v in values:
        if v is None:
            continue
        s = clean_text(v)
        if not s:
            continue
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return "; ".join(out)


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_payload_table(conn: sqlite3.Connection, table: str) -> List[dict]:
    rows: List[dict] = []
    try:
        cursor = conn.execute(f"select payload_json from {table}")
    except sqlite3.OperationalError:
        return rows
    for (payload_json,) in cursor:
        try:
            rows.append(json.loads(payload_json))
        except Exception:
            continue
    return rows


def save_payload_rows(
    conn: sqlite3.Connection,
    table: str,
    key_field: str,
    rows: Sequence[Mapping[str, Any]],
    updated_field: str = "enriched_at_utc",
) -> None:
    conn.execute(
        f"create table if not exists {table} ({key_field} text primary key, payload_json text, updated_at_utc text)"
    )
    payloads = []
    for row in rows:
        key = str(row.get(key_field, "")).strip()
        if not key:
            continue
        payloads.append((key, json_dumps(dict(row)), str(row.get(updated_field) or utc_now_iso())))
    if not payloads:
        return
    conn.executemany(
        f"insert into {table} ({key_field}, payload_json, updated_at_utc) values (?, ?, ?) "
        f"on conflict({key_field}) do update set payload_json = excluded.payload_json, "
        "updated_at_utc = excluded.updated_at_utc",
        payloads,
    )
    conn.commit()


def payload_rows_to_frame(rows: Sequence[Mapping[str, Any]], preferred: Optional[Sequence[str]] = None):
    if pd is None:
        raise RuntimeError("pandas is required for CSV exports")
    df = pd.DataFrame(list(rows))
    if df.empty or not preferred:
        return df
    ordered = [c for c in preferred if c in df.columns]
    rest = [c for c in df.columns if c not in ordered]
    return df[ordered + rest]


def write_csv(path: str | Path, rows: Sequence[Mapping[str, Any]], preferred: Optional[Sequence[str]] = None) -> None:
    ensure_dir(Path(path).parent)
    if pd is not None:
        payload_rows_to_frame(rows, preferred=preferred).to_csv(path, index=False)
        return
    all_cols: List[str] = []
    for row in rows:
        for col in row.keys():
            if col not in all_cols:
                all_cols.append(col)
    if preferred:
        ordered = [c for c in preferred if c in all_cols] + [c for c in all_cols if c not in preferred]
    else:
        ordered = all_cols
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered)
        writer.writeheader()
        writer.writerows(rows)


def find_nct_ids(*texts: Any) -> List[str]:
    ids = []
    seen = set()
    for text in texts:
        for match in NCT_RE.findall(clean_text(text)):
            nct = match.upper()
            if nct not in seen:
                seen.add(nct)
                ids.append(nct)
    return ids


def sentence_split(text: str) -> List[str]:
    text = clean_text(text)
    if not text:
        return []
    # Keep this simple; abstracts are usually short enough and often use section labels.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", text)
    return [p.strip() for p in parts if p.strip()]


def first_nonblank(*values: Any) -> str:
    for value in values:
        if not is_blankish(value):
            return clean_text(value)
    return ""


def text_blob(*parts: Any) -> str:
    return " ".join(clean_text(p) for p in parts if clean_text(p))


class CachedHTTPClient:
    """Small cached HTTP client for polite, restartable enrichment runs."""

    def __init__(self, config: APIConfig):
        self.config = config
        self.cache_dir = Path(config.cache_dir)
        ensure_dir(self.cache_dir)
        self.last_request = 0.0
        if requests is None and config.api_enabled:
            raise RuntimeError("requests is required for live API calls")

    def _cache_path(self, namespace: str, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:180]
        return self.cache_dir / namespace / f"{safe}.json"

    def get_json(self, namespace: str, key: str, url: str, params: Optional[Mapping[str, Any]] = None) -> Tuple[Optional[dict], str]:
        cache_path = self._cache_path(namespace, key)
        ensure_dir(cache_path.parent)
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8")), "cache"
            except Exception:
                pass
        if not self.config.api_enabled:
            return None, "api_disabled"
        assert requests is not None
        elapsed = time.time() - self.last_request
        if elapsed < self.config.min_interval_sec:
            time.sleep(self.config.min_interval_sec - elapsed)
        headers = {"User-Agent": self.config.user_agent or f"retarats-enrichment (mailto:{self.config.contact_email})"}
        try:
            resp = requests.get(url, params=dict(params or {}), headers=headers, timeout=self.config.timeout_sec)
            self.last_request = time.time()
            if resp.status_code >= 400:
                return {"error": f"HTTP {resp.status_code}", "text": resp.text[:1000]}, "http_error"
            data = resp.json()
            cache_path.write_text(json_dumps(data), encoding="utf-8")
            return data, "api"
        except Exception as exc:
            return {"error": type(exc).__name__, "text": str(exc)[:1000]}, "exception"

    def get_text(self, namespace: str, key: str, url: str, params: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
        cache_path = self._cache_path(namespace, key).with_suffix(".txt")
        ensure_dir(cache_path.parent)
        if cache_path.exists():
            try:
                return cache_path.read_text(encoding="utf-8"), "cache"
            except Exception:
                pass
        if not self.config.api_enabled:
            return "", "api_disabled"
        assert requests is not None
        elapsed = time.time() - self.last_request
        if elapsed < self.config.min_interval_sec:
            time.sleep(self.config.min_interval_sec - elapsed)
        headers = {"User-Agent": self.config.user_agent or f"retarats-enrichment (mailto:{self.config.contact_email})"}
        try:
            resp = requests.get(url, params=dict(params or {}), headers=headers, timeout=self.config.timeout_sec)
            self.last_request = time.time()
            if resp.status_code >= 400:
                return f"ERROR HTTP {resp.status_code}: {resp.text[:1000]}", "http_error"
            text = resp.text or ""
            cache_path.write_text(text, encoding="utf-8")
            return text, "api"
        except Exception as exc:
            return f"ERROR {type(exc).__name__}: {str(exc)[:1000]}", "exception"
