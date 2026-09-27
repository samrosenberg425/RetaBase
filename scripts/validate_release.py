#!/usr/bin/env python3
"""Validate a built site directory as ONE consistent release.

Checks required assets (index.html, site_data.json, every listed shard,
site_detail.json, trials_data.json, preprints_data.json, status.json), that each is
present/non-empty/parseable, that every JSON asset carries the SAME release_id
(mixed-release artifacts are rejected, including orphaned shards), and (optionally)
that feed counts have not collapsed versus a baseline manifest.

Exit 0 = valid, 1 = invalid. Pure stdlib; no network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline.release_validation import DEFAULT_MAX_DROP, validate_release  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site-dir", default="exports/site")
    ap.add_argument("--release-id", default="", help="expected release_id (default: assets must merely agree)")
    ap.add_argument("--baseline-manifest", default="",
                    help="last-good manifest-<id>.json; feed counts may not drop > --max-drop below its 'site' counts")
    ap.add_argument("--max-drop", type=float, default=DEFAULT_MAX_DROP)
    args = ap.parse_args()

    baseline = None
    if args.baseline_manifest:
        try:
            with open(args.baseline_manifest, encoding="utf-8") as fh:
                baseline = json.load(fh).get("site")
        except (OSError, ValueError) as exc:
            print(f"::error::cannot read baseline manifest: {exc}")
            return 1
    res = validate_release(args.site_dir, args.release_id or None, baseline, args.max_drop)
    print(res.render())
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
