from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from .common import APIConfig, CachedHTTPClient, clean_text, find_nct_ids, first_nonblank, semicolon_join

CTG_BASE = "https://clinicaltrials.gov/api/v2/studies"
PMC_IDCONV = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
NCBI_ELINK = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
BIOC_PMC = "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi"
EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_ANNOTATIONS = "https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds"
CROSSREF_WORKS = "https://api.crossref.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"
OPENALEX_WORKS = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_PAPER = "https://api.semanticscholar.org/graph/v1/paper"
PUBTATOR3_EXPORT = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"
PUBTATOR_LEGACY_EXPORT = "https://www.ncbi.nlm.nih.gov/research/pubtator-api/publications/export/biocjson"


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class ClinicalTrialsClient:
    def __init__(self, http: CachedHTTPClient):
        self.http = http

    def fetch_nct(self, nct_id: str) -> Tuple[Optional[dict], str]:
        nct = clean_text(nct_id).upper()
        if not re.match(r"^NCT\d{8}$", nct):
            return None, "invalid_nct"
        url = f"{CTG_BASE}/{nct}"
        data, source = self.http.get_json("clinicaltrials_nct", nct, url, params={"format": "json"})
        if data and not data.get("error"):
            return data, source
        return data, source

    def search(self, query: str, page_size: int = 5) -> Tuple[List[dict], str]:
        query = clean_text(query)
        if not query:
            return [], "empty_query"
        params = {"format": "json", "pageSize": page_size, "query.term": query}
        data, source = self.http.get_json("clinicaltrials_search", query[:160] + f"_{page_size}", CTG_BASE, params=params)
        if not data or data.get("error"):
            return [], source
        return data.get("studies") or [], source

    @staticmethod
    def parse_study(study: Mapping[str, Any]) -> Dict[str, Any]:
        protocol = study.get("protocolSection") or {}
        results = study.get("resultsSection") or {}
        ident = protocol.get("identificationModule") or {}
        status = protocol.get("statusModule") or {}
        design = protocol.get("designModule") or {}
        cond = protocol.get("conditionsModule") or {}
        arms_mod = protocol.get("armsInterventionsModule") or {}
        outcomes = protocol.get("outcomesModule") or {}
        eligibility = protocol.get("eligibilityModule") or {}
        sponsor = protocol.get("sponsorCollaboratorsModule") or {}

        arms = arms_mod.get("armGroups") or []
        interventions = arms_mod.get("interventions") or []
        primary_outcomes = outcomes.get("primaryOutcomes") or []
        secondary_outcomes = outcomes.get("secondaryOutcomes") or []
        adverse = (results.get("adverseEventsModule") or {})

        phase = semicolon_join(design.get("phases") or [])
        enrollment = design.get("enrollmentInfo") or {}
        enrollment_count = enrollment.get("count") if isinstance(enrollment, Mapping) else None
        enrollment_type = enrollment.get("type") if isinstance(enrollment, Mapping) else ""

        parsed = {
            "nct_id": ident.get("nctId", ""),
            "brief_title": ident.get("briefTitle", ""),
            "official_title": ident.get("officialTitle", ""),
            "overall_status": status.get("overallStatus", ""),
            "start_date": _date_struct_text(status.get("startDateStruct")),
            "primary_completion_date": _date_struct_text(status.get("primaryCompletionDateStruct")),
            "completion_date": _date_struct_text(status.get("completionDateStruct")),
            "study_type": design.get("studyType", ""),
            "phases": phase,
            "enrollment_count": enrollment_count or "",
            "enrollment_type": enrollment_type or "",
            "conditions": semicolon_join(cond.get("conditions") or []),
            "keywords": semicolon_join(cond.get("keywords") or []),
            "arms": semicolon_join(_arm_label(a) for a in arms),
            "interventions": semicolon_join(_intervention_label(i) for i in interventions),
            "primary_outcomes": semicolon_join(_outcome_label(o) for o in primary_outcomes),
            "secondary_outcomes": semicolon_join(_outcome_label(o) for o in secondary_outcomes),
            "eligibility_summary": clean_text(eligibility.get("eligibilityCriteria", ""))[:3000],
            "healthy_volunteers": eligibility.get("healthyVolunteers", ""),
            "sex": eligibility.get("sex", ""),
            "minimum_age": eligibility.get("minimumAge", ""),
            "maximum_age": eligibility.get("maximumAge", ""),
            "lead_sponsor": ((sponsor.get("leadSponsor") or {}).get("name", "")),
            "has_results": bool(results),
            "adverse_events_available": bool(adverse),
            "serious_events": semicolon_join(_event_label(e) for e in (adverse.get("seriousEvents") or [])[:20]),
            "other_events": semicolon_join(_event_label(e) for e in (adverse.get("otherEvents") or [])[:20]),
        }
        return parsed


def _date_struct_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return clean_text(value.get("date") or value.get("type") or "")


def _arm_label(arm: Mapping[str, Any]) -> str:
    label = first_nonblank(arm.get("label"), arm.get("type"))
    typ = clean_text(arm.get("type", ""))
    desc = clean_text(arm.get("description", ""))[:200]
    parts = [p for p in [label, typ, desc] if p]
    return " | ".join(parts)


def _intervention_label(intervention: Mapping[str, Any]) -> str:
    typ = clean_text(intervention.get("type", ""))
    name = clean_text(intervention.get("name", ""))
    desc = clean_text(intervention.get("description", ""))[:400]
    arm_labels = semicolon_join(intervention.get("armGroupLabels") or [])
    parts = [p for p in [typ, name, desc, arm_labels] if p]
    return " | ".join(parts)


def _outcome_label(outcome: Mapping[str, Any]) -> str:
    measure = clean_text(outcome.get("measure", ""))
    time_frame = clean_text(outcome.get("timeFrame", ""))
    desc = clean_text(outcome.get("description", ""))[:300]
    parts = [measure]
    if time_frame:
        parts.append(f"time frame: {time_frame}")
    if desc:
        parts.append(desc)
    return " | ".join([p for p in parts if p])


def _event_label(event: Mapping[str, Any]) -> str:
    term = clean_text(event.get("term", ""))
    organ = clean_text(event.get("organSystem", ""))
    return " | ".join([p for p in [term, organ] if p])


class IdentifierMetadataClient:
    def __init__(self, http: CachedHTTPClient, config: APIConfig):
        self.http = http
        self.config = config

    def pmc_idconv(self, identifier: str) -> Tuple[Optional[dict], str]:
        ident = clean_text(identifier)
        if not ident:
            return None, "empty_identifier"
        params = {"ids": ident, "format": "json", "email": self.config.contact_email, "tool": self.config.tool_name}
        data, source = self.http.get_json("pmc_idconv", ident, PMC_IDCONV, params=params)
        return data, source

    def crossref_by_doi(self, doi: str) -> Tuple[Optional[dict], str]:
        doi = clean_text(doi)
        if not doi:
            return None, "empty_doi"
        url = f"{CROSSREF_WORKS}/{quote(doi, safe='')}"
        data, source = self.http.get_json("crossref_doi", doi, url, params={"mailto": self.config.contact_email})
        return data, source

    def crossref_search_title(self, title: str, rows: int = 3) -> Tuple[Optional[dict], str]:
        title = clean_text(title)
        if not title:
            return None, "empty_title"
        params = {"query.title": title[:500], "rows": rows, "mailto": self.config.contact_email}
        data, source = self.http.get_json("crossref_title", title[:160] + f"_{rows}", CROSSREF_WORKS, params=params)
        return data, source

    def unpaywall(self, doi: str) -> Tuple[Optional[dict], str]:
        doi = clean_text(doi)
        if not doi:
            return None, "empty_doi"
        url = f"{UNPAYWALL}/{quote(doi, safe='')}"
        data, source = self.http.get_json("unpaywall", doi, url, params={"email": self.config.contact_email})
        return data, source

    def europepmc_search(self, query: str, page_size: int = 5) -> Tuple[Optional[dict], str]:
        query = clean_text(query)
        if not query:
            return None, "empty_query"
        params = {"query": query, "format": "json", "pageSize": page_size}
        data, source = self.http.get_json("europepmc_search", query[:180] + f"_{page_size}", EUROPEPMC_SEARCH, params=params)
        return data, source

    def openalex_cited_by(self, doi: str = "", pmid: str = "") -> Tuple[Optional[int], str]:
        """Return (cited_by_count, source) from OpenAlex by DOI (preferred) or PMID.

        OpenAlex needs no API key — only a contact email for the polite pool. We
        query the single-work endpoint by canonical id and read ``cited_by_count``.
        """
        mailto = self.config.contact_email
        doi = clean_text(doi)
        pmid = clean_text(pmid)
        if doi:
            url = f"{OPENALEX_WORKS}/https://doi.org/{quote(doi, safe='')}"
            data, source = self.http.get_json("openalex_doi", doi, url, params={"mailto": mailto})
            n = _openalex_cited_by(data)
            if n is not None:
                return n, f"openalex_doi:{source}"
        if pmid:
            url = f"{OPENALEX_WORKS}/pmid:{quote(pmid, safe='')}"
            data, source = self.http.get_json("openalex_pmid", pmid, url, params={"mailto": mailto})
            n = _openalex_cited_by(data)
            if n is not None:
                return n, f"openalex_pmid:{source}"
        return None, "openalex_not_found"

    # Fields we ask the (keyless) Semantic Scholar Graph API to return.
    _S2_FIELDS = "citationCount,influentialCitationCount,venue,year,externalIds,authors.name,authors.authorId,authors.url"

    def semanticscholar_paper(self, doi: str = "", pmid: str = "") -> Tuple[Optional[dict], str]:
        """Fetch one paper's record from Semantic Scholar (keyless Graph API).

        Prefers DOI (``/paper/DOI:{doi}``) then PMID (``/paper/PMID:{pmid}``). No
        API key is required for low-volume polite use. Returns the raw JSON record
        (or ``None``) plus a source string for provenance. This is the *fallback*
        source when OpenAlex has no match — it covers the ~32 no-DOI papers via
        PMID and any DOI OpenAlex missed.
        """
        doi = clean_text(doi)
        pmid = clean_text(pmid)
        params = {"fields": self._S2_FIELDS}
        if doi:
            url = f"{SEMANTIC_SCHOLAR_PAPER}/DOI:{quote(doi, safe='')}"
            data, source = self.http.get_json("semanticscholar_doi", doi, url, params=params)
            if _s2_ok(data):
                return data, f"semanticscholar_doi:{source}"
        if pmid:
            url = f"{SEMANTIC_SCHOLAR_PAPER}/PMID:{quote(pmid, safe='')}"
            data, source = self.http.get_json("semanticscholar_pmid", pmid, url, params=params)
            if _s2_ok(data):
                return data, f"semanticscholar_pmid:{source}"
        return None, "semanticscholar_not_found"


def _s2_ok(data: Optional[Mapping[str, Any]]) -> bool:
    """True when the S2 payload is a usable record (not an error/not-found)."""
    if not isinstance(data, Mapping):
        return False
    if data.get("error") or data.get("code"):
        return False
    # S2 404s carry {"error": "..."} but some misses just lack a paperId/fields;
    # treat a record with any of our requested fields present as usable.
    return any(k in data for k in ("citationCount", "influentialCitationCount", "authors", "paperId", "venue", "year"))


def semanticscholar_citation_count(data: Optional[Mapping[str, Any]]) -> Optional[int]:
    """Extract the total citation count from an S2 paper record."""
    if not isinstance(data, Mapping):
        return None
    v = data.get("citationCount")
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def semanticscholar_influential_count(data: Optional[Mapping[str, Any]]) -> Optional[int]:
    """Extract the influential-citation count from an S2 paper record."""
    if not isinstance(data, Mapping):
        return None
    v = data.get("influentialCitationCount")
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def semanticscholar_authors(data: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """Extract a normalized author list [{name, authorId, url}, ...] from S2.

    ``url`` is the public Semantic Scholar author page (falls back to the id-based
    URL when S2 omits the direct link). Always returns a list (possibly empty).
    """
    out: List[Dict[str, str]] = []
    if not isinstance(data, Mapping):
        return out
    for a in data.get("authors") or []:
        if not isinstance(a, Mapping):
            continue
        name = clean_text(a.get("name", ""))
        author_id = clean_text(a.get("authorId", ""))
        url = clean_text(a.get("url", ""))
        if not url and author_id:
            url = f"https://www.semanticscholar.org/author/{author_id}"
        if name or author_id:
            out.append({"name": name, "authorId": author_id, "url": url})
    return out


def _openalex_cited_by(data: Optional[Mapping[str, Any]]) -> Optional[int]:
    if not isinstance(data, Mapping) or data.get("error"):
        return None
    v = data.get("cited_by_count")
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class PMCFullTextClient:
    """NCBI PMC lookup and XML fetch helper.

    Uses PMC ID Converter first, then ELink as a fallback. Full-text fetch uses
    EFetch with `db=pmc` and `retmode=xml`.
    """

    def __init__(self, http: CachedHTTPClient, config: APIConfig):
        self.http = http
        self.config = config

    def pmcid_for_pmid(self, pmid: str) -> Tuple[str, str]:
        pmid = clean_text(pmid)
        if not pmid:
            return "", "empty_pmid"
        idconv_params = {"ids": pmid, "format": "json", "email": self.config.contact_email, "tool": self.config.tool_name}
        data, source = self.http.get_json("pmc_idconv", f"pmid_{pmid}", PMC_IDCONV, params=idconv_params)
        pmcid = _pmcid_from_idconv(data)
        if pmcid:
            return pmcid, f"pmc_idconv:{source}"
        elink_params = {
            "dbfrom": "pubmed",
            "db": "pmc",
            "id": pmid,
            "linkname": "pubmed_pmc",
            "retmode": "json",
            "email": self.config.ncbi_email,
            "tool": self.config.tool_name,
        }
        if self.config.ncbi_api_key:
            elink_params["api_key"] = self.config.ncbi_api_key
        elink, elink_source = self.http.get_json("ncbi_elink_pubmed_pmc", pmid, NCBI_ELINK, params=elink_params)
        pmcid = _pmcid_from_elink(elink)
        return pmcid, f"elink_pubmed_pmc:{elink_source}"

    def fetch_bioc_json(self, identifier: str) -> Tuple[Optional[dict], str]:
        ident = clean_text(identifier)
        if not ident:
            return None, "empty_identifier"
        url = f"{BIOC_PMC}/BioC_json/{quote(ident, safe='')}/unicode"
        data, source = self.http.get_json("bioc_pmc_json", ident, url)
        if isinstance(data, Mapping) and data.get("error"):
            return data, source
        return data, source

    def fetch_pmc_xml(self, pmcid: str) -> Tuple[str, str]:
        pmcid = clean_text(pmcid).upper()
        if not re.match(r"^PMC\d+$", pmcid):
            return "", "invalid_pmcid"
        params = {
            "db": "pmc",
            "id": pmcid.replace("PMC", ""),
            "retmode": "xml",
            "email": self.config.ncbi_email,
            "tool": self.config.tool_name,
        }
        if self.config.ncbi_api_key:
            params["api_key"] = self.config.ncbi_api_key
        return self.http.get_text("ncbi_efetch_pmc_xml", pmcid, NCBI_EFETCH, params=params)


def _pmcid_from_idconv(data: Optional[Mapping[str, Any]]) -> str:
    if not data:
        return ""
    records = data.get("records") if isinstance(data, Mapping) else None
    if isinstance(records, list):
        for rec in records:
            pmcid = clean_text((rec or {}).get("pmcid", ""))
            if re.match(r"^PMC\d+$", pmcid, re.I):
                return pmcid.upper()
    return ""


def _pmcid_from_elink(data: Optional[Mapping[str, Any]]) -> str:
    if not data:
        return ""
    try:
        linksets = data.get("linksets") or []
        for linkset in linksets:
            for db in linkset.get("linksetdbs") or []:
                for raw_id in db.get("links") or []:
                    ident = clean_text(raw_id)
                    if ident:
                        return f"PMC{ident}" if ident.isdigit() else ident.upper()
    except Exception:
        return ""
    return ""


class AnnotationClient:
    def __init__(self, http: CachedHTTPClient):
        self.http = http

    def pubtator_pmids(self, pmids: Sequence[str]) -> Tuple[List[dict], str]:
        clean_pmids = [clean_text(p) for p in pmids if clean_text(p)]
        if not clean_pmids:
            return [], "empty_pmids"
        key = "_".join(clean_pmids[:50])
        params = {"pmids": ",".join(clean_pmids)}
        data, source = self.http.get_json("pubtator3_biocjson", key, PUBTATOR3_EXPORT, params=params)
        if data and not data.get("error"):
            return _extract_pubtator_documents(data), source
        data2, source2 = self.http.get_json("pubtator_legacy_biocjson", key, PUBTATOR_LEGACY_EXPORT, params=params)
        if data2 and not data2.get("error"):
            return _extract_pubtator_documents(data2), source2
        return [], source2 if source2 else source

    def europepmc_annotations(self, pmid: str) -> Tuple[List[dict], str]:
        pmid = clean_text(pmid)
        if not pmid:
            return [], "empty_pmid"
        # MED:<pmid> is the documented article-id style for PubMed records in this endpoint.
        params = {"articleIds": f"MED:{pmid}", "format": "JSON"}
        data, source = self.http.get_json("europepmc_annotations", pmid, EUROPEPMC_ANNOTATIONS, params=params)
        if not data:
            return [], source
        if isinstance(data, Mapping) and data.get("error"):
            return [], source
        if isinstance(data, list):
            return data, source
        return data.get("annotations") or data.get("results") or [], source

    @staticmethod
    def summarize_pubtator_documents(docs: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
        by_type: Dict[str, List[str]] = {}
        for doc in docs:
            for passage in doc.get("passages", []) or []:
                for ann in passage.get("annotations", []) or []:
                    infons = ann.get("infons") or {}
                    typ = clean_text(infons.get("type") or infons.get("identifier") or "entity")
                    text = clean_text(ann.get("text", ""))
                    if text:
                        by_type.setdefault(typ.lower(), []).append(text)
        return {f"pubtator_{k}": semicolon_join(v[:50]) for k, v in by_type.items()}


def _extract_pubtator_documents(data: Any) -> List[dict]:
    if not data:
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        if "PubTator3" in data and isinstance(data["PubTator3"], list):
            return data["PubTator3"]
        if "documents" in data and isinstance(data["documents"], list):
            return data["documents"]
        if "passages" in data:
            return [data]
    return []
