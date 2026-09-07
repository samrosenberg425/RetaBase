# RetaBase — Master Work Plan (restartable)

<!-- ============================================================= -->
<!-- SESSION STATE — READ THIS FIRST, UPDATE IT LAST -->
## ▶ RESUME HERE
- **Last done:** Phase 6.3 (keyboard part) — WAI tablist model: roving tabindex
  (active tab=0, others=-1, synced in showTab) + Left/Right/Home/End arrow navigation
  that focuses and activates the adjacent VISIBLE tab. node-checked. This session also:
  count fix, field registry 1.1/1.2/1.2c, ranking fix, PHASE 2 complete, Phase 3.1
  (Web Worker feed parse w/ fallback), Phase 6.1 (impact weight 0.05→0.10). All green:
  site 364, curation 177, extractors 90, sources 53.
- **Also done:** Phase 6.2, Phase 5.4, and Phase 4 (theme, first pass) — LIGHT
  scientific theme + clinical blue-teal accent (Sam's choice): retargeted `:root`
  tokens, retuned tier/tag colours for white, masthead accent strip + accent product
  name + solid active tabs, card depth/hover, light regulatory banner. Plus a local
  PREVIEW tool: `scripts/preview_site.py` builds `exports/preview.html` (self-contained
  sample, opens by double-click) for eyeballing changes without the corpus/server.
  All green: site 372, curation 183, extractors 90, sources 53.
- **Also done:** theme iteration 1 — labelled the reliability bar "Rigor" (+ tooltip;
  matches the directness badge), and interactive polish (accent focus rings on inputs,
  hover cues on chips/buttons). Sam likes the light blue-teal ("feels official").
- **Also done:** theme iteration 2 — Rigor now carries a "?" opening a shared styled
  popover ("How to read these scores") that explains Rigor (within-class, judged on its
  own curve, with a worked example), Directness and Rank in one place; hover tooltip
  shortened to the quick example. About "How to read this" + About/Methods carry the
  fuller within-class wording. Sam approved the direction. site 377.
- **Also done:** Phase 6.4 — corpus freshness alert + cache-collapse guard
  (`scripts/check_corpus_freshness.py`, wired into `update.yml`; 18 tests) + `pip check`
  drift step. All green: site 377, curation 183, extractors 90, sources 53, freshness 18.
- **Also done:** load-time fix (Sam: "takes way too long to load") — SPLIT FEED. Diagnosed
  the real cost: the whole corpus (~101k records, ~150-230 MB JSON) was parsed up front; the
  web worker can't fix that (it must structured-clone the parsed object back to the main
  thread). Fix: `field_registry` gains a `detail` flag; the 4 modal-only fields
  (reliability_components, rank_components, appraisal_strengths, appraisal_limitations) now go
  to a lazy-loaded `site_detail.json` (fetched after first paint), and `facet_all` (a redundant
  search blob) is fully retired (search rebuilt client-side from facet_* via `hayFor`). Initial
  list feed ~36% smaller. `build_curated_database` emits both files (detail keyed by
  pmid|molecule_id|title[:40] = client rid()); client `dval()` reads detail with record
  fallback so inline/preview + a slow/failed detail load degrade gracefully; inline mode embeds
  detail (filtered to inlined records). `update.yml` copies site_detail.json. Also changed the
  masthead tagline (dropped "retatrutide", now "bioactives gaining new attention as emerging
  medications or novel use cases"). All green: curation 186, site 381, extractors 98, sources
  53, freshness 18. NOTE: this is a data-contract change — the deploy now produces
  site_detail.json; verify it appears in exports/site on the next update.yml run.
- **Also done:** Clinical practice guidelines (Sam: gold-standard docs were scoring low on
  rigor/directness). New rules-based handling — detected by PubMed pubtype "Guideline"/
  "Practice Guideline" (+ conservative title fallback guarded against "adherence to guidelines"
  papers). New evidence class `clinical_guideline`: directness=92 (high), rigor NOT graded
  (tier not_applicable, score 0) since we lack AGREE-II-type inputs and shouldn't imply we
  graded a gold standard. Ranking uses an 85 quality PROXY so they surface near the top;
  publication_status features them into the Human evidence section. UI: the Rigor bar is
  replaced by an "n/a · authoritative guideline" badge, and the "?" popover gains a Practice-
  guidelines row. Also moved the "Min times cited" filter to the bottom of the sidebar +
  de-emphasized it (citation counts may be stale — a later fix). All green: curation 194,
  site 385, extractors 98, sources 53, freshness 18; e2e fixture parses.
- **Also done:** CSP fix + PROGRESSIVE FEED (Sam: load still too slow; worker "didn't work").
  Root cause of the dead worker: CSP had no worker-src, so the blob worker was blocked and
  silently fell back to main-thread parsing every load. Fixed: `worker-src 'self' blob:`
  (+child-src), script-src still hash-locked. Then the real load-time fix — the page no longer
  loads the whole corpus up front. `build_curated_database` now ships `site_data.json` with only
  the top FEED_FIRST_CHUNK=1500 rank-sorted records + a shard manifest (`shards`, `total_records`),
  and writes `site_records_NNN.json` shards (FEED_SHARD_SIZE=10000) for the rest. Client boots on
  the top chunk (instant), then `startProgressiveLoad` streams shards via `fetchJson` (blob worker,
  main-thread fallback), appending to RECORDS — which stays a rank-ordered prefix, so the default
  view + Load more work immediately; a "loading full corpus N%" indicator shows progress;
  `applyFilters(preserveWindow)` refreshes counts without yanking the user's scroll. Modal detail
  (site_detail.json) is now fetched ON DEMAND on first card open (via `ensureDetail`), not eagerly.
  Inline/preview mode reconstructs the full list from shards in `load_site_data` and skips the
  network path (MODE guard). update.yml copies the shards. All green: curation 199, site 392,
  extractors 98, sources 53, freshness 18; e2e fixture parses. Scales: bigger corpus = more small
  shards, first paint unchanged.
- **REDESIGN TEST RUN (uncommitted — awaiting Sam's variant choice):** Big UI overhaul, all
  in the working tree (NOT committed), so `git restore . && git clean -fd exports/` reverts to
  the current committed style. Delivered as 5 stylistic variants: `exports/preview_{clinical,
  indigo,emerald,slate,warm}.html` (`python3 scripts/preview_site.py --all`). Changes:
  (1) Nike-style sticky top bar `.topbar` with logo slot (`.logo-word` placeholder wordmark,
  swap `#logo-slot` for Sam's real logo image later) + nav tabs; (2) Home landing page
  (`#home-view`, default tab) with what-is / how-to-use nav buttons / how-to-read-the-rings /
  safety; Methods split into its own tab (`#methods-view`, renamed from about-view;
  renderAbout→renderMethods, +renderHome); (3) score RINGS replace the bar/badge — overarching
  Rank ring + Rigor/Directness/Impact sub-rings in a `.score-box`, SVG donuts (pathLength=100,
  dasharray=score), guideline rigor shows n/a; modal `.scope-score` = rings + `.scope-why` panel
  that cross-highlights on hover (data-metric); (4) Trials filter sidebar `#trials-sidebar`
  (status/phase/study-type/molecule/year/sort); (5) Bioactives tab → "Bioactive overview" + intro
  blurb; (6) 5 `body.v-*` variant skins (token overrides) via build_site(variant=...) /
  preview_site --all/--variant. Reviewed by a subagent: fixed the ring-number centering (dial
  wrapper) + completed tablist ARIA (role=tabpanel/aria-controls). e2e specs updated (Home is
  now default → click Evidence first). All green: site 399, curation 199, extractors 98,
  sources 53, freshness 18; e2e fixture parses. NOTE: I cannot see rendered pixels (no browser in
  sandbox) — Sam picks the variant visually. Once chosen: keep that body class (or set it as the
  build default), then commit.
- **REDESIGN ROUND 2 (uncommitted test run):** Sam locked the CLINICAL theme (default; the 5
  variants still exist via `preview_site.py --all`/`--variant`). This round (all in working tree,
  `git restore . && git clean -fd exports/ config/site_copy/` to revert): (1) score rings reworked
  — big overarching **Rank** ring left, 3 smaller labeled rings (Rigor/Directness/Impact) stacked
  right under a "Contributes to rank" caption; (2) **instant** hover tooltips (`attachTip`/`.hovertip`)
  replace slow native title=; (3) **RetaRats site link** top-right in the bar (https://www.retarats.com/);
  (4) **6 sample trials** added to `preview_site.py` so the trials sidebar is testable; (5)
  **question-tolerant search** (`queryTerms` strips filler/question words via `SEARCH_STOP`) + a hint
  under the box; (6) **regulatory dual links** — name-based DailyMed + Drugs@FDA deep links in
  `regulatoryPanel`; molecule "featured" relabeled **"spotlight"** with a tooltip; (7) **EDITABLE COPY**:
  Home + Methods prose moved to `config/site_copy/{home,methods}.md`, parsed at build (`_parse_copy_md`)
  into blocks and rendered by JS `renderBlocks`/`renderSpans` (no innerHTML). Methods restructured:
  no "About" start, small section titles, a class→directness table, rank/rigor equation blocks, source
  links. Sam edits those .md files to change text (figures later). Reviewed by a subagent: fixed a
  should-fix (renderSpans now falls back to text if an editor writes a non-http link, so a bad link
  can't blank the page) + tooltip scroll-hide + home formula CSS + featured/spotlight reconciliation.
  All green: site 406, curation 199, extractors 98, sources 53, freshness 18; e2e fixture parses.
  Preview: `exports/preview.html` (clinical). Editable copy is inlined in BOTH modes (small trusted config).
- **REDESIGN ROUND 3 (uncommitted test run):** (1) fixed the "missing tags"/"all weight-loss"
  confusion — it was sparse PREVIEW sample data, not a regression; enriched `preview_site.py`
  `_record()` with the full 11-facet set + varied indications. (2) New **Guide tab** (plain-language
  glossary): `config/glossary.csv` (editable; 11 categories, 83 options with a one-sentence
  definition each) → `_load_glossary()` → `renderGuide()` (category <select> menu, mobile-friendly,
  lazy). (3) **Tag hover definitions** on card aspect-tags, gated by a `tag_hovers` build flag so
  `preview_site.py` now emits TWO files for comparison: `exports/preview_tags_hover.html` and
  `preview_tags_plain.html` (default preview.html = hovers on). Glossary + copy inlined both modes
  (+~8 KB gzipped total — negligible; Guide renders on tab open). All green: site 410, curation 199,
  extractors 98, sources 53, freshness 18; e2e fixture parses. Sam to pick hovers on/off and can edit
  `config/glossary.csv` for wording.
- **REDESIGN ROUND 4 — READY TO DEPLOY:** (1) card definition hovers are now a RUNTIME toggle
  (`TAG_TIPS`, persisted in localStorage, checkbox in the Guide tab), initialized from the
  `tag_hovers` build flag. (2) EVERY tag + metric now has a hover: aspect tags fall back to
  "Name (category)" when no glossary def; metric pills (evidence class, RCR, clinical, journal
  tier, Cited-by) got `pillTip` definition tips. (3) `_load_glossary()` now MERGES config/FACETS.csv
  so any facet value auto-gets at least its display label — no live tag can lack an entry. Hover
  text and Guide come from the SAME source (config/glossary.csv) so editing it updates both.
  (4) Fixed preview sample data to use only real facet values (the "HbA1c" gap was an invented
  sample value). All green: site 414, curation 199, extractors 98, sources 53, freshness 18; e2e
  fixture parses; fetch-mode build smoke-tested (CSP + glossary/copy/guide/rings all present).
  **This whole multi-round redesign is uncommitted on a clean main** — one commit ships it. New
  files: config/glossary.csv, config/site_copy/{home,methods}.md. Deploy = commit + push + run
  update.yml (fetch build loads glossary/copy from config/, inlines them; tag_hovers defaults ON).
- **EVIDENCE HIERARCHY (uncommitted, on top of the shipped redesign):** new dedicated axis =
  the evidence pyramid, PRIMARY feed ordering; other scores compare only WITHIN a level (Sam's
  principle). Editable ladder `config/evidence_hierarchy.csv` (14 levels: systematic review >
  meta-analysis > guideline > RCT > non-RCT > cohort > case-control > cross-sectional > case series
  > case report > narrative review > preclinical > in vitro > other). `reliability.py`:
  `evidence_level()` detects the level from PubMed pubtypes + class + title cues; adds
  evidence_level_{key,rank,label,short} to Reliability + RELIABILITY_FIELDS; registered in
  field_registry (site_json). `build_curated_database` sorts feed (level asc, rank desc) in both
  places. Client: default "rank" sort is hierarchy-first (`levelRank`), an "L#" badge on card +
  modal (`evidenceLevelBadge`, banded hi/mid/lo), a pyramid in the Guide, and the score-legend +
  methods.md explain it. Preview sample spans levels 1-13 (64 records). All green: curation 209,
  site 418, extractors 98, sources 53, freshness 18; e2e fixture parses. Ships with the next
  update.yml run (rebuild re-scores the real corpus with levels). NOTE: retune placements by
  editing evidence_hierarchy.csv (e.g., move guidelines).
- **ROUND: logo + sort/filter + hierarchy source + methods docs (uncommitted):**
  (1) RetaRats LOGO top-right: `assets/retarats-logo.png` (optimized 193x80, white->transparent)
  -> `_logo_data_uri()` inlined as a data URI in a clickable "logo + Main site" link to
  https://www.retarats.com/ (new tab, accessible). (2) SORT: added "Rank (mix all levels)"
  (pure rank, ignores hierarchy) alongside default "Rank (evidence level first)"; sortRecords
  handles rank_mixed. (3) FILTER: "Evidence level" added to FILTER_FACETS (evidence_level_short)
  so people can include/exclude levels. (4) HIERARCHY SOURCE: cited OCEBM 2011 (Oxford CEBM) for
  the human levels + the classic evidence pyramid for the preclinical extension, GRADE noted as
  the appraisal layer we don't do — links in the Guide pyramid + methods.md. (5) METHODS docs:
  methods.md now enumerates every rigor variable + points per class, the rank weight rationale
  (venue-vs-impact decision, recency floor), how each axis is derived, and a "Where the data comes
  from" section (PubMed/E-utilities, iCite, OpenAlex, ClinicalTrials, Europe PMC, DailyMed/
  Drugs@FDA, PubChem, curated journal table). All green: site 425, curation 209, extractors 98,
  sources 53, freshness 18; e2e fixture parses. Logo is inlined (no update.yml asset copy needed).
  New files: assets/retarats-logo.png. Ships with next update.yml run.
- **Next action (was):** 6.5 (duration rule gap #2) — harden the duration extractor regex and
  add synthetic unit tests here; the actual ~400-record re-extraction needs the corpus DB
  in the Actions cache (runs in CI, not sandbox). One-time user step for full dep hashing:
  `pip install pip-tools && pip-compile --generate-hashes -o requirements.lock requirements.txt`
  then switch `update.yml` install to `pip install --require-hashes -r requirements.lock`.
  Optional later: more theme iteration with Sam (other card labels, density,
  triangle/evidence-map light pass), 6.3 card de-nesting.
- **Deferred (judgement):** 6.3 de-nesting interactive links/tags out of `role=button`
  cards — real ARIA nit but a risky card restructure for modest payoff; revisit with
  Phase 4 (theme) since cards get touched then. Also Phase 3.2 (filter in worker) and
  1.3/1.4 (data-drive modal/cards) — deferred earlier, low marginal value.
- **Uncommitted right now:** `scripts/build_public_site.py`, `scripts/build_curated_database.py`,
  `retarats_pipeline/curation/field_registry.py`, `tests/test_curation.py`,
  `docs/*`. Commit locally (protocol below).
- **Working state:** green — test_curation 160, test_extractors 90, test_site 350,
  test_sources 53; validate_config OK; 9 workflows parse.

### Handoff protocol (how to continue in a fresh session without losing anything)
1. New session, first message: "Read docs/WORKPLAN.md and continue from ▶ RESUME HERE."
2. The assistant works one checklist item at a time, runs the four test suites, and
   updates this RESUME block + ticks the box BEFORE moving on.
3. Because the sandbox can't write git, YOU commit after each item locally:
   `git add -A && git commit -m "..."` (no push — stays private).
4. Nothing depends on chat history: the plan + the code + the test suites are the
   whole state. If in doubt, run the four suites to confirm green before continuing.
<!-- ============================================================= -->


A single ordered, resumable checklist for the post-audit work. Each item is
self-contained: do it, run the four test suites + `validate_config`, commit, tick the
box. If a session ends mid-item, the "resume" note says exactly where to pick up.
Priority order reflects Sam's audit comments (regulatory = ASAP; extensibility unlocks
the rest).

Status legend: [ ] not started · [~] in progress (see resume note) · [x] done.

Test gate for every code item: `test_curation`, `test_extractors`, `test_site`,
`test_sources` all green + `validate_config` OK; workflows must still parse.

---

## PHASE 0 — running now (no code)
- [~] Full-text backfill: `fulltext.yml`, `max_records 8000`, repeat until "0 need
  enrichment", then `update.yml`. (~1/3 of papers are OA = the ceiling.)
- [~] Historical backfill: floor year **1966** (PubMed). Enrichment thins pre-1995.

---

## PHASE 1 — Field registry (audit #12, the extensibility unlock)
**Why first:** adding a record field today means editing ~5 hand-synced allowlists +
the card + the modal. Every later feature (regulatory, new card data) pays that tax.
A registry makes "add a field" a one-line change. This is a BUILD-CODE change, NOT a
corpus rebuild — the existing corpus keeps working unchanged. Nothing here requires
reprocessing data.

- [x] **1.1** `retarats_pipeline/curation/field_registry.py` — descriptors with
  `site_json`/`record`/`modal`/`card`/`group`/`label` flags + `record_fields()` /
  `site_json_fields()`. Fidelity-lock test in test_curation (`run_field_registry_tests`).
- [x] **1.2** `SITE_JSON_FIELDS` (build_curated) and `RECORD_FIELDS` (build_public_site)
  now DERIVE from the registry. Set-equal to the historical lists (locked by test).
- [x] **1.2c** flipped the two drift fields to `site_json=True`; feed now 65 fields,
  the "Field citation rate" + "iCite citations" modal rows populate on next build.
- [ ] **1.3** Drive the modal key/value grid from the registry (`modal`/`label`/`group`).
- [ ] **1.4** Drive the card preview pills from the registry (`card`).
- [ ] **1.5** Acceptance test: "adding one registry entry surfaces it in JSON + modal
  with no other edits."
Resume note: safe to stop after any sub-item; each keeps the build green.

## PHASE 2 — Regulatory / access-pathway feature (audit #14, HIGH PRIORITY)
Full spec already in `BACKLOG.md` item B (statuses, ex-US, access pathways, the
mandatory safety framing copy). Uses the Phase-1 registry so the new fields are cheap.
Free APIs: openFDA (drugsfda + label), DailyMed RESTful, RxNorm/RxNav (name→product),
ChEMBL `max_phase` (global stage), CT.gov mirror (per-condition phase).

- [x] **2.1** `config/regulatory.csv` schema + `_load_regulatory()` + 8 reg fields on
  the molecule index + CSV columns + site `MOLECULE_FIELDS`; 2 seed rows; tested.
- [x] **2.2** Trial-derived dev stage per indication from the local mirror
  (`_load_trial_stages`; max phase overall + per condition + counts). Tested.
- [x] **2.3** `scripts/run_regulatory_enrich.py` — ChEMBL max_phase + openFDA
  (drugsfda + label) + DailyMed. Safe-by-default (proposals → `_suggested.csv`;
  `--emit` merges NEW rows only, never clobbers curated without `--force`; grey-market/
  compounding never auto-inferred). `--mock` for offline. Tested.
- [x] **2.4** UI: status tags on each bioactive card + expandable `regulatoryPanel`
  (approved indications, ex-US status, access pathways w/ microcopy, per-use trial stage).
- [x] **2.5** REQUIRED safety framing — banner renders FIRST (panel can't exist
  without it), per-pathway microcopy, approved-vs-in-trials separated, no vendors/
  sourcing; longer statement added to About. Locked by 8 tests.
Resume note: 2.1 alone (curated CSV) delivers value; API enrichment (2.3) can follow.

## PHASE 3 — Web Worker for parse + filter (audit #8)
Removes the ~7 s main-thread parse on the full corpus. Free to implement.
- [x] **3.1** Feed fetch+parse moved into a Web Worker (`loadFeedViaWorker`) with a
  watchdog + full fallback to `loadMainFeed`. node-checked worker body + main logic.
- [~] **3.2** DEFERRED — filtering in the worker is large/risky (RECORDS + SELECT state
  duplication) and marginal after the O(F)/memoize/debounce/300-cap work already done.
Resume note: 3.1 delivered the main win (off-thread parse).

## PHASE 4 — Visual identity / theme (audit #13)
Goal: modern scientific-database look (think PubMed/Europe PMC/ChEMBL with a cleaner,
more contemporary touch), NOT generic dark dashboard. Easier after Phase 1 (CSS is
still in `_TEMPLATE`; consider extracting CSS to its own string first).
- [~] **4.1** SKIPPED (deliberate) — restyled in place via `:root` tokens instead of
  extracting CSS first; lower risk. Extraction can still be done later if desired.
- [x] **4.2** Light scientific theme + clinical blue-teal accent applied via design
  tokens + masthead + card polish. Tested (test_site light-theme checks). A local
  preview tool (`scripts/preview_site.py`) was added for iteration.
Resume note: token/CSS-only; revert is trivial. Iterate with the preview.

## PHASE 5 — Self-sustaining automation (Sam's follow-up asks)
- [ ] **5.1** New-literature scan — already `update.yml` daily; confirm cadence + that
  new molecules/lanes are picked up automatically.
- [ ] **5.2** Cited-by refresh — already `citations.yml`; confirm it keeps counts fresh.
- [ ] **5.3** Trial→paper linkage — exists; needs the one-time `registry.yml`
  `refresh_trials`, then stays current.
- [x] **5.4** Manual paper add — `config/manual_pmids.csv` (molecule_id, pmid, note)
  → `retarats_pipeline/manual_add.py` builds synthetic `<pmid>[uid]` rules appended in
  `retarats_v2.py` (honours --molecule, skips unknown molecule_ids/bad pmids/dups). Tested.
- [ ] **5.5** Preprint→published dedup (LOW priority) — when a preprint's DOI/title
  matches a later published paper, mark it superseded.

## PHASE 6 — Smaller fixes (from the audit)
- [x] **6.1** Ranking (#3): impact weight 0.05→0.10, percentile-driven (time-
  normalized, so recent papers aren't buried); directness 0.33→0.30, relevance
  0.20→0.18; sum 1.0; About formula + description updated. Tested.
- [x] **6.2** UX (#6): "clinical" de-overloaded (Human data tab, Human-evidence first
  preset, iCite clinical articles filter); "New here?" on-ramp on the evidence view;
  About clarifies the Human-data-tab vs iCite-filter distinction. Tested.
- [~] **6.3** A11y (#7): DONE — tablist roving tabindex + arrow/Home/End navigation.
  Remaining (deferred): de-nest links/tags from `role=button` cards (do with Phase 4).
- [x] **6.4** Pipeline (#4) + CI (#5): DONE (deps partial). New `scripts/check_corpus_freshness.py`
  with two modes wired into `update.yml`: `guard` (hard gate, pre-fetch — fails the build if
  the restored corpus cache collapsed below 90% of last recorded size, so a lost cache never
  republishes a hollow site) and `freshness` (soft — records corpus size in
  `data/freshness_state.json`; emits `::warning::` + job-summary if no growth for ≥14 days,
  surfacing a silently-dead fetch). Both compare raw `papers` row counts (apples-to-apples).
  18 unit tests in `tests/test_freshness.py`. Added a `pip check` + `pip freeze` step for
  dependency-drift observability. NOT done: full hash-locked requirements — needs a networked
  `pip-compile --generate-hashes` run (see note below); the sandbox has no network.
- [x] **6.5** Duration rule gap (#2): DONE (rule side). `parse_duration` now also catches
  SPELLED-OUT durations ("twelve weeks", "six-month", "fifty-two weeks") via a new
  `_DURATION_WORD_RE` with a compositional tens+ones prefix (so "fifty-two"/"twenty-four"
  resolve without enumerating every compound), normalised to digits so they dedupe with any
  digit form. Also hardened `_AGE_DISTRACTOR` to catch hyphenated "six-year-old"/"1-year-old".
  5 new tests in test_extractors (95 total). The ~400-record re-extraction runs in CI on the
  next `update.yml` rebuild (corpus DB lives in the Actions cache, not the sandbox).

---

## Answers to the audit-comment questions (decisions, so future me doesn't re-litigate)

1. **"paper×molecule"** — one paper can be evidence for several bioactives; each
   (paper, molecule) pair is one "evidence record." A paper on "metformin vs
   semaglutide" is 2 records. So 101k records > distinct papers. Fix = label both
   numbers clearly (6.2). **Fine-tuning an LLM to score/extract:** not recommended as
   the engine — it reintroduces non-determinism, un-auditability, and hallucination
   risk into the one thing (the data) that must be trustworthy, and the 12/12 test
   showed the LLM's wins were reproducible by rules once given full text. Best use of
   an LLM: OFFLINE, to generate candidate labels that we verify and turn into new
   rules — intelligence in the toolchain, not in the output.
2. **Coverage** — full text recovers the ~1/3 that are OA (Phase 0). The rest is
   inherent: you cannot extract what no source states. Not a "fix," a ceiling. The one
   actionable rule gap is duration (6.5).
3. **Citations** — yes, raise their weight modestly, but via iCite percentile (already
   field- AND time-normalized) so newer papers aren't penalised for being recent; avoid
   leaning on raw counts (inflatable, age-biased). See 6.1.
4/5. **Pipeline/CI** — freshness alerting + cache assertions + dep pinning (6.4).
6. **UX** — 6.2.
7. **A11y** — 6.3.
8. **Performance** — yes, free: Web Worker (Phase 3).
9. **Security** — low priority; only `style-src` unsafe-inline + one benign
   `innerHTML=""` remain. Leave unless a redesign touches it.
10. **Testing terms (not cybersecurity):** "JS behavior / Playwright / E2E / Chromium"
    = automated tests that open the built page in a real (headless) browser and click
    through it to prove the buttons/filters actually work, vs. the cheaper tests that
    only check the code text. It's quality assurance, not security. Nothing to do.
11. **Reproducibility** — optional; the fingerprint/snapshot already lets a build be
    identified and restored. Full byte-reproducibility is a nice-to-have, not needed.
12. **Extensibility** — Phase 1. NOT a corpus rebuild; it's a build-code refactor. The
    flexibility Sam wants (evolving fields/equations) is exactly what the registry
    enables, so leaving it flexible is correct.
13. **Theme** — Phase 4; target modern scientific-DB aesthetic.
14. **Regulatory** — Phase 2, high priority.
