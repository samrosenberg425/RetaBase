# RetaBase — Honest Code & Site Audit

_Read-only audit of the deployed site and its supporting code. Every claim is grounded in a specific file/function/line. Grades are deliberately critical — the point is to find what can be improved, not to flatter._

_Generated: 2026-07-15_

---

## Dimension grades

| # | Dimension | Grade |
|---|-----------|-------|
| 1 | Scientific rigor & methodology transparency | **A−** |
| 2 | Data quality & extraction accuracy | **C+** |
| 3 | Ranking & classification soundness | **B** |
| 4 | Frontend accessibility (a11y) | **D+** |
| 5 | Frontend security (XSS) | **A−** |
| 6 | Frontend performance | **C+** |
| 7 | UX & information design | **B−** |
| 8 | Data pipeline robustness | **B+** |
| 9 | CI/CD & workflow reliability | **A−** |
| 10 | Testing coverage | **C+** |
| 11 | Reproducibility & provenance | **B−** |
| 12 | Maintainability & code structure | **C+** |
| | **Overall** | **B−** |

---

## 1. Scientific rigor & methodology transparency — A−
The strongest part of the project. The About/Methods panel explicitly states the score is *not* a formal RoB assessment (Cochrane RoB 2 / ROBINS-I) and *not* a GRADE rating; the modal hard-labels "Formal risk of bias: not assessed (automated rigor signals only)"; the evidence map is framed as "a map, NOT an efficacy verdict"; and the safety caution list ("Absence of reported harms is not evidence of safety") is always shown. Two-axis quality-vs-directness separation is methodologically sound.

**Improve:** reconcile the misleading headline count (see Risk #3); surface per-record provenance for extracted dose/route/N; expose the already-computed `appraisal_confidence` on the card.

## 2. Data quality & extraction accuracy — C+
Regexes are thoughtfully defended (BMI `kg/m²` exclusion + Lancet middle-dot handling, age-distractor window, `n=` unit exclusion). Core flaw: **dose/route/duration/sample-size are parsed from the whole title+abstract with no link to the record's molecule**, yet the modal presents them as authoritative facts. A retatrutide card can show a comparator drug's dose.

**Improve:** bind extraction to molecule-name/synonym co-occurrence (mark "not localized" otherwise); add a negation guard to rigor scoring (currently credits "double-blind" even in "unlike double-blind trials…"); restrict outcome sentiment to the efficacy/safety sentence and handle negation; fix arm-count summing ("n=50 enrolled … n=48 analyzed" → 98).

## 3. Ranking & classification soundness — B
Weights are explicit, sum to 1.0, every axis emitted in `rank_components` for audit. Venue-vs-impact separation is well reasoned; APT nudges bounded to ±8 and gated so preclinical can never outrank human evidence; iCite triangle preferred over keyword model only when confident.

**Improve:** unknown-journal venue defaults to ~50 (flat ~2 pts on nearly every record) — document or zero-center; impact is capped at 5% but the "0-until-backfill" rationale is now obsolete (iCite is backfilled) — consider raising; restrict reliability keyword matching to a methods span so background mentions don't leak rigor credit.

## 4. Frontend accessibility — D+
Weakest dimension. Cards and tag chips are clickable `<div>`/`<span>` with **no keyboard path** (no `tabindex`/`role`/keydown) — a keyboard or screen-reader user cannot open any paper detail. Grep for `tabindex|role="dialog"|aria-modal|aria-live` returns zero hits.

**Improve:** add `role="dialog"`+`aria-modal`+focus trap+focus restore to the modal; give cards `tabindex="0"`+`role="button"`+Enter/Space; add `role="tablist"`/`aria-selected` to tabs; add `aria-live="polite"` to the "Showing X of Y" region; add visible focus rings and verify small-text contrast.

## 5. Frontend security (XSS) — A−
Genuinely strong and tested. Feed values render via `textContent`/`el()`; the JSON block neutralizes `</`→`<\/`; links are scheme-vetted (`safeLink` rejects non-`https?`) and built with `encodeURIComponent`; downloads use Blob URLs. `test_site.py` asserts breakout neutralization and no `javascript:` scheme.

**Improve:** add a CSP `<meta>` and `Referrer-Policy`; defense-in-depth `<`→`<` in the JSON block; clamp length/charset on values pushed into the search box.

## 6. Frontend performance — C+
300-card render cap with `DocumentFragment` bounds DOM cost; feed is section-capped upstream. But: **no debounce** on the search `oninput` — every keystroke runs a full `applyFilters` + `crossFilterCounts` (O(n·facets)); no list virtualization (Load-more re-renders from scratch); fetch mode loads one monolithic `site_data.json`.

**Improve:** add a ~150ms debounce; consider a prebuilt inverted index for facet counts; virtualize the list.

## 7. UX & information design — B−
Rich: include/exclude cross-filtered facet counts, single-molecule evidence map + translational triangle, feed-cap disclosure, clearly separated Trials/Preprints with "NOT results / NOT peer-reviewed" banners, real empty states.

**Improve:** fixed 340px sidebar has no mobile collapse (crowds a phone) — add a media query/drawer; clickable cards lack an affordance that they open a detail — add an explicit "Details" control (also fixes a11y); fix the inaccurate "papers" label (Risk #3).

## 8. Data pipeline robustness — B+
Solid: polite rate-limited caching HTTP client with restartable runs; iCite batch fetch with exponential backoff; graceful enrichment degradation; idempotent upserts; anomaly gate fails the build if `total_papers`/`molecules_with_data`/`published_records` collapse below 50% of baseline.

**Improve:** no evidence-record dedup for duplicate (pmid, molecule) rows from multiple matching rules; schema-drift rows are skipped silently (`continue`) with no counter — add a dropped-row stat; iCite `provisional` flag is fetched but never surfaced.

## 9. CI/CD & workflow reliability — A−
Mature: all corpus writers share one `retarats-corpus` serialization lock; Pages deploy has its own `pages` group with `cancel-in-progress`; deploy is gated (validation has no `|| true`, so a bad corpus keeps the last good site up), then promotes stats to baseline only on success; durable weekly snapshot (compressed corpus + sha256 → Release, restore-only); `cache-gc` manages the cache chain.

**Improve:** add a job asserting restored `total_papers` ≥ baseline before proceeding (belt-and-suspenders if a future workflow forgets the lock); pin `requirements.txt`; add a fetch-freshness alert (incremental fetch runs with `|| true`).

## 10. Testing coverage — C+
~466 `check()` assertions incl. the security-critical XSS breakout tests and full filter logic. But the **JS is never executed** — tests assert substrings exist in the generated HTML, so a refactor that keeps the string but breaks behavior passes. No end-to-end SQLite→curated→site test; no tests for the negation/misattribution extraction failure modes.

**Improve:** add a headless-DOM (jsdom/Playwright) test that actually filters and opens a modal; add an E2E pipeline test; add tests for feed-cap and the anomaly gate.

## 11. Reproducibility & provenance — B−
Good scaffolding: MIT license, `CITATION.cff` with a real Zenodo DOI, open rule-based scoring, config-driven vocab, weekly checksummed snapshot. But the corpus is **not reproducible from the repo** (`data/`/`exports/`/`*.json` gitignored; daily incremental fetch is date-dependent/non-deterministic).

**Improve:** pin each site build to a snapshot tag/DOI shown in the footer; add a build hash to `site_data.json`; fill the placeholder ORCID in `CITATION.cff`.

## 12. Maintainability & code structure — C+
The single ~2,800-line `build_public_site.py` embeds the whole app as one `str.format` `_TEMPLATE`, forcing every literal `{`/`}` in ~1,700 lines of JS/CSS to be doubled — fragile (fails at build time, not lint time), unlintable/untestable in isolation, and the root cause of the "JS never executed" gap.

**Improve:** externalize JS/CSS into real files inlined via file-read (enables ESLint + browser tests); centralize duplicated allowlists/`_truthy`/`_MISSING` sets; derive the three overlapping field lists from one source.

---

## Top 8 highest-leverage improvements (impact ÷ effort)

1. **Debounce the search `oninput`** — one line, removes the biggest perf cliff. _(high/trivial)_
2. **Make cards keyboard-operable + modal a dialog** — `tabindex`/`role`/Enter-Space on cards; `role="dialog"`+focus trap on the modal. Fixes the worst a11y failure. _(high/low)_
3. **Relabel or reconcile the "papers" count** — subtitle/counts use evidence-record counts while the corpus strip uses distinct papers; the two visible numbers disagree. _(high/low)_
4. **Bind dose/route/N extraction to the record's molecule** — gate on molecule-name co-occurrence; eliminates cross-drug misattribution shown as fact. _(high/medium)_
5. **Add a negation guard to rigor scoring** — stop crediting negated "double-blind"/"randomized"/"controls"; this feeds 28% of the rank. _(high/medium)_
6. **Dedup evidence records at the feed layer** so the same paper+molecule doesn't render twice. _(medium/low)_
7. **Externalize JS/CSS out of the `str.format` template** so it can be linted and browser-tested; retire brace-doubling. _(high/high)_
8. **Pin each deployed site to a corpus snapshot/DOI** (footer + build hash) for reproducibility/citability. _(medium/low)_

## Three most serious correctness / trust risks

1. **Structured extractions are not attributed to the molecule.** `parse_dose/route/duration` scan the whole abstract with no drug linkage, yet the modal shows them as facts about the record's molecule. On any paper with a comparator/co-treatment these can be wrong — the most likely way a user is actively misled. _(Partially mitigated 2026-07-15: BMI/body-mass "kg" false positives and Lancet middle-dot decimals fixed; molecule-binding still open.)_
2. **Keyword rigor scoring is negation- and context-blind, and drives 28% of the rank.** Design points are credited on bare substrings over a blob that includes title/background — papers get credit for methods they explicitly lack or merely cite.
3. **"Papers" counts overstate the literature.** The headline and "Showing X of Y papers" count evidence records (paper×molecule×rule), not distinct papers, while the corpus strip shows the true distinct count — the two visible numbers disagree, with no cross-record dedup.
