#!/usr/bin/env python3
"""Propose LOCAL-ONLY identity-benchmark candidates for human review (never approves anything).

    python3 scripts/build_identity_review.py --work work [--out-dir docs/retrieval_review/ws45]

For the high-risk molecules x sources it picks (deterministically, seeded) records of four kinds and proposes a label
from the CURRENT identity rules. A proposal is only a proposal: rows are written with status=proposed and nobody
reviewed them. A human edits ``expected`` where the proposal is wrong and flips ``status`` to approved; approved rows
are then copied into config/retrieval_benchmark.csv by hand (this script never touches the approved benchmark).

    true_positive         the title names the molecule                          -> expected include
    alias_only_positive   passes only through an alias / context rule            -> expected include
    false_positive        no name of the molecule anywhere in the stored text    -> expected exclude
    ambiguous             an ambiguous alias is present but its context is missing / vetoed -> expected exclude
                          (or present WITH context but no canonical name: include, flagged ambiguous)

Outputs (all under docs/retrieval_review/, git-excluded -- never commit): identity_benchmark_candidates.csv (every
proposal with a keyword-in-context snippet) and IDENTITY_REVIEW_QUEUE.md (compact checklist grouped by molecule).
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audit_identity as A  # noqa: E402
from retarats_pipeline import identity as idn  # noqa: E402
from retarats_pipeline.enrichment.registry import load_active_molecules  # noqa: E402

HIGH_RISK = [
    "melanotan_ii", "vip", "ldn", "thymosin_beta_4", "thymosin_alpha_1", "thymopentin", "dsip", "mots_c", "nadh", "nmn",
    "glutathione", "akg", "myostatin", "ergothioneine", "kisspeptin", "alcar", "rapamycin", "teriparatide", "sermorelin",
    "tesamorelin", "metformin", "p22_2", "gdf15", "taurine", "spermidine", "humanin", "tb_500", "cjc_1295", "ll_37",
    "ss_31", "angiotensin_1_7", "nicotinamide_riboside", "mgf_igf1ec", "pt_141", "sr9009",
]
ID_TYPE = {"pubmed": "pmid", "ctgov": "nct_id", "preprints": "doi"}
PER_KIND = 1  # records per (molecule, source, kind)


def snippet(zones: Dict[str, str], pattern: Optional["re.Pattern[str]"], width: int = 80) -> str:
    text = " || ".join(v for v in zones.values() if v)
    if pattern is not None:
        m = pattern.search(text)
        if m:
            a, b = max(0, m.start() - width), min(len(text), m.end() + width)
            return ("..." if a else "") + text[a:b].replace("\n", " ") + ("..." if b < len(text) else "")
    return text[:2 * width].replace("\n", " ")


def build(work: str, out_dir: str, public_records: str = "") -> None:
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = A.load_sources(work, public_records)
    rows: List[dict] = []
    for mid in HIGH_RISK:
        mi = cfg.by_molecule.get(mid)
        if mi is None:
            continue
        canon_patterns = [r.pattern for r in mi.canonical]
        ctx_rules = {r.term: r for r in mi.contextual}
        for source, recs in data.items():
            pool = [r for r in recs if r["molecule_id"] == mid and r["published"] is not False]
            if not pool:
                continue
            kinds: Dict[str, list] = defaultdict(list)
            for r in pool:
                v = idn.evaluate(cfg, source, mid, r["zones"], r["key"])
                title = r["zones"].get("title") or r["zones"].get("brief_title") or ""
                if v.outcome == idn.PASS and v.match_type == idn.M_CANONICAL and any(p.search(title) for p in canon_patterns):
                    kinds["true_positive"].append((r, v))
                elif v.outcome == idn.PASS and v.match_type in (idn.M_SPECIFIC, idn.M_CONTEXTUAL, idn.M_INSUFFICIENT):
                    kinds["alias_only_positive" if v.match_type != idn.M_CONTEXTUAL else "ambiguous"].append((r, v))
                elif v.outcome == idn.HOLD and v.match_type == idn.M_NONE:
                    kinds["false_positive"].append((r, v))
                elif v.match_type in (idn.M_CONTEXTUAL, idn.M_EXCLUSION):
                    kinds["ambiguous"].append((r, v))
            for kind, items in kinds.items():
                items.sort(key=lambda rv: rv[0]["key"])
                random.Random(f"{mid}|{source}|{kind}").shuffle(items)
                for r, v in items[:PER_KIND + (1 if kind == "ambiguous" else 0)]:
                    expected = "include" if v.outcome == idn.PASS else "exclude"
                    pat = (ctx_rules[v.matched_term].pattern if v.matched_term in ctx_rules else
                           next((x.pattern for x in mi.canonical + mi.specific if x.term == v.matched_term), None))
                    rows.append({
                        "molecule_id": mid, "id_type": ID_TYPE[source], "id": r["key"], "expected": expected,
                        "reason": f"[{kind}] model-proposed from identity rules: {v.outcome}/{v.match_type}"
                                  f"{('/' + v.matched_term) if v.matched_term else ''} {v.reason}".strip(),
                        "status": "proposed", "reviewed_by": "", "reviewed_on": "",
                        "kind": kind, "source": source,
                        "title": (r["zones"].get("title") or r["zones"].get("brief_title") or "")[:140],
                        "snippet": snippet(r["zones"], pat),
                    })
    os.makedirs(out_dir, exist_ok=True)
    cols = ["molecule_id", "id_type", "id", "expected", "reason", "status", "reviewed_by", "reviewed_on", "kind", "source",
            "title", "snippet"]
    with open(os.path.join(out_dir, "identity_benchmark_candidates.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    by_mol: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_mol[r["molecule_id"]].append(r)
    lines = ["# Identity benchmark review queue (LOCAL ONLY)", "",
             f"{len(rows)} model-PROPOSED rows over {len(by_mol)} molecules. Nothing here is approved. For each line, "
             "tick **keep** if the proposed label is right, or write the corrected label. Approved rows are then added to "
             "`config/retrieval_benchmark.csv` by hand (`status=approved`, your name, date).", "",
             "Legend: `include` = the record IS about the molecule; `exclude` = it is NOT. Kinds: TP obvious true positive, "
             "ALIAS true positive found only via an alias, FP no name at all, AMBIG ambiguous alias (context decides).", ""]
    tag = {"true_positive": "TP", "alias_only_positive": "ALIAS", "false_positive": "FP", "ambiguous": "AMBIG"}
    for mid, items in by_mol.items():
        lines.append(f"## {mid}")
        lines.append("")
        for r in sorted(items, key=lambda r: (r["source"], r["kind"])):
            lines.append(f"- [ ] **{r['expected']}** `{tag[r['kind']]}` {r['source']} `{r['id']}` - {r['title'][:90]}")
            lines.append(f"  - `{r['snippet'][:230]}`")
        lines.append("")
    with open(os.path.join(out_dir, "IDENTITY_REVIEW_QUEUE.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    cnt = defaultdict(int)
    for r in rows:
        cnt[(r["source"], r["kind"])] += 1
    print(f"{len(rows)} proposed rows ({dict(cnt)}) -> {out_dir}/identity_benchmark_candidates.csv, IDENTITY_REVIEW_QUEUE.md")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="work")
    ap.add_argument("--out-dir", default=A.DEFAULT_OUT)
    ap.add_argument("--public-records", default="", help="curated public_records.csv of the CURRENT build (to know which "
                                                         "PubMed records are published); blank = all stored evidence")
    args = ap.parse_args()
    build(args.work, args.out_dir, args.public_records)


if __name__ == "__main__":
    main()
