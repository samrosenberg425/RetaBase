from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List
from urllib.parse import quote

import requests

from .profiles import PROFILE_FIELDS
from .paper_characterizer import PAPER_CHARACTERIZATION_FIELDS
from .processing_router import PROCESSING_ROUTE_FIELDS
from .role_classifier import ROLE_FIELDS


PAPER_FIELDS = [
    "pmid", "title", "abstract", "journal", "authors", "doi", "pub_date_iso", "pub_year",
    "pubtypes", "mesh_terms", "keywords", "chemicals", "language", "article_ids", "pubmed_url",
    "updated_at_utc",
]

EVIDENCE_FIELDS = [
    "evidence_id", "pmid", "molecule_id", "molecule_name", "rule_id", "match_strength",
    "source_query_hash", "run_id", "fetched_at_utc", "pub_year", "primary_study_type", "study_design_tags",
    "model_type", "species_or_population", "human_flag", "animal_flag", "in_vitro_flag",
    "classification_confidence", "classification_notes", "molecule_relevance", "relevance_confidence",
    "website_include", "relevance_notes", "evidence_summary", "key_result_sentence",
    "safety_signal_sentence", "summary_source", "summary_needs_review",
    *ROLE_FIELDS, *PAPER_CHARACTERIZATION_FIELDS, *PROCESSING_ROUTE_FIELDS, "review_status",
]


class PipelineState:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "create table if not exists seen_evidence (evidence_id text primary key, first_seen_at text)"
        )
        self.conn.commit()

    def seen(self, evidence_id: str) -> bool:
        cur = self.conn.execute("select 1 from seen_evidence where evidence_id = ?", (evidence_id,))
        return cur.fetchone() is not None

    def mark_seen(self, evidence_id: str, timestamp: str) -> None:
        self.conn.execute(
            "insert or ignore into seen_evidence (evidence_id, first_seen_at) values (?, ?)",
            (evidence_id, timestamp),
        )
        self.conn.commit()


class BaseSink:
    def upsert_molecules(self, molecules: Iterable[dict]) -> None:
        pass

    def upsert_papers(self, papers: List[dict]) -> None:
        pass

    def upsert_evidence(self, evidence: List[dict]) -> None:
        pass

    def upsert_molecule_profiles(self, profiles: List[dict]) -> None:
        pass


class LocalSQLiteSink(BaseSink):
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute(
            "create table if not exists molecules (molecule_id text primary key, payload_json text)"
        )
        self.conn.execute(
            "create table if not exists papers (pmid text primary key, payload_json text, updated_at_utc text)"
        )
        self.conn.execute(
            "create table if not exists evidence (evidence_id text primary key, payload_json text, updated_at_utc text)"
        )
        self.conn.execute(
            "create table if not exists molecule_profiles (molecule_id text primary key, payload_json text, updated_at_utc text)"
        )
        self.conn.commit()

    def upsert_molecules(self, molecules: Iterable[dict]) -> None:
        rows = [(m.get("molecule_id", ""), json.dumps(m, ensure_ascii=False, sort_keys=True)) for m in molecules]
        self.conn.executemany(
            "insert into molecules (molecule_id, payload_json) values (?, ?) "
            "on conflict(molecule_id) do update set payload_json = excluded.payload_json",
            rows,
        )
        self.conn.commit()

    def upsert_papers(self, papers: List[dict]) -> None:
        rows = [(p["pmid"], json.dumps(p, ensure_ascii=False, sort_keys=True), p.get("updated_at_utc", "")) for p in papers]
        self.conn.executemany(
            "insert into papers (pmid, payload_json, updated_at_utc) values (?, ?, ?) "
            "on conflict(pmid) do update set payload_json = excluded.payload_json, updated_at_utc = excluded.updated_at_utc",
            rows,
        )
        self.conn.commit()

    def upsert_evidence(self, evidence: List[dict]) -> None:
        rows = [
            (e["evidence_id"], json.dumps(e, ensure_ascii=False, sort_keys=True), e.get("fetched_at_utc", ""))
            for e in evidence
        ]
        self.conn.executemany(
            "insert into evidence (evidence_id, payload_json, updated_at_utc) values (?, ?, ?) "
            "on conflict(evidence_id) do update set payload_json = excluded.payload_json, updated_at_utc = excluded.updated_at_utc",
            rows,
        )
        self.conn.commit()

    def upsert_molecule_profiles(self, profiles: List[dict]) -> None:
        rows = [
            (p["molecule_id"], json.dumps(p, ensure_ascii=False, sort_keys=True), p.get("profile_updated_at_utc", ""))
            for p in profiles
        ]
        self.conn.executemany(
            "insert into molecule_profiles (molecule_id, payload_json, updated_at_utc) values (?, ?, ?) "
            "on conflict(molecule_id) do update set payload_json = excluded.payload_json, updated_at_utc = excluded.updated_at_utc",
            rows,
        )
        self.conn.commit()


class GoogleSheetsSink(BaseSink):
    def __init__(self, *, spreadsheet_name: str, worksheet_name: str = "PAPERS_MASTER", profiles_worksheet_name: str = "MOLECULE_PROFILES"):
        import gspread
        from google.auth import default

        creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        try:
            sh = gc.open(spreadsheet_name)
        except gspread.SpreadsheetNotFound:
            sh = gc.create(spreadsheet_name)
        self.ws = _open_or_create_ws(sh, worksheet_name, EVIDENCE_FIELDS)
        self.profile_ws = _open_or_create_ws(sh, profiles_worksheet_name, PROFILE_FIELDS)

    def upsert_evidence(self, evidence: List[dict]) -> None:
        if not evidence:
            return
        rows = [[_sheet_value(row.get(field, "")) for field in EVIDENCE_FIELDS] for row in evidence]
        self.ws.append_rows(rows, value_input_option="RAW")

    def upsert_molecule_profiles(self, profiles: List[dict]) -> None:
        if not profiles:
            return
        self.profile_ws.clear()
        self.profile_ws.append_row(PROFILE_FIELDS)
        rows = [[_sheet_value(row.get(field, "")) for field in PROFILE_FIELDS] for row in profiles]
        self.profile_ws.append_rows(rows, value_input_option="RAW")


class AirtableSink(BaseSink):
    def __init__(
        self,
        *,
        api_key: str,
        base_id: str,
        molecules_table: str = "Molecules",
        papers_table: str = "Papers",
        evidence_table: str = "Evidence",
        profiles_table: str = "MoleculeProfiles",
    ):
        if not api_key or not base_id:
            raise ValueError("AIRTABLE_API_KEY and AIRTABLE_BASE_ID are required for AirtableSink")
        self.api_key = api_key
        self.base_id = base_id
        self.tables = {
            "molecules": molecules_table,
            "papers": papers_table,
            "evidence": evidence_table,
            "profiles": profiles_table,
        }

    def upsert_molecules(self, molecules: Iterable[dict]) -> None:
        records = [{"fields": _airtable_fields(m)} for m in molecules]
        self._upsert(self.tables["molecules"], records, ["molecule_id"])

    def upsert_papers(self, papers: List[dict]) -> None:
        records = [{"fields": _airtable_fields(p)} for p in papers]
        self._upsert(self.tables["papers"], records, ["pmid"])

    def upsert_evidence(self, evidence: List[dict]) -> None:
        records = [{"fields": _airtable_fields(e)} for e in evidence]
        self._upsert(self.tables["evidence"], records, ["evidence_id"])

    def upsert_molecule_profiles(self, profiles: List[dict]) -> None:
        records = [{"fields": _airtable_fields(p)} for p in profiles]
        self._upsert(self.tables["profiles"], records, ["molecule_id"])

    def _upsert(self, table: str, records: List[dict], merge_fields: List[str]) -> None:
        if not records:
            return
        url = f"https://api.airtable.com/v0/{self.base_id}/{quote(table, safe='')}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for chunk in _chunks(records, 10):
            payload = {
                "records": chunk,
                "performUpsert": {"fieldsToMergeOn": merge_fields},
                "typecast": True,
            }
            response = requests.patch(url, headers=headers, json=payload, timeout=60)
            if response.status_code == 429:
                time.sleep(30)
                response = requests.patch(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            time.sleep(0.22)


@dataclass
class SinkSet:
    sinks: List[BaseSink]

    def upsert_molecules(self, molecules: Iterable[dict]) -> None:
        molecules = list(molecules)
        for sink in self.sinks:
            sink.upsert_molecules(molecules)

    def upsert_papers(self, papers: List[dict]) -> None:
        for sink in self.sinks:
            sink.upsert_papers(papers)

    def upsert_evidence(self, evidence: List[dict]) -> None:
        for sink in self.sinks:
            sink.upsert_evidence(evidence)

    def upsert_molecule_profiles(self, profiles: List[dict]) -> None:
        for sink in self.sinks:
            sink.upsert_molecule_profiles(profiles)


def build_sinks(names: str, *, local_db: str, google_sheet_name: str) -> SinkSet:
    sinks: List[BaseSink] = []
    for name in [x.strip().lower() for x in names.split(",") if x.strip()]:
        if name == "local":
            sinks.append(LocalSQLiteSink(local_db))
        elif name == "google":
            sinks.append(GoogleSheetsSink(spreadsheet_name=google_sheet_name))
        elif name == "airtable":
            sinks.append(
                AirtableSink(
                    api_key=os.getenv("AIRTABLE_API_KEY", ""),
                    base_id=os.getenv("AIRTABLE_BASE_ID", ""),
                    molecules_table=os.getenv("AIRTABLE_MOLECULES_TABLE", "Molecules"),
                    papers_table=os.getenv("AIRTABLE_PAPERS_TABLE", "Papers"),
                    evidence_table=os.getenv("AIRTABLE_EVIDENCE_TABLE", "Evidence"),
                    profiles_table=os.getenv("AIRTABLE_PROFILES_TABLE", "MoleculeProfiles"),
                )
            )
        else:
            raise ValueError(f"Unknown sink: {name}")
    return SinkSet(sinks=sinks)


def _chunks(items: List[dict], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _sheet_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _airtable_fields(row: dict) -> Dict:
    fields = {}
    for key, value in row.items():
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            fields[key] = json.dumps(value, ensure_ascii=False)
        else:
            fields[key] = value
    return fields


def _open_or_create_ws(sh, worksheet_name: str, fields: List[str]):
    import gspread

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=max(len(fields), 20))
    if not ws.row_values(1):
        ws.append_row(fields)
    return ws
