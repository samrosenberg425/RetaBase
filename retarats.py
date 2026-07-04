# -*- coding: utf-8 -*-
"""
RetaRats PubMed Pipeline (script-friendly)

Runs as a normal Python script (no Colab magics).

Config sources:
- CONFIG_MODE="local"  -> reads config/MOLECULES.csv + config/SEARCH_RULES.csv
- CONFIG_MODE="google" -> reads Google Sheet named CONFIG_SHEET_NAME with tabs MOLECULES + SEARCH_RULES

Google auth:
- Uses Application Default Credentials (ADC) for Sheets + Drive APIs.

Required env vars:
- NCBI_EMAIL (required)
- CONFIG_MODE (default: "local")
- LOCAL_CONFIG_DIR (default: "config") when CONFIG_MODE="local"

Optional env vars:
- NCBI_API_KEY (recommended)
- CONFIG_SHEET_NAME (default: "Moleculessearch") when CONFIG_MODE="google"
- NCBI_TOOL (default: "retarats_pubmed_bot")
- DRIVE_FOLDER_PATH (default: "My Drive/Retarats")
- OUTPUT_BASE_NAME (default: "RetaRats_PubMed")
- RUN_MODE (default: "AUTO")  # AUTO, BACKFILL_ONLY, DAILY_ONLY
- DEFAULT_START_YEAR (default: 2000)
- DEFAULT_DAILY_DAYS (default: 1)
"""

import os, json, time, random, re, hashlib, datetime as dt
from typing import List, Tuple  # FIX: use typing.Tuple for Python 3.8 compat

import requests
import pandas as pd

# FIX: load .env file so desktop users don't need to manually export env vars
from dotenv import load_dotenv
load_dotenv()

import gspread
from google.auth import default
from googleapiclient.discovery import build

from requests.exceptions import (
    ChunkedEncodingError,
    ContentDecodingError,
    ConnectionError as ReqConnectionError,
    Timeout as ReqTimeout,
)
from urllib3.exceptions import ProtocolError


# =========================
# Defaults / env
# =========================
BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

NCBI_TOOL  = os.getenv("NCBI_TOOL", "retarats_pubmed_bot")
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "").strip()
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()

CONFIG_MODE = os.getenv("CONFIG_MODE", "local").strip().lower()  # local | google
LOCAL_CONFIG_DIR = os.getenv("LOCAL_CONFIG_DIR", "config").strip()
CONFIG_SHEET_NAME = os.getenv("CONFIG_SHEET_NAME", "Moleculessearch").strip()

DRIVE_FOLDER_PATH = os.getenv("DRIVE_FOLDER_PATH", "My Drive/Retarats").strip()
OUTPUT_BASE_NAME  = os.getenv("OUTPUT_BASE_NAME", "RetaRats_PubMed").strip()

RUN_MODE = os.getenv("RUN_MODE", "AUTO").strip().upper()
DEFAULT_START_YEAR = int(os.getenv("DEFAULT_START_YEAR", "2000"))
DEFAULT_DAILY_DAYS = int(os.getenv("DEFAULT_DAILY_DAYS", "1"))
DEFAULT_RETMAX = 200

# Throttle/retry tuning
EFETCH_BATCH = 50
MAX_RETRIES  = 7
BACKOFF_BASE = 1.7
TIMEOUT_S    = 60

# Sheets append chunking
APPEND_CHUNK_SIZE = 250

# Output schema
MASTER_COLUMNS = [
    "pmid",
    "molecule_id",
    "rule_id",
    "match_strength",
    "title",
    "abstract",
    "journal",
    "authors",
    "doi",
    "pub_date_iso",
    "pub_year",
    "pubtypes",
    "mesh_terms",
    "keywords",
    "PaperType_Final",
    "StudyClassification",
    "themes",
    "source_query_hash",
    "run_id",
    "fetched_at_utc",
]

TAB_MASTER   = "PAPERS_MASTER"
TAB_STATS    = "STATS"
TAB_QUALITY  = "QUALITY_ALERTS"


# =========================
# Google auth (ADC)
# =========================
def google_clients():
    """
    Uses Application Default Credentials (ADC).
    Needs Sheets + Drive scopes.
    """
    creds, _ = default(scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return creds, gc, drive_service


# =========================
# Drive helpers
# =========================
def _get_drive_folder_id(drive_service, folder_path: str) -> str:
    parts = [p for p in folder_path.strip("/").split("/") if p]
    parent = "root"
    if parts and parts[0].strip().lower() in ("my drive", "mydrive", "root"):
        parts = parts[1:]

    for part in parts:
        q = (
            f"name='{part}' and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent}' in parents and trashed=false"
        )
        res = drive_service.files().list(q=q, fields="files(id,name)").execute()
        if not res.get("files"):
            raise ValueError(f"Drive folder not found: '{part}' (in path '{folder_path}'). "
                             "Please create this folder in Google Drive first.")
        parent = res["files"][0]["id"]
    return parent

def ensure_sheet_in_folder(drive_service, sh, folder_path: str):
    folder_id = _get_drive_folder_id(drive_service, folder_path)
    meta = drive_service.files().get(fileId=sh.id, fields="parents").execute()
    parents = meta.get("parents", [])
    if folder_id in parents:
        return
    remove_parents = ",".join(parents) if parents else None
    kwargs = dict(fileId=sh.id, addParents=folder_id, fields="id,parents")
    if remove_parents:
        kwargs["removeParents"] = remove_parents
    drive_service.files().update(**kwargs).execute()

def open_sheet(gc, drive_service, spreadsheet_name: str, folder_path: str):
    created = False
    try:
        sh = gc.open(spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(spreadsheet_name)
        created = True
    if created:
        ensure_sheet_in_folder(drive_service, sh, folder_path)
    return sh


# =========================
# PubMed HTTP (throttle + retry)
# =========================
_last_req_ts = 0.0

def make_http_get(
    NCBI_TOOL: str,
    NCBI_EMAIL: str,
    NCBI_API_KEY: str,
    MAX_REQ_PER_SEC: float,
    MAX_RETRIES: int,
    BACKOFF_BASE: float,
    TIMEOUT_S: int,
):
    min_interval = 1.0 / max(MAX_REQ_PER_SEC, 0.1)

    def _throttle():
        global _last_req_ts
        now = time.time()
        wait = min_interval - (now - _last_req_ts)
        if wait > 0:
            time.sleep(wait)
        _last_req_ts = time.time()

    def http_get(url, params, timeout=TIMEOUT_S):
        params = dict(params or {})
        params.setdefault("tool", NCBI_TOOL)
        params.setdefault("email", NCBI_EMAIL)
        if NCBI_API_KEY:
            params["api_key"] = NCBI_API_KEY

        attempt = 0
        while True:
            _throttle()
            try:
                r = requests.get(url, params=params, timeout=timeout, stream=False)
                if r.status_code in (429, 500, 502, 503, 504):
                    attempt += 1
                    if attempt > MAX_RETRIES:
                        r.raise_for_status()
                    time.sleep((BACKOFF_BASE ** attempt) + random.uniform(0, 0.8))
                    continue
                r.raise_for_status()
                return r
            except (ReqTimeout, ReqConnectionError, ChunkedEncodingError, ContentDecodingError, ProtocolError):
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                time.sleep((BACKOFF_BASE ** attempt) + random.uniform(0, 0.8))

    return http_get


# =========================
# PubMed API helpers
# =========================
def esearch(http_get, term, mindate=None, maxdate=None, reldate=None, retstart=0, retmax=200):
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retstart": retstart,
        "retmax": retmax,
        "sort": "pub date",
    }
    if reldate is not None:
        params["reldate"] = reldate
    if mindate:
        params["mindate"] = mindate
    if maxdate:
        params["maxdate"] = maxdate

    r = http_get(f"{BASE}/esearch.fcgi", params)
    return r.json()["esearchresult"]

def esummary(http_get, id_list: List[str]):
    if not id_list:
        return {}
    params = {"db": "pubmed", "id": ",".join(id_list), "retmode": "json"}
    r = http_get(f"{BASE}/esummary.fcgi", params)
    return r.json()["result"]


# =========================
# Publication date parsing
# =========================
_MONTH_MAP = {
    "jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
    "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"
}

def _norm_pub_date(y, m=None, d=None):
    if not y:
        return ""
    m2 = "01"
    d2 = "01"
    if m:
        ms = str(m).strip()
        if ms.isdigit():
            m2 = ms.zfill(2)
        else:
            m2 = _MONTH_MAP.get(ms[:3].lower(), "01")
    if d:
        ds = str(d).strip()
        if ds.isdigit():
            d2 = ds.zfill(2)
    return f"{y}-{m2}-{d2}"

def extract_year_from_date(s: str) -> str:
    m = re.search(r"(19|20)\d{2}", s or "")
    return m.group(0) if m else ""

def efetch_details_batched(http_get, pmids: List[str], batch_size=50):
    if not pmids:
        return {}, {}

    abstracts = {}
    pubdates  = {}

    for i in range(0, len(pmids), batch_size):
        batch = pmids[i:i + batch_size]
        params = {"db":"pubmed", "id":",".join(batch), "retmode":"xml"}
        r = http_get(f"{BASE}/efetch.fcgi", params)
        xml = r.text

        blocks = xml.split("<PubmedArticle>")
        for blk in blocks[1:]:
            pmid = ""
            if "<PMID" in blk and "</PMID>" in blk:
                pmid = blk.split("<PMID", 1)[1].split(">", 1)[1].split("</PMID>", 1)[0].strip()
            if not pmid:
                continue

            parts = []
            segs = blk.split("<AbstractText")
            for seg in segs[1:]:
                if "</AbstractText>" in seg:
                    body = seg.split(">", 1)[1].split("</AbstractText>", 1)[0]
                    parts.append(body.replace("\n", " ").strip())
            abstracts[pmid] = " ".join(parts)[:6000] if parts else ""

            y = m = d = None
            if "<ArticleDate" in blk and "</ArticleDate>" in blk:
                ad = blk.split("<ArticleDate",1)[1].split("</ArticleDate>",1)[0]
                if "<Year>" in ad:  y = ad.split("<Year>",1)[1].split("</Year>",1)[0].strip()
                if "<Month>" in ad: m = ad.split("<Month>",1)[1].split("</Month>",1)[0].strip()
                if "<Day>" in ad:   d = ad.split("<Day>",1)[1].split("</Day>",1)[0].strip()

            if not y and "<PubDate>" in blk and "</PubDate>" in blk:
                pdx = blk.split("<PubDate>",1)[1].split("</PubDate>",1)[0]
                if "<Year>" in pdx:  y = pdx.split("<Year>",1)[1].split("</Year>",1)[0].strip()
                if "<Month>" in pdx: m = pdx.split("<Month>",1)[1].split("</Month>",1)[0].strip()
                if "<Day>" in pdx:   d = pdx.split("<Day>",1)[1].split("</Day>",1)[0].strip()

            pubdates[pmid] = _norm_pub_date(y, m, d) if y else ""

    return abstracts, pubdates


# =========================
# Classification
# =========================
def paper_type_final(pubtypes: str, mesh_terms: str, abstract: str) -> str:
    pt = (pubtypes or "").lower()
    mh = (mesh_terms or "").lower()
    ab = (abstract or "").lower()

    def has_mh(x): return x.lower() in mh

    if ("meta-analysis" in pt or "systematic review" in pt or
        "meta-analysis" in ab or "systematic review" in ab):
        return "Systematic review / Meta-analysis"

    if ("randomized controlled trial" in pt or "randomised controlled trial" in pt or
        "randomized" in ab or "placebo" in ab):
        if has_mh("humans") or ("humans" in ab and "mice" not in ab and "rat" not in ab):
            return "RCT"

    humans  = has_mh("humans")
    animals = has_mh("animals") or has_mh("mice") or has_mh("rats")

    if "case reports" in pt or "case report" in ab or "case series" in ab:
        return "Case report / Case series"

    if humans and ("clinical trial" in pt or "intervention" in ab or "treated" in ab) and "random" not in ab:
        return "Human interventional non-RCT"

    if humans and any(k in ab for k in ["cohort", "case-control", "cross-sectional", "observational", "registry"]):
        return "Human observational"

    if animals and any(k in ab for k in ["mouse", "mice", "rat", "rats", "murine", "in vivo"]):
        return "Animal in vivo"

    if any(k in ab for k in ["in vitro", "cell line", "cells were", "cultured", "fibroblast", "hepg2", "hela"]):
        return "In vitro / cell"

    if any(k in pt for k in ["methods", "technical", "validation"]) or any(k in ab for k in ["mechanism", "pathway", "assay", "protocol"]):
        return "Methods / Mechanistic"

    if "review" in pt or "review" in ab:
        return "Review / narrative"

    return "Other"

def study_classification_multi(pubtypes: str, mesh_terms: str, abstract: str, title: str = "") -> str:
    pt = (pubtypes or "").lower()
    mh = (mesh_terms or "").lower()
    ab = (abstract or "").lower()
    ti = (title or "").lower()
    text = f"{ti} {ab}"

    labels = set()

    def has_any(s: str, terms) -> bool:
        return any(t in s for t in terms)

    if "meta-analysis" in pt or has_any(text, ["meta-analysis", "meta analysis"]):
        labels.add("Meta-Analysis")
    if "systematic review" in pt or "systematic review" in text:
        labels.add("Systematic Review")
    if ("review" in pt) or (("review" in text) and "systematic review" not in text):
        labels.add("Review")
    if "practice guideline" in pt or has_any(text, ["guideline", "consensus statement", "position statement"]):
        labels.add("Guideline/Consensus")

    if "randomized controlled trial" in pt or has_any(text, ["randomized", "randomised", "placebo", "double-blind", "single-blind"]):
        labels.add("Randomized")
    if "clinical trial" in pt or "clinical trial" in text or has_any(text, ["open-label", "single-arm", "dose-escalation", "dose escalation"]):
        labels.add("Clinical Trial")

    if has_any(text, ["phase i", "phase 1"]): labels.add("Phase 1")
    if has_any(text, ["phase ii", "phase 2"]): labels.add("Phase 2")
    if has_any(text, ["phase iii", "phase 3"]): labels.add("Phase 3")
    if has_any(text, ["phase iv", "phase 4"]): labels.add("Phase 4")

    if has_any(text, ["cohort", "case-control", "case control", "cross-sectional", "cross sectional", "observational"]):
        labels.add("Observational")
    if has_any(text, ["registry", "real-world", "real world", "claims database", "electronic health record", "ehr"]):
        labels.add("RWE/Registry")
    if "prospective" in text: labels.add("Prospective")
    if "retrospective" in text: labels.add("Retrospective")

    if "case reports" in pt or "case report" in text:
        labels.add("Case Report")
    if "case series" in text:
        labels.add("Case Series")

    if ("animals" in mh) or has_any(text, ["mouse", "mice", "rat", "rats", "murine", "porcine", "canine", "in vivo"]):
        labels.add("Animal/In Vivo")
    if has_any(text, ["in vitro", "cell line", "cells were", "cultured", "organoid"]):
        labels.add("In Vitro/Cell")

    if has_any(text, ["pharmacokinetic", "pharmacodynamic", "pk/pd", "bioavailability", "half-life"]):
        labels.add("PK/PD")
    if has_any(text, ["adverse event", "safety", "tolerability", "toxicity"]):
        labels.add("Safety/Tolerability")
    if has_any(text, ["protocol", "study protocol", "trial protocol", "feasibility", "pilot study"]):
        labels.add("Protocol/Pilot")
    if has_any(text, ["validation", "assay", "method", "workflow", "pipeline", "technique"]):
        labels.add("Methods/Validation")

    return "; ".join(sorted(labels)) if labels else "Other"


# =========================
# Config loaders
# =========================
def _truthy(x) -> bool:
    s = str(x).strip().lower()
    return s in ("true", "t", "1", "yes", "y")

def load_molecules_and_rules_google(gc, config_sheet_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    sh = gc.open(config_sheet_name)
    mol_ws = sh.worksheet("MOLECULES")
    rules_ws = sh.worksheet("SEARCH_RULES")
    mol = pd.DataFrame(mol_ws.get_all_records())
    rules = pd.DataFrame(rules_ws.get_all_records())
    return _normalize_config_frames(mol, rules)

def load_molecules_and_rules_local(config_dir: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    mol_path = os.path.join(config_dir, "MOLECULES.csv")
    rules_path = os.path.join(config_dir, "SEARCH_RULES.csv")
    if not os.path.exists(mol_path):
        raise FileNotFoundError(f"Missing {mol_path} — make sure your config directory is correct (LOCAL_CONFIG_DIR={config_dir})")
    if not os.path.exists(rules_path):
        raise FileNotFoundError(f"Missing {rules_path} — make sure your config directory is correct (LOCAL_CONFIG_DIR={config_dir})")
    mol = pd.read_csv(mol_path)
    rules = pd.read_csv(rules_path)
    return _normalize_config_frames(mol, rules)

def _normalize_config_frames(mol: pd.DataFrame, rules: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    mol = mol.copy()
    rules = rules.copy()

    mol["molecule_id"] = mol["molecule_id"].astype(str).str.strip()
    rules["molecule_id"] = rules["molecule_id"].astype(str).str.strip()
    rules["rule_id"] = rules["rule_id"].astype(str).str.strip()
    rules["match_strength"] = rules["match_strength"].astype(str).str.strip().str.lower()
    rules["query_string"] = rules["query_string"].astype(str).str.strip()

    if "active" in mol.columns:
        mol = mol[mol["active"].apply(_truthy)]
    if "active" in rules.columns:
        rules = rules[rules["active"].apply(_truthy)]

    mol_ids = set(mol["molecule_id"].tolist())
    rules = rules[rules["molecule_id"].isin(mol_ids)].copy()
    return mol, rules


# =========================
# Google Sheets hardening
# =========================
def gs_call(fn, *args, max_retries=7, **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            # FIX: surface quota/permission errors immediately instead of silently retrying
            status = getattr(e, "response", None)
            code = status.status_code if status else None
            if code in (401, 403):
                raise RuntimeError(
                    f"Google Sheets permission error (HTTP {code}): {e}\n"
                    "Check that your credentials have access to the spreadsheet."
                ) from e
            if attempt == max_retries:
                raise
            time.sleep((1.8 ** attempt) + random.uniform(0, 0.8))
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep((1.8 ** attempt) + random.uniform(0, 0.8))

def gs_append_rows(ws, rows, value_input_option="RAW", max_retries=7):
    return gs_call(ws.append_rows, rows, value_input_option=value_input_option, max_retries=max_retries)

def gs_append_row(ws, row, value_input_option="RAW", max_retries=7):
    return gs_call(ws.append_row, row, value_input_option=value_input_option, max_retries=max_retries)

def gs_update(ws, values, range_name="A1", max_retries=7):
    return gs_call(ws.update, values=values, range_name=range_name, max_retries=max_retries)

def gs_clear(ws, max_retries=7):
    return gs_call(ws.clear, max_retries=max_retries)

def _safe_sheet_title(s: str, max_len=90) -> str:
    s = (s or "").strip()
    s = re.sub(r"[:\\/?*\[\]]", "_", s)
    return s[:max_len]

def open_or_prepare_tab(sh, tab_name: str, headers: List[str], rows_hint=2000, cols_hint=None):
    tab_name = _safe_sheet_title(tab_name)
    if cols_hint is None:
        cols_hint = max(len(headers) + 2, 10)
    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = gs_call(sh.add_worksheet, title=tab_name, rows=str(rows_hint), cols=str(cols_hint))
        gs_append_row(ws, headers)
    header = gs_call(ws.row_values, 1)
    if not header:
        gs_append_row(ws, headers)
    elif header != headers:
        gs_update(ws, [headers], range_name="A1")
    return ws

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


# =========================
# Bucketing
# =========================
def _norm_type(t: str) -> str:
    t = (t or "").strip().lower()
    if t in ("small molecule", "small-molecule", "small_molecule", "smallmolecule"):
        return "small_molecule"
    if t in ("mixture", "blend", "extract"):
        return "mixture"
    if t in ("peptide", "protein", "biologic"):
        return "peptide"
    return t or "other"

def bucket_for_molecule_type(mol_type: str) -> str:
    t = _norm_type(mol_type)
    if t == "peptide": return "PEPTIDE"
    if t == "small_molecule": return "SMALL_MOLECULE"
    if t == "mixture": return "MIXTURE"
    return "OTHER"

# FIX: use Tuple from typing instead of lowercase tuple[] (Python 3.8 compat)
def sheet_names_for_bucket(bucket: str) -> Tuple[str, str]:
    return (f"{OUTPUT_BASE_NAME}_{bucket}_DATA", f"{OUTPUT_BASE_NAME}_{bucket}_TABS")


# =========================
# Query hash + row builder
# =========================
def query_hash12(q: str) -> str:
    return hashlib.sha1((q or "").encode("utf-8")).hexdigest()[:12]

def build_master_rows(uids, meta, abs_map, pubdate_map, molecule_id, rule_id, match_strength, qhash):
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

    out = []
    for pid in uids:
        item = meta.get(pid, {})

        title   = (item.get("title") or "").strip()
        journal = (item.get("fulljournalname") or item.get("source") or "").strip()

        doi = ""
        for x in item.get("articleids", []):
            if x.get("idtype") == "doi":
                doi = (x.get("value") or "").strip()
                break

        authors = ", ".join(a.get("name","") for a in item.get("authors", [])[:12])

        abstract = abs_map.get(pid, "") or ""
        pub_date_iso = (pubdate_map.get(pid) or "").strip()
        pub_year = extract_year_from_date(pub_date_iso) if pub_date_iso else extract_year_from_date(item.get("pubdate","") or "")

        pubtypes = "; ".join(pt.get("name","") for pt in item.get("publicationtypes", []) if pt.get("name"))
        mesh_terms = "; ".join(item.get("mesh_heading_list", [])) if item.get("mesh_heading_list") else ""
        keywords   = "; ".join(item.get("keywords", [])) if item.get("keywords") else ""

        ptype = paper_type_final(pubtypes, mesh_terms, abstract)
        themes = study_classification_multi(pubtypes, mesh_terms, abstract, title=title)

        row = [
            pid, molecule_id, rule_id, match_strength,
            title, abstract, journal, authors, doi,
            pub_date_iso, pub_year,
            pubtypes, mesh_terms, keywords,
            ptype, ptype, themes,
            qhash, run_id, fetched_at
        ]
        out.append(row)
    return out


# =========================
# Main runner
# =========================
def main():
    if not NCBI_EMAIL:
        raise SystemExit(
            "NCBI_EMAIL is not set.\n"
            "Copy .env.example to .env and fill in your email address.\n"
            "  cp .env.example .env\n"
            "Then edit .env and set: NCBI_EMAIL=your_email@example.com"
        )

    # FIX: surface Google auth errors with clear instructions instead of a raw traceback
    try:
        creds, gc, drive_service = google_clients()
    except Exception as e:
        raise SystemExit(
            f"Google authentication failed: {e}\n\n"
            "To fix this, run ONE of the following:\n"
            "  Option A (personal account):  gcloud auth application-default login\n"
            "  Option B (service account):   set GOOGLE_APPLICATION_CREDENTIALS=/path/to/service_account.json\n\n"
            "If you don't have the gcloud CLI, install it from: https://cloud.google.com/sdk/docs/install"
        )

    has_real_key = bool(NCBI_API_KEY and len(NCBI_API_KEY.strip()) > 10)
    max_rps = 4.0 if has_real_key else 2.0

    http_get = make_http_get(
        NCBI_TOOL=NCBI_TOOL,
        NCBI_EMAIL=NCBI_EMAIL,
        NCBI_API_KEY=NCBI_API_KEY,
        MAX_REQ_PER_SEC=max_rps,
        MAX_RETRIES=MAX_RETRIES,
        BACKOFF_BASE=BACKOFF_BASE,
        TIMEOUT_S=TIMEOUT_S,
    )

    # --- Load config ---
    if CONFIG_MODE == "local":
        mol_df, rules_df = load_molecules_and_rules_local(LOCAL_CONFIG_DIR)
    else:
        mol_df, rules_df = load_molecules_and_rules_google(gc, CONFIG_SHEET_NAME)

    mol_type_map = {str(r.get("molecule_id","")).strip(): str(r.get("type","peptide")) for _, r in mol_df.iterrows()}
    buckets = ["PEPTIDE","SMALL_MOLECULE","MIXTURE"]

    out_data = {}
    out_tabs = {}
    out_seen = {}

    # FIX: anchor state dir to the script's location, not CWD, so it works
    # regardless of where the user runs the script from
    script_dir = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.join(script_dir, "_state_pubmed_master")
    os.makedirs(state_dir, exist_ok=True)

    def seen_path(sheet_name: str) -> str:
        safe = re.sub(r"\W+","_", sheet_name).strip("_")
        return os.path.join(state_dir, f"seen__{safe}.json")

    def load_seen(sheet_name: str) -> set:
        p = seen_path(sheet_name)
        if os.path.exists(p):
            with open(p, "r") as f:
                data = json.load(f)
            return set(tuple(x) for x in data)
        return set()

    def save_seen(sheet_name: str, seen: set):
        p = seen_path(sheet_name)
        with open(p, "w") as f:
            json.dump([list(x) for x in sorted(seen)], f)

    # Prep outputs
    for b in buckets:
        data_name, tabs_name = sheet_names_for_bucket(b)

        sh_data = open_sheet(gc, drive_service, data_name, DRIVE_FOLDER_PATH)
        sh_tabs = open_sheet(gc, drive_service, tabs_name, DRIVE_FOLDER_PATH)

        ws_master = open_or_prepare_tab(sh_data, TAB_MASTER, MASTER_COLUMNS, rows_hint=2000, cols_hint=max(len(MASTER_COLUMNS)+2,10))

        out_data[b] = (sh_data, ws_master)
        out_tabs[b] = sh_tabs
        out_seen[b] = load_seen(data_name)

        # Pre-create molecule tabs
        for mid, mtype in mol_type_map.items():
            if bucket_for_molecule_type(mtype) == b:
                open_or_prepare_tab(sh_tabs, mid, MASTER_COLUMNS, rows_hint=200, cols_hint=max(len(MASTER_COLUMNS)+2,10))

    total_added = 0
    current_year = dt.datetime.today().year

    for _, rr in rules_df.iterrows():
        molecule_id = str(rr["molecule_id"]).strip()
        rule_id = str(rr["rule_id"]).strip()
        match_strength = str(rr["match_strength"]).strip().lower()
        query = str(rr["query_string"]).strip()

        bucket = bucket_for_molecule_type(mol_type_map.get(molecule_id, "peptide"))
        if bucket not in out_data:
            continue

        sh_data, ws_master = out_data[bucket]
        sh_tabs = out_tabs[bucket]
        data_sheet_name = sh_data.title
        seen = out_seen[bucket]
        qhash = query_hash12(query)

        # BACKFILL
        if RUN_MODE in ("AUTO","BACKFILL_ONLY"):
            for y in range(DEFAULT_START_YEAR, current_year + 1):
                start, end = f"{y}/01/01", f"{y}/12/31"
                first = esearch(http_get, query, mindate=start, maxdate=end, retstart=0, retmax=DEFAULT_RETMAX)
                count = int(first.get("count", 0))
                if count == 0:
                    continue

                retstart = 0
                while retstart < count:
                    res = esearch(http_get, query, mindate=start, maxdate=end, retstart=retstart, retmax=DEFAULT_RETMAX)
                    ids = res.get("idlist", [])
                    if not ids:
                        break

                    new_ids = [pid for pid in ids if (pid, molecule_id, rule_id) not in seen]
                    if new_ids:
                        meta = esummary(http_get, new_ids)
                        uids = meta.get("uids", [])
                        abs_map, pubdate_map = efetch_details_batched(http_get, uids, batch_size=EFETCH_BATCH)

                        rows = build_master_rows(uids, meta, abs_map, pubdate_map, molecule_id, rule_id, match_strength, qhash)

                        rows2 = []
                        for row in rows:
                            k = (row[0], row[1], row[2])
                            if k not in seen:
                                rows2.append(row)
                                seen.add(k)

                        if rows2:
                            for chunk in chunked(rows2, APPEND_CHUNK_SIZE):
                                gs_append_rows(ws_master, chunk)
                                total_added += len(chunk)

                            ws_mol = open_or_prepare_tab(sh_tabs, molecule_id, MASTER_COLUMNS, rows_hint=200, cols_hint=max(len(MASTER_COLUMNS)+2,10))
                            for chunk in chunked(rows2, APPEND_CHUNK_SIZE):
                                gs_append_rows(ws_mol, chunk)

                    retstart += DEFAULT_RETMAX

        # DAILY
        if RUN_MODE in ("AUTO","DAILY_ONLY"):
            first = esearch(http_get, query, reldate=DEFAULT_DAILY_DAYS, retstart=0, retmax=DEFAULT_RETMAX)
            count = int(first.get("count", 0))

            retstart = 0
            while retstart < count:
                res = esearch(http_get, query, reldate=DEFAULT_DAILY_DAYS, retstart=retstart, retmax=DEFAULT_RETMAX)
                ids = res.get("idlist", [])
                if not ids:
                    break

                new_ids = [pid for pid in ids if (pid, molecule_id, rule_id) not in seen]
                if new_ids:
                    meta = esummary(http_get, new_ids)
                    uids = meta.get("uids", [])
                    abs_map, pubdate_map = efetch_details_batched(http_get, uids, batch_size=EFETCH_BATCH)

                    rows = build_master_rows(uids, meta, abs_map, pubdate_map, molecule_id, rule_id, match_strength, qhash)

                    rows2 = []
                    for row in rows:
                        k = (row[0], row[1], row[2])
                        if k not in seen:
                            rows2.append(row)
                            seen.add(k)

                    if rows2:
                        for chunk in chunked(rows2, APPEND_CHUNK_SIZE):
                            gs_append_rows(ws_master, chunk)
                            total_added += len(chunk)

                        ws_mol = open_or_prepare_tab(sh_tabs, molecule_id, MASTER_COLUMNS, rows_hint=200, cols_hint=max(len(MASTER_COLUMNS)+2,10))
                        for chunk in chunked(rows2, APPEND_CHUNK_SIZE):
                            gs_append_rows(ws_mol, chunk)

                retstart += DEFAULT_RETMAX

        out_seen[bucket] = seen
        save_seen(data_sheet_name, seen)

    print(f"✅ DONE. Total rows added across outputs: {total_added}")


if __name__ == "__main__":
    main()
