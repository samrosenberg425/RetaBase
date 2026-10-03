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
import os
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
    published_pmid, published_doi = _published_version(result)
    version_number, withdrawn, pub_types = _version_and_withdrawal(result)
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
        # From resultType=core (blank on a "lite" fetch): the abstract and, once the
        # preprint has been published in a journal, the published article's PMID/DOI.
        "abstract": clean_text(result.get("abstractText", "")),
        "published_pmid": published_pmid,
        "published_doi": published_doi,
        # Version/withdrawal status EuropePMC records per preprint (also core-only).
        "version_number": version_number,
        "withdrawn": withdrawn,
        "pub_types": pub_types,
    }


def _version_and_withdrawal(result: Mapping[str, Any]) -> tuple[Any, bool, str]:
    """Extract (version_number, withdrawn, pub_types) from a EuropePMC ``core``
    preprint result. ``withdrawn`` is True when the CURRENT version's own
    ``pubTypeList`` names a withdrawal (e.g. "preprint-withdrawal"); an earlier
    version being a withdrawal notice for a *later* one is not what we want, so
    this reads the top-level (current) ``pubTypeList``, not ``versionList``."""
    version_number = result.get("versionNumber", "")
    pub_type_list = result.get("pubTypeList") or {}
    pub_types_raw = pub_type_list.get("pubType") if isinstance(pub_type_list, Mapping) else None
    pub_types = [clean_text(t) for t in (pub_types_raw or []) if clean_text(t)]
    withdrawn = any("withdraw" in t.lower() for t in pub_types)
    return version_number, withdrawn, semicolon_join(pub_types)


def _published_version(result: Mapping[str, Any]) -> tuple[str, str]:
    """Extract the published journal article's PMID/DOI for a preprint, if EuropePMC
    records one. EuropePMC links a preprint to its published version via
    ``commentCorrectionList`` (a comment/correction whose type names a published
    version). Tolerant of shape: returns ("","") when no link is present."""
    ccl = result.get("commentCorrectionList")
    entries: list = []
    if isinstance(ccl, Mapping):
        cc = ccl.get("commentCorrection")
        if isinstance(cc, list):
            entries = [e for e in cc if isinstance(e, Mapping)]
        elif isinstance(cc, Mapping):
            entries = [cc]
    for e in entries:
        etype = clean_text(e.get("type", "")).lower()
        # The preprint's record points at its published version with a type such as
        # "Preprint of Publication" / "...published version...".
        if "publi" in etype or "preprint of" in etype:
            pmid = clean_text(e.get("id", "")) if clean_text(e.get("source", "")).upper() in {"MED", "PMC", ""} else ""
            pmid = pmid if pmid.isdigit() else ""
            return pmid, clean_text(e.get("doi", ""))
    return "", ""


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


REGISTRY_TERM_BLOCKLIST_PATH = os.path.join("config", "registry_term_blocklist.csv")


def _blocked_registry_terms(molecule_id: str, path: str = REGISTRY_TERM_BLOCKLIST_PATH) -> set:
    """Lower-cased terms a curator has barred from the registry/preprint queries of this
    molecule (config/registry_term_blocklist.csv: molecule_id, term, reason). These are
    ambiguous abbreviations that the registries match on unrelated records (e.g. bare "NR")."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        return {(r.get("term") or "").strip().lower() for r in csv.DictReader(fh)
                if (r.get("molecule_id") or "").strip() == molecule_id and (r.get("term") or "").strip()}


def registry_terms(molecule: Mapping[str, Any]) -> List[str]:
    """Query terms for the CT.gov and EuropePMC (preprint) searches: the molecule's key terms
    minus any blocklisted ambiguous ones. The display name can never be blocked, so a molecule
    is never left with no query."""
    terms = molecule_query_terms(molecule)
    blocked = _blocked_registry_terms(str(molecule.get("molecule_id", "") or ""))
    display = clean_text(molecule.get("display_name", "")).lower()
    kept = [t for t in terms if t.lower() == display or t.lower() not in blocked]
    return kept or terms[:1]


REGISTRY_KEEP_PATH = os.path.join("config", "registry_keep.csv")
REGISTRY_EXPECTED_EMPTY_PATH = os.path.join("config", "registry_expected_empty.csv")


def load_registry_keep(path: str = REGISTRY_KEEP_PATH) -> set:
    """{(molecule_id, id)} a human has reviewed and said to KEEP even though its record does not name
    the molecule (e.g. a trial that only uses a brand name missing from our synonyms). The id is an
    NCT id (upper-cased) or a preprint id. Overrides the precision guard."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        return {((r.get("molecule_id") or "").strip(), (r.get("id") or "").strip().upper())
                for r in csv.DictReader(fh) if (r.get("molecule_id") or "").strip() and (r.get("id") or "").strip()}


def load_registry_expected_empty(path: str = REGISTRY_EXPECTED_EMPTY_PATH) -> set:
    """molecule_ids a human has confirmed have NO real registry/preprint records, so an empty search
    result is legitimate and stored rows for them can be reconciled (otherwise an empty result for a
    molecule with many stored rows is treated as an API anomaly and skipped)."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        return {(r.get("molecule_id") or "").strip() for r in csv.DictReader(fh) if (r.get("molecule_id") or "").strip()}


def molecule_all_names(molecule: Mapping[str, Any], min_len: int = 3) -> List[str]:
    """Every name we know for the molecule (display name + ALL synonyms, not just the few that go
    into the query). Names shorter than ``min_len`` are ignored: 2-letter codes are too ambiguous
    to count as proof a record is about the molecule."""
    names = [clean_text(molecule.get("display_name", ""))] + [clean_text(x) for x in
                                                              str(molecule.get("synonyms_csv", "") or "").split(",")]
    out, seen = [], set()
    for n in names:
        if len(n) >= min_len and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def names_molecule(text: str, names: List[str]) -> bool:
    """Does ``text`` contain any of ``names`` as a whole token (case-, hyphen- and space-insensitive)?

    This is the registry precision guard: CT.gov and EuropePMC match on tokenisation, server-side
    synonym expansion and full text, so a hit is only kept when the record itself names the
    molecule. Fail-open: with no usable names the guard cannot judge, so everything passes."""
    if not names:
        return True
    from retarats_pipeline.manual_exclusions import _term_regex
    return any(_term_regex(n).search(text) for n in names)


def _quote_term(term: str) -> str:
    """Always a quoted phrase. A bare token such as MT-II is split by the registries into
    "MT" + "II" and matches every "Phase II" trial (1,336 junk hits for 1 real one)."""
    return '"' + term.replace('"', "") + '"'


def trials_query(molecule: Mapping[str, Any]) -> str:
    """CT.gov v2 free-text query.term: OR of the molecule's key terms, each quoted."""
    return " OR ".join(_quote_term(t) for t in registry_terms(molecule))


def preprints_query(molecule: Mapping[str, Any]) -> str:
    """EuropePMC query: ("term" OR ...) AND SRC:PPR (preprint source filter), terms quoted."""
    inner = " OR ".join(_quote_term(t) for t in registry_terms(molecule))
    return f"({inner}) AND SRC:PPR"
