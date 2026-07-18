# RetaBase — Remaining Work (handoff / resume guide)

_Everything in the two code audits (`docs/SITE_AUDIT.md` + the agent-vs-agent pass) is
done and tested EXCEPT the items below. To resume, say e.g. "work on BACKLOG item 1".
All test suites currently green: test_curation, test_extractors, test_site, test_sources;
`validate_config` OK; all workflows parse._

## Big structural items (deferred on purpose — they need care + a backup first)

1. ~~**Drop `'unsafe-inline'` from the CSP.**~~ **DONE (2026-07-18).** All 31 inline
   `on*=` handlers moved to `addEventListener` (`wireStaticEvents()`); the CSP now uses
   a **sha256 hash** of the executable inline `<script>` (`_apply_csp` in `_render_html`,
   two-pass) so `script-src 'self' 'sha256-…'` with NO `'unsafe-inline'`. Verified: 0
   inline handlers, hash matches the script byte-for-byte, JS parses (node --check),
   all suites green. `style-src` keeps `'unsafe-inline'` for inline style attributes
   (low risk). NOTE: the Playwright E2E workflow is the real browser gate — confirm it
   passes after deploy, since a hash mismatch would only surface in a browser.

2. **Load time.** PARTLY DONE (2026-07-18): the real bottleneck was payload BYTES,
   not parsing — 63 fields/record meant ~1.1 KB of key names per record even when
   blank. `_write_site_json` now omits empty values (missing key behaves exactly like
   `""` for the UI), ~79% smaller on sparse records. STILL OPEN if load is still slow:
   (a) offload `JSON.parse` + filtering to a **Web Worker** — this fixes main-thread
   jank/responsiveness, NOT wall-clock download time; (b) shorten JSON keys via a
   rename map (another big byte win, more invasive); (c) verify the `<link rel=preload
   as=fetch crossorigin>` is actually reused and not double-downloading (DevTools →
   Network; drop `crossorigin` if it isn't).

3. **De-nest interactive controls from `role="button"` cards.** A card is
   `role="button"` yet contains real `<a>` links (PubMed/DOI/authors) + filter tags —
   invalid ARIA nesting. Restructure so the card isn't a button-with-links (e.g. an
   explicit "Details" button, or make the card a region with a labelled open control).

## Smaller polish (nice-to-have, low risk)

- **Tablist keyboard model:** add arrow-key roving + `role="tabpanel"` /
  `aria-labelledby` / `aria-controls` to the tab bar (currently role=tab + aria-selected only).
- **Mobile filter drawer focus:** move focus into the drawer on open + close on Escape.
- **`ranking.py`:** `_CURRENT_YEAR` is bound at import; compute it per build for
  long-lived processes. Also consider re-normalizing rank weights when impact data is
  absent (impact is 0 until iCite backfill runs).
- **`_corpus_stats` fingerprint under `--limit`:** stamp `"partial": true` so a
  debug/limited build's fingerprint isn't mistaken for a full-corpus one.
- **`classify_outcome` recall:** passive directional phrasing ("mortality was higher")
  isn't caught; single-drug dose-ranging in one sentence ("5 mg and 10 mg of X") is
  over-conservatively flagged `ambiguous_multidrug`.
- **`_molecule_terms` (extractors):** short molecule names (<3 chars) yield no anchor,
  so comparator papers for them revert to document-wide extraction scope.
- **LLM layer:** the experimental comparison tool exists (`scripts/experimental_llm_extract.py`,
  opt-in, never in the pipeline). If it proves reliable on a bigger local test, consider
  a real opt-in LLM enrichment layer (with the rules-based output as a guard).

## Done & shipped (for reference)
Trust risks (molecule-scoped extraction, negation-aware rigor, honest counts),
dedup, density tiers, faster load (preload/lazy tabs/non-blocking boot/auto-retry),
mobile-responsive + a11y (drawer, tablist roles, skip link, focus trap, tap targets,
keyboard tags, live counts), reproducibility stamp (fingerprint/DOI), pipeline
robustness (schema-drift canary, pinned deps, validation gates), CSP + referrer,
Playwright E2E harness, extraction fixes (BMI/kg, middle-dot decimals, lab
concentrations, arm-sum vs cohort-flow), and the experimental LLM compare tool.
