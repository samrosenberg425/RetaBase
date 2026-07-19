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

## Next up (agreed priorities)

**A. Feed open-access FULL TEXT to the rules pipeline (highest measured value).**
`scripts/audit_missing_fields.py` shows ~90% of records lack a dose, and ~96% of
those gaps are because the ABSTRACT never states it — only full text can recover
them. The fetching already exists (`retarats_pipeline/enrichment/context.py`:
`pmc_ids_bulk` for bulk PMID→PMCID, `bioc_fulltext` for structured OA text, Europe
PMC fallback) but is only used by the experimental LLM script. To productionise:
store OA Methods/Results alongside the abstract in the corpus, feed the combined
text to `refine_extraction`, and add a backfill workflow (bulk ID conversion is
200 PMIDs/request, so this is cheap). Verified impact on a 12-paper sample: rules
found a dose on 2/12 without full text vs 12/12 with it.

**B. Regulatory / development status per bioactive.** Show FDA status, approved
indications, and where each molecule sits in development — including approved drugs
that also have trials for OTHER uses. Design notes:
- Development stage per indication is **derivable from data already held**: the
  trials mirror has `phases`, `conditions`, `overall_status` per NCT per molecule.
  Max phase per condition gives "Phase 3 for obesity, Phase 2 for NASH".
- **Free/open APIs to use** (all no-key or free-key, no scraping):
  - **openFDA** (`api.fda.gov`) — `/drug/drugsfda.json` for approval status +
    application type, `/drug/label.json` for the indications section.
  - **DailyMed RESTful** (`dailymed.nlm.nih.gov/dailymed/services/v2`) — current
    marketed SPL labels; authoritative for "is there a currently marketed US
    product" and for the indications text when openFDA is thin.
  - **RxNorm / RxNav** (`rxnav.nlm.nih.gov`) — normalise molecule name → RxCUI →
    products; the reliable way to join a bioactive to drug products.
  - **ChEMBL** (`ebi.ac.uk/chembl/api/data/molecule`) — `max_phase` (0–4) gives a
    GLOBAL highest development stage, useful where a compound is developed outside
    the US.
  - **ClinicalTrials.gov** — already mirrored; per-condition phases.
  - Optional: Health Canada Drug Product Database API, WHO ATC codes. EMA has no
    good free REST API — use its public EPAR dataset export if EU status is wanted.
  - A curated `config/REGULATORY.csv` remains the override/fallback (with a
    `source` + `source_url` column per row) for supplements, peptides and research
    chemicals that no registry API covers. Curated rows must be auditable.
- **Model** (per molecule):
  - `regulatory_status`: approved / investigational / supplement / research-only /
    withdrawn
  - `fda_approved_indications` (list), `fda_application` (NDA/BLA/ANDA), `us_marketed` (bool)
  - `ex_us_status`: approved elsewhere + which jurisdictions (e.g. "approved in EU,
    Japan"), since several tracked molecules are approved outside the US only
  - `access_pathway` (multi-valued): physician-prescribed / OTC / compounding
    pharmacy (503A/503B) / clinical-trial-only / research-use-only / grey-market.
    Derive what's derivable (an active DailyMed Rx label ⇒ physician-prescribed;
    ChEMBL max_phase<4 with trials ⇒ clinical-trial-only) and curate the rest —
    FDA's 503A/503B bulk-substance categories are the citable source for compounded
    peptides. Grey-market must be a curated, sourced judgement, never inferred.
  - per-indication: `condition`, `max_phase`, `trial_count`, `is_approved_use`
- **UI**: a status tag on each bioactive card (e.g. "FDA approved", "Phase 3",
  "Research only") plus an expandable panel listing approved indications, ex-US
  status, access pathways, and per-use trial stages.
- **Honesty requirements** (this section carries real-world risk):
  - An approved drug being trialled for a NEW use must not read as if that use is
    approved — separate "approved for" from "in trials for" visually and in text.
  - "Research only" / "grey market" must be stated plainly, with a source, and must
    never be presented as a purchasing route or endorsement.
  - Every regulatory claim needs a source + retrieval date, since status changes.

### REQUIRED safety framing (not optional — ship with the panel or not at all)

Placement rule: the warning renders **inline with the data, at point of use**, not
as a link to a page nobody opens. The access-pathway panel must not be renderable
without it. No vendor names, no sourcing routes, no purchasing guidance, ever —
documenting that a pathway exists is not describing how to use it.

**Persistent banner on the regulatory/access panel:**

> **Informational only — not medical advice, and not a recommendation.** This
> section documents the regulatory status and the access routes that are *reported
> to exist*, so readers can understand the landscape. Describing a pathway is not
> endorsing it. RetaBase does not endorse compounded, grey-market, or research-only
> sourcing, or the use of any substance outside an FDA-approved indication under a
> qualified clinician's supervision. Decisions about any of these belong with a
> clinician who knows your history and medications.

**Per-pathway microcopy (renders beside the tag it describes):**

- *FDA approved* — "Approved for the indications listed below. Approval for one use
  is not approval for any other use."
- *Physician-prescribed* — "Available only on prescription, under clinical
  supervision."
- *Clinical-trial only* — "Available only within a registered clinical trial, with
  informed consent and monitoring. Trial participation is not the same as approved
  treatment."
- *Compounding pharmacy (503A/503B)* — "Compounded preparations are NOT FDA-approved
  products. FDA has not evaluated them for safety, effectiveness, or manufacturing
  quality, and some substances have been restricted for compounding."
- *Research use only* — "Supplied for laboratory research only, not manufactured to
  human-use standards, and not approved for human use in any indication."
- *Grey market* — "No regulatory oversight of identity, purity, dose accuracy, or
  sterility; contamination and mislabelling are documented in this supply channel.
  Listed because it is reported to occur, not because it is advisable."

**Longer statement for About/Methods:**

> RetaBase exists to document what the published literature and public registries
> say — including what people are reported to be doing — so that readers, clinicians
> and researchers can see the evidence and the regulatory picture in one place. It
> is not medical advice, creates no clinician–patient relationship, and is not a
> guide to obtaining anything.
>
> We are explicitly against the use of these substances without a qualified
> clinician. Many interact with prescription medicines, several have
> contraindications that depend on individual history, and several are being studied
> precisely because their risks are not yet characterised. An absence of reported
> harms in this database is not evidence of safety — it frequently means nobody has
> looked.
>
> Regulatory status varies by country and changes over time; every status shown
> carries its source and the date it was retrieved, and may already be out of date.
> Legality differs by jurisdiction and is the reader's responsibility. Nothing here
> should be read as encouragement to obtain a substance through compounding,
> research-chemical, or grey-market channels.

**C. Rule gaps worth chasing** (from the same audit): ~401 records where a duration
appears in the abstract but wasn't extracted, ~155 for dose. Small but real.

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
