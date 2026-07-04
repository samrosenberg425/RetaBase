"""Multi-API metadata validation + backfill.

Goal (handoff "efficiently getting the papers using multiple APIs,
validating/retrieving missing info"): for records whose identity/metadata is
incomplete (missing DOI, year, journal, abstract, or PMCID), consult multiple
sources and propose values -- non-destructively and with provenance.

Sources, in preference order per field:
    doi        -> EuropePMC(pmid) -> Crossref(title)
    pub_year   -> EuropePMC(pmid) -> Crossref(doi/title)
    journal    -> EuropePMC(pmid) -> Crossref(doi)
    abstract   -> EuropePMC(pmid) [-> PMC full text handled by pmc.py]
    pmcid      -> PMC id converter -> ELink
    oa_status  -> Unpaywall(doi)

Design notes:
* Nothing here overwrites an existing field. Proposals land in ``backfilled_*``
  keys plus a ``*_source`` provenance key, mirroring the repo's non-destructive
  enrichment philosophy (abstract_*/pmc_*/suggested_*).
* ``assess_paper`` needs no network and works fully offline -- it reports which
  fields are missing and which source *would* be tried. That powers the
  in-sandbox dry-run / coverage report.
* ``backfill_paper`` performs the live lookups (cached), and degrades gracefully:
  with ``api_enabled=False`` the cached client returns "api_disabled" and the
  function simply reports that nothing could be filled offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .clients import IdentifierMetadataClient, PMCFullTextClient
from .common import APIConfig, CachedHTTPClient, clean_text, is_blankish

# Field -> ordered list of source labels that can supply it (for the offline plan).
BACKFILL_PLAN = {
    "doi": ["europepmc_pmid", "crossref_title"],
    "pub_year": ["europepmc_pmid", "crossref_doi", "crossref_title"],
    "journal": ["europepmc_pmid", "crossref_doi"],
    "abstract": ["europepmc_pmid", "pmc_full_text"],
    "pmcid": ["pmc_idconv", "elink_pubmed_pmc"],
    "oa_status": ["unpaywall_doi"],
}


@dataclass
class MissingReport:
    pmid: str
    missing: List[str] = field(default_factory=list)
    backfillable: Dict[str, List[str]] = field(default_factory=dict)  # field -> source order

    @property
    def has_gaps(self) -> bool:
        return bool(self.missing)


def assess_paper(paper: dict) -> MissingReport:
    """Offline: which identity/metadata fields are missing and how they'd be filled."""
    pmid = clean_text(paper.get("pmid", ""))
    report = MissingReport(pmid=pmid)
    checks = {
        "doi": paper.get("doi"),
        "pub_year": paper.get("pub_year"),
        "journal": paper.get("journal"),
        "abstract": paper.get("abstract"),
        "pmcid": paper.get("pmcid"),
    }
    for fieldname, value in checks.items():
        if is_blankish(value):
            report.missing.append(fieldname)
            report.backfillable[fieldname] = BACKFILL_PLAN.get(fieldname, [])
    # oa_status is always "unknown" until looked up; only plannable if we have a DOI.
    if not is_blankish(paper.get("doi")) and is_blankish(paper.get("oa_status")):
        report.missing.append("oa_status")
        report.backfillable["oa_status"] = BACKFILL_PLAN["oa_status"]
    return report


@dataclass
class BackfillResult:
    pmid: str
    proposals: Dict[str, str] = field(default_factory=dict)      # backfilled_<field> -> value
    provenance: Dict[str, str] = field(default_factory=dict)     # <field>_source -> label
    attempted: List[str] = field(default_factory=list)
    notes: str = ""

    def merged_row(self, paper: dict) -> dict:
        out = dict(paper)
        out.update(self.proposals)
        out.update(self.provenance)
        return out


class MetadataBackfiller:
    def __init__(self, config: Optional[APIConfig] = None):
        self.config = config or APIConfig.from_env(api_enabled=True)
        self.http = CachedHTTPClient(self.config)
        self.meta = IdentifierMetadataClient(self.http, self.config)
        self.pmc = PMCFullTextClient(self.http, self.config)

    def backfill_paper(self, paper: dict) -> BackfillResult:
        pmid = clean_text(paper.get("pmid", ""))
        result = BackfillResult(pmid=pmid)
        report = assess_paper(paper)
        if not report.has_gaps:
            result.notes = "complete; no backfill needed"
            return result

        epmc_record = None
        if any(f in report.missing for f in ("doi", "pub_year", "journal", "abstract")) and pmid:
            epmc_record = self._europepmc_record(pmid)
            result.attempted.append("europepmc_pmid")

        # DOI
        if "doi" in report.missing:
            doi = _epmc_field(epmc_record, "doi")
            if not doi and not is_blankish(paper.get("title")):
                doi = self._crossref_doi_from_title(paper.get("title"))
                result.attempted.append("crossref_title")
            if doi:
                result.proposals["backfilled_doi"] = doi
                result.provenance["doi_source"] = "europepmc" if _epmc_field(epmc_record, "doi") else "crossref_title"

        # year
        if "pub_year" in report.missing:
            year = _epmc_field(epmc_record, "pubYear")
            if year:
                result.proposals["backfilled_pub_year"] = year
                result.provenance["pub_year_source"] = "europepmc"

        # journal
        if "journal" in report.missing:
            journal = _epmc_field(epmc_record, "journalTitle") or _epmc_field(epmc_record, "journalInfoTitle")
            if journal:
                result.proposals["backfilled_journal"] = journal
                result.provenance["journal_source"] = "europepmc"

        # abstract
        if "abstract" in report.missing:
            abstract = _epmc_field(epmc_record, "abstractText")
            if abstract:
                result.proposals["backfilled_abstract"] = abstract
                result.provenance["abstract_source"] = "europepmc"

        # pmcid
        if "pmcid" in report.missing and pmid:
            pmcid, src = self.pmc.pmcid_for_pmid(pmid)
            result.attempted.append("pmc_idconv")
            if pmcid:
                result.proposals["backfilled_pmcid"] = pmcid
                result.provenance["pmcid_source"] = src

        # open-access status
        doi_for_oa = clean_text(paper.get("doi")) or result.proposals.get("backfilled_doi", "")
        if "oa_status" in report.missing and doi_for_oa:
            data, src = self.meta.unpaywall(doi_for_oa)
            result.attempted.append("unpaywall_doi")
            if data and not data.get("error"):
                oa = data.get("is_oa")
                result.proposals["backfilled_oa_status"] = "open_access" if oa else "closed"
                result.provenance["oa_status_source"] = "unpaywall"

        filled = len(result.proposals)
        result.notes = f"filled {filled}/{len(report.missing)} missing fields" if filled else "no source returned values"
        return result

    def _europepmc_record(self, pmid: str) -> Optional[dict]:
        data, _src = self.meta.europepmc_search(f"EXT_ID:{pmid} AND SRC:MED", page_size=1)
        if not data or data.get("error"):
            return None
        results = ((data.get("resultList") or {}).get("result")) or []
        return results[0] if results else None

    def _crossref_doi_from_title(self, title: str) -> str:
        data, _src = self.meta.crossref_search_title(title, rows=1)
        if not data or data.get("error"):
            return ""
        items = ((data.get("message") or {}).get("items")) or []
        if items:
            return clean_text(items[0].get("DOI", ""))
        return ""


def _epmc_field(record: Optional[dict], key: str) -> str:
    if not record:
        return ""
    return clean_text(record.get(key, ""))
