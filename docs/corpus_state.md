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

## Registry / preprint hygiene (trials + preprints) and deliberate shrinks

CT.gov and EuropePMC match on tokenisation, server-side synonym expansion and (EuropePMC) full text,
so a hit is not proof a record is about the molecule. Three layers keep the published trial and
preprint feeds on-topic:

1. **Search terms** (`registry.registry_terms`): every term is a quoted phrase; ambiguous ones are
   listed in `config/registry_term_blocklist.csv` (the display name can never be blocked).
2. **Identity guard** at ingest (`run_trials_fetch.py`, `run_preprints_fetch.py`): a hit is stored only
   if its own stored text establishes the molecule under the identity rules (see "Molecule identity"
   below). Trials are judged on titles, conditions, keywords, interventions (incl. other names) and arm
   groups; preprints on title + abstract (no abstract and no name in the title = kept).
   `config/registry_keep.csv` is a reviewed always-keep list (e.g. a trial that only uses a brand
   name missing from our synonyms). Every skipped id is printed in the run log (`skipped[molecule]`).
3. **Stale marking** (`enrichment/registry_stale.py`): stored rows that a molecule's COMPLETED search
   no longer returns get `stale_query` and are dropped from `trials_data.json` / `preprints_data.json`,
   molecule counts and the benchmark. Nothing is deleted, and a row the search returns again is
   un-marked automatically. A partial or failed retrieval never marks anything. An empty result for a
   molecule with >= 20 stored rows is treated as an API anomaly and skipped unless the molecule is on
   `config/registry_expected_empty.csv` (human-confirmed to have no real records).

Tool: `scripts/audit_registry_queries.py` measures, per molecule and per term, how many live hits
really name the molecule (read-only).

**Deliberate shrink.** The promotion gate refuses a published-feed shrink of more than 10% vs last-good.
A reviewed cleanup that is meant to remove records needs the manual dispatch input for that run only:

    gh workflow run update.yml --ref main -f allow_shrink=true

Leave it off for normal runs.

## Molecule identity (WS4.5): SOURCE RECORD -> MOLECULE

Retrieval completeness is not identity. A search (PubMed `[tiab]`, CT.gov, EuropePMC) returns what its engine
matches; whether a stored record is really ABOUT the molecule it is filed under is decided separately, offline,
from the stored text, by `retarats_pipeline/identity.py`.

**Term roles** (`config/MOLECULE_IDENTITY.csv` is an overlay on `config/MOLECULES.csv`; molecule ids are unchanged
and a molecule with no overlay row behaves like the old name guard):

| role | meaning |
|---|---|
| `canonical` | display name (implicit) |
| `specific_alias` | specific enough to identify the molecule alone (thymalfasin, Zadaxin, PTH(1-34), Geref ...) |
| `contextual_alias` | ambiguous token (VIP, LDN, TB4, MT-II ...): counts only with `context_any` text present and no `exclude_any` text; all-caps acronyms are case-sensitive |
| `exclusion` | a known unrelated meaning; vetoes every non-canonical match of that molecule |
| manual keep | `registry_keep.csv`, approved benchmark includes, `manual_pmids.csv`, gold PMIDs: override every automated outcome |

**Discovery is a separate column** (`discovery` = `ctgov;preprints`): a term is searched only if listed there (or it is
one of the first display + 3 synonyms and not a contextual alias). The overlay can never change PubMed retrieval:
PubMed discovery stays in `config/SEARCH_RULES.csv`.

**Match roles (provenance, and policy).** Every verdict records HOW the molecule appears (`role` in `record_identity`):
`exposure` (CT.gov interventions / other names / arms), `subject` (titles, conditions, keywords), `measured_outcome`
(outcome measure titles and outcome descriptions: a genuine biomarker study is kept), `background` (brief summary /
detailed description / eligibility: mentioned, not the subject), `text_mention` (abstracts), `indexing` (MeSH /
substance headings), `ambiguous_acronym` (an ambiguous alias without its context, or next to an unrelated meaning) and
`unrelated` (no name anywhere). `config/identity_policy.csv` (`publish_roles`) lists the roles that may establish
identity: trials = exposure + subject + measured_outcome (add `background` to publish those too); PubMed/preprints = all
but `indexing`. **MeSH / substance headings never establish identity alone**, even an exact descriptor: papers about
derivatives and neighbours (zotarolimus/everolimus stents, isoquercitrin, taurolidine, acamprosate) carry the parent
descriptor. They are held as `indexing_only` with the heading recorded.

**Outcomes**: `pass` / `hold` (no identity evidence, or an ambiguous alias without its context) / `exclude`
(positive evidence of another meaning). Fail-open wherever the stored text cannot support a judgement.

**Applied in three places, one function**: the ingest guard, the feed builders (`build_trials_json.py`,
`build_preprints_json.py`, the trial-count in the curated build) and the PubMed curated build (hold-in-place,
`excluded_noise`, `publish_rule_id = identity:hold|exclude`, listed in `identity_report_pubmed.csv`). Nothing is
deleted and no stored payload is rewritten. `config/identity_policy.csv` can switch a source off without code.

**CT.gov identity fields** (`scripts/run_trials_identity_backfill.py`, a step in `update.yml`): rows stored before WS4.5
lack official title, keywords, arms, other names and outcome/summary/eligibility text. The backfill adds those fields from
CT.gov (100 trials per request, resumable, bounded), never changes an existing field, and marks the row
`identity_fields_v`. Until a row has them it is "legacy": it is judged but never HELD on the incomplete evidence
(`legacy_unverified`, fail-open). Daily fetches write the fields for every row they return.

**Stored-row re-evaluation** (`scripts/run_identity_reeval.py`, run by `build_release.py` before every build): judges
EVERY stored record, offline, regardless of whether the latest retrieval was complete, and (re)writes the provenance
table `record_identity` (molecule, outcome, match type, matched term, zone, reason, rules version, timestamp) in each
SQLite DB. It answers "why is this record associated with this molecule?". Changing an alias or rule changes
`rules_version` and is picked up by simply running again.

**Search-cache key**: search responses are cached under a key that is now a hash of the exact query + paging
(`common.search_cache_key`). Before, the file name collapsed punctuation, so a corrected (quoted) query was answered
from the cached results of the old unquoted one for 6 hours -- which made retrieval look "partial" and blocked
stale-marking.

Review tooling (local-only output, never committed): `scripts/audit_identity.py` (baseline / compare / terms /
contexts / regress / benchmark) and `scripts/build_identity_review.py` (proposed benchmark rows + review queue; it
never approves anything).

### Known residual issues (WS4.5), deliberately not fixed yet

Found in the pre-production sanity sample; none is introduced by the identity layer (the old name guard behaved the same) and
none blocks the controlled shrink. Each needs its own decision.

* `p22_2`: every stored hit is noise (chromosome bands such as p22.2, bacterial strain "P22-2", protein names). The molecule may
  not be findable by name; consider retiring or renaming it.
* `sermorelin`, alias "GRF 1-29": also matches GHRH antagonist analogues and non-human GRF (about half of a 10-row published sample).
* `spermidine`, `glutathione`: enzyme names (spermidine/spermine N1-acetyltransferase, spermidine synthase, glutathione peroxidase /
  S-transferase / reductase) and neighbouring compounds leak into published rows; candidates for exclusion terms.
* `metformin`, `indexing_only` holds: about 15-25% of the abstract-less metformin rows held only on a MeSH heading look genuinely about
  metformin (brand/old-name gaps such as Glumetza). Not rescued: MeSH alone never establishes identity.
* CT.gov `background` tier (~870 trials, ~18% genuine): held. A narrow rule (endogenous-biomarker molecules + a measurement
  sentence in the summary, ~33 trials, ~85% precision) is a possible later rescue.
