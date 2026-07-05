# RetaBase

**A transparent, auto-updating evidence database for retatrutide and related bioactives** (peptides, incretin agonists, metabolic & longevity compounds, and more).

🔎 **Live dashboard:** https://samrosenberg425.github.io/RetaBase/

RetaBase continuously pulls the biomedical literature for a curated set of bioactive molecules, scores each paper for **study quality** and **translational directness** with rule-based, auditable methods, and publishes a browsable, filterable dashboard — ranked so the most reliable and impactful evidence comes first. It also surfaces **ClinicalTrials.gov** registry studies and **preprints** in separate, clearly-labeled sections.

> ⚠️ **Not medical advice.** RetaBase is a research/literature-aggregation tool. Nothing here is a recommendation to use, dose, or avoid any substance. Many of these compounds are experimental or not approved for the uses discussed. Consult a qualified clinician.

---

## Who it's for

- **Physicians** whose patients ask about these compounds and who want the evidence landscape at a glance.
- **Researchers** who want a filterable, exportable map of the literature (human vs preclinical, by indication, endpoint, mechanism, etc.).
- **Curious readers** who want to understand what the science actually says, with reliability made explicit.

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
2. **Enrich** — citation counts from **OpenAlex** (fallback **Semantic Scholar**); registry trials from **ClinicalTrials.gov**; preprints from **EuropePMC** (bioRxiv/medRxiv).
3. **Curate** (`retarats_pipeline/curation/`, pure rule-based, offline, non-destructive):
   - **facets** — normalized tags (species incl. non-human primate, indication, endpoint, mechanism, route, drug class, population, sex, formulation, evidence direction).
   - **reliability** — study quality scored *within evidence class* (GRADE/SYRCLE/ARRIVE-informed), so a rigorous in-vitro study can score high for its type.
   - **directness** — how directly the evidence applies to humans (human RCT high → in-vitro low).
   - **ranking** — a transparent blend that surfaces the best evidence first (see below).
   - **publication status** — broad inclusion; only genuinely off-topic records are excluded.
   - **appraisal** — rule-based strengths/limitations + an LLM-ready summary slot.
4. **Publish** — `build_curated_database.py` writes a compact `site_data.json`; `build_public_site.py` renders a single self-contained `index.html`; GitHub Pages serves it.

### The ranking (fully auditable)

`rank_score` (0–100) is a weighted blend, each axis shown in the record's breakdown:

| axis | weight | meaning |
|---|---|---|
| directness | 33% | translational evidence level (human RCT > … > in-vitro) |
| quality | 28% | within-class study quality (reliability) |
| relevance | 20% | how central the molecule is to the paper |
| recency | 10% | newer evidence ranked higher |
| impact | 5% | citation count (log-scaled; 0 until backfilled) |
| venue | 4% | journal reputation (curated; neutral for unknown) |

Full method write-up is in the dashboard's **About / Methods** tab and `docs/curation_and_publication.md`.

---

## The dashboard

- **Evidence** — all records, rank-sorted, with include/exclude multi-select filters (with select-all per domain), year (before/after/range), journal-name, and min-citations filters, plus cross-filter counts.
- **Clinical evidence** — human data only (no animal/in-vitro/methods).
- **Trials registry** — ongoing & completed ClinicalTrials.gov studies (registrations, not results).
- **Preprints** — bioRxiv/medRxiv (not peer-reviewed).
- **Bioactives** — per-molecule index.
- **About / Methods** — how every metric is defined and computed.

Each paper shows authors (linked to Google Scholar), journal + reputation tier, a reliability meter, a directness badge, citation count, a plain-language summary, and strengths/limitations. A per-paper detail view shows every field with the score breakdowns. Everything renders safely (all values via `textContent`; no injection).

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
| `update.yml` | **daily** | incremental PubMed fetch + citation top-up → rebuild → **deploy to Pages** |
| `backfill.yml` | **every 6 h** (auto) | historical fill, **3 years per run** (bounded so it finishes & caches before the 6 h job limit); resumes via checkpoint until it reaches `min_year`/`target_gb` |
| `citations.yml` | every 6 h | OpenAlex→Semantic-Scholar citation backfill |
| `registry.yml` | weekly | ClinicalTrials.gov trials + EuropePMC preprints |

You can also trigger any of them manually (Actions → *workflow* → **Run workflow**).

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
  build_curated_database.py, build_public_site.py, run_curation_pipeline.py
  validate_curated.py, list_experimental.py, cite_cycle.sh
.github/workflows/             update, backfill, citations, registry
config/                        molecules, search rules, facets, policy
docs/                          curation_and_publication.md, ONLINE_DEPLOYMENT.md, …
tests/                         test_curation, test_extractors, test_site, test_sources
```

---

## Data & credentials

- **PubMed/PMC, OpenAlex, Crossref, EuropePMC, ClinicalTrials.gov** — free, keyless (OpenAlex/Crossref/Unpaywall just want a contact email). An **NCBI API key** raises PubMed limits 3→10 req/s (recommended for backfills).
- Secrets live in a gitignored `.env` locally and in **GitHub Actions secrets** (`NCBI_API_KEY`, `NCBI_EMAIL`) — never committed. Deployment details: `docs/ONLINE_DEPLOYMENT.md`.

## Tests

```bash
python3 tests/test_curation.py && python3 tests/test_extractors.py \
  && python3 tests/test_site.py && python3 tests/test_sources.py
```

## Design principles

Rule-based and **auditable** (every tag, score, and decision is explainable from config), **non-destructive** (enrichment proposes, never overwrites), **offline curation** (no LLM/network needed to rebuild the site), and **broad inclusion** (reliability is a label, not a hide-gate). No PRISMA compliance is claimed, but the search/curation is PRISMA-S-informed and defensible.

## How to cite

If RetaBase is useful in your work, please credit it. GitHub shows a **"Cite this repository"** button (generated from `CITATION.cff`) with a ready-made citation.

Provisional citation:

> Rosenberg, S. (2026). *RetaBase: a transparent, auto-updating evidence database for retatrutide and related bioactives* [Software]. https://github.com/samrosenberg425/RetaBase

**For a permanent, academically-citable DOI (recommended):** connect the repo to [Zenodo](https://zenodo.org) (log in with GitHub → toggle on the RetaBase repo), then publish a release on GitHub. Zenodo mints a DOI for that release; add it to `CITATION.cff` (`doi:` field) and to the citation above.

## License

Licensed under the **MIT License** — anyone may use, modify, and build on RetaBase **provided the copyright notice (attribution) is retained**. See [`LICENSE`](LICENSE). The underlying literature metadata comes from public sources (PubMed/PMC, OpenAlex, Crossref, EuropePMC, ClinicalTrials.gov) under their own terms; RetaBase's contribution is the curation, scoring, and presentation layer.

---

*RetaBase is an independent research tool and is not affiliated with, or endorsed by, any drug manufacturer or regulatory body.*
