from __future__ import annotations

import os
from zipfile import ZipFile
from dataclasses import dataclass
from typing import Tuple
from xml.etree import ElementTree as ET

import pandas as pd


XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_REL_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass(frozen=True)
class LoadedConfig:
    molecules: pd.DataFrame
    rules: pd.DataFrame


def truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "t", "1", "yes", "y"}


def load_config(
    *,
    mode: str = "local",
    local_config_dir: str = "config",
    google_sheet_name: str = "Moleculessearch",
    input_workbook: str = "inputs/Moleculessearch.xlsx",
    summary_workbook: str = "inputs/Summary Sheet.xlsx",
    gspread_client=None,
) -> LoadedConfig:
    mode = mode.strip().lower()
    if mode == "local":
        molecules, rules = _load_local(local_config_dir)
    elif mode in {"inputs", "excel", "xlsx"}:
        molecules, rules = _load_input_workbooks(input_workbook, summary_workbook)
    elif mode == "google":
        if gspread_client is None:
            raise ValueError("gspread_client is required when config mode is 'google'")
        molecules, rules = _load_google(gspread_client, google_sheet_name)
    else:
        raise ValueError(f"Unsupported config mode: {mode}")

    molecules, rules = normalize_config_frames(molecules, rules)
    return LoadedConfig(molecules=molecules, rules=rules)


def _load_local(config_dir: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    mol_path = os.path.join(config_dir, "MOLECULES.csv")
    rules_path = os.path.join(config_dir, "SEARCH_RULES.csv")
    if not os.path.exists(mol_path):
        raise FileNotFoundError(f"Missing {mol_path}")
    if not os.path.exists(rules_path):
        raise FileNotFoundError(f"Missing {rules_path}")
    return pd.read_csv(mol_path), pd.read_csv(rules_path)


def _load_google(gc, spreadsheet_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    sh = gc.open(spreadsheet_name)
    return (
        pd.DataFrame(sh.worksheet("MOLECULES").get_all_records()),
        pd.DataFrame(sh.worksheet("SEARCH_RULES").get_all_records()),
    )


def _load_input_workbooks(input_workbook: str, summary_workbook: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if not os.path.exists(input_workbook):
        raise FileNotFoundError(f"Missing input workbook: {input_workbook}")

    molecules = read_excel_sheet(input_workbook, "MOLECULES")
    rules = read_excel_sheet(input_workbook, "SEARCH_RULES")

    if summary_workbook and os.path.exists(summary_workbook):
        summary = read_excel_sheet(summary_workbook, "Main")
        molecules = merge_summary_fields(molecules, summary)

    return molecules, rules


def read_excel_sheet(path: str, sheet_name: str) -> pd.DataFrame:
    """Read an .xlsx sheet, falling back to a small stdlib parser.

    Pandas needs openpyxl for .xlsx files. Colab usually has it after
    requirements install, but this fallback makes local experiments less brittle.
    It supports the simple tabular workbooks used in `inputs/`.
    """
    try:
        return pd.read_excel(path, sheet_name=sheet_name)
    except ImportError:
        return _read_xlsx_sheet_stdlib(path, sheet_name)


def _read_xlsx_sheet_stdlib(path: str, sheet_name: str) -> pd.DataFrame:
    with ZipFile(path) as zf:
        sheet_path = _xlsx_sheet_path(zf, sheet_name)
        shared_strings = _xlsx_shared_strings(zf)
        root = ET.fromstring(zf.read(sheet_path))
        rows = []
        for row_node in root.findall(".//a:sheetData/a:row", XLSX_NS):
            row = []
            for cell in row_node.findall("a:c", XLSX_NS):
                idx = _xlsx_col_index(cell.attrib.get("r", "A"))
                while len(row) < idx:
                    row.append("")
                row.append(_xlsx_cell_value(cell, shared_strings))
            rows.append(row)

    if not rows:
        return pd.DataFrame()
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    headers = [str(x).strip() for x in rows[0]]
    data = rows[1:]
    return pd.DataFrame(data, columns=headers)


def _xlsx_sheet_path(zf: ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", XLSX_REL_NS)}
    for sheet in workbook.findall(".//a:sheet", XLSX_NS):
        if sheet.attrib.get("name") != sheet_name:
            continue
        rid = sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id")
        target = rel_map[rid]
        return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError(f"Sheet {sheet_name!r} not found in {zf.filename}")


def _xlsx_shared_strings(zf: ZipFile) -> list:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(text.text or "" for text in node.findall(".//a:t", XLSX_NS)) for node in root.findall("a:si", XLSX_NS)]


def _xlsx_cell_value(cell: ET.Element, shared_strings: list) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//a:t", XLSX_NS)).strip()
    value_node = cell.find("a:v", XLSX_NS)
    if value_node is None:
        return ""
    raw = value_node.text or ""
    if cell_type == "s" and raw.isdigit():
        index = int(raw)
        return shared_strings[index] if index < len(shared_strings) else raw
    return raw


def _xlsx_col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter.upper()) - 64
    return max(index - 1, 0)


def merge_summary_fields(molecules: pd.DataFrame, summary: pd.DataFrame) -> pd.DataFrame:
    """Attach curator-facing summary fields from inputs/Summary Sheet.xlsx.

    The summary workbook currently keys rows by display_name rather than molecule_id.
    Keeping this merge here lets that workbook act as a lightweight product/content
    guide without forcing its columns into the stricter search-rule workbook.
    """
    molecules = molecules.copy()
    summary = summary.copy()
    if "display_name" not in molecules.columns or "display_name" not in summary.columns:
        return molecules

    molecules["_display_name_key"] = molecules["display_name"].fillna("").astype(str).str.strip().str.lower()
    summary["_display_name_key"] = summary["display_name"].fillna("").astype(str).str.strip().str.lower()

    duplicate_cols = {
        "type",
        "mechanism_class",
        "status",
        "synonyms_csv",
    }
    keep_cols = [
        col
        for col in summary.columns
        if col == "_display_name_key" or (col not in molecules.columns and col not in duplicate_cols)
    ]
    merged = molecules.merge(summary[keep_cols], on="_display_name_key", how="left")
    return merged.drop(columns=["_display_name_key"])


def normalize_config_frames(molecules: pd.DataFrame, rules: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    molecules = molecules.copy()
    rules = rules.copy()

    required_molecule_cols = {"molecule_id", "display_name", "type"}
    required_rule_cols = {"rule_id", "molecule_id", "match_strength", "query_string"}
    missing_molecule_cols = required_molecule_cols - set(molecules.columns)
    missing_rule_cols = required_rule_cols - set(rules.columns)
    if missing_molecule_cols:
        raise ValueError(f"MOLECULES is missing required columns: {sorted(missing_molecule_cols)}")
    if missing_rule_cols:
        raise ValueError(f"SEARCH_RULES is missing required columns: {sorted(missing_rule_cols)}")

    for col in molecules.columns:
        molecules[col] = molecules[col].fillna("").astype(str).str.strip()
    for col in ["rule_id", "molecule_id", "match_strength", "query_string"]:
        rules[col] = rules[col].fillna("").astype(str).str.strip()

    rules["match_strength"] = rules["match_strength"].str.lower()

    if "active" in molecules.columns:
        molecules = molecules[molecules["active"].apply(truthy)].copy()
    if "active" in rules.columns:
        rules = rules[rules["active"].apply(truthy)].copy()

    active_molecule_ids = set(molecules["molecule_id"])
    rules = rules[rules["molecule_id"].isin(active_molecule_ids)].copy()
    return molecules.reset_index(drop=True), rules.reset_index(drop=True)


def molecule_lookup(molecules: pd.DataFrame) -> dict:
    out = {}
    for _, row in molecules.iterrows():
        molecule_id = str(row.get("molecule_id", "")).strip()
        out[molecule_id] = {k: _clean_value(v) for k, v in row.to_dict().items()}
    return out


def _clean_value(value):
    if pd.isna(value):
        return ""
    return value
