"""Force-include specific papers via config/manual_pmids.csv.

Sometimes a paper that clearly belongs to a bioactive isn't surfaced by any search
rule (odd indexing, an unusual title, a very new record). This lets a curator name it
explicitly. Each row (molecule_id, pmid, note) is turned into a synthetic search rule
that queries that exact PMID (``<pmid>[uid]``), so the normal fetch path picks it up
and attributes it to the named molecule -- no special-case code in the fetch loop.

Kept dependency-free (stdlib csv only) so it imports cheaply and is easy to test.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional, Set

MANUAL_PMIDS_PATH = os.path.join("config", "manual_pmids.csv")


def load_manual_rules(path: str = MANUAL_PMIDS_PATH,
                      known_molecule_ids: Optional[Set[str]] = None) -> List[Dict[str, str]]:
    """Return synthetic search-rule rows for each valid manual entry.

    Skips rows with a non-numeric PMID or (when ``known_molecule_ids`` is given) an
    unknown molecule_id, so a typo can't create an orphan rule. Missing file -> [].
    """
    rows: List[Dict[str, str]] = []
    if not os.path.exists(path):
        return rows
    seen: Set[tuple] = set()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                mid = str(r.get("molecule_id", "") or "").strip()
                pmid = str(r.get("pmid", "") or "").strip()
                if not mid or not pmid.isdigit():
                    continue
                if known_molecule_ids is not None and mid not in known_molecule_ids:
                    continue
                key = (pmid, mid)
                if key in seen:
                    continue
                seen.add(key)
                note = str(r.get("note", "") or "").strip()
                rows.append({
                    "molecule_id": mid,
                    "rule_id": f"manual_{pmid}",
                    "match_strength": "strong",   # curator-asserted -> keep it
                    "query_string": f"{pmid}[uid]",  # esearch returns exactly this PMID
                    "active": "True",
                    "notes": ("manual add: " + note) if note else "manual add",
                })
    except (OSError, csv.Error):
        return []
    return rows
