#!/usr/bin/env python3
"""CLI for the durable corpus state (see retarats_pipeline/corpus_store.py).

    corpus_state.py release-id                       print a new release_id
    corpus_state.py restore  --dest work [--cache-dir data] [--bootstrap [--seed-dir DIR]]
    corpus_state.py promote  --work work --site-dir exports/site --release-id ID [--cache-dir data]
    corpus_state.py prune    [--keep 3]
    corpus_state.py status                           show pointer + releases (read-only)
    corpus_state.py simulate --kind empty_trials|mixed_release --work work --site-dir exports/site

Store: ``--local-store DIR`` (local/dev/tests) or, by default, the GitHub Release
``corpus-store`` of ``--repo`` / $GITHUB_REPOSITORY via the ``gh`` CLI. Writes to the
GitHub store are refused outside GitHub Actions unless --allow-remote-write is given.

Exit codes: 0 ok, 1 explicit corpus-state failure (nothing was changed), 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retarats_pipeline import corpus_store as cs  # noqa: E402


def _store(args, write: bool):
    if args.local_store:
        return cs.LocalStore(args.local_store)
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise cs.CorpusStateError("no store: pass --local-store DIR or --repo OWNER/NAME (or set GITHUB_REPOSITORY)")
    if write and os.environ.get("GITHUB_ACTIONS") != "true" and not args.allow_remote_write:
        raise cs.CorpusStateError("refusing to WRITE to the GitHub corpus store outside GitHub Actions "
                                  "(pass --allow-remote-write to override)")
    return cs.GitHubReleaseStore(repo, args.tag)


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("release-id", "restore", "promote", "prune", "status", "simulate"))
    ap.add_argument("--local-store")
    ap.add_argument("--repo", default="")
    ap.add_argument("--tag", default=cs.STORE_TAG)
    ap.add_argument("--allow-remote-write", action="store_true")
    ap.add_argument("--dest", default="work", help="restore: staging directory")
    ap.add_argument("--work", default="work", help="promote/simulate: staged working directory")
    ap.add_argument("--site-dir", default="exports/site")
    ap.add_argument("--release-id", default="")
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--bootstrap", action="store_true", help="restore: allow starting an empty/seeded corpus (empty store only)")
    ap.add_argument("--seed-dir", default="", help="restore --bootstrap: seed from this directory (e.g. legacy cache)")
    ap.add_argument("--keep", type=int, default=cs.DEFAULT_KEEP)
    ap.add_argument("--max-drop", type=float, default=0.10)
    ap.add_argument("--allow-shrink", action="store_true", help="promote: skip count-collapse gates (manual, deliberate)")
    ap.add_argument("--kind", default="")
    args = ap.parse_args(argv)

    try:
        if args.command == "release-id":
            print(cs.make_release_id(sha=os.environ.get("GITHUB_SHA", "")))
            return 0
        if args.command == "simulate":
            print("simulate:", cs.simulate_fault(args.kind, args.work, args.site_dir))
            return 0
        if args.command == "status":
            store = _store(args, write=False)
            assets = store.list_assets()
            ptr = cs.read_pointer(store, assets)
            print(json.dumps({"pointer": ptr, "releases": cs.manifest_release_ids(assets), "assets": assets}, indent=2))
            return 0
        if args.command == "restore":
            store = _store(args, write=False)
            res = cs.restore(store, args.dest, cache_dir=args.cache_dir or None,
                             bootstrap=args.bootstrap, seed_dir=args.seed_dir or None)
            _summary(f"Corpus restored from **{res.source}** (release `{res.release_id}`)")
            print(f"restored: source={res.source} release={res.release_id}")
            return 0
        if args.command == "promote":
            if not args.release_id:
                raise cs.CorpusStateError("--release-id is required")
            store = _store(args, write=True)
            res = cs.promote(store, args.work, args.site_dir, args.release_id, cache_dir=args.cache_dir or None,
                             keep=args.keep, build_sha=os.environ.get("GITHUB_SHA", ""),
                             max_drop=args.max_drop, allow_shrink=args.allow_shrink)
            _summary(f"Corpus promoted: last-good is now `{res.release_id}` (pruned {len(res.pruned)} old asset(s))")
            print(f"promoted: {res.release_id}")
            return 0
        if args.command == "prune":
            store = _store(args, write=True)
            print("pruned:", cs.prune(store, keep=args.keep))
            return 0
    except cs.CorpusStateError as exc:
        print(f"::error::corpus_state {args.command} failed: {exc}", file=sys.stderr)
        _summary(f"❌ **corpus_state {args.command} failed** — {exc}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
