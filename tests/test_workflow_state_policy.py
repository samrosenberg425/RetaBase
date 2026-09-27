#!/usr/bin/env python3
"""Static policy test: every corpus writer uses the single durable-state path.

Checks the GitHub workflow/composite-action text (no YAML lib, no network):

* corpus state is never cached directly (`path: data` only inside the composite actions);
  the Actions cache is restore/save only via those actions (acceleration, save AFTER promote);
* no workflow points a fetch/enrichment step at data/ -- mutation happens in work/ only;
* every workflow that mutates the corpus restores via corpus-restore, builds via
  build_release.py, and promotes via corpus-promote, in that order, with contents: write and
  the shared `retarats-corpus` lock;
* `corpus_state.py promote` is invoked from exactly one place (the corpus-promote action);
* update.yml promotes BEFORE it uploads the Pages artifact and exposes bootstrap/simulate inputs.

Plain script: run `python3 tests/test_workflow_state_policy.py`.
"""

from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
ACT = os.path.join(ROOT, ".github", "actions")
_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {name}")


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


WRITER_RE = re.compile(r"retarats_v2\.py|run_(?:trials|preprints)_fetch\.py|run_impact_backfill\.py|"
                       r"run_icite_backfill\.py|run_fulltext_backfill\.py|run_backfill\.py")


def workflows():
    return {os.path.basename(p): read(p) for p in sorted(glob.glob(os.path.join(WF, "*.yml")))}


def actions():
    return {os.path.relpath(p, ACT): read(p) for p in sorted(glob.glob(os.path.join(ACT, "*", "action.yml")))}


def test_no_direct_state_cache_or_data_dir_writes():
    for name, text in workflows().items():
        check(f"{name}: no `path: data` cache (state cache only inside the composite actions)", not re.search(r"path:\s*data\s*$", text, re.M))
        check(f"{name}: no data/ DB paths (--db/--local-db/--state-db work/ only)",
              not re.search(r"(--db|--local-db|--state-db|--checkpoint)\s+data/", text) and "data/retarats_" not in text)
        for m in re.finditer(r"uses:\s*actions/cache@[^\n]*\n(?:[^\n]*\n){0,3}?\s*path:\s*(\S+)", text):
            check(f"{name}: plain actions/cache@ only for the full-text HTTP cache", m.group(1) == ".cache/context")
        check(f"{name}: never calls the promote CLI directly", "corpus_state.py promote" not in text)
    acts = actions()
    check("composite actions exist", {"corpus-restore/action.yml", "corpus-promote/action.yml"} <= set(acts))
    restore, promote = acts["corpus-restore/action.yml"], acts["corpus-promote/action.yml"]
    check("restore action: cache/restore only, never a saving cache", "actions/cache/restore@" in restore and "actions/cache/save@" not in restore
          and "actions/cache@" not in restore)
    check("restore action: verified restore into work/ (fail hard, no bootstrap unless input)",
          "corpus_state.py restore --dest work" in restore and "BOOTSTRAP" in restore and "|| true" not in restore)
    check("promote action: promote step precedes cache/save", "corpus_state.py promote" in promote and "actions/cache/save@" in promote
          and promote.index("corpus_state.py promote") < promote.index("actions/cache/save@"))
    check("promote action: never `|| true`", "|| true" not in promote)
    check("cache/save appears only in the promote action",
          all("actions/cache/save@" not in t for n, t in list(workflows().items()) + [(k, v) for k, v in acts.items() if k != "corpus-promote/action.yml"]))
    n_promote = sum(t.count("corpus_state.py promote") for t in list(workflows().values()) + list(acts.values()))
    check("exactly one promotion call site in all CI", n_promote == 1)


def test_every_writer_follows_the_single_path():
    writers = {n: t for n, t in workflows().items() if WRITER_RE.search(t)}
    check("writer workflows detected (update/backfill/fulltext/registry/citations)",
          {"update.yml", "backfill.yml", "fulltext.yml", "registry.yml", "citations.yml"} <= set(writers))
    for name, t in writers.items():
        i_restore, i_promote = t.find("corpus-restore"), t.find("corpus-promote")
        i_build = t.find("scripts/build_release.py")
        first_mut = min(m.start() for m in WRITER_RE.finditer(t))
        check(f"{name}: restore -> mutate -> build_release -> promote in order",
              -1 < i_restore < first_mut < i_build < i_promote)
        check(f"{name}: contents: write (corpus-store release)", re.search(r"contents:\s*write", t) is not None)
        check(f"{name}: shares the retarats-corpus lock, no cancel-in-progress", "group: retarats-corpus" in t
              and re.search(r"group: retarats-corpus\s*\n\s*cancel-in-progress: false", t) is not None)
        check(f"{name}: release_id allocated before mutation", 0 < t.find("corpus_state.py release-id") < first_mut)
        check(f"{name}: build/promote steps are hard gates (no `|| true`)",
              not re.search(r"build_release\.py[^\n]*\|\|\s*true", t))
        check(f"{name}: mutation targets work/", "work/retarats_" in t)
    # read-only consumers restore via the verified path and never promote
    for name in ("snapshot.yml", "audit.yml"):
        t = workflows()[name]
        check(f"{name}: read-only consumer uses verified restore, never promotes",
              "corpus-restore" in t and "corpus-promote" not in t and "actions/cache" not in t)


def test_update_workflow_gates():
    t = workflows()["update.yml"]
    check("update: promote happens before the Pages artifact upload", t.find("corpus-promote") < t.find("upload-pages-artifact"))
    check("update: deploy still depends on the build job", re.search(r"deploy:\s*\n\s*needs:\s*build", t) is not None)
    check("update: bootstrap input is manual + boolean default false", re.search(r"bootstrap:\s*\n(?:.*\n)*?\s*type:\s*boolean\s*\n\s*default:\s*false", t) is not None)
    for sim in ("cache_miss", "empty_trials", "mixed_release"):
        check(f"update: simulate={sim} supported", sim in t)
    check("update: fault-injection steps run only when explicitly dispatched", t.count("github.event.inputs.simulate ==") >= 2
          and "simulate: ${{ github.event.inputs.simulate || '' }}" in t)
    check("restore action honours simulate=cache_miss", "inputs.simulate != 'cache_miss'" in actions()["corpus-restore/action.yml"])
    check("update: per-source outcomes recorded", all(f"--source {s}" in t for s in ("pubmed_daily", "openalex", "icite", "ctgov", "europepmc_preprints")))
    check("update: old guard/baseline-in-cache mechanics removed", "corpus_stats_baseline.json" not in t and "--mode guard" not in t)


def main():
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
    print(f"{_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
