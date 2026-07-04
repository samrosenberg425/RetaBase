# retarats-pubmed-pipeline

A PubMed ingestion + classification pipeline that reads a Google Sheet config (molecules + search rules), queries PubMed, and writes results into Google Sheets dashboards.

## New companion pipeline

This repo now also includes `retarats_v2.py`, a local/Colab-friendly companion pipeline. It keeps rule-based classification, adds concise comparison summaries, reads the local Excel workbooks in `inputs/`, characterizes the molecule's role in each paper, and can write to local SQLite, Google Sheets, and Airtable.

Start here for the v2 flow:

Double-click this file in Finder:

```text
RUN_PIPELINE.command
```

Then choose option `1` for a smoke test. This avoids Terminal paste issues such
as stray `00~` text.

Terminal backup:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_full_local_pipeline.py --mode daily --daily-days 30 --molecule retatrutide --max-records-per-rule 5
```

By default, `config/ROLE_RULES.csv` separates likely public direct-intervention
evidence from comparator/background, biomarker, pathway, assay, synthesis, and
environmental/material records. The public-facing flag is intentionally
conservative: records are public candidates only when the molecule appears to be
directly tested/administered and the evidence is at least animal-level.

When role rules change, re-score local results without calling PubMed:

```bash
python scripts/run_full_local_pipeline.py --skip-fetch
```

The postprocessing pipeline treats broad role categories as routing signals,
then sends records into lane-specific files for interventions, mechanisms,
biomarkers, reviews, methods, comparators, and unclear records.

The enrichment layer adds the newer multilayer schema after paper
characterization. It now uses PubMed title/abstract/MeSH metadata as a formal
first extraction source, then optionally tries PMC full text only for eligible
incomplete records. It proposes `abstract_*`, `pmc_*`, `suggested_*`, and
`suggest_replace_*` fields without overwriting the original extraction fields.
To smoke test the abstract-first layer without API calls or SQLite writes,
choose option `5` in `RUN_PIPELINE.command` or run:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_enrichment_pipeline.py --db data/retarats_pubmed.sqlite --offline --csv-only --max-records 25
```

To test a very small live PMC pass, choose option `7` or run:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_enrichment_pipeline.py --db data/retarats_pubmed.sqlite --mode basic --csv-only --max-records 100 --enable-pmc --pmc-max-records 2
```

Saved review slices in `config/REVIEW_SLICES.csv` work like filtered decks built
from tags. They export narrower PICO/PECO-like spreadsheets under
`exports/review_slices/` plus PRISMA-S-informed audit files under
`exports/prisma/`.

See `docs/v2_local_and_colab.md` for Airtable, Google Sheets, Colab, and role-review notes. See `docs/repo_architecture.md` for the full repo map and the defensible query/classification principles.

## What the script reads (required)
This script reads a Google Spreadsheet named:
* `CONFIG_SHEET_NAME` (default: `Moleculessearch`)

That Google Sheet must contain **two tabs** with these exact names:
* `MOLECULES`
* `SEARCH_RULES`

The repo includes the exact CSV exports of those two tabs:

* `MOLECULES.csv`
* `SEARCH_RULES.csv`

You should import/paste these into your own Google Sheet to get started.

## Quick start
### 1) Create your config Google Sheet

1. Go to Google Sheets and create a new spreadsheet
2. Name it: `Moleculessearch` (or any name you want — just match `CONFIG_SHEET_NAME`)
3. Create two tabs:

   * `MOLECULES`
   * `SEARCH_RULES`

### 2) Import the CSVs into the tabs

* Open `MOLECULES.csv` from this repo, copy all rows, paste into the `MOLECULES` tab starting at cell A1
* Open `SEARCH_RULES.csv` from this repo, copy all rows, paste into the `SEARCH_RULES` tab starting at cell A1

(You can also use File → Import → Upload in Google Sheets and import each CSV into the correct tab.)

### 3) Set environment variables (“bring your own keys”)

Copy `.env.example` to `.env` and fill in:

* `NCBI_EMAIL` (required)
* `NCBI_API_KEY` (recommended)
* `CONFIG_SHEET_NAME` (default: `Moleculessearch`)

### 4) Install dependencies

```bash
pip install -r requirements.txt
```

### 5) Authenticate Google access

This script uses Google Application Default Credentials (ADC).

Common approaches:

* `gcloud auth application-default login`
* OR set `GOOGLE_APPLICATION_CREDENTIALS=/path/to/service_account.json`

### 6) Run

```bash
python retarats.py
```

## Output

The pipeline creates Google Sheets in your Drive (folder: `DRIVE_FOLDER_PATH`, default `My Drive/Retarats`) including:

* `..._PEPTIDE_DATA` / `..._PEPTIDE_TABS`
* `..._SMALL_MOLECULE_DATA` / `..._SMALL_MOLECULE_TABS`
* `..._MIXTURE_DATA` / `..._MIXTURE_TABS`
  with `PAPERS_MASTER`, `STATS`, and `QUALITY_ALERTS` tabs
