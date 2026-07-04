# Enrichment build iteration log

## Iteration 1 — mapped current data flow
- Confirmed local SQLite stores JSON payload tables: `molecules`, `papers`, `evidence`, and `molecule_profiles`.
- Chose a non-destructive design that writes `enriched_*`, `suggested_*`, and `suggest_replace_*` fields first.

## Iteration 2 — built shared utilities
- Added JSON payload table helpers for loading/saving SQLite payload rows.
- Added safe CSV export, missing-value handling, NCT ID extraction, sentence splitting, and a cached HTTP client.
- Added `.env` support without requiring `python-dotenv`.

## Iteration 3 — added API client layer
- Added ClinicalTrials.gov v2 client for NCT lookup and search.
- Added parser for trial status, phase, enrollment, arms, interventions, outcomes, eligibility, and adverse-event availability.
- Added identifier/full-text helper client stubs for PMC ID Converter, Crossref, Unpaywall, and Europe PMC.
- Added PubTator3/legacy PubTator and Europe PMC annotation clients with graceful failure.

## Iteration 4 — built human intervention enrichment
- Added `enrich_human_interventions()`.
- It extracts NCT IDs from paper text, queries ClinicalTrials.gov when possible, and creates trial match/audit rows.
- It keeps registry-only trials separate from PubMed papers.

## Iteration 5 — built preclinical/basic-science enrichment
- Added `enrich_basic_science()`.
- It detects species/model systems, mechanisms, conditions, endpoints, and contextual dose/duration/sample-size hints.
- It supports PubTator/Europe PMC annotations but still works offline.

## Iteration 6 — added wrapper scripts
- Added `scripts/run_enrichment_pipeline.py` for all/human/basic modes.
- Added convenience wrappers for human-only and basic-only enrichment.
- Added `scripts/run_postprocessing_pipeline_enriched.py`, which inserts enrichment between `characterize_papers.py` and lane postprocessors.

## Iteration 7 — added review queues
- Added `scripts/build_review_queue.py`.
- It creates targeted review queues such as `pico_incomplete.csv`, `trial_registry_needed.csv`, `role_ambiguous.csv`, and `basic_science_incomplete.csv`.

## Iteration 8 — fixed audit logic after offline test
- Initial offline audit showed NCT IDs found in text but a confusing `no_trial_registry_match` reason in offline mode.
- Revised human review logic so an NCT found in text does not automatically create a no-registry-match reason when APIs are disabled.

## Iteration 9 — separated human vs animal candidates better
- Offline audit showed animal/in-vivo rows inside the human enrichment queue because some existing `processing_lane` assignments were broad.
- Revised human enrichment to exclude animal/in-vitro rows.
- Revised basic-science enrichment to include animal/in-vitro rows even if their current lane is not perfectly assigned.

## Iteration 10 — tested packaging and smoke tests
- Added a smoke-test script.
- Confirmed `py_compile` passes for all added Python files.
- Confirmed offline CSV-only mode writes expected outputs.
- Confirmed writing to a copied SQLite database adds `enriched_*` fields without modifying the original database.
- Confirmed convenience wrappers run in offline/csv-only test mode.

## Iteration 7 — curation layer (faceting, reliability, publication status, appraisal)
- Added `retarats_pipeline/curation/` package: `facets.py`, `strength.py`, `publication_status.py`, `appraisal.py`.
- **Faceting**: controlled vocabulary in `config/FACETS.csv`; normalizes species (incl. `nonhuman_primate`), indication, endpoint, mechanism, route, study type, model, signals. Emits wide `facet_*` columns + a tidy `facets_long` table with per-tag `facet_source` provenance. Optimized to a token-set/substring fast path (full 14.9k-row build ~30s).
- **Reliability score**: transparent 0–100 composite with a JSON component breakdown (study_design/directness/sample_size/comparator/completeness/recency) and tier label; methods/assay/environmental roles scored 0 / `non_efficacy`.
- **Publication status**: replaced the single `public_candidate` flag with `publication_status`, `website_section`, `auto_publish_eligible`, `review_reason`, `publish_rule_id`, `display_priority`, `required_fields_present`, `missing_required_fields`. Moderate policy; thresholds tunable in `config/PUBLICATION_RULES.csv`; required fields in `config/REQUIRED_FIELDS.csv` (DOI/PMID, drug, model, year, study type, role). First full run: 1,050 auto-published, 4,844 review candidates, 9,000 held.
- **Appraisal**: rule-based strengths/limitations + one-line synopsis, plus an empty `llm_summary` scaffold (`llm_summary_status=not_generated`, `summary_provenance`) so a cheap/free LLM pass can slot in later for the ~1,050 public records only.
- **Builder**: `scripts/build_curated_database.py` writes the curated Sheets/Airtable-ready exports (`curated_evidence`, `facets_long`, `public_records`, `review_queue`, `molecule_index`, `field_dictionary`, `schema.json`).
- **Backfill**: `enrichment/backfill.py` + `scripts/run_metadata_backfill.py` (EuropePMC/Crossref/PMC/Unpaywall) validate & fill missing DOI/year/abstract/PMCID/OA non-destructively; offline coverage plan works in-sandbox, live mode for a networked machine.
- **Tests**: `tests/test_curation.py` — 22 offline assertions, all passing.
- See `docs/curation_and_publication.md`.

## Remaining intentional limitations
- Live API calls were not executed in the sandbox; the code is written against current public API documentation and tested offline.
- The first implementation only proposes replacements. It does not overwrite original fields.
- A future approved-replacement script should copy selected `suggested_*` values into main fields after audit.
- ClinicalTrials.gov matching by search is intentionally conservative; NCT IDs found in paper text are treated as high-confidence, while molecule/title/condition search matches are medium/low confidence.

## Iteration 8 — autonomous working copy (extractors, disambiguation, validation, public site)
- Public static-site generator (`scripts/build_public_site.py`) with client-side facet filtering; security-reviewed (HTML-escaped, `</script>` neutralized, textContent-only, encodeURIComponent links). `tests/test_site.py` (11).
- Deeper extractors (`retarats_pipeline/curation/extractors.py`): dose/route/duration/sample-size re-parse + model disambiguation (`model_primary` etc., non-destructive). Reviewer-driven fixes: total-aware sample size (no total+arm double-count), local age-window for durations, BMI/`kg·m⁻²` dose exclusion, word-bounded `_N_EXCLUDE` (fixed "ug"-in-"drug" false exclude), human-reagent text no longer implies human model, precompiled route regexes. `tests/test_extractors.py` (37).
- QA validator (`scripts/validate_curated.py`) — passes on real data incl. NHP-vs-text check (15/15 backed at 6k rows). Wired via `run_curation_pipeline.py` + `--curate` flag.
- Collaboration model: implementer agent → independent reviewer agent → orchestrator applied fixes and re-verified. 70/70 tests pass.
