"""Registry-source helpers: ClinicalTrials.gov trials + EuropePMC preprints.

These are the SEPARATE, non-peer-reviewed corpus sources (trial registry and
preprints) kept clearly apart from the curated peer-reviewed evidence. Pure
stdlib normalizers live here so they can be unit-tested offline without any
network, and so the fetch scripts + JSON builders share one definition.

Nothing here performs I/O: the fetch scripts own the HTTP clients / SQLite and
call these normalizers on the parsed payloads.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .common import clean_text, semicolon_join

# --- Ongoing-status set (ClinicalTrials.gov v2 overallStatus values) ----------
# A trial is "ongoing" when it is actively enrolling or running. CT.gov v2 emits
# these as ALL-CAPS enum tokens (e.g. "RECRUITING"); older/JSON-humanized feeds
# use the spaced Title Case form. We accept both so the flag is robust to either
# shape. NOT ongoing: Completed, Terminated, Withdrawn, Suspended, Unknown, etc.
ONGOING_STATUSES = {
    # human / title-case
    "recruiting",
    "not yet recruiting",
    "active, not recruiting",
    "enrolling by invitation",
    # v2 enum tokens
    "not_yet_recruiting",
    "active_not_recruiting",
    "enrolling_by_invitation",
}

TRIAL_URL_TEMPLATE = "https://clinicaltrials.gov/study/{nct}"


def is_ongoing(overall_status: Any) -> bool:
    """True when the CT.gov overallStatus means the trial is still running."""
    s = clean_text(overall_status).lower()
    if not s:
        return False
    if s in ONGOING_STATUSES:
        return True
    # tolerate enum tokens with underscores vs spaces interchangeably
    return s.replace("_", " ") in ONGOING_STATUSES


def trial_url(nct_id: str) -> str:
    nct = clean_text(nct_id).upper()
    return TRIAL_URL_TEMPLATE.format(nct=nct) if nct else ""


def normalize_trial(
    parsed: Mapping[str, Any],
    molecule_id: str = "",
    molecule_name: str = "",
) -> Dict[str, Any]:
    """Turn a ``ClinicalTrialsClient.parse_study`` dict into a compact trial row.

    Keyed downstream by ``nct_id``. Adds molecule attribution, the canonical
    study URL and the derived ``ongoing`` flag.
    """
    nct_id = clean_text(parsed.get("nct_id", "")).upper()
    status = clean_text(parsed.get("overall_status", ""))
    # Linked publications parsed from the study's referencesModule (each a
    # {"pmid", "type"} dict). ``result_pmids`` keeps only the papers CT.gov marks
    # as reporting this trial's results (RESULT / DERIVED); ``reference_pmids``
    # keeps every linked PubMed id. Stored "; "-joined like other multi-value
    # trial fields, blank-safe when the study links no publications.
    references = parsed.get("references") or []
    result_pmids: List[str] = []
    reference_pmids: List[str] = []
    for ref in references:
        if not isinstance(ref, Mapping):
            continue
        pmid = clean_text(ref.get("pmid", ""))
        if not pmid:
            continue
        reference_pmids.append(pmid)
        if clean_text(ref.get("type", "")).upper() in {"RESULT", "DERIVED"}:
            result_pmids.append(pmid)
    row: Dict[str, Any] = {
        "nct_id": nct_id,
        "molecule_id": clean_text(molecule_id),
        "molecule_name": clean_text(molecule_name),
        "brief_title": clean_text(parsed.get("brief_title", "")),
        "overall_status": status,
        "phases": clean_text(parsed.get("phases", "")),
        "study_type": clean_text(parsed.get("study_type", "")),
        "conditions": clean_text(parsed.get("conditions", "")),
        "interventions": clean_text(parsed.get("interventions", "")),
        "enrollment_count": parsed.get("enrollment_count", "") or "",
        "start_date": clean_text(parsed.get("start_date", "")),
        "primary_completion_date": clean_text(parsed.get("primary_completion_date", "")),
        "completion_date": clean_text(parsed.get("completion_date", "")),
        "lead_sponsor": clean_text(parsed.get("lead_sponsor", "")),
        "has_results": bool(parsed.get("has_results")),
        "result_pmids": semicolon_join(result_pmids),
        "reference_pmids": semicolon_join(reference_pmids),
        "url": trial_url(nct_id),
        "ongoing": is_ongoing(status),
    }
    return row


# --- EuropePMC preprint normalizer -------------------------------------------

DOI_URL_TEMPLATE = "https://doi.org/{doi}"
EUROPEPMC_URL_TEMPLATE = "https://europepmc.org/article/{source}/{ext_id}"


def _authors_short(author_string: Any, max_authors: int = 3) -> str:
    """Trim EuropePMC's ``authorString`` ("A B, C D, E F.") to first N + et al."""
    text = clean_text(author_string).rstrip(".")
    if not text:
        return ""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) <= max_authors:
        return "; ".join(parts)
    return "; ".join(parts[:max_authors]) + " et al."


def preprint_id(result: Mapping[str, Any]) -> str:
    """Stable id for a preprint: prefer DOI, else the EuropePMC id."""
    doi = clean_text(result.get("doi", ""))
    if doi:
        return doi.lower()
    ext = clean_text(result.get("id", ""))
    return ext


def preprint_url(doi: str, source: str = "", ext_id: str = "") -> str:
    doi = clean_text(doi)
    if doi:
        return DOI_URL_TEMPLATE.format(doi=doi)
    source = clean_text(source)
    ext_id = clean_text(ext_id)
    if source and ext_id:
        return EUROPEPMC_URL_TEMPLATE.format(source=source, ext_id=ext_id)
    return ""


def normalize_preprint(
    result: Mapping[str, Any],
    molecule_id: str = "",
    molecule_name: str = "",
) -> Dict[str, Any]:
    """Normalize one EuropePMC ``resultList.result`` entry into a preprint row.

    ``server`` prefers the granular ``bookOrReportDetails``/``source`` server name
    (bioRxiv / medRxiv) and falls back to the generic ``source`` (usually "PPR").
    """
    doi = clean_text(result.get("doi", ""))
    ext_id = clean_text(result.get("id", ""))
    source = clean_text(result.get("source", ""))
    # EuropePMC surfaces the preprint server under a few possible keys.
    server = (
        clean_text(result.get("server", ""))
        or clean_text(result.get("publisher", ""))
        or clean_text(result.get("journalTitle", ""))
        or source
    )
    date = clean_text(result.get("firstPublicationDate", "")) or clean_text(
        result.get("firstIndexDate", "")
    )
    return {
        "id": preprint_id(result),
        "molecule_id": clean_text(molecule_id),
        "molecule_name": clean_text(molecule_name),
        "title": clean_text(result.get("title", "")),
        "authors_short": _authors_short(result.get("authorString", "")),
        "server": server,
        "date": date,
        "doi": doi,
        "url": preprint_url(doi, source, ext_id),
    }


def europepmc_results(payload: Optional[Mapping[str, Any]]) -> List[dict]:
    """Extract the ``resultList.result`` list from a EuropePMC search payload."""
    if not isinstance(payload, Mapping):
        return []
    result_list = payload.get("resultList") or {}
    results = result_list.get("result") if isinstance(result_list, Mapping) else None
    if isinstance(results, list):
        return [r for r in results if isinstance(r, Mapping)]
    return []


# --- Molecule loading ---------------------------------------------------------

def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "t"}


def load_active_molecules(path: str | Path = "config/MOLECULES.csv") -> List[Dict[str, str]]:
    """Load active molecules from MOLECULES.csv (molecule_id/display_name/synonyms)."""
    p = Path(path)
    if not p.exists():
        return []
    out: List[Dict[str, str]] = []
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not (row.get("molecule_id") or "").strip():
                continue
            if "active" in row and not _truthy(row.get("active")):
                continue
            out.append({k: (v or "").strip() for k, v in row.items()})
    return out


def molecule_query_terms(molecule: Mapping[str, Any], max_synonyms: int = 3) -> List[str]:
    """Build the search terms for a molecule: display_name + a few key synonyms.

    Keeps it conservative (display name plus up to ``max_synonyms`` synonyms) to
    avoid noisy/ambiguous short codes; exclusions are not applied here since the
    per-source query strings quote the exact terms.
    """
    terms: List[str] = []
    seen = set()

    def _add(term: str) -> None:
        t = clean_text(term)
        if not t:
            return
        key = t.lower()
        if key not in seen:
            seen.add(key)
            terms.append(t)

    _add(molecule.get("display_name", ""))
    syn_raw = molecule.get("synonyms_csv", "") or ""
    for syn in syn_raw.split(","):
        if len([t for t in terms]) >= max_synonyms + 1:
            break
        _add(syn)
    return terms


def trials_query(molecule: Mapping[str, Any]) -> str:
    """CT.gov v2 free-text query.term: OR of the molecule's key terms."""
    terms = molecule_query_terms(molecule)
    quoted = [f'"{t}"' if " " in t else t for t in terms]
    return " OR ".join(quoted)


def preprints_query(molecule: Mapping[str, Any]) -> str:
    """EuropePMC query: (terms...) AND SRC:PPR (preprint source filter)."""
    terms = molecule_query_terms(molecule)
    quoted = [f'"{t}"' if " " in t else t for t in terms]
    inner = " OR ".join(quoted)
    return f"({inner}) AND SRC:PPR"
