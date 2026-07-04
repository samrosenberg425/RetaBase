# RetaRats PubMed Pipeline v2

This companion pipeline keeps the original `retarats.py` intact and adds a more website-ready flow:

- PubMed ingestion uses ESearch history plus EFetch XML parsing.
- Classification stays rule-based and auditable.
- A deterministic evidence-summary layer adds concise comparison-friendly summaries.
- A role-characterization layer separates direct intervention evidence from
  comparator/background, biomarker, pathway, assay, synthesis, and
  environmental/material records.
- A paper-characterization and routing layer sends records into lane-specific
  postprocessors for interventions, mechanisms, biomarkers, reviews, methods,
  comparators, and unclear records.
- Outputs can go to local SQLite, Google Sheets, Airtable, or all three.

## Local Quick Start

Create `.env` from `.env.example`, then set at least:

```bash
NCBI_EMAIL=you@example.com
NCBI_API_KEY=optional-but-recommended
```

Use `V2_RUN_MODE`, `V2_DEFAULT_DAILY_DAYS`, and `V2_DEFAULT_START_YEAR` for
the companion runner defaults. The original `retarats.py` still uses
`RUN_MODE=AUTO|BACKFILL_ONLY|DAILY_ONLY`, so keeping the v2 settings under
`V2_*` avoids mode-name collisions.

Install dependencies:

```bash
pip install -r requirements.txt
```

Run a tiny local test using the Excel workbooks in `inputs/`:

Double-click `RUN_PIPELINE.command` in Finder, then choose option `1`.

Terminal backup:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_full_local_pipeline.py --mode daily --daily-days 30 --molecule retatrutide --max-records-per-rule 5
```

Prefer the double-click launcher if Terminal paste is inserting stray text such
as `00~`. If Terminal keeps doing that, type this reset command manually and
press Return:

```bash
printf '\033[?2004l'
```

If you already generated local results before the relevance/profile fields were
added, include `--refresh-seen` once to regenerate rows with the new fields.
Current runs load `config/ROLE_RULES.csv` by default when that file exists.

## Multi-Stage Postprocessing

The broad role/review categories are not meant to be the final website
taxonomy. They are routing signals. The richer postprocessing flow is:

1. `scripts/characterize_roles.py` identifies the molecule's apparent role in
   the paper.
2. `scripts/characterize_papers.py` extracts paper anatomy fields such as
   `paper_purpose`, `what_it_is`, `evidence_question`, `condition_tags`,
   `endpoint_tags`, `intervention_or_exposure`, `comparator_or_control`,
   `dose_route`, `duration`, `sample_size`, `efficacy_signal`,
   `safety_signal`, and `mechanistic_focus`.
3. `retarats_pipeline/processing_router.py` assigns a `processing_lane` and
   `next_postprocessing_script`.
4. Lane scripts write focused files for deeper refinement:
   `postprocess_interventions.py`, `postprocess_reviews.py`,
   `postprocess_mechanisms.py`, `postprocess_biomarkers.py`,
   `postprocess_comparators.py`, `postprocess_methods.py`, and
   `postprocess_unclear.py`.

Run the full local chain:

```bash
python scripts/run_full_local_pipeline.py --skip-fetch
```

This produces:

- `exports/evidence_characterized.csv`
- `exports/evidence_review.csv`
- `exports/processing_routes_summary.csv`
- `exports/prisma/review_slice_manifest.csv`
- `exports/prisma/flow_counts_by_slice.csv`
- `exports/prisma/exclusion_reasons_by_slice.csv`
- `exports/prisma/methods_summary.md`
- `exports/review_slices/*.csv`
- `exports/lanes/*.csv`
- `exports/postprocessed/*_refined.csv`

The current lane files are intentionally simple but explicit handoff points.
Each can grow its own rules later without destabilizing the others.

## Enrichment Layer

The newer multilayer schema is installed as a separate enrichment pass so it
can be tested without changing the main extraction fields. It runs after
`characterize_papers.py` and before the lane-specific exporters.

The current order is:

1. PubMed title/abstract/MeSH metadata extraction.
2. Rule-based characterization and processing-lane routing.
3. Missing-field and completeness audit.
4. Optional PMC full-text fallback for eligible incomplete records.
5. Audit CSVs and review queues.

For human intervention rows, it proposes NCT/trial metadata, population,
comparator, dose, duration, sample-size, endpoint, and review-queue fields. For
preclinical and basic-science rows, it proposes model/species, mechanism,
condition, endpoint, and contextual dose/duration/sample-size fields.

The first pass is deliberately non-destructive: it writes `enriched_*`,
`abstract_*`, `pmc_*`, `suggested_*`, and `suggest_replace_*` fields, plus
audit CSVs. The original fields such as `dose_route`, `sample_size`, and
`mechanistic_focus` are not replaced automatically.

Smoke test the enrichment layer without API calls or SQLite writes:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_enrichment_pipeline.py --db data/retarats_pubmed.sqlite --offline --csv-only --max-records 25
```

Run a small live PMC smoke test:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_enrichment_pipeline.py --db data/retarats_pubmed.sqlite --mode basic --csv-only --max-records 100 --enable-pmc --pmc-max-records 2
```

Run the full postprocessing chain with offline enrichment:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_postprocessing_pipeline_enriched.py --db data/retarats_pubmed.sqlite --config-mode inputs --role-rules config/ROLE_RULES.csv --review-slices config/REVIEW_SLICES.csv --offline-enrichment
```

New outputs include:

- `exports/enriched/human_intervention_enrichment_audit.csv`
- `exports/enriched/basic_science_enrichment_audit.csv`
- `exports/enriched/pmc_full_text_audit.csv`
- `exports/enriched/evidence_enriched_subset.csv`
- `exports/enriched/clinicaltrials_matches.csv`
- `exports/enriched/clinicaltrials_registry_records.csv`
- `exports/review_queue/*.csv`

PMC fallback is disabled unless `--enable-pmc` is passed. The router currently
targets incomplete `preclinical_intervention`, `mechanism_or_pathway`,
`biomarker_or_readout`, and future review records. It skips methods, diagnostic,
environmental/materials, comparator/background, general context, and unclear
manual-triage rows by default.

## Review Slices

Review slices are saved PICO/PECO-like filters over the broad database. They
are similar to Anki filtered decks built from tags: the underlying evidence
database stays broad, and each slice pulls out records matching a specific
molecule/use/model/outcome question.

Definitions live in:

- `config/REVIEW_SLICES.csv`

Each row can filter by molecule, processing lane, paper purpose, model type,
study type, role category, condition tags, endpoint tags, evidence strength,
publication year, and text include/exclude terms. Terms inside one field are
OR logic; different fields are AND logic.

Build only the slices:

```bash
python scripts/build_review_slices.py --db data/retarats_pubmed.sqlite --slices config/REVIEW_SLICES.csv
```

Example outputs:

- `exports/review_slices/retatrutide_obesity_t2d_human_intervention.csv`
- `exports/review_slices/tirzepatide_obesity_t2d_human_intervention.csv`
- `exports/review_slices/glutathione_direct_intervention.csv`

The audit files under `exports/prisma/` show the flow counts and exclusion
steps for each slice. This is PRISMA-S-informed evidence mapping, not a full
PRISMA systematic review.

## Role Characterization

`config/ROLE_RULES.csv` is the auditable rule set for molecule-specific role
classification. These labels are broad by design because they are used for
routing and review, not as the final display taxonomy:

- `direct_intervention` means the paper appears to administer, prescribe, or
  test the molecule itself.
- `comparator_or_background_drug` means the molecule is probably one of several
  therapies, a background medication, or a comparator.
- `biomarker_readout` means the molecule is measured as a level, ratio, or
  readout rather than administered.
- `pathway_component` means the molecule appears as part of a mechanism,
  receptor, enzyme, signaling, metabolic, or endogenous pathway discussion.
- `assay_or_detection`, `synthesis_or_production`, and
  `environmental_or_material_use` are kept internally but are not public
  evidence candidates.

The resulting triage fields are:

- `role_category`: the best machine-readable role label.
- `evidence_strength_label`: a simple evidence tier based on study type and
  model, not a claim about effect size or clinical validity.
- `role_review_bucket`: `public_candidate`, `curator_review`,
  `background_only`, or `exclude_noise`.
- `public_candidate`: `True` only when the role is direct intervention and the
  model/study type is at least animal evidence.

This is meant to be statistically and scientifically defensible because the
pipeline does not infer efficacy from keyword matches. It preserves records,
labels the apparent role of the molecule, then routes different paper types to
different extraction scripts.

After changing `config/ROLE_RULES.csv`, you can re-score an existing local
SQLite database without calling PubMed again:

```bash
python scripts/characterize_roles.py \
  --db data/retarats_pubmed.sqlite \
  --role-rules config/ROLE_RULES.csv \
  --config-mode inputs
```

That updates the local `evidence` and `molecule_profiles` tables and writes:

- `exports/evidence_roles.csv`
- `exports/molecule_profiles_roles.csv`

Export the local database to CSV:

```bash
python scripts/export_sqlite_to_csv.py
```

After a refreshed run, this writes `molecules.csv`, `papers.csv`,
`evidence.csv`, and `molecule_profiles.csv`.

## Notebook Runner

For interactive tuning, open:

```text
notebooks/retarats_v2_colab_runner.ipynb
```

The notebook prompts for credentials, previews the input workbooks, streams run progress live, shows local SQLite outputs, and exports CSVs. This is the best starting point while search rules and Airtable fields are still changing.

## Rule-Based Evidence Summaries

The summary layer is intentionally deterministic for now. It writes one concise
`evidence_summary` per evidence row and a cumulative `molecule_profiles` table
for side-by-side comparison. Use `--summary-mode rule_based`; `auto` and
`heuristic` are accepted aliases for the same rule-based behavior.

The summary is not an OpenAI-generated structured abstract. It is a concise
evidence note built from the PubMed title, abstract snippets, model/study
classification, relevance class, and molecule role. Use it to compare papers
quickly, then keep the PMID/abstract available for curator review.

## Airtable Output

Create Airtable tables named:

- `Molecules`
- `Papers`
- `Evidence`
- `MoleculeProfiles`

Then set:

```bash
AIRTABLE_API_KEY=...
AIRTABLE_BASE_ID=...
OUTPUT_SINKS=local,airtable
```

The sink uses stable merge keys:

- `Molecules.molecule_id`
- `Papers.pmid`
- `Evidence.evidence_id`
- `MoleculeProfiles.molecule_id`

For website embedding, the eventual best source should be a curated view built
from `processing_lane`, `paper_purpose`, and curator approval status, not only
from `public_candidate`. Keep all lanes in Airtable/Google Sheets so mechanism,
biomarker, review, method, and comparator records can still power useful
sections of the final database.

## Input Workbooks

The recommended local config source is now:

- `inputs/Moleculessearch.xlsx`: tabs `MOLECULES` and `SEARCH_RULES`
- `inputs/Summary Sheet.xlsx`: tab `Main`

`Moleculessearch.xlsx` controls what the pipeline searches. `Summary Sheet.xlsx` is treated as a curator/product guide and is merged onto molecule records by `display_name`. That means fields such as `Evidence Stage`, `US Regulatory Status`, `ex-US Status`, and `Access Pathway` can travel into local/Airtable molecule records without changing PubMed search behavior.

## Google Sheets Output

Authenticate with Application Default Credentials:

```bash
gcloud auth application-default login
```

Then run:

```bash
python retarats_v2.py --sinks local,google --output-google-sheet RetaRats_PubMed_v2
```

## Colab Shape

In Colab, the simplest version is:

```python
!git clone https://github.com/jbrenner-dextrosedaddy/retarats-pubmed-pipeline.git
%cd retarats-pubmed-pipeline
!pip install -r requirements.txt
```

Then set environment variables in notebook cells:

```python
import os
os.environ["NCBI_EMAIL"] = "you@example.com"
os.environ["NCBI_API_KEY"] = "..."
os.environ["AIRTABLE_API_KEY"] = "..."
os.environ["AIRTABLE_BASE_ID"] = "..."
```

Run:

```python
!python scripts/run_full_local_pipeline.py --mode daily --daily-days 7 --sinks local,airtable
```

For live updates, schedule the notebook or move this command into GitHub Actions / Cloud Run once credentials are stable.
