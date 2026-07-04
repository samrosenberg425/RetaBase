# Curation, faceting, and publication layer

This layer turns the broad internal evidence database (14.9k rows across 86
molecules) into a **filterable, scorable, publishable** curated dataset that can
drop into Google Sheets now and Airtable later. Everything here is **rule-based,
offline, and auditable** — no LLM calls, no network. Every tag, score, and
publish decision is explainable from a config file.

Package: `retarats_pipeline/curation/`
Builder: `scripts/build_curated_database.py`
Configs: `config/FACETS.csv`, `config/PUBLICATION_RULES.csv`, `config/REQUIRED_FIELDS.csv`
Tests:  `tests/test_curation.py`

## What it produces

`python3 scripts/build_curated_database.py --db data/retarats_pubmed.sqlite --out-dir exports/curated`

writes to `exports/curated/`:

| file | purpose |
|---|---|
| `curated_evidence.csv` | one row per evidence record, wide — identity + structured fields + `facet_*` + reliability + publication status + appraisal. This is the main Sheets/Airtable table. |
| `facets_long.csv` | tidy `(evidence_id, facet_group, facet_value, facet_label, facet_source)` — the filter/pivot table (e.g. `facet_group=species, facet_value=nonhuman_primate`). |
| `public_records.csv` | `auto_publish_eligible == True` subset (the curated public feed). |
| `review_queue.csv` | records awaiting human review, sorted by `display_priority`. |
| `molecule_index.csv` | per-molecule rollup for profile pages. |
| `field_dictionary.csv` + `schema.json` | backend-agnostic schema describing every table/field/facet. |
| `metadata_backfill_plan.csv` | (from `run_metadata_backfill.py --offline`) which papers are missing DOI/year/abstract/PMCID/OA and which API would fill them. |

## 1. Faceting — "drug X for use Y", "non-human primate data", …

`curation/facets.py` maps each record to normalized facets so users can slice the
database. Facet groups: `molecule, species, model_system, study_type, indication,
endpoint, mechanism, route, drug_class, population, sex, formulation,
evidence_direction, molecule_role, signal, year_bucket`. The literature-informed
groups `drug_class` (GLP-1/GIP/glucagon agonist, amylin analog, SGLT2i, NAD
precursor, senolytic, …), `population`, `sex`, `formulation` (oral peptide,
long-acting/weekly, nanoparticle), and `evidence_direction`
(positive/null/mixed/adverse) are pattern-driven from `config/FACETS.csv` — add
rows there, no code change.

- **Controlled vocabulary** lives in `config/FACETS.csv` (group, value, label,
  `||`-separated patterns). Edit this file to extend tagging — no code change.
- **Species granularity** includes `nonhuman_primate` (monkey/macaque/cynomolgus/
  rhesus/marmoset/baboon), rodents, pig, dog, rabbit, zebrafish, drosophila,
  c_elegans, cell lines — so "show me NHP data" is a real filter (19 records in the
  current DB).
- **Provenance**: `facet_source` records whether a tag came from a structured field
  (`structured:model_type`) or a text hit (`text:pattern`), and flags `:unmapped`
  ids so the vocabulary can be curated over time.
- **Performance**: single-word patterns are matched by token-set intersection,
  phrases by substring, and only genuinely regex-y patterns hit the regex engine —
  the full 14.9k-row build runs in ~30s.

To filter, either use the `facet_*` columns in `curated_evidence.csv` (semicolon
lists) or pivot `facets_long.csv` (one row per tag).

## 2. Reliability / evidence-strength score

`curation/strength.py` emits a transparent 0–100 `reliability_score` plus
`reliability_tier` (high/moderate/limited/low/non_efficacy) and a JSON
`reliability_components` breakdown. Components and max points:

| component | max | basis |
|---|---|---|
| study_design | 40 | RCT/meta-analysis > systematic review > human non-RCT > human obs > animal > in vitro |
| directness | 20 | is the molecule the direct intervention; human > animal > in vitro |
| sample_size | 15 | reported N |
| comparator | 10 | placebo > active/standard > vehicle > any control |
| completeness | 10 | how many key structured fields are populated |
| recency | 5 | publication year |

Methods/assay/synthesis/environmental roles are scored 0 and tiered
`non_efficacy` so they never compete with therapeutic evidence. `reliability_rationale`
gives a one-line justification for each record.

### 2b. Journal reputation (venue signal)

`curation/journal.py` emits a rule-based `journal_reputation` (0-100), a
`journal_tier` (`flagship`/`top`/`strong`/`standard`/`low`/`predatory`), and a
`journal_rationale`. It is a **curated, auditable allowlist** of high-reputation
biomedical venues (NEJM, Lancet, JAMA, Nature/Cell/Science families, Diabetes
Care, Circulation, Cochrane, …) plus a tiny low/predatory-signal list — no live
impact-factor lookup. **Unknown/blank journals default to a neutral 50
(`standard`)**; an unrecognized venue is never punished to 0. Extend the table in
`_VENUE_TIERS`.

This feeds a small `venue` axis in ranking (see below).

## 3. Publication-status decision layer (replaces `public_candidate`)

`curation/publication_status.py` emits: `publication_status`, `website_section`,
`auto_publish_eligible`, `review_reason`, `publish_rule_id`, `display_priority`,
`required_fields_present`, `missing_required_fields`.

- **Policy = moderate** (the old single flag was too conservative). Thresholds live
  in `config/PUBLICATION_RULES.csv` — per website section, a `min_score_auto`
  (auto-publish) and `min_score_candidate` (queue-but-eligible). Tune these numbers
  to make the site more or less aggressive without touching code.
- **Required fields** (`config/REQUIRED_FIELDS.csv`) gate eligibility: a record must
  have molecule, an identifier (PMID *or* DOI), title, model, year, study type, and
  role before it can publish. `unclear` counts as a populated model (it's a real
  classification), only truly-blank fields are "missing".
- **Statuses**: `auto_published` (strong + complete), `review_candidate` (queued but
  eligible), `held_low_evidence`, `held_missing_fields`, `held_out_of_scope`.

Current run: **1,050 auto-published, 4,844 review candidates, 9,000 held** — plus
654 high / 1,385 moderate reliability records.

## 4. Rule-based appraisal + LLM-ready scaffold

`curation/appraisal.py` emits `appraisal_strengths`, `appraisal_limitations`,
`appraisal_summary` (one-line synopsis), and `appraisal_confidence`. Strengths/
limitations are rule-derived (e.g. *placebo-controlled* vs *no comparator reported*,
*animal evidence may not translate*, *abstract-only extraction*).

It also writes the **scaffold for a future LLM pass**: an empty `llm_summary`
column, `llm_summary_status = not_generated`, and `summary_provenance` JSON. When you
want cheap/free prose summaries later, a batched local model can fill `llm_summary`
for the ~1,050 public records only (not all 14.9k) without reshaping the schema.

## 4b. Combined ranking (`rank_score`)

`curation/ranking.py` blends six auditable axes into a 0-100 `rank_score`
(`rank_components` records each axis's contribution):

| axis | weight | basis |
|---|---|---|
| directness | 33% | translational evidence level |
| quality | 28% | within-class study quality (`reliability_score`) |
| relevance | 20% | how central the molecule is to the paper |
| recency | 10% | newer evidence higher |
| impact | 5% | citation count (0 until backfilled) |
| venue | 4% | `journal_reputation` (curated; neutral 50 default) |

**Venue vs. impact design decision:** journal reputation is its own small axis
rather than folded into `impact`. `impact` is citation-driven and 0 for every
record until the citation backfill runs; `venue` is available (~50) for *every*
record. Folding them would inflate `impact` from 0 to ~50 DB-wide and silently
shift every score. A separate 4% axis keeps both independently auditable.
Directness + quality stay dominant (61% combined); venue never sinks a record.

## 4c. Citation impact backfill (OpenAlex → Semantic Scholar)

`scripts/run_impact_backfill.py` fills `citation_count` (+ `citation_source`) onto
papers to power the `impact` axis. It tries **OpenAlex first** (by DOI, then PMID)
and **falls back to the keyless Semantic Scholar Graph API** when OpenAlex returns
nothing — covering the ~32 no-DOI papers via PMID. From Semantic Scholar it also
fills `s2_authors` (JSON `{name, authorId, url}`) and `influential_citation_count`.

- **Offline:** `--offline` prints the coverage plan (no network).
- **Live (networked machine):** drop `--offline`.
- **Recency-first:** the historical fetch (`run_backfill.py`) walks
  **newest→oldest**, so the papers table is already recency-ordered and
  `--max-records` backfills the most recent papers first. `--newest-first` makes
  this explicit (sorts missing papers by `pub_year` descending before capping),
  satisfying "prioritize recent papers".

```
python3 scripts/run_impact_backfill.py --db data/retarats_pubmed.sqlite --offline
python3 scripts/run_impact_backfill.py --db data/retarats_pubmed.sqlite --max-records 500 --newest-first  # live
```

## 4d. Experimental (candidate) molecules

`config/EXPERIMENTAL_MOLECULES.csv` proposes ~15 candidate peptides/drugs that fit
the metabolic/longevity/peptide-therapeutic set (survodutide, mazdutide, CagriSema,
orforglipron, danuglipron, bimagrumab, MOTS-c, humanin, epitalon, BPC-157,
thymosin beta-4, NMN, urolithin A, pemvidutide, …). They have **no fetched data**.

`scripts/list_experimental.py` prints / validates / exports them
(`--json`, `--csv out.csv`, `--include-experimental`). To bring a candidate into
the pipeline: add it to `config/MOLECULES.csv` (reuse `example_search_terms` as
synonyms) + `config/SEARCH_RULES.csv`, run a fetch/backfill on a networked
machine, then re-run `build_curated_database.py`.

## 5. Multi-API metadata validation + backfill

`enrichment/backfill.py` + `scripts/run_metadata_backfill.py` validate and fill
missing identity/metadata (DOI, year, journal, abstract, PMCID, open-access status)
from **EuropePMC, Crossref, PMC ID Converter/ELink, and Unpaywall** — non-destructively
(`backfilled_*` + `*_source` provenance).

- **Offline (this sandbox / any machine):** `--offline` produces a coverage plan
  only. Current DB: 14,229 papers all missing PMCID, 14,197 missing OA status, 337
  missing abstract, 32 missing DOI.
- **Live (run on a networked machine — NCBI/EuropePMC must be reachable):**
  ```
  python3 scripts/run_metadata_backfill.py --db data/retarats_pubmed.sqlite --max-records 200
  ```
  writes `exports/curated/metadata_backfill_audit.csv` with proposed fills.

## Google Sheets now → Airtable later

`schema.json` describes the tables backend-agnostically. For Sheets: import
`curated_evidence.csv` and `facets_long.csv` as two tabs; filter/pivot on `facet_*`
or `facets_long`. For Airtable later: `curated_evidence` = main table (primary key
`evidence_id`), `facets_long` = a linked "Facets" table keyed by `evidence_id`,
`molecule_index` = a "Molecules" table. The same CSVs feed either backend.

## Run summary

```
# build the curated dataset (all offline)
python3 scripts/build_curated_database.py --db data/retarats_pubmed.sqlite --out-dir exports/curated

# metadata gap report (offline) / live backfill (networked machine)
python3 scripts/run_metadata_backfill.py --db data/retarats_pubmed.sqlite --out-dir exports/curated --offline
python3 scripts/run_metadata_backfill.py --db data/retarats_pubmed.sqlite --max-records 200   # live

# tests
python3 tests/test_curation.py
```

> Note: on the offline sandbox the 155 MB SQLite lives on a slow mount; if a build
> feels slow there, copy the DB to local disk first (`cp data/…sqlite /tmp/db.sqlite`
> and point `--db` at it). On your Mac's local disk this is not needed.

## Autonomous-cycle additions (extractors, disambiguation, validation, site)

All rule-based / offline / non-destructive:

- `retarats_pipeline/curation/extractors.py` — `refine_extraction()` re-parses dose,
  route, duration, sample size (total-aware: a stated total is not summed with its
  arms), and proposes `model_primary`/`model_flags`/`model_confidence`/
  `model_disambiguation_reason` for records with mixed human/animal/in-vitro signals.
  Never overwrites `model_type`; the build reports `model_primary != model_type` (~10%).
- `scripts/validate_curated.py` — QA gate (reliability range, status/tier vocab,
  auto-published integrity, unique evidence_id, species vocab, and with `--db` an
  NHP-vs-text check). Nonzero exit on failure; writes `validation_report.txt`.
- `scripts/build_public_site.py` — self-contained `exports/site/index.html` browser
  with client-side facet filtering; HTML-escaped, `</script>` breakout neutralized.
- `scripts/run_curation_pipeline.py` + `--curate`/`--build-site` on the postprocessing runner.
- Tests: `tests/test_extractors.py` (37) + `tests/test_site.py` (11) + `tests/test_curation.py` (22) = 70 assertions.
