# RetaBase — Full Site Audit (2026-08)

Audited: https://samrosenberg425.github.io/RetaBase/ (deployed) + the source that
builds it. Corpus at audit: **101,143 papers, 123 bioactives.** Grades are relative
to what a credible, self-hosted scientific evidence database should be — not to a
weekend project. This supersedes the earlier `docs/SITE_AUDIT.md`; most of that
document's findings have since been fixed.

**Overall: B+.** A genuinely trustworthy, transparent, security-conscious database
whose engineering and honesty are well above hobby grade. The ceiling is set by
three things: data *coverage* (limited by what abstracts state, now being addressed
with full text), a monolithic frontend template that resists change, and a generic
visual identity.

| # | Domain | Grade |
|---|--------|-------|
| 1 | Scientific rigor & transparency | A− |
| 2 | Data quality & extraction | B |
| 3 | Ranking & classification | B+ |
| 4 | Data pipeline & enrichment | B+ |
| 5 | CI/CD & reliability | A− |
| 6 | UX & information design | B |
| 7 | Accessibility | B |
| 8 | Performance & load | B− |
| 9 | Security | A− |
| 10 | Testing | B |
| 11 | Reproducibility & provenance | B+ |
| 12 | Maintainability & extensibility | C+ |
| 13 | Visual design & identity | C |
| 14 | Content safety & regulatory framing | B |

---

## 1. Scientific rigor & transparency — A−
**Strengths:** Every number is rule-based and documented (`docs/RULES.md`). The site
never overclaims: rigor is labelled "not a formal risk-of-bias assessment", evidence
maps are "not an efficacy verdict", and the persistent caution ("absence of reported
harms is not evidence of safety") is always shown. Two-axis separation of rigor
(within-class quality) vs directness (translational level) is methodologically sound.
Outcome direction is deliberately rules-only to avoid inheriting author spin.
**Weaknesses:** The headline "101,143 papers" counts evidence records (paper×molecule),
not distinct papers, and can read as bigger than the distinct-paper corpus. No formal
GRADE/RoB by design — correct for an automated system, but a reader wanting certainty
ratings won't find them.

## 2. Data quality & extraction — B
**Strengths:** The extraction *logic* is now strong and battle-tested — molecule-scoped
attribution, proximity ownership in multi-drug papers, placebo-arm exclusion, frequency
retention ("500 mg twice daily"), unit coverage (micrograms/hyphen/comma-thousands),
lab-value rejection, and a separate synthesis parser (k studies + pooled N). A
12-paper hand check matched an LLM 12/12.
**Weaknesses:** *Coverage*, not logic, is the limit. ~90% of records lack a dose — and
an audit (`audit_missing_fields.py`) shows ~96% of those gaps are because the abstract
never states it. Full-text integration is now built but not yet backfilled, and its
ceiling is the ~30–40% of papers that are open access. So most preclinical records
will remain thin on structured detail no matter what. This is honest and unavoidable,
but it means the structured fields are sparse and should be presented as "when stated"
rather than as a complete matrix.

## 3. Ranking & classification — B+
**Strengths:** Explicit blend summing to 1.00 (directness .33, rigor .28, relevance
.20, recency .10, impact .05, venue .04), every component emitted per record for
audit, iCite-primary with keyword fallback, venue neutral for unknown journals.
**Weaknesses:** Impact capped at 5% is arguably low now that iCite is backfilled;
relevance defaults for unmapped roles sit at a flat mid value with no signal that a
role went unrecognised.

## 4. Data pipeline & enrichment — B+
**Strengths:** Resumable, idempotent backfill; enrichment from iCite, PubChem,
OpenAlex, CT.gov, EuropePMC, and now OA full text; schema-drift canary; audit-and-add
rather than restart. Genuinely a lot of moving data handled defensibly.
**Weaknesses:** Historical backfill is still catching up, so coverage is uneven by
molecule (Rapamycin alone has ~1,500 missing doses). Enrichment steps run with
`|| true`, so a silent multi-day source outage wouldn't alert.

## 5. CI/CD & reliability — A−
**Strengths:** Nine workflows, all sharing a repo-wide `retarats-corpus` serialization
lock; deploy is hard-gated on validation (a bad corpus keeps the last good site up);
corpus-collapse anomaly gates vs a promoted baseline; weekly durable snapshot with
sha256; cache-gc to stay under the 10 GB budget; Playwright E2E as an independent job.
**Weaknesses:** Correctness depends on every workflow remembering the shared lock; no
assertion that a restored cache isn't stale beyond the anomaly gate; deps only
major-version pinned.

## 6. UX & information design — B
**Strengths:** Rich and honest — include/exclude facets with cross-filtered counts,
ranking presets, evidence map, translational triangle with clickable dots, density
badges, provenance stamp, and prominent "NOT results / NOT peer-reviewed" banners on
trials/preprints. Real empty and error states.
**Weaknesses:** "Clinical" is overloaded across five controls (tab, filter, preset,
sort, pill). The interface is information-dense and assumes a technical user; there is
no gentle on-ramp or guided "what am I looking at" for a first-time visitor. The
evidence-record vs distinct-paper count distinction is a cognitive tax.

## 7. Accessibility — B
**Strengths:** Large improvement from the earlier D+: skip link, `role=tablist`/`tab`
with live `aria-selected`, keyboard-operable cards and tags, a real modal focus trap
with `aria-hidden` background, `aria-live` count regions, visible focus rings, 44px
mobile tap targets, 16px inputs.
**Weaknesses:** Tablist has no arrow-key roving; interactive links/tags are nested
inside `role=button` cards (invalid ARIA nesting); no `role=tabpanel`/`aria-labelledby`
on the panels; contrast on the smallest muted text is borderline.

## 8. Performance & load — B−
**Strengths:** Payload shrunk ~79% by omitting empty fields; lazy-rendered tabs;
non-blocking boot; auto-retry loader; `splitVals` memoised; cross-filter counts
reduced to O(facets); 300-card render cap; debounced search; preload hint.
**Weaknesses:** ~7 s initial load remains. The whole `site_data.json` is parsed
synchronously on the main thread on first paint, and there is no Web Worker or list
virtualization. On the full 100k-record corpus this is the main user-visible drag.

## 9. Security — A−
**Strengths:** Excellent for a static site. Hash-based CSP with **no** `'unsafe-inline'`
for scripts, all feed values via `textContent`/`el()`, `safeLink` scheme-vetting,
`encodeURIComponent` on URL parts, `_safe_json_block` neutralising `</script>`,
`frame-ancestors 'none'`, referrer `no-referrer`. Verified by the test suite.
**Weaknesses:** `style-src` still allows `'unsafe-inline'` (low risk — inline style
attributes); a single benign `innerHTML=""` remains (a clear, not an injection).

## 10. Testing — B
**Strengths:** ~650 assertions across curation/extractors/site/sources plus a real
Playwright E2E job that executes the JS in Chromium (filter, modal, focus trap, tabs).
Security invariants and the anomaly gate are explicitly tested.
**Weaknesses:** JS behaviour beyond the E2E fixture is still substring-tested; no
end-to-end SQLite→curated→site test; extraction is tested on crafted strings, not a
labelled gold-standard set of real abstracts.

## 11. Reproducibility & provenance — B+
**Strengths:** Deterministic corpus fingerprint + build SHA + Zenodo DOI on every build,
weekly checksummed corpus snapshots, MIT license, `CITATION.cff`, fully open scoring,
`docs/RULES.md`.
**Weaknesses:** The corpus is not reproducible from the repo alone — `data/` is
gitignored and the incremental fetch is date-dependent. A given deployed site can be
tied to a fingerprint but not rebuilt byte-for-byte by a third party.

## 12. Maintainability & extensibility — C+
**Strengths:** Adding a **molecule** is genuinely easy — it's config-driven
(`MOLECULES.csv` + `SEARCH_RULES.csv`), which is the common case and well designed.
Curation logic is modular (`reliability`, `ranking`, `facets`, `extractors`).
**Weaknesses:** The frontend is one ~3,100-line `str.format` `_TEMPLATE` where every
literal JS/CSS brace must be doubled — fragile and hard to test in isolation. Adding a
new **record field** means editing several hand-synced allowlists (`SITE_JSON_FIELDS`,
`RECORD_FIELDS`, merge fields) plus the card and modal renderers — exactly the friction
that makes new features expensive. This is the biggest structural debt.

## 13. Visual design & identity — C
**Strengths:** Clean, legible dark dashboard; consistent spacing; sensible colour
coding for tiers.
**Weaknesses:** Generic. It reads as a default dark developer dashboard rather than a
named scientific product — no logo, no distinctive typography, no identity, default
GitHub-Pages feel. Functional but forgettable, and easy to mistake for a template.

## 14. Content safety & regulatory framing — B
**Strengths:** Strong, honest safety language already present (About/Methods caution
list, trials/preprints "not results / not peer-reviewed" banners). The specced
regulatory feature carries rigorous, mandatory CYA framing.
**Weaknesses:** The regulatory/access-pathway feature isn't built yet, so the site
currently shows evidence without the "what's approved / how it's accessed" context a
reader needs to interpret it responsibly. That gap is the highest-value content add.

---

## The three highest-leverage moves (impact ÷ effort)
1. **Finish the full-text backfill** — directly lifts the sparsest, most-wanted data
   (dose/duration) on the ~35% of papers that are open access. Already built; just run.
2. **Refactor the frontend out of the monolithic template + make fields registry-driven**
   — this is the unlock for *every* future feature (regulatory panel, new peptides, new
   card fields) and for a visual redesign. Highest structural payoff.
3. **Web Worker for parse + filter** — removes the one remaining user-visible drag
   (the ~7 s load / main-thread parse) on the full corpus.
