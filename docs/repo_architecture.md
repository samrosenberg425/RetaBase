# Repository Architecture

This repository has two related pipelines:

- `retarats.py` is the original Google Sheets pipeline. It reads a Google Sheet
  config, queries PubMed, and writes Google Sheet dashboards.
- `retarats_v2.py` is the companion local/Colab/Airtable pipeline. It keeps the
  original script intact while adding local SQLite, Airtable output, evidence
  summaries, relevance triage, role categorization, paper characterization,
  processing lanes, and molecule profiles.

## Main v2 Flow

1. Load molecules and search rules from `inputs/Moleculessearch.xlsx` or the
   fallback CSVs in `config/`.
2. Merge curator/product fields from `inputs/Summary Sheet.xlsx` when available.
3. Query PubMed with NCBI ESearch history by rule and date window.
4. Fetch PubMed XML with EFetch and parse title, abstract, publication type,
   MeSH terms, chemicals, identifiers, dates, journal, and authors.
5. Classify study type and model with deterministic rules.
6. Classify molecule relevance with deterministic rules.
7. Create a concise rule-based evidence summary.
8. Characterize the molecule role with `config/ROLE_RULES.csv`.
9. Characterize paper anatomy fields such as purpose, model, condition,
   endpoints, intervention/comparator, dose/duration/sample-size hints,
   efficacy/safety signals, and mechanistic focus.
10. Route each evidence row to a lane-specific postprocessor.
11. Build saved review slices for PICO/PECO-like exports.
12. Write `molecules`, `papers`, `evidence`, and `molecule_profiles` to the
   configured sinks.

## Modules

- `retarats_pipeline/config.py`: loads local CSV, Google Sheet, or Excel input
  configuration.
- `retarats_pipeline/pubmed.py`: NCBI ESearch/EFetch client, retry/throttle
  handling, and PubMed XML parser.
- `retarats_pipeline/classifier.py`: rule-based study design and model
  classification.
- `retarats_pipeline/relevance.py`: first-pass molecule-paper relevance triage.
- `retarats_pipeline/summarizers.py`: concise deterministic evidence notes.
- `retarats_pipeline/role_classifier.py`: deeper role characterization for
  broad molecules and peptide mechanisms.
- `retarats_pipeline/paper_characterizer.py`: extracts richer paper anatomy
  fields for downstream review and comparison.
- `retarats_pipeline/processing_router.py`: assigns `processing_lane`,
  `database_section`, and `next_postprocessing_script`.
- `retarats_pipeline/lane_exporter.py`: shared helper for lane-specific
  postprocessing scripts.
- `retarats_pipeline/review_slices.py`: applies saved PICO/PECO-like filters
  and creates PRISMA-S-informed slice flow counts.
- `retarats_pipeline/profiles.py`: molecule-level evidence profile summaries.
- `retarats_pipeline/sinks.py`: local SQLite, Google Sheets, and Airtable
  writers.

## Scripts

- `scripts/characterize_roles.py`: re-runs role categorization against an
  existing local SQLite database. Use this after editing `config/ROLE_RULES.csv`
  so you do not have to query PubMed again.
- `scripts/characterize_papers.py`: adds richer paper anatomy fields and
  assigns each record to a processing lane.
- `scripts/run_postprocessing_pipeline.py`: runs role/paper characterization,
  lane-specific postprocessors, and CSV export as one local/Colab-safe chain.
- `scripts/build_review_slices.py`: exports saved review slices from
  `config/REVIEW_SLICES.csv`.
- `scripts/postprocess_interventions.py`: exports human and preclinical
  intervention records for deeper intervention extraction.
- `scripts/postprocess_reviews.py`: exports review/meta-analysis records.
- `scripts/postprocess_mechanisms.py`: exports mechanism/pathway records.
- `scripts/postprocess_biomarkers.py`: exports biomarker/readout records.
- `scripts/postprocess_comparators.py`: exports comparator/background records.
- `scripts/postprocess_methods.py`: exports methods/tool/material records.
- `scripts/postprocess_unclear.py`: exports unclear/general records.
- `scripts/export_sqlite_to_csv.py`: exports local SQLite payload tables to
  CSVs under `exports/`.

## Rule Files

- `config/MOLECULES.csv`: fallback molecule metadata.
- `config/SEARCH_RULES.csv`: fallback PubMed query rules.
- `config/ROLE_RULES.csv`: auditable role-classification rules.
- `config/REVIEW_SLICES.csv`: saved review-slice definitions for molecule/use,
  model, endpoint, and study-type filters.

The preferred editable source while you are tuning the project is
`inputs/Moleculessearch.xlsx`, with tabs `MOLECULES` and `SEARCH_RULES`.

## Outputs

The local SQLite database stores JSON payloads in:

- `molecules`
- `papers`
- `evidence`
- `molecule_profiles`

CSV exports mirror those tables:

- `exports/molecules.csv`
- `exports/papers.csv`
- `exports/evidence.csv`
- `exports/molecule_profiles.csv`
- `exports/evidence_review.csv`
- `exports/evidence_characterized.csv`
- `exports/processing_routes_summary.csv`
- `exports/review_slices/*.csv`
- `exports/prisma/review_slice_manifest.csv`
- `exports/prisma/flow_counts_by_slice.csv`
- `exports/prisma/exclusion_reasons_by_slice.csv`
- `exports/prisma/methods_summary.md`

Role re-scoring also writes:

- `exports/evidence_roles.csv`
- `exports/molecule_profiles_roles.csv`

Processing-lane exports write:

- `exports/lanes/*.csv`
- `exports/postprocessed/*_refined.csv`

## Defensible Query And Classification Principles

Search rules should be broad enough to capture the literature but specific
enough to avoid predictable false positives. A defensible rule set should:

- Include exact molecule names and important synonyms.
- Avoid overly short aliases unless they are paired with disambiguating terms.
- Track each search rule by `rule_id` and keep the exact query string stored
  through `source_query_hash`.
- Use publication dates consistently through PubMed date windows.
- Preserve non-public records internally instead of deleting them.

Classification rules should separate workflow routing from final scientific
claims. The pipeline does not infer efficacy, causality, or clinical usefulness
from keyword matches. It labels the apparent role of the molecule in the paper:

- `public_candidate`: likely direct intervention evidence and at least
  animal-level evidence.
- `curator_review`: potentially important, but ambiguous, broad, comparator,
  mechanistic, diagnostic, or high-level-review context.
- `background_only`: useful for mechanism/background but not public evidence for
  the molecule as an intervention.
- `exclude_noise`: analytical, synthesis, assay, formulation, environmental, or
  materials records that should remain internal.

Those labels are broad routing signals, not the final taxonomy. The more useful
final database fields come from `paper_characterizer.py` and lane-specific
postprocessors: `paper_purpose`, `what_it_is`, `evidence_question`,
`condition_tags`, `endpoint_tags`, `intervention_or_exposure`,
`comparator_or_control`, `efficacy_signal`, `safety_signal`,
`mechanistic_focus`, and `processing_lane`.

Review slices then narrow the broad database into PICO/PECO-like exports. They
work like saved filters: OR within one field and AND across fields. A record can
belong to multiple slices, so the database remains broad while the exports can
answer narrower questions such as retatrutide for obesity/T2D human
intervention evidence or BPC-157 in preclinical tissue repair models.

## Typical Local Commands

```bash
python retarats_v2.py \
  --config-mode inputs \
  --mode daily \
  --daily-days 30 \
  --molecule retatrutide \
  --max-records-per-rule 5 \
  --summary-mode rule_based \
  --role-rules config/ROLE_RULES.csv \
  --sinks local

python scripts/characterize_roles.py \
  --db data/retarats_pubmed.sqlite \
  --role-rules config/ROLE_RULES.csv \
  --config-mode inputs

python scripts/run_postprocessing_pipeline.py \
  --db data/retarats_pubmed.sqlite \
  --config-mode inputs \
  --role-rules config/ROLE_RULES.csv

python scripts/export_sqlite_to_csv.py
```
