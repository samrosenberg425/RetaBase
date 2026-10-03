#!/usr/bin/env python3
"""Build ONE release from a staged corpus directory (the 'build' + 'validate' steps).

    build_release.py --work work --out exports --release-id ID

Runs, in order, against ``<work>`` (never against last-good):
  run_identity_reeval     (offline provenance of molecule identity for every stored record; advisory)
  build_curated_database  -> <out>/curated   (site JSON stamped with the release_id)
  build_public_site       -> <out>/site      (--mode fetch)
  validate_curated        (baseline = last-good stats carried in the stage dir)
  copy site_data/site_detail/site_records_* into <out>/site
  build_trials_json / build_preprints_json -> <out>/site   (stamped)
  status.json             (source policy x source_status.json, stamped)
  validate_release        (assets present + one release_id)

Any failing step aborts with a non-zero exit, so promotion (which re-validates) is never
reached with a bad build. <out>/curated and <out>/site are wiped first so no stale shard
from an earlier build can leak into this release.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from retarats_pipeline import corpus_store as cs  # noqa: E402
from retarats_pipeline import source_policy as sp  # noqa: E402
from retarats_pipeline.release_validation import validate_release  # noqa: E402


def _run(*argv: str) -> None:
    print("+", " ".join(argv), flush=True)
    r = subprocess.run([sys.executable, *argv], cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"build_release: step failed ({r.returncode}): {' '.join(argv)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="staged corpus dir (from corpus_state restore)")
    ap.add_argument("--out", required=True, help="output root; writes <out>/curated and <out>/site")
    ap.add_argument("--release-id", required=True)
    args = ap.parse_args()

    work, out = os.path.abspath(args.work), os.path.abspath(args.out)
    if cs.read_stage_marker(work) is None:
        raise SystemExit(f"build_release: {work} was not staged by corpus_state restore")
    curated, site = os.path.join(out, "curated"), os.path.join(out, "site")
    for d in (curated, site):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    rid = args.release_id
    db = lambda n: os.path.join(work, n)  # noqa: E731

    # Offline identity re-evaluation of every STORED record (provenance table `record_identity` in each DB).
    # Independent of retrieval completeness; never deletes anything. Advisory: the builds below apply the same
    # deterministic gate in memory, so a failure here must not block a release.
    try:
        _run("scripts/run_identity_reeval.py", "--work", work)
    except SystemExit as exc:
        print(f"build_release: WARNING identity provenance refresh failed ({exc}); the builds still apply the gate", flush=True)

    _run("scripts/build_curated_database.py", "--db", db("retarats_pubmed.sqlite"),
         "--out-dir", curated, "--release-id", rid)
    _run("scripts/build_public_site.py", "--curated-dir", curated, "--out-dir", site, "--mode", "fetch")
    baseline = os.path.join(work, cs.BASELINE_STATS)
    vc = ["scripts/validate_curated.py", "--curated-dir", curated]
    if os.path.isfile(baseline) and os.path.getsize(baseline) > 2:
        vc += ["--baseline", baseline]
    _run(*vc)
    for name in ("site_data.json", "site_detail.json"):
        shutil.copyfile(os.path.join(curated, name), os.path.join(site, name))
    for shard in glob.glob(os.path.join(curated, "site_records_*.json")):
        shutil.copyfile(shard, os.path.join(site, os.path.basename(shard)))
    _run("scripts/build_trials_json.py", "--db", db("retarats_trials.sqlite"),
         "--out", os.path.join(site, "trials_data.json"), "--release-id", rid,
         "--identity-report", os.path.join(curated, "identity_held_trials.csv"))
    _run("scripts/build_preprints_json.py", "--db", db("retarats_preprints.sqlite"),
         "--out", os.path.join(site, "preprints_data.json"), "--release-id", rid,
         "--identity-report", os.path.join(curated, "identity_held_preprints.csv"))

    with open(os.path.join(curated, "site_data.json"), encoding="utf-8") as fh:
        fp = (json.load(fh).get("corpus_stats") or {}).get("corpus_fingerprint", "")
    try:
        from retarats_pipeline.rules_version import compute_rules_version
        rules_version = compute_rules_version()
    except Exception:
        rules_version = ""
    status = sp.build_status(rid, os.path.join(work, "source_status.json"),
                             build_sha=os.environ.get("GITHUB_SHA", "")[:7] or "local", corpus_fingerprint=fp,
                             rules_version=rules_version)
    sp.write_status(os.path.join(site, "status.json"), status)
    for line in sp.summary_lines(status):
        print(line)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as fh:
            fh.write("```\n" + "\n".join(sp.summary_lines(status)) + "\n```\n")
    for name, s in status["sources"].items():
        if s["status"] in (sp.DEGRADED, sp.FAILED):
            print(f"::warning::source {name} is {s['status']} (last success {s['as_of_utc'] or 'never'})")

    res = validate_release(site, rid)
    print(res.render())
    if not res.ok:
        raise SystemExit("build_release: release validation failed")
    print(f"build_release: OK -> {site} (release {rid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
