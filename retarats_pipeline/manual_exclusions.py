"""Hold specific records OUT of the public site without deleting them (config/manual_exclusions.csv).

The mirror image of ``manual_add`` (force-include). A held record stays in the corpus and in
``curated_evidence.csv`` (so dedup still knows about it and it can be restored by deleting a
config row); it is only marked ``excluded_noise`` so it drops out of ``public_records.csv``.

Two row kinds, one file (columns: molecule_id, pmid, hold_if_text_has, but_not_if_text_has,
reason, added_on):

* PAPER row   -- ``pmid`` set: hold that one paper for that one molecule.
* PATTERN row -- ``pmid`` blank: hold every record of that molecule whose title/abstract
  contains any ``hold_if_text_has`` term (``|``-separated) UNLESS it also contains any
  ``but_not_if_text_has`` term. The protect list is REQUIRED, so a pattern can never be a
  blunt "drop everything mentioning X".

Fail-open by design: whenever something is doubtful, the record is KEPT, never hidden.

* Exact (molecule_id, pmid) matching only; a paper held for one molecule stays live under
  every other molecule it belongs to.
* A malformed row (no reason, unknown shape, empty protect list) is skipped with a warning.
* Records on the protected set -- approved benchmark ``include`` rows, ``manual_pmids.csv``
  force-includes, ``gold_standard_pmids.csv`` must-retrieve PMIDs -- are NEVER held.
* A pattern that would hold more than ``MAX_PATTERN_HOLD_FRACTION`` of a molecule's records
  is switched off for that build (and reported), so a bad pattern cannot gut a page.

Stdlib only, so it imports cheaply and is easy to test.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MANUAL_EXCLUSIONS_PATH = os.path.join("config", "manual_exclusions.csv")
BENCHMARK_PATH = os.path.join("config", "retrieval_benchmark.csv")
MANUAL_PMIDS_PATH = os.path.join("config", "manual_pmids.csv")
GOLD_PATH = os.path.join("config", "gold_standard_pmids.csv")

COLUMNS = ["molecule_id", "pmid", "hold_if_text_has", "but_not_if_text_has", "reason", "added_on"]
MAX_PATTERN_HOLD_FRACTION = 0.7
MIN_RECORDS_FOR_CAP = 20  # below this a fraction is meaningless; the cap only guards real pages


@dataclass
class Exclusion:
    molecule_id: str
    pmid: str = ""
    hold_terms: Tuple[str, ...] = ()
    protect_terms: Tuple[str, ...] = ()
    reason: str = ""
    added_on: str = ""
    row_no: int = 0

    @property
    def is_paper_row(self) -> bool:
        return bool(self.pmid)

    @property
    def label(self) -> str:
        what = f"pmid {self.pmid}" if self.is_paper_row else "pattern " + "|".join(self.hold_terms)
        return f"{self.molecule_id}: {what}"


@dataclass
class Exclusions:
    rules: List[Exclusion] = field(default_factory=list)
    protected: Set[Tuple[str, str]] = field(default_factory=set)  # (molecule_id, pmid) never held
    warnings: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.rules)


def _terms(value: str) -> Tuple[str, ...]:
    return tuple(t.strip() for t in (value or "").split("|") if t.strip())


def _term_regex(term: str) -> "re.Pattern[str]":
    """Case-insensitive whole-token match; hyphen/space/underscore are interchangeable AND optional
    so 'MT-II' also finds 'MT II' and 'MTII' (and 'LL-37' finds 'LL37'), while 'metallothionein'
    does not match inside longer words."""
    parts = [re.escape(p) for p in re.split(r"[\s_\-]+", term.strip()) if p]
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s_\-]*".join(parts) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _any_term(terms: Iterable[str], text: str) -> bool:
    return any(_term_regex(t).search(text) for t in terms)


def _read_rows(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_protected(benchmark_path: str = BENCHMARK_PATH, manual_path: str = MANUAL_PMIDS_PATH,
                   gold_path: str = GOLD_PATH) -> Set[Tuple[str, str]]:
    """(molecule_id, pmid) pairs that a human has said belong -- never hold these."""
    out: Set[Tuple[str, str]] = set()
    for r in _read_rows(benchmark_path):
        if (r.get("status", "").strip().lower() == "approved" and r.get("expected", "").strip().lower() == "include"
                and r.get("id_type", "").strip().lower() == "pmid"):
            out.add((r.get("molecule_id", "").strip(), r.get("id", "").strip()))
    for r in _read_rows(manual_path):
        out.add((r.get("molecule_id", "").strip(), r.get("pmid", "").strip()))
    for r in _read_rows(gold_path):
        for pmid in re.split(r"[;\s]+", r.get("must_retrieve_pmids", "") or ""):
            if pmid.isdigit():
                out.add((r.get("molecule_id", "").strip(), pmid))
    out.discard(("", ""))
    return out


def load_exclusions(path: str = MANUAL_EXCLUSIONS_PATH, known_molecule_ids: Optional[Set[str]] = None,
                    protected: Optional[Set[Tuple[str, str]]] = None) -> Exclusions:
    result = Exclusions(protected=set(protected) if protected is not None else load_protected())
    for i, r in enumerate(_read_rows(path), start=2):
        mid = (r.get("molecule_id") or "").strip()
        pmid = (r.get("pmid") or "").strip()
        hold = _terms(r.get("hold_if_text_has") or "")
        protect = _terms(r.get("but_not_if_text_has") or "")
        reason = (r.get("reason") or "").strip()
        where = f"{path}:{i}"
        if not mid:
            continue  # blank line
        if known_molecule_ids is not None and mid not in known_molecule_ids:
            result.warnings.append(f"{where}: unknown molecule_id {mid!r}; row ignored")
            continue
        if not reason:
            result.warnings.append(f"{where}: a reason is required; row ignored")
            continue
        if pmid and hold:
            result.warnings.append(f"{where}: set EITHER pmid OR hold_if_text_has, not both; row ignored")
            continue
        if pmid:
            if not pmid.isdigit():
                result.warnings.append(f"{where}: pmid {pmid!r} is not numeric; row ignored")
                continue
            if (mid, pmid) in result.protected:
                result.warnings.append(f"{where}: {mid}/{pmid} is protected (approved include / manual / gold); row ignored")
                continue
        elif hold:
            if not protect:
                result.warnings.append(f"{where}: pattern rows need but_not_if_text_has (protect terms); row ignored")
                continue
        else:
            result.warnings.append(f"{where}: needs a pmid or hold_if_text_has; row ignored")
            continue
        result.rules.append(Exclusion(mid, pmid, hold, protect, reason, (r.get("added_on") or "").strip(), i))
    return result


def record_text(row: dict) -> str:
    return " ".join(str(row.get(k, "") or "") for k in ("title", "abstract", "keywords"))


@dataclass
class HoldDecision:
    index: int          # position in the evidence list
    rule: Exclusion


def plan_holds(excl: Exclusions, rows: Sequence[dict]) -> Tuple[Dict[int, Exclusion], List[str]]:
    """Decide which rows to hold. ``rows`` are evidence rows already carrying title/abstract.

    Returns ({row_index: rule}, messages). Pure function: no I/O, deterministic."""
    holds: Dict[int, Exclusion] = {}
    msgs: List[str] = list(excl.warnings)
    if not excl.rules:
        return holds, msgs

    by_mol: Dict[str, List[int]] = {}
    for i, r in enumerate(rows):
        by_mol.setdefault(str(r.get("molecule_id", "") or ""), []).append(i)

    for rule in excl.rules:
        idxs = by_mol.get(rule.molecule_id, [])
        if rule.is_paper_row:
            hit = [i for i in idxs if str(rows[i].get("pmid", "") or "") == rule.pmid]
            if not hit:
                msgs.append(f"{rule.label}: matches no record (stale row; safe to delete)")
            for i in hit:
                holds.setdefault(i, rule)
            continue
        hit = []
        for i in idxs:
            pmid = str(rows[i].get("pmid", "") or "")
            if (rule.molecule_id, pmid) in excl.protected:
                continue
            text = record_text(rows[i])
            if _any_term(rule.hold_terms, text) and not _any_term(rule.protect_terms, text):
                hit.append(i)
        frac = len(hit) / len(idxs) if idxs else 0.0
        if len(idxs) >= MIN_RECORDS_FOR_CAP and frac > MAX_PATTERN_HOLD_FRACTION:
            msgs.append(f"{rule.label}: would hold {len(hit)}/{len(idxs)} ({frac:.0%}) of the molecule's records, "
                        f"above the {MAX_PATTERN_HOLD_FRACTION:.0%} safety cap -- rule NOT applied")
            continue
        for i in hit:
            holds.setdefault(i, rule)
        if not hit:
            msgs.append(f"{rule.label}: holds nothing this build")
    return holds, msgs


def hold_decision_fields(rule: Exclusion) -> dict:
    """The publication fields for a held record (same shape as PublicationDecision.to_dict())."""
    return {
        "publication_status": "excluded_noise",
        "website_section": "",
        "auto_publish_eligible": False,
        "review_reason": f"manual exclusion: {rule.reason}",
        "publish_rule_id": "manual:exclude",
        "display_priority": 0,
    }
