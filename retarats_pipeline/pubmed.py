from __future__ import annotations

import datetime as dt
import html
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional

import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError, HTTPError, Timeout
from urllib3.exceptions import ProtocolError


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


@dataclass
class PubMedSearch:
    query: str
    count: int
    webenv: str = ""
    query_key: str = ""
    ids: List[str] = field(default_factory=list)


@dataclass
class PubMedRecord:
    pmid: str
    title: str = ""
    abstract: str = ""
    journal: str = ""
    authors: List[str] = field(default_factory=list)
    doi: str = ""
    pub_date_iso: str = ""
    pub_year: str = ""
    pubtypes: List[str] = field(default_factory=list)
    mesh_terms: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    chemicals: List[str] = field(default_factory=list)
    language: str = ""
    article_ids: Dict[str, str] = field(default_factory=dict)

    @property
    def pubmed_url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/" if self.pmid else ""

    def to_dict(self) -> dict:
        return asdict(self) | {"pubmed_url": self.pubmed_url}


class PubMedClient:
    def __init__(
        self,
        *,
        email: str,
        api_key: str = "",
        tool: str = "retarats_pubmed_pipeline_v2",
        max_requests_per_second: float = 2.5,
        max_retries: int = 6,
        timeout_seconds: int = 60,
    ):
        if not email:
            raise ValueError("NCBI_EMAIL is required")
        self.email = email
        self.api_key = api_key
        self.tool = tool
        self.max_requests_per_second = max_requests_per_second
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self._last_request_at = 0.0

    def esearch(
        self,
        *,
        term: str,
        mindate: Optional[str] = None,
        maxdate: Optional[str] = None,
        reldate: Optional[int] = None,
        datetype: str = "pdat",
        retmax: int = 0,
        usehistory: bool = True,
    ) -> PubMedSearch:
        params = {
            "db": "pubmed",
            "term": term,
            "retmode": "json",
            "retmax": retmax,
            "sort": "pub date",
            "datetype": datetype,
        }
        if usehistory:
            params["usehistory"] = "y"
        if mindate:
            params["mindate"] = mindate
        if maxdate:
            params["maxdate"] = maxdate
        if reldate is not None:
            params["reldate"] = reldate

        data = self._get_json(f"{EUTILS_BASE}/esearch.fcgi", params)
        result = data.get("esearchresult", {})
        return PubMedSearch(
            query=term,
            count=int(result.get("count", 0)),
            webenv=result.get("webenv", ""),
            query_key=result.get("querykey", ""),
            ids=list(result.get("idlist", [])),
        )

    def iter_records_from_search(self, search: PubMedSearch, *, batch_size: int = 100) -> Iterable[PubMedRecord]:
        if search.count <= 0:
            return
        for retstart in range(0, search.count, batch_size):
            xml_text = self.efetch_xml(
                webenv=search.webenv,
                query_key=search.query_key,
                retstart=retstart,
                retmax=batch_size,
            )
            for record in parse_pubmed_xml(xml_text):
                yield record

    def efetch_xml(
        self,
        *,
        ids: Optional[List[str]] = None,
        webenv: str = "",
        query_key: str = "",
        retstart: int = 0,
        retmax: int = 100,
    ) -> str:
        params = {"db": "pubmed", "retmode": "xml"}
        if ids:
            params["id"] = ",".join(ids)
        else:
            params.update({
                "WebEnv": webenv,
                "query_key": query_key,
                "retstart": retstart,
                "retmax": retmax,
            })
        return self._get_text(f"{EUTILS_BASE}/efetch.fcgi", params)

    def _get_json(self, url: str, params: dict) -> dict:
        return self._request(url, params).json()

    def _get_text(self, url: str, params: dict) -> str:
        return self._request(url, params).text

    def _request(self, url: str, params: dict) -> requests.Response:
        params = dict(params)
        params.setdefault("tool", self.tool)
        params.setdefault("email", self.email)
        if self.api_key:
            params["api_key"] = self.api_key

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = requests.get(url, params=params, timeout=self.timeout_seconds)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise HTTPError(f"retryable HTTP {response.status_code}", response=response)
                response.raise_for_status()
                return response
            except (HTTPError, Timeout, ConnectionError, ChunkedEncodingError, ProtocolError):
                if attempt == self.max_retries:
                    raise
                sleep_s = min(60, 1.7 ** attempt) + random.uniform(0, 0.8)
                time.sleep(sleep_s)
        raise RuntimeError("unreachable")

    def _throttle(self) -> None:
        min_interval = 1.0 / max(self.max_requests_per_second, 0.1)
        now = time.time()
        wait_s = min_interval - (now - self._last_request_at)
        if wait_s > 0:
            time.sleep(wait_s)
        self._last_request_at = time.time()


def parse_pubmed_xml(xml_text: str) -> List[PubMedRecord]:
    root = ET.fromstring(xml_text)
    records: List[PubMedRecord] = []
    for article in root.findall(".//PubmedArticle"):
        records.append(_parse_article(article))
    return records


def _parse_article(article: ET.Element) -> PubMedRecord:
    pmid = _text(article.find("./MedlineCitation/PMID"))
    article_node = article.find("./MedlineCitation/Article")

    title = _node_text(article_node.find("./ArticleTitle") if article_node is not None else None)
    abstract = _abstract_text(article_node)
    journal = (
        _text(article.find("./MedlineCitation/Article/Journal/Title"))
        or _text(article.find("./MedlineCitation/Article/Journal/ISOAbbreviation"))
    )
    pub_date_iso = _publication_date(article)
    pub_year = _extract_year(pub_date_iso)

    article_ids = {}
    for node in article.findall("./PubmedData/ArticleIdList/ArticleId"):
        id_type = (node.attrib.get("IdType") or "").strip().lower()
        value = _text(node)
        if id_type and value:
            article_ids[id_type] = value

    doi = article_ids.get("doi", "")
    if not doi and article_node is not None:
        for node in article_node.findall("./ELocationID"):
            if (node.attrib.get("EIdType") or "").lower() == "doi":
                doi = _text(node)
                break

    return PubMedRecord(
        pmid=pmid,
        title=html.unescape(title),
        abstract=html.unescape(abstract),
        journal=html.unescape(journal),
        authors=_authors(article),
        doi=doi,
        pub_date_iso=pub_date_iso,
        pub_year=pub_year,
        pubtypes=_list_text(article, "./MedlineCitation/Article/PublicationTypeList/PublicationType"),
        mesh_terms=_mesh_terms(article),
        keywords=_list_text(article, "./MedlineCitation/KeywordList/Keyword"),
        chemicals=_list_text(article, "./MedlineCitation/ChemicalList/Chemical/NameOfSubstance"),
        language=_text(article.find("./MedlineCitation/Article/Language")),
        article_ids=article_ids,
    )


def _authors(article: ET.Element) -> List[str]:
    out = []
    for author in article.findall("./MedlineCitation/Article/AuthorList/Author"):
        collective = _text(author.find("./CollectiveName"))
        if collective:
            out.append(collective)
            continue
        last = _text(author.find("./LastName"))
        fore = _text(author.find("./ForeName"))
        initials = _text(author.find("./Initials"))
        if last and fore:
            out.append(f"{fore} {last}")
        elif last and initials:
            out.append(f"{initials} {last}")
        elif last:
            out.append(last)
    return out


def _abstract_text(article_node: Optional[ET.Element]) -> str:
    if article_node is None:
        return ""
    parts = []
    for node in article_node.findall("./Abstract/AbstractText"):
        label = node.attrib.get("Label") or node.attrib.get("NlmCategory") or ""
        text = _node_text(node)
        if text:
            parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts).strip()


def _publication_date(article: ET.Element) -> str:
    for base in [
        "./MedlineCitation/Article/ArticleDate",
        "./MedlineCitation/Article/Journal/JournalIssue/PubDate",
    ]:
        node = article.find(base)
        parsed = _date_from_node(node)
        if parsed:
            return parsed

    for history_date in article.findall("./PubmedData/History/PubMedPubDate"):
        if (history_date.attrib.get("PubStatus") or "").lower() in {"epublish", "pubmed", "entrez"}:
            parsed = _date_from_node(history_date)
            if parsed:
                return parsed
    return ""


def _date_from_node(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    year = _text(node.find("./Year"))
    month = _text(node.find("./Month"))
    day = _text(node.find("./Day"))
    medline = _text(node.find("./MedlineDate"))
    if not year and medline:
        match = re.search(r"(18|19|20)\d{2}", medline)
        if match:
            year = match.group(0)
    if not year:
        return ""
    return f"{year}-{_month_to_num(month)}-{_day_to_num(day)}"


def _month_to_num(value: str) -> str:
    if not value:
        return "01"
    value = value.strip()
    if value.isdigit():
        return value.zfill(2)[:2]
    month_map = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    return month_map.get(value[:3].lower(), "01")


def _day_to_num(value: str) -> str:
    if value and value.strip().isdigit():
        return value.strip().zfill(2)[:2]
    return "01"


def _extract_year(value: str) -> str:
    match = re.search(r"(18|19|20)\d{2}", value or "")
    return match.group(0) if match else ""


def _mesh_terms(article: ET.Element) -> List[str]:
    out = []
    for mesh in article.findall("./MedlineCitation/MeshHeadingList/MeshHeading"):
        descriptor = _text(mesh.find("./DescriptorName"))
        qualifiers = [_text(q) for q in mesh.findall("./QualifierName") if _text(q)]
        if descriptor and qualifiers:
            out.append(f"{descriptor}: {', '.join(qualifiers)}")
        elif descriptor:
            out.append(descriptor)
    return out


def _list_text(parent: ET.Element, path: str) -> List[str]:
    return [_node_text(node) for node in parent.findall(path) if _node_text(node)]


def _node_text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return " ".join(" ".join(node.itertext()).split())


def _text(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
