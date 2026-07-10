"""NIH iCite / Open Citation Collection client.

iCite (https://icite.od.nih.gov) publishes, for essentially every PubMed article,
a rich set of NIH-curated metrics we can use to categorize and rank far better
than a raw citation count:

  * relative_citation_ratio (RCR) - field- and time-normalized impact (1.0 = the
    NIH field median). Far fairer across old/new and different fields than a raw
    count.
  * nih_percentile - 0-100 percentile of that RCR.
  * apt - Approximate Potential to Translate (0-1): model-estimated likelihood the
    work will eventually inform clinical research. A strong "is this translational"
    signal for the directness axis.
  * human / animal / molecular_cellular - the fractions that place the paper on the
    biomedical "triangle of translation"; x_coord / y_coord are the triangle
    position. These give an evidence-based human/animal/in-vitro classification to
    replace/augment our keyword heuristics.
  * is_clinical + cited_by_clin - whether it's a clinical article and how many
    clinical articles cite it (clinical influence).
  * field_citation_rate, expected_citations_per_year, citations_per_year,
    citation_count.

Free, keyless. Batch API: GET /api/pubs?pmids=1,2,3 (up to ~1000 PMIDs/request).
NETWORK REQUIRED - runs on your machine or the Actions runner, not the sandbox.
"""

from __future__ import annotations

import time
from typing import Dict, Iterable, List, Optional

import requests

ICITE_API = "https://icite.od.nih.gov/api/pubs"

# The subset of iCite fields we ingest (their key names). Kept explicit so the
# stored data is auditable and stable if iCite adds fields.
ICITE_FIELDS = [
    "relative_citation_ratio",
    "nih_percentile",
    "citation_count",
    "field_citation_rate",
    "expected_citations_per_year",
    "citations_per_year",
    "apt",
    "human",
    "animal",
    "molecular_cellular",
    "x_coord",
    "y_coord",
    "is_clinical",
    "cited_by_clin",
    "is_research_article",
    "provisional",
]


def _get(params: dict, *, timeout: int, retries: int) -> Optional[dict]:
    headers = {"User-Agent": "RetaBase/1.0 (biomedical evidence database; polite)"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(ICITE_API, params=params, timeout=timeout, headers=headers)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"retryable HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries:
                return None
            time.sleep(min(30, 2 ** attempt))
    return None


def fetch_icite(
    pmids: Iterable,
    *,
    batch_size: int = 200,
    sleep: float = 0.3,
    timeout: int = 60,
    retries: int = 4,
) -> Dict[str, dict]:
    """Return {pmid: {icite_field: value}} for the given PMIDs (batched)."""
    clean: List[str] = [str(p).strip() for p in pmids if str(p).strip().isdigit()]
    out: Dict[str, dict] = {}
    for i in range(0, len(clean), batch_size):
        batch = clean[i:i + batch_size]
        data = _get({"pmids": ",".join(batch)}, timeout=timeout, retries=retries)
        for rec in (data or {}).get("data", []) or []:
            pmid = str(rec.get("pmid", "")).strip()
            if pmid:
                out[pmid] = {k: rec.get(k) for k in ICITE_FIELDS}
        if sleep and i + batch_size < len(clean):
            time.sleep(sleep)
    return out
