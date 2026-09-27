#!/usr/bin/env python3
"""Record the outcome of one source refresh step into the durable source_status.json.

    record_source_run.py --state work/source_status.json --source pubmed_daily --exit-code $rc
    record_source_run.py --state work/source_status.json --source ctgov --error "HTTP 503"

Never fails a job (always exits 0 unless the source name/policy is invalid): it
records facts; ``build_release.py`` turns them into status.json and labels."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline import source_policy as sp  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="work/source_status.json")
    ap.add_argument("--source", required=True)
    ap.add_argument("--exit-code", type=int, default=None, help="exit status of the step (0 = success)")
    ap.add_argument("--error", default="", help="record a failure with this message")
    ap.add_argument("--outcome", choices=sp.OUTCOMES, default="",
                     help="what kind of attempt this was: live/cached/partial/failed")
    ap.add_argument("--retrieval-json", default="",
                     help="JSON object with pagination completeness metadata "
                          "(total_reported/rows_retrieved/pages/exhausted/partial)")
    # Count flags are namespaced (--count-new, --count-error, ...): a bare --error would
    # collide with the failure-message flag above.
    for k in sp.COUNT_KEYS:
        ap.add_argument(f"--count-{k}", dest=f"count_{k}", type=int, default=None)
    args = ap.parse_args(argv)
    ok = (args.exit_code == 0) if args.exit_code is not None else not args.error
    err = args.error or ("" if ok else f"step exited with status {args.exit_code}")
    counts = {k: getattr(args, f"count_{k}") for k in sp.COUNT_KEYS if getattr(args, f"count_{k}") is not None}
    retrieval = None
    if args.retrieval_json:
        try:
            retrieval = json.loads(args.retrieval_json)
        except ValueError as exc:
            print(f"::error::--retrieval-json is not valid JSON: {exc}")
            return 1
    try:
        sp.record_run(args.state, args.source, ok, counts=counts, error=err,
                       outcome=args.outcome, retrieval=retrieval)
    except sp.PolicyError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"recorded {args.source}: {'ok' if ok else 'FAILED'}" + (f" ({args.outcome})" if args.outcome else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
