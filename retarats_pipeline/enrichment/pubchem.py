"""PubChem PUG-REST client: resolve a molecule name to a CID (for a 'learn more'
link) and fetch its synonyms (candidate search terms).

Free, keyless. Be polite: PubChem allows ~5 requests/second. NETWORK REQUIRED.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import List, Optional

import requests

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


def _get(url: str, *, timeout: int = 30, retries: int = 4) -> Optional[dict]:
    headers = {"User-Agent": "RetaBase/1.0 (biomedical evidence database; polite)"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"retryable HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries:
                return None
            time.sleep(min(30, 2 ** attempt))
    return None


def pubchem_cids(name: str, limit: int = 10) -> List[int]:
    """Candidate CIDs for a compound name (best-match order), up to ``limit``.

    Returning several lets the caller pick the CID that actually corresponds to the
    intended drug (via synonym/MeSH/PubMed validation) instead of blindly taking the
    first, which for biologics/ambiguous names can be a wrong record.
    """
    name = (name or "").strip()
    if not name:
        return []
    url = f"{PUG}/compound/name/{urllib.parse.quote(name)}/cids/JSON"
    data = _get(url)
    cids = ((data or {}).get("IdentifierList") or {}).get("CID") or []
    out: List[int] = []
    for c in cids[:limit]:
        try:
            out.append(int(c))
        except (TypeError, ValueError):
            continue
    return out


def pubchem_cid(name: str) -> Optional[int]:
    """First matching CID for a compound name, or None (back-compat)."""
    cids = pubchem_cids(name, limit=1)
    return cids[0] if cids else None


def pubchem_synonyms(cid: int) -> List[str]:
    """All PubChem synonyms for a CID (ordered by PubChem's own ranking)."""
    url = f"{PUG}/compound/cid/{cid}/synonyms/JSON"
    data = _get(url)
    info = ((data or {}).get("InformationList") or {}).get("Information") or []
    return list(info[0].get("Synonym", [])) if info else []
