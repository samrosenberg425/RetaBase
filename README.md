# RetaBase

**Transparent, rule-based evidence on bioactives gaining new attention as emerging medications or novel use cases** 

🔎 **Live dashboard:** https://samrosenberg425.github.io/RetaBase/

RetaBase continuously pulls the biomedical literature for a curated set of bioactive molecules, classifies each paper by **study design** with rule-based, auditable methods, and publishes a browsable, filterable dashboard ordered by evidence level first. It does not rate how well any individual study was conducted, and its internal 0–100 heuristics are not validated quality measures. It also surfaces **ClinicalTrials.gov** registry studies and **preprints** in separate, clearly-labeled sections.

> ⚠️ **Not medical advice.** RetaBase is a research/literature-aggregation tool. Nothing here is a recommendation to use, dose, or avoid any substance. Many of these compounds are experimental or not approved for the uses discussed. Consult a qualified clinician.

---

## Who it's for

- **Physicians** whose patients ask about these compounds and who want the evidence landscape at a glance.
- **Researchers** who want a filterable, exportable map of the literature (human vs preclinical, by indication, endpoint, mechanism, etc.).
- **Curious readers** who want to understand what the science actually says, with study type, and what could not be read from the text, made explicit.

---

## How it works

```
PubMed / PMC ─┐
OpenAlex ─────┤  fetch + enrich        curation (rule-based, offline)          publish
Semantic ─────┼──► SQLite corpus ─────► facets → reliability → directness ─────► site_data.json ─► GitHub Pages
  Scholar     │    (Actions cache)      → relevance → recency → impact           (+ trials/preprints    (dashboard)
CT.gov ───────┤                         → venue → rank → publication status       feeds)
EuropePMC ────┘                         → appraisal
```

1. **Fetch** — `retarats_v2.py` queries PubMed for every molecule/rule in `config/SEARCH_RULES.csv` and stores titles, abstracts, MeSH, authors, journal, DOI, etc. in a SQLite corpus.
2. **Enrich** — citation counts from **OpenAlex** (fallback **Semantic Scholar**); **NIH iCite** field/time-normalized metrics (Relative Citation Ratio, NIH percentile, Approximate Potential to Translate, the human/animal/molecular "triangle of translation", and clinical-article flags); **PubChem** compound IDs (CID links + synonyms); registry trials from **ClinicalTrials.gov**; preprints from **EuropePMC** (bioRxiv/medRxiv).
3. **Curate** (`retarats_pipeline/curation/`, pure rule-based, offline, non-destructive):
   - **facets** — normalized tags (species incl. non-human primate, indication, endpoint, mechanism, route, drug class, population, sex, formulation, evidence direction, plus iCite-derived evidence-impact tier and clinical-article status).
   - **reliability** (legacy heuristic, hidden from the public site) — a study-type starting value plus keyword credits from the title/abstract. Not a validated measure of rigor or quality, and not comparable across study types.
   - **directness** (legacy heuristic, hidden from the public site) — a fixed value per study type (human RCT high → in-vitro low). It ignores the molecule's role in the paper and is not a measure of how directly the evidence applies; iCite's human/animal/molecular triangle and clinical-article flag inform the human/animal/in-vitro classification when the keyword heuristics are silent.
   - **ranking** — a legacy within-level ordering score (see below). It is not a quality measure.
   - **publication status** — broad inclusion; only genuinely off-topic records are excluded.
   - **appraisal** — rule-based strengths/limitations + an LLM-ready summary slot.
4. **Publish** — `build_curated_database.py` writes a compact `site_data.json`; `build_public_site.py` renders a single self-contained `index.html`; GitHub Pages serves it.

### The ordering score (legacy; auditable, not a quality measure)

The feed is ordered by evidence level first. Within a level, `rank_score` (0–100) orders records. It is a weighted blend with the weights hard-coded in `retarats_pipeline/curation/ranking.py`; it is stored in the data files but not shown on the public site:

| axis | weight | meaning |
|---|---|---|
| directness | 30% | fixed value per study type (a legacy heuristic) |
| quality | 28% | study-type starting value plus keyword credits (a legacy heuristic) |
| relevance | 18% | how central the molecule is to the paper |
| recency | 10% | newer evidence ranked higher |
| impact | 10% | prefers iCite field-normalized metrics (NIH percentile, then RCR) over raw citation count (log-scaled); 0 when unavailable, which is common for recent papers |
| venue | 4% | journal reputation (curated; neutral for unknown) |

Full method write-up is in the dashboard's **About / Methods** tab and `docs/curation_and_publication.md`.

---

## The dashboard

- **Evidence** — all records, ordered by evidence level first, with include/exclude multi-select filters (with select-all per domain), year (before/after/range/exact), journal-name, and min-citations filters, plus cross-filter counts. Sort by the default order (evidence level first), year, citations, **impact percentile**, **translational potential (APT)**, or **clinical influence**; toggle **clinical articles only**; and open a **Triangle view** that plots the filtered papers on the NIH iCite biomedicine triangle (Human / Animal / Molecular).
- **Clinical evidence** — records the automated classification places in human categories (no animal/in-vitro/methods; the classification can be wrong).
- **Trials registry** — ongoing & completed ClinicalTrials.gov studies (registrations, not results).
- **Preprints** — bioRxiv/medRxiv (not peer-reviewed).
- **Bioactives** — per-molecule index.
- **About / Methods** — how every metric is defined and computed.

Each paper shows authors (linked to Google Scholar), journal + reputation tier, an evidence-level badge, citation counts, a plain-language summary, and strengths/limitations. A per-paper detail view shows every field. The legacy 0–100 values are not shown in the public build. Everything renders safely (all values via `textContent`; no injection).

**Embed it anywhere** with an iframe:
```html
<iframe src="https://samrosenberg425.github.io/RetaBase/"
        style="width:100%;height:900px;border:0;border-radius:8px" loading="lazy"></iframe>
```

---

## Automation (set-and-forget)

All free, on GitHub Actions; the growing SQLite corpus lives in the **Actions cache** (~10 GB) so the git repo stays small. Workflows are serialized by a shared `retarats-corpus` concurrency lock so they never corrupt the cache.

| Workflow | Schedule | Does |
|---|---|---|
| `update.yml` | **daily** | `validate_config` fail-fast → incremental PubMed fetch + citation top-up → rebuild → **deploy to Pages** |
| `backfill.yml` | **every 6 h** (auto) | `validate_config` fail-fast → historical fetch (bounded per run; resumes via checkpoint until `min_year`/`target_gb`) **+ OpenAlex citation top-up + NIH iCite enrichment**, all in one job |
| `citations.yml` | **daily** | OpenAlex→Semantic-Scholar citation backfill (a lazy daily backstop to the 6 h job) |
| `registry.yml` | **twice weekly** (Mon + Thu) | ClinicalTrials.gov trials + EuropePMC preprints |

You can also trigger any of them manually (Actions → *workflow* → **Run workflow**). The full recommended run order is in [`docs/UPLOAD_CHECKLIST.md`](docs/UPLOAD_CHECKLIST.md).

---

## Run it locally

```bash
git clone https://github.com/samrosenberg425/RetaBase.git
cd RetaBase
cp .env.example .env          # add NCBI_EMAIL, NCBI_API_KEY, API_CONTACT_EMAIL
bash setup.sh                 # venv + deps + offline tests

# fetch a small window, backfill citations, rebuild the site:
./run_local.sh 2025 2022      # start_year min_year
open exports/site/index.html
```

- Historical backfill (resumable, size-capped): `python3 scripts/run_backfill.py --start-year 2025 --min-year 1990 --target-gb 8 --rebuild`
- Citations for the whole DB in the background: `nohup ./scripts/cite_cycle.sh &`
- Rebuild the site only: `python3 scripts/run_curation_pipeline.py --db data/retarats_pubmed.sqlite`
- Internal (curator) build with approve/reject UI: add `--internal` to `build_public_site.py`.

---

## Configuration

| File | Purpose |
|---|---|
| `config/MOLECULES.csv` | the bioactives tracked (id, name, class, synonyms, exclusions, active) |
| `config/SEARCH_RULES.csv` | per-molecule PubMed `[tiab]` queries with synonyms + ambiguity guards |
| `config/FACETS.csv` | controlled facet vocabulary (species, indication, endpoint, …) |
| `config/PUBLICATION_RULES.csv`, `REQUIRED_FIELDS.csv` | inclusion / section policy |
| `config/EXPERIMENTAL_MOLECULES.csv` | candidate molecules (promoted ones marked `live_*`) |
| `retarats_pipeline/curation/ranking.py` | `RANK_WEIGHTS` — tune the ordering here |

Add a molecule: append a row to `MOLECULES.csv` + a `[tiab]` rule to `SEARCH_RULES.csv`; the next backfill picks it up.

---

## Repo layout

```
retarats_v2.py                 PubMed fetch → SQLite
retarats_pipeline/
  config.py                    load molecules + search rules
  pubmed.py, classifier.py …   fetch/parse/classify
  curation/                    facets, reliability, ranking, publication_status,
                               appraisal, extractors, journal   (rule-based, offline)
  enrichment/                  clients (OpenAlex, S2, CT.gov, EuropePMC, PMC), backfill, registry
scripts/
  run_backfill.py, run_impact_backfill.py, run_trials_fetch.py, run_preprints_fetch.py
  run_icite_backfill.py, enrich_pubchem.py            NIH iCite + PubChem enrichment
  build_curated_database.py, build_public_site.py, run_curation_pipeline.py
  validate_config.py, validate_curated.py             fail-fast config + curated checks
  audit_corpus_years.py, audit_rule_counts.py         coverage / rule-count audits
  list_experimental.py, cite_cycle.sh
.github/workflows/             update, backfill, citations, registry
config/                        molecules, search rules, facets, policy
docs/                          curation_and_publication.md, ONLINE_DEPLOYMENT.md, …
tests/                         test_curation, test_extractors, test_site, test_sources
```

---

## Data & credentials

- **PubMed/PMC, OpenAlex, Crossref, EuropePMC, ClinicalTrials.gov, NIH iCite, PubChem** — free, keyless (OpenAlex/Crossref/Unpaywall just want a contact email; iCite and PubChem need no key). An **NCBI API key** raises PubMed limits 3→10 req/s (recommended for backfills).
- Secrets live in a gitignored `.env` locally and in **GitHub Actions secrets** (`NCBI_API_KEY`, `NCBI_EMAIL`) — never committed. Deployment details: `docs/ONLINE_DEPLOYMENT.md`.

## Tests

```bash
python3 tests/test_curation.py && python3 tests/test_extractors.py \
  && python3 tests/test_site.py && python3 tests/test_sources.py
```

## Design principles

Rule-based and **auditable** (every tag, score, and decision is explainable from config), **non-destructive** (enrichment proposes, never overwrites), **offline curation** (no LLM/network needed to rebuild the site), and **broad inclusion** (automated labels never hide a record). No PRISMA compliance is claimed, but the search/curation is PRISMA-S-informed and defensible.

## How to cite

If RetaBase is useful in your work, please credit it. GitHub also shows a **"Cite this repository"** button (generated from `CITATION.cff`).

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21207064.svg)](https://doi.org/10.5281/zenodo.21207064)

> Rosenberg, S. (2026). *RetaBase: a transparent, auto-updating evidence database for retatrutide and related bioactives* (Version 0.1.0-beta) [Software]. Zenodo. https://doi.org/10.5281/zenodo.21207064

This DOI archives the `v0.1.0-beta` pre-release. On the [Zenodo record](https://doi.org/10.5281/zenodo.21207064) you'll also find a **"Cite all versions"** concept DOI that always resolves to the latest release — use that one if you'd prefer a citation that stays current as RetaBase is updated. Cut a new GitHub release (e.g. `v1.0.0` when it leaves beta) and Zenodo mints a fresh version DOI automatically; update `CITATION.cff` to match.

## License

Licensed under the **MIT License** — anyone may use, modify, and build on RetaBase **provided the copyright notice (attribution) is retained**. See [`LICENSE`](LICENSE). The underlying literature metadata comes from public sources (PubMed/PMC, OpenAlex, Crossref, EuropePMC, ClinicalTrials.gov) under their own terms; RetaBase's contribution is the curation, scoring, and presentation layer.

---

*RetaBase is an independent research tool and is not affiliated with, or endorsed by, any drug manufacturer or regulatory body.*
