#!/usr/bin/env python3
"""WS4.5 identity audit: how well do the stored records of each source match the molecule they are filed under?

Read-only against the staged corpus (``scripts/corpus_state.py restore --dest work``); writes local review
tables (CSV) to ``--out-dir`` (default ``docs/retrieval_review/ws45/``, git-excluded -- never commit them).

    python3 scripts/audit_identity.py baseline  --work work --out-dir docs/retrieval_review/ws45
    python3 scripts/audit_identity.py terms     --work work --out-dir docs/retrieval_review/ws45
    python3 scripts/audit_identity.py compare   --work work --out-dir docs/retrieval_review/ws45

baseline  per molecule x source: stored / published / old-name-guard pass+fail (judged on the STORED text) /
          held-stale / manual-keep, sorted by the largest suspicious mismatch (published but unnamed).
terms     per molecule x source x name: how many published records contain the name, and how many of those
          contain NO other name of the molecule (the term is carrying the association alone).
compare   before/after: published records per molecule x source under the current identity rules.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline import identity as idn  # noqa: E402
from retarats_pipeline.enrichment.common import load_payload_table  # noqa: E402
from retarats_pipeline.enrichment.registry import (  # noqa: E402
    load_active_molecules, load_registry_keep, molecule_all_names, names_molecule)
from retarats_pipeline.enrichment.registry_stale import is_stale  # noqa: E402

DEFAULT_OUT = os.path.join("docs", "retrieval_review", "ws45")


def _rows(path: str, table: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    try:
        return load_payload_table(conn, table)
    finally:
        conn.close()


def load_sources(work: str, public_records: str = "") -> Dict[str, List[dict]]:
    """source -> list of normalised records {key, molecule_id, zones, stale, raw}."""
    out: Dict[str, List[dict]] = {}
    trials = []
    for r in _rows(os.path.join(work, "retarats_trials.sqlite"), "trials"):
        trials.append({"key": str(r.get("nct_id", "")).upper(), "molecule_id": r.get("molecule_id", ""),
                       "zones": idn.zones_for_trial(r), "stale": is_stale(r), "published": not is_stale(r), "raw": r})
    out["ctgov"] = trials
    pre = []
    for r in _rows(os.path.join(work, "retarats_preprints.sqlite"), "preprints"):
        pre.append({"key": str(r.get("id", "")), "molecule_id": r.get("molecule_id", ""),
                    "zones": idn.zones_for_preprint(r), "stale": is_stale(r), "published": not is_stale(r), "raw": r})
    out["preprints"] = pre

    pubmed_db = os.path.join(work, "retarats_pubmed.sqlite")
    published_eids = None
    if public_records and os.path.exists(public_records):
        csv.field_size_limit(10 ** 9)
        with open(public_records, newline="", encoding="utf-8") as fh:
            published_eids = {r["evidence_id"] for r in csv.DictReader(fh)}
    papers = {str(p.get("pmid", "")): p for p in _rows(pubmed_db, "papers")}
    pm = []
    for ev in _rows(pubmed_db, "evidence"):
        pmid = str(ev.get("pmid", ""))
        z = idn.zones_for_paper(ev, papers.get(pmid))
        pm.append({"key": pmid, "evidence_id": ev.get("evidence_id", ""), "molecule_id": ev.get("molecule_id", ""),
                   "zones": z, "stale": False,
                   "published": (ev.get("evidence_id", "") in published_eids) if published_eids is not None else None,
                   "raw": ev, "rule_id": ev.get("rule_id", "")})
    out["pubmed"] = pm
    return out


def old_guard(rec: Mapping, source: str, molecule: Mapping) -> bool:
    """The pre-WS4.5 name guard (any name, whole token) applied to the STORED text."""
    names = molecule_all_names(molecule)
    text = " ".join(str(v) for k, v in rec["zones"].items() if k in idn.SOURCE_ZONES[source] and k not in idn.INDEXING_ZONES)
    return names_molecule(text, names)


def baseline(work: str, out_dir: str, public_records: str) -> None:
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    keep_reg = load_registry_keep()
    data = load_sources(work, public_records)
    rows = []
    for source, recs in data.items():
        by_mol: Dict[str, List[dict]] = defaultdict(list)
        for r in recs:
            by_mol[r["molecule_id"]].append(r)
        for mid, rs in by_mol.items():
            m = mols.get(mid)
            if not m:
                continue
            pub = [r for r in rs if r["published"] in (True, None)]
            g_pass = sum(1 for r in pub if old_guard(r, source, m))
            verdicts = Counter(idn.evaluate(cfg, source, mid, r["zones"], r["key"]).outcome for r in pub)
            kept = sum(1 for r in pub if (mid, r["key"].upper()) in cfg.keep)
            rows.append({
                "molecule_id": mid, "source": source, "stored": len(rs),
                "published": len(pub) if source != "pubmed" or any(r["published"] is not None for r in rs) else "",
                "held_stale": sum(1 for r in rs if r["stale"]),
                "old_guard_pass": g_pass, "old_guard_fail": len(pub) - g_pass,
                "new_pass": verdicts[idn.PASS], "new_hold": verdicts[idn.HOLD], "new_exclude": verdicts[idn.EXCLUDE],
                "manual_keep_in_published": kept,
                "suspicious_gap": len(pub) - g_pass,
            })
    rows.sort(key=lambda r: -r["suspicious_gap"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "baseline_by_molecule_source.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows); identity rules {cfg.version}")
    for r in rows[:15]:
        print(r["molecule_id"], r["source"], "stored", r["stored"], "published", r["published"], "oldfail", r["old_guard_fail"],
              "hold", r["new_hold"], "excl", r["new_exclude"])


def _norm(t: str) -> str:
    return "".join(idn._alnum_tokens(t)).lower()


def terms(work: str, out_dir: str, public_records: str) -> None:
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = load_sources(work, public_records)
    out = []
    for source, recs in data.items():
        by_mol: Dict[str, List[dict]] = defaultdict(list)
        for r in recs:
            if r["published"] in (True, None):
                by_mol[r["molecule_id"]].append(r)
        for mid, rs in by_mol.items():
            mi = cfg.by_molecule.get(mid)
            if not mi:
                continue
            rules = mi.canonical + mi.specific + mi.contextual
            texts = [(r, " ".join(v for k, v in r["zones"].items() if k in idn.SOURCE_ZONES[source])) for r in rs]
            hit = {id(rule): [bool(rule.pattern.search(t)) for _, t in texts] for rule in rules}
            for rule in rules:
                n = sum(hit[id(rule)])
                if not n:
                    continue
                alone = 0
                for i in range(len(texts)):
                    if hit[id(rule)][i] and not any(hit[id(o)][i] for o in rules if o is not rule and _norm(o.term) != _norm(rule.term)):
                        alone += 1
                out.append({"molecule_id": mid, "source": source, "term": rule.term, "role": rule.role,
                            "origin": rule.origin, "published_records": len(rs), "records_with_term": n,
                            "term_alone": alone, "alone_pct": round(100 * alone / n, 1)})
    out.sort(key=lambda r: (-r["term_alone"]))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "term_stats.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"wrote {path} ({len(out)} rows)")


def compare(work: str, out_dir: str, public_records: str) -> None:
    """Before/after on the PUBLISHED set: published now vs published after the identity gate, per molecule
    x source, plus the records the old name guard would have dropped but identity keeps (rescued)."""
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = load_sources(work, public_records)
    rows = []
    for source, recs in data.items():
        by_mol: Dict[str, List[dict]] = defaultdict(list)
        for r in recs:
            if r["published"] is not False:
                by_mol[r["molecule_id"]].append(r)
        for mid, rs in by_mol.items():
            m = mols.get(mid)
            if not m:
                continue
            res = [(r, idn.evaluate(cfg, source, mid, r["zones"], r["key"])) for r in rs]
            after = sum(1 for _, v in res if v.published)
            rescued = sum(1 for r, v in res if v.published and not old_guard(r, source, m))
            by_type = Counter(v.match_type for _, v in res if v.published)
            kinds = Counter(v.outcome for _, v in res)
            rows.append({"molecule_id": mid, "source": source, "before": len(rs), "after": after,
                         "lost": len(rs) - after, "lost_pct": round(100 * (len(rs) - after) / len(rs), 1) if rs else 0,
                         "hold": kinds[idn.HOLD], "exclude": kinds[idn.EXCLUDE], "rescued_vs_old_guard": rescued,
                         "pass_by_type": "; ".join(f"{k}={v}" for k, v in by_type.most_common())})
    rows.sort(key=lambda r: -r["lost"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "before_after_by_molecule_source.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    tot = defaultdict(lambda: [0, 0])
    for r in rows:
        tot[r["source"]][0] += r["before"]
        tot[r["source"]][1] += r["after"]
    print(f"wrote {path}; rules {cfg.version}")
    for src, (b, a) in tot.items():
        print(f"  {src}: published {b} -> {a} ({b - a} held, {100 * (b - a) / max(b, 1):.1f}%)")


def regress(work: str, out_dir: str, public_records: str, per_group: int = 5) -> None:
    """Records the OLD name guard accepts (any name, anywhere in the stored text) that the identity rules do
    not -- the places where the new rules are stricter, i.e. where a true record could be lost. Grouped by
    source/molecule/outcome/reason with sample titles, written to old_pass_new_hold.csv."""
    import random
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = load_sources(work, public_records)
    groups: Dict[tuple, List[tuple]] = defaultdict(list)
    for source, recs in data.items():
        for r in recs:
            if r["published"] is False:
                continue
            m = mols.get(r["molecule_id"])
            if not m:
                continue
            v = idn.evaluate(cfg, source, r["molecule_id"], r["zones"], r["key"])
            if v.published or not old_guard(r, source, m):
                continue
            groups[(source, r["molecule_id"], v.outcome, v.match_type, v.matched_term)].append(
                (r["key"], (r["zones"].get("title") or r["zones"].get("brief_title") or "")[:110], v.reason))
    rows = []
    rnd = random.Random(7)
    for (source, mid, outcome, mtype, term), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        for key, title, reason in rnd.sample(items, min(per_group, len(items))):
            rows.append({"source": source, "molecule_id": mid, "outcome": outcome, "match_type": mtype,
                         "matched_term": term, "group_size": len(items), "record_key": key, "title": title,
                         "reason": reason})
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "old_pass_new_hold.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path}")
    tot = Counter()
    for (source, *_), items in groups.items():
        tot[source] += len(items)
    print("old-guard PASS but identity not-pass, by source:", dict(tot))
    for (source, mid, outcome, mtype, term), items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:30]:
        print(f"  {len(items):>5} {source:9} {mid:22} {outcome:7} {mtype}/{term}")


def benchmark(work: str, out_dir: str, public_records: str) -> None:
    """Approved benchmark rows vs the identity RULES ALONE (manual keeps ignored, otherwise every approved include
    would trivially pass): include rows must pass by name/context, exclude rows must not."""
    mols = list(load_active_molecules())
    cfg = idn.load_identity_config(mols, keep=set())
    data = load_sources(work, public_records)
    idx = {"pmid": ("pubmed", "key"), "nct_id": ("ctgov", "key"), "doi": ("preprints", "key")}
    by_key = {}
    for src, recs in data.items():
        for r in recs:
            by_key[(src, r["molecule_id"], r["key"].lower())] = r
    rows, bad = [], 0
    with open("config/retrieval_benchmark.csv", newline="", encoding="utf-8") as fh:
        for b in csv.DictReader(fh):
            if b["status"].strip().lower() != "approved":
                continue
            src = idx[b["id_type"]][0]
            rec = by_key.get((src, b["molecule_id"], b["id"].lower()))
            if rec is None:
                out = "not stored for this molecule"
                ok = True
            else:
                v = idn.evaluate(cfg, src, b["molecule_id"], rec["zones"], rec["key"])
                out = f"{v.outcome}/{v.match_type}/{v.matched_term}"
                ok = (v.published == (b["expected"] == "include"))
            bad += (not ok)
            rows.append({"molecule_id": b["molecule_id"], "source": src, "id": b["id"], "expected": b["expected"],
                         "rules_only_verdict": out, "ok": ok})
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "approved_benchmark_vs_identity.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{'OK ' if r['ok'] else 'BAD'} {r['molecule_id']:16} {r['source']:9} {r['id']:12} expected={r['expected']:7} {r['rules_only_verdict']}")
    print(f"{len(rows)} approved rows, {bad} disagree with the identity rules (rules only, manual keeps ignored)")


def samples(work: str, out_dir: str, public_records: str, per_group: int = 3) -> None:
    """Stratified review samples of what the identity rules change on the PUBLISHED set:
    rescued_samples.csv  published records the old name guard rejects but identity accepts, by alias (excluding
                         MeSH/indexing-zone matches, which are reported as a count only);
    removed_samples.csv  published records identity holds/excludes, per source x molecule (largest losses first)."""
    import random
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = load_sources(work, public_records)
    rescued: Dict[tuple, list] = defaultdict(list)
    removed: Dict[tuple, list] = defaultdict(list)
    idx_rescued = Counter()
    for source, recs in data.items():
        for r in recs:
            if r["published"] is False:
                continue
            mid = r["molecule_id"]
            m = mols.get(mid)
            if not m:
                continue
            v = idn.evaluate(cfg, source, mid, r["zones"], r["key"])
            title = (r["zones"].get("title") or r["zones"].get("brief_title") or "")[:120]
            if v.published and not old_guard(r, source, m):
                if v.zone in idn.INDEXING_ZONES:
                    idx_rescued[(source, mid)] += 1
                elif v.match_type in (idn.M_SPECIFIC, idn.M_CONTEXTUAL, idn.M_CANONICAL):
                    rescued[(source, mid, v.matched_term, v.match_type)].append((r["key"], title, v.zone))
            elif not v.published:
                removed[(source, mid, v.outcome, v.match_type)].append((r["key"], title, v.reason))
    rnd = random.Random(11)
    out = []
    for (source, mid, term, mtype), items in sorted(rescued.items(), key=lambda kv: -len(kv[1])):
        for key, title, zone in rnd.sample(items, min(per_group, len(items))):
            out.append({"source": source, "molecule_id": mid, "matched_term": term, "match_type": mtype, "zone": zone,
                        "group_size": len(items), "record_key": key, "title": title})
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "rescued_samples.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"rescued_samples.csv: {sum(len(v) for v in rescued.values())} records rescued by an alias/spelling "
          f"(+{sum(idx_rescued.values())} via MeSH/indexing zone, count only)")
    for (source, mid, term, mtype), items in sorted(rescued.items(), key=lambda kv: -len(kv[1]))[:20]:
        print(f"  {len(items):>5} {source:9} {mid:20} {mtype:15} {term}")
    out = []
    for (source, mid, outcome, mtype), items in sorted(removed.items(), key=lambda kv: -len(kv[1])):
        for key, title, reason in rnd.sample(items, min(per_group, len(items))):
            out.append({"source": source, "molecule_id": mid, "outcome": outcome, "match_type": mtype,
                        "group_size": len(items), "record_key": key, "title": title, "reason": reason})
    with open(os.path.join(out_dir, "removed_samples.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"removed_samples.csv: {sum(len(v) for v in removed.values())} records held/excluded in total")


def ambiguous(work: str, out_dir: str, public_records: str, n: int = 3) -> None:
    """AMBIGUOUS_TERMS.md: for every contextual alias, true-positive and false-positive keyword-in-context samples."""
    import random
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = load_sources(work, public_records)
    lines = ["# Ambiguous-alias audit (LOCAL ONLY)", "",
             "Every contextual alias in the identity rules, with what the stored records look like when the alias is accepted "
             "(context present) and when it is held/excluded. Rules: `config/MOLECULE_IDENTITY.csv`.", ""]
    for mid, mi in sorted(cfg.by_molecule.items()):
        for rule in mi.contextual:
            per_src = {}
            for source, recs in data.items():
                if source not in rule.applies_to:
                    continue
                tp, fp, total = [], [], 0
                for r in recs:
                    if r["molecule_id"] != mid or r["published"] is False:
                        continue
                    text = " || ".join(v for k, v in r["zones"].items() if v and k in idn.SOURCE_ZONES[source] and k not in idn.INDEXING_ZONES)
                    m = rule.pattern.search(text)
                    if not m:
                        continue
                    total += 1
                    v = idn.evaluate(cfg, source, mid, r["zones"], r["key"])
                    a, b = max(0, m.start() - 70), min(len(text), m.end() + 70)
                    item = (r["key"], text[a:b].replace("\n", " "), v)
                    (tp if v.published else fp).append(item)
                if total:
                    per_src[source] = (total, tp, fp)
            if not per_src:
                continue
            lines.append(f"## {mid}: `{rule.term}`" + (" (case-sensitive)" if rule.case_sensitive else ""))
            lines.append("")
            lines.append(f"- context any of: {', '.join(rule.context_terms) or '(none: can never identify alone)'}")
            if rule.veto_terms:
                lines.append(f"- vetoed by: {', '.join(rule.veto_terms)}")
            if rule.zones:
                lines.append(f"- zones: {', '.join(sorted(rule.zones))}")
            for source, (total, tp, fp) in per_src.items():
                lines.append(f"- **{source}**: {total} published records contain it; accepted {len(tp)}, held/excluded {len(fp)}")
                rnd = random.Random(f"{mid}{rule.term}{source}")
                for label, items in (("accepted", tp), ("held", fp)):
                    for key, snip, v in rnd.sample(items, min(n, len(items))):
                        lines.append(f"  - {label} `{key}`: ...{snip}...")
            lines.append("")
    with open(os.path.join(out_dir, "AMBIGUOUS_TERMS.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out_dir}/AMBIGUOUS_TERMS.md ({len(lines)} lines)")


def _mesh_kind(label: str, cache: dict) -> str:
    """NLM MeSH: is ``label`` a DESCRIPTOR heading, a descriptor ENTRY term, or a supplementary concept / unknown?
    (live read-only lookup against id.nlm.nih.gov, cached in ``cache``)."""
    import json as _json
    import urllib.parse
    import urllib.request
    if label in cache:
        return cache[label]
    kind = "not found in MeSH"
    for what, name in (("descriptor", "descriptor"), ("term", "entry term / supplementary concept")):
        try:
            url = (f"https://id.nlm.nih.gov/mesh/lookup/{what}?label={urllib.parse.quote(label)}&match=exact&limit=3")
            with urllib.request.urlopen(url, timeout=30) as r:
                data = _json.load(r)
            if data:
                kind = name
                break
        except Exception:  # noqa: BLE001
            kind = "lookup failed"
    cache[label] = kind
    return kind


def mesh(work: str, out_dir: str, public_records: str, per_group: int = 3, lookup: bool = True) -> None:
    """Which published PubMed records are identified ONLY through MeSH / substance headings, and by what?

    old rule  = a name occurring anywhere inside a heading element (what the first WS4.5 version accepted)
    new rule  = the WHOLE heading equals a name of the molecule (exact descriptor / entry term / substance name)
    Writes mesh_rescue_audit.csv and mesh_sibling_contamination.csv and prints a per-molecule summary."""
    import json as _json
    import random
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = load_sources(work, public_records)
    groups: Dict[tuple, List[tuple]] = defaultdict(list)
    sibling: Dict[tuple, int] = defaultdict(int)
    only_text = Counter()
    for r in data["pubmed"]:
        if r["published"] is False:
            continue
        mid = r["molecule_id"]
        mi = cfg.by_molecule.get(mid)
        if not mi:
            continue
        rules = mi.canonical + mi.specific
        z = r["zones"]
        text_hit = any(rule.pattern.search(z.get(zn, "")) for rule in rules for zn in ("title", "abstract", "keywords") if z.get(zn))
        if text_hit:
            only_text[mid] += 1
            continue
        found = None
        for zn in ("mesh_terms", "chemicals"):
            for el in [e for e in z.get(zn, "").split("; ") if e.strip()]:
                head = el.split(":", 1)[0].strip()
                for rule in rules:
                    if rule.pattern.fullmatch(head):
                        found = (zn, head, rule.term, "exact heading equals a name of the molecule")
                        break
                    if rule.pattern.search(el):
                        found = found or (zn, head, rule.term, "heading CONTAINS the name (derivative / neighbour)")
                if found and found[3].startswith("exact"):
                    break
            if found and found[3].startswith("exact"):
                break
        if found:
            groups[(mid, found[0], found[1], found[3])].append((r["key"], z.get("title", "")[:110]))
        else:
            # held: record the heading(s) that look like siblings (share the first 5 letters with a name)
            for zn in ("mesh_terms", "chemicals"):
                for el in [e for e in z.get(zn, "").split("; ") if e.strip()]:
                    head = el.split(":", 1)[0].strip()
                    for rule in rules:
                        stem = re.sub(r"[^a-z]", "", rule.term.lower())[:5]
                        if len(stem) >= 5 and stem in re.sub(r"[^a-z]", "", head.lower()):
                            sibling[(mid, head)] += 1
    cache_path = os.path.join(out_dir, "mesh_lookup_cache.json")
    cache = _json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    rnd = random.Random(5)
    out = []
    per_mol: Dict[str, Counter] = defaultdict(Counter)
    for (mid, zn, head, cls), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        kind = ""
        if cls.startswith("exact"):
            kind = _mesh_kind(head, cache) if lookup and zn == "mesh_terms" else ("substance name" if zn == "chemicals" else "")
        per_mol[mid]["exact" if cls.startswith("exact") else "contained"] += len(items)
        out.append({"molecule_id": mid, "zone": zn, "heading": head, "class": "exact" if cls.startswith("exact") else "contained_derivative_or_neighbour",
                    "mesh_kind": kind, "records_only_via_this_heading": len(items),
                    "sample_titles": " || ".join(t for _, t in rnd.sample(items, min(per_group, len(items))))})
    if lookup:
        os.makedirs(out_dir, exist_ok=True)
        _json.dump(cache, open(cache_path, "w"), indent=1)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "mesh_rescue_audit.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    sib = sorted(sibling.items(), key=lambda kv: -kv[1])
    with open(os.path.join(out_dir, "mesh_sibling_contamination.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["molecule_id", "sibling_heading_on_held_records", "records"])
        for (mid, head), n in sib:
            w.writerow([mid, head, n])
    tot = Counter()
    for mid, c in per_mol.items():
        tot.update(c)
    print(f"PubMed records identified ONLY through MeSH/substance headings (no name in title/abstract/keywords): "
          f"exact={tot['exact']}  contained(derivative/neighbour; old rule only)={tot['contained']}")
    print("per molecule (exact / contained):")
    for mid, c in sorted(per_mol.items(), key=lambda kv: -(kv[1]['exact'] + kv[1]['contained']))[:20]:
        print(f"  {mid:20} exact={c['exact']:5} contained={c['contained']:5}")
    print("largest groups:")
    for row in out[:20]:
        print(f"  {row['molecule_id']:18} {row['zone']:10} {row['heading'][:38]:38} {row['class'][:10]:10} {row['mesh_kind'][:20]:20} n={row['records_only_via_this_heading']}")


def contexts(work: str, public_records: str, molecule: str, term: str, source: str, n: int, seed: int,
             case_sensitive: bool, only_alone: bool) -> None:
    """Print random keyword-in-context snippets for a term, so its meaning can be judged by eye."""
    import random
    mols = {m["molecule_id"]: m for m in load_active_molecules()}
    cfg = idn.load_identity_config(list(mols.values()))
    data = load_sources(work, public_records)
    mi = cfg.by_molecule[molecule]
    pat = idn.compile_term(term, case_sensitive)
    others = [r for r in mi.canonical + mi.specific if idn.compile_term(r.term).pattern != pat.pattern]
    hits = []
    for r in data[source]:
        if r["molecule_id"] != molecule or r["published"] is False:
            continue
        text = " || ".join(v for k, v in r["zones"].items() if v and k in idn.SOURCE_ZONES[source])
        m = pat.search(text)
        if not m:
            continue
        if only_alone and any(o.pattern.search(text) for o in others):
            continue
        hits.append((r, text, m))
    print(f"{molecule}/{source} term={term!r}: {len(hits)} records" + (" (term alone)" if only_alone else ""))
    random.Random(seed).shuffle(hits)
    for r, text, m in hits[:n]:
        a, b = max(0, m.start() - 90), min(len(text), m.end() + 90)
        title = r["zones"].get("title") or r["zones"].get("brief_title") or ""
        print(f"- [{r['key']}] {title[:90]}\n    ...{text[a:b]}...".replace("\n", " ") if False else f"- [{r['key']}] {title[:90]}\n    ...{text[a:b]}...")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("baseline", "terms", "contexts", "compare", "regress", "benchmark", "samples", "ambiguous", "mesh"))
    ap.add_argument("--molecule", default="")
    ap.add_argument("--term", default="")
    ap.add_argument("--source", default="preprints", choices=idn.SOURCES)
    ap.add_argument("-n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--case-sensitive", action="store_true")
    ap.add_argument("--alone", action="store_true", help="only records with no other name of the molecule")
    ap.add_argument("--work", default="work")
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--public-records", default="work/build_current/public_records.csv")
    args = ap.parse_args()
    if args.command == "contexts":
        contexts(args.work, args.public_records, args.molecule, args.term, args.source, args.n, args.seed,
                 args.case_sensitive, args.alone)
        return
    {"baseline": baseline, "terms": terms, "compare": compare, "regress": regress, "benchmark": benchmark, "samples": samples, "ambiguous": ambiguous, "mesh": mesh}[args.command](args.work, args.out_dir, args.public_records)


if __name__ == "__main__":
    main()
