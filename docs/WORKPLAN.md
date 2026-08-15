# RetaBase — Master Work Plan (restartable)

<!-- ============================================================= -->
<!-- SESSION STATE — READ THIS FIRST, UPDATE IT LAST -->
## ▶ RESUME HERE
- **Last done:** Phase 2.2 — trial-derived dev stage per indication
  (`_load_trial_stages` from the CT.gov mirror; `max_trial_phase`, `trial_count`,
  `ongoing_trial_count`, `trial_stages_by_use` on the molecule index + CSV + site
  allowlist). Tested on synthetic data (real trials DB not in sandbox; populates on
  the real build). This session also: count fix (#1), field registry 1.1/1.2/1.2c,
  ranking current-year fix, regulatory 2.1.
- **Next action:** Phase 2.4 — the Bioactives-card regulatory/access UI: a status
  tag (FDA approved / Phase N / Research only) + expandable panel (approved
  indications, ex-US status, access pathways, per-use trial stage). It reads the
  molecule fields already populated in 2.1/2.2. Then 2.3 (API enrichment script to
  fill config/regulatory.csv from openFDA/DailyMed/RxNorm/ChEMBL).
- **2.5 IS A SHIP-BLOCKER for 2.4:** the panel must render the REQUIRED safety framing
  (banner + per-pathway microcopy) inline — copy is verbatim in BACKLOG.md under
  "REQUIRED safety framing". Do NOT ship the panel without it. Remember _TEMPLATE
  brace-doubling ({{ }}) and that a successful `_render_html` proves brace balance.
- **Deferred (judgement):** Phase 1.3/1.4 (data-drive modal/cards) — core registry
  value already delivered; high-effort/low-marginal-value. Revisit only if needed.
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
- [ ] **2.3** Enrichment script hitting openFDA + DailyMed + RxNorm + ChEMBL, writing
  regulatory fields onto the molecule index (audit-and-add, resumable, cached).
- [ ] **2.4** UI: status tag(s) on each bioactive card + expandable panel (approved
  indications, ex-US status, access pathways, per-use trial stage).
- [ ] **2.5** REQUIRED safety framing (see BACKLOG "REQUIRED safety framing" block) —
  inline with the data, per-pathway microcopy, no vendors/sourcing. Panel must not
  render without it. This is a ship-blocker, not optional.
Resume note: 2.1 alone (curated CSV) delivers value; API enrichment (2.3) can follow.

## PHASE 3 — Web Worker for parse + filter (audit #8)
Removes the ~7 s main-thread parse on the full corpus. Free to implement.
- [ ] **3.1** Move `JSON.parse(site_data.json)` into a Worker (fetch mode); post
  arrays back; keep a no-Worker fallback for inline/file:// mode.
- [ ] **3.2** Move `crossFilterCounts` + `base.filter` into the Worker; render stays on
  the main thread. Keep the 300-card cap.
Resume note: 3.1 is shippable alone; 3.2 is the bigger win.

## PHASE 4 — Visual identity / theme (audit #13)
Goal: modern scientific-database look (think PubMed/Europe PMC/ChEMBL with a cleaner,
more contemporary touch), NOT generic dark dashboard. Easier after Phase 1 (CSS is
still in `_TEMPLATE`; consider extracting CSS to its own string first).
- [ ] **4.1** Extract CSS out of `_TEMPLATE` into a dedicated block/file (safe, no logic).
- [ ] **4.2** New design tokens: palette, typography, spacing, header/masthead, subtle
  card styling. Light+dark. Placeholder for a logo (Sam will supply later).
Resume note: token/CSS-only; revert is trivial if a look doesn't land.

## PHASE 5 — Self-sustaining automation (Sam's follow-up asks)
- [ ] **5.1** New-literature scan — already `update.yml` daily; confirm cadence + that
  new molecules/lanes are picked up automatically.
- [ ] **5.2** Cited-by refresh — already `citations.yml`; confirm it keeps counts fresh.
- [ ] **5.3** Trial→paper linkage — exists; needs the one-time `registry.yml`
  `refresh_trials`, then stays current.
- [ ] **5.4** Manual paper add — a `config/MANUAL_PMIDS.csv` (pmid, molecule_id, note)
  that the fetch honors, so Sam can force-include a specific paper.
- [ ] **5.5** Preprint→published dedup (LOW priority) — when a preprint's DOI/title
  matches a later published paper, mark it superseded.

## PHASE 6 — Smaller fixes (from the audit)
- [ ] **6.1** Ranking (#3): raise impact weight (0.05 → ~0.10) but drive it from the
  TIME-NORMALIZED iCite percentile, not raw counts, so recent papers aren't buried.
  Re-normalize the blend to sum to 1.0; keep component transparency.
- [ ] **6.2** UX (#6): de-overload "clinical" labels; add a one-line "what am I looking
  at" intro; clarify evidence-records vs distinct-papers count.
- [ ] **6.3** A11y (#7): tablist arrow-key roving, `role=tabpanel`/`aria-labelledby`,
  de-nest links/tags from `role=button` cards.
- [ ] **6.4** Pipeline (#4) + CI (#5): freshness alert if a fetch adds nothing for N
  days; assert restored-cache paper count ≥ baseline; pin deps by hash.
- [ ] **6.5** Duration rule gap (#2): ~400 records have a duration in the abstract that
  wasn't extracted — chase that pattern.

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
