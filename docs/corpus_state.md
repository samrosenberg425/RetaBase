# Durable corpus state, staging and promotion (WS1 + WS3-core)

The corpus used to live only in the GitHub Actions cache (LRU-evicted, overwritten by any
writer). It now has a durable, versioned home, and every writer follows one path:

    restore -> stage (work/) -> mutate -> build -> validate -> promote

## Where state lives

GitHub Release **`corpus-store`** (created on the first promotion):

| asset | purpose |
|---|---|
| `corpus-<release_id>.tar.gz` | pubmed / trials / preprints / state SQLite DBs + small state JSON (checkpoint, freshness, `source_status.json`). Deterministic archive. |
| `manifest-<release_id>.json` | release_id, build_sha, per-file sha256 + table counts, archive sha256, logical `corpus_fingerprint`, DB counts, feed counts, stats baseline, per-source status |
| `last-good.json` | tiny pointer: release_id, asset name, sha256. The only asset that is ever overwritten. |

`release_id` = `YYYYMMDDTHHMM-<sha7>`; it is stamped into `site_data.json`, every
`site_records_*.json`, `site_detail.json`, `trials_data.json`, `preprints_data.json` and
`status.json`. The Actions cache is **acceleration only** (`data/`), refreshed after promotion.

## Restore order (`scripts/corpus_state.py restore`)

1. cache copy whose sha256 equals the last-good manifest → used;
2. else the archive named by `last-good.json`, sha256-verified;
3. else (pointer unreadable / archive bad) the newest older release whose manifest and archive verify;
4. else **fail explicitly**. There is no silent bootstrap: `--bootstrap [--seed-dir DIR]` is honoured
   only when the store holds no corpus state at all (workflow_dispatch `bootstrap=true`; the seed is the
   legacy cache in `data/`). An unreachable GitHub API is an error, never an "empty store".

`restore` refuses to stage into a non-empty directory it did not create.

## Promotion (`scripts/corpus_state.py promote`)

1. refuse a work dir not staged by `restore`, or if last-good moved since the restore (concurrent writer);
2. validate: `validate_release` (all assets present/parseable, one `release_id`, feed count-collapse vs
   last-good) + corpus gates (integrity, papers > 0, no DB dropped, papers/evidence/trials/preprints not down >10%);
3. upload archive as a **new** versioned asset (never overwrites), download it back, verify sha256;
   on mismatch the candidate asset is deleted and the pointer is untouched;
4. publish the manifest, then **replace the pointer last**, then read it back;
5. only then refresh the cache; prune corpus assets older than the newest 3 releases (never the
   pointer's release).

Any failure before step 4 leaves the pointer, previous assets and the live site unchanged.
`--allow-shrink` (manual) skips the collapse gates for a deliberate shrink.

## Rollback / recovery

* Bad release published: `python3 scripts/corpus_state.py status --repo OWNER/REPO` lists releases; rewrite
  `last-good.json` to name an older retained release (or delete the bad `manifest-*`/`corpus-*` assets).
* Pointer lost/corrupt: restore falls back to the newest verified manifest automatically.

## Source policy / status (WS3-core)

`config/source_policy.csv` sets per-source cadence, degraded/failed staleness and whether stale last-good data
may deploy. Fetch steps call `scripts/record_source_run.py`; `build_release.py` renders `status.json`
(`current` / `degraded` / `failed` / `unknown`). Stale data is never discarded. Not yet built: the public status
UI, red-job-on-failed-source, and the stronger gates (growth, truncation, duplicate ids, coverage deltas).

## Failure drills (workflow_dispatch → update.yml `simulate`)

`cache_miss` (must restore from the store), `empty_trials` and `mixed_release` (promotion must be refused,
job red, `last-good.json` and live Pages unchanged).

## Tests

`python3 tests/test_corpus_state.py` (restore/promote/prune/validation failure simulations, fake `gh` store) and
`python3 tests/test_workflow_state_policy.py` (every writer uses the single path).
