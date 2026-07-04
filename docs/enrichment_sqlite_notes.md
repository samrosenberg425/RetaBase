# RetaRats enrichment layer: SQLite and audit notes

## What SQLite stores now

The v2 local database uses JSON payload tables rather than many normalized SQL columns.
The main tables are:

- `molecules`: one JSON payload per `molecule_id`.
- `papers`: one JSON payload per `pmid`.
- `evidence`: one JSON payload per `evidence_id`; this is the molecule-paper link and contains classification/routing fields.
- `molecule_profiles`: one JSON payload per `molecule_id`.

Each row has a stable primary key plus `payload_json`. To inspect it manually:

```python
import sqlite3, json
conn = sqlite3.connect("data/retarats_pubmed.sqlite")
row = conn.execute("select payload_json from evidence limit 1").fetchone()
payload = json.loads(row[0])
print(payload.keys())
```

To export back to CSV after enrichment:

```bash
/opt/anaconda3/envs/research/bin/python scripts/export_sqlite_to_csv.py --db data/retarats_pubmed.sqlite
```

## Why enriched fields are prefixed

The first enrichment pass writes `enriched_*`, `suggested_*`, and `suggest_replace_*` fields into the `evidence` JSON payloads.
It does **not** overwrite original fields such as `dose_route`, `duration`, `sample_size`, or `comparator_or_control`.
This makes the first pass auditable.

The PubMed title/abstract/MeSH extraction layer also writes `abstract_*` fields.
When enabled, PMC full-text fallback writes `pmc_*` fields and PMC provenance
fields. These are also proposals/audit fields, not replacements.

Typical fields added to human intervention rows:

- `enriched_nct_ids_from_text`
- `enriched_nct_id`
- `enriched_trial_status`
- `enriched_trial_phase`
- `enriched_trial_enrollment`
- `enriched_trial_arms`
- `enriched_trial_interventions`
- `enriched_trial_primary_outcomes`
- `enriched_trial_secondary_outcomes`
- `suggested_dose_route`
- `suggested_duration`
- `suggested_sample_size`
- `suggested_comparator_or_control`
- `suggest_replace_dose_route`
- `suggest_replace_duration`
- `suggest_replace_sample_size`
- `enriched_human_original_completeness`
- `enriched_human_proposed_completeness`

Typical fields added to preclinical/basic-science rows:

- `abstract_model_system_detail`
- `abstract_mechanistic_focus`
- `abstract_condition_tags`
- `abstract_endpoint_tags`
- `abstract_dose_route`
- `abstract_duration`
- `abstract_sample_size`
- `enriched_model_type`
- `enriched_model_system_detail`
- `enriched_mechanistic_focus`
- `enriched_condition_tags`
- `enriched_endpoint_tags`
- `enriched_pubtator_chemical`, `enriched_pubtator_disease`, etc. when PubTator returns annotations
- `suggested_model_type`
- `suggested_model_system_detail`
- `suggested_mechanistic_focus`
- `suggested_condition_tags`
- `suggested_endpoint_tags`
- `enriched_basic_original_completeness`
- `enriched_basic_proposed_completeness`

Typical fields added when PMC fallback is enabled and a full-text record is
available:

- `pmc_enrichment_eligible`
- `pmc_enrichment_attempted`
- `pmc_enrichment_reason`
- `pmc_enrichment_status`
- `pmcid`
- `pmc_lookup_source`
- `pmc_fetch_source`
- `pmc_sections_used`
- `pmc_model_system_detail`
- `pmc_mechanistic_focus`
- `pmc_condition_tags`
- `pmc_endpoint_tags`
- `pmc_dose_route`
- `pmc_duration`
- `pmc_sample_size`

## Registry-only trials

ClinicalTrials.gov records that do not clearly map to a PubMed paper are exported separately to:

```text
exports/enriched/clinicaltrials_registry_records.csv
```

These are not inserted into `papers.csv`, because they are registry records rather than literature records.
They carry linking fields such as `linked_molecule_id`, `linked_molecule_name`, `linked_pmid`, and `linked_evidence_id` when a candidate link exists.

## First commands to run

Dry-run audit without external APIs:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_enrichment_pipeline.py \
  --db data/retarats_pubmed.sqlite \
  --offline \
  --csv-only \
  --max-records 100
```

Small live PMC audit with CSV outputs only:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_enrichment_pipeline.py \
  --db data/retarats_pubmed.sqlite \
  --mode basic \
  --csv-only \
  --max-records 100 \
  --enable-pmc \
  --pmc-max-records 2
```

Live audit with APIs and SQLite `enriched_*` updates:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_enrichment_pipeline.py \
  --db data/retarats_pubmed.sqlite \
  --contact-email sr2007@rwjms.rutgers.edu \
  --ncbi-email samrosenberg425@gmail.com
```

Full postprocessing chain with enrichment inserted after paper characterization:

```bash
/opt/anaconda3/envs/research/bin/python scripts/run_postprocessing_pipeline_enriched.py \
  --db data/retarats_pubmed.sqlite \
  --config-mode inputs \
  --role-rules config/ROLE_RULES.csv \
  --review-slices config/REVIEW_SLICES.csv \
  --contact-email sr2007@rwjms.rutgers.edu \
  --ncbi-email samrosenberg425@gmail.com
```

## API behavior

- Use `--offline` or `--offline-enrichment` for tests without API calls.
- Without an NCBI API key, the scripts should be run politely and will use slower request pacing.
- `API_CONTACT_EMAIL=sr2007@rwjms.rutgers.edu` is recommended in `.env` for Crossref/Unpaywall/polite user-agent behavior.
- `NCBI_EMAIL=samrosenberg425@gmail.com` can remain the NCBI email unless you decide to switch it later.
- PMC fallback uses NCBI BioC-PMC first, because it can retrieve open-access/author-manuscript full text in BioC JSON by PMID/PMCID when available.
- If BioC-PMC is unavailable for a record, the fallback tries NCBI ELink with `dbfrom=pubmed`, `db=pmc`, and `linkname=pubmed_pmc` to find a PMCID, then EFetch with `db=pmc` and `retmode=xml`.
- NCBI E-utility calls include `tool` and `email` parameters through `APIConfig`; `NCBI_API_KEY` is used when present.

## Recommended audit workflow

1. Run `--offline --csv-only --max-records 100` to verify local behavior.
2. Run live API mode on `--max-records 100`.
3. Inspect:
   - `exports/enriched/human_intervention_enrichment_audit.csv`
   - `exports/enriched/basic_science_enrichment_audit.csv`
   - `exports/enriched/pmc_full_text_audit.csv`
   - `exports/enriched/clinicaltrials_matches.csv`
   - `exports/enriched/clinicaltrials_registry_records.csv`
   - `exports/review_queue/*.csv`
4. Only after the audit looks good, add a separate replacement script that copies approved `suggested_*` fields into the main fields.
