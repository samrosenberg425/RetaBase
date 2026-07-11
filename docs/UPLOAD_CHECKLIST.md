# RetaBase — upload & full-population checklist

Everything needed to take the current code to a fully-populated, self-updating
live site, and to confirm all the data actually got retrieved and used. Run the
numbered steps in order; after the first full pass the schedules keep it current
with no further action.

## 0. One-time prerequisites (already done, verify)

- GitHub repo public; **Settings → Pages → Source: GitHub Actions**.
- **Settings → Secrets and variables → Actions**: `NCBI_API_KEY`, `NCBI_EMAIL`
  set (raises PubMed limits 3→10 rps; also used as the OpenAlex/iCite/PubChem
  contact email).
- Local `.env` (gitignored) with the same values for local scripts.

## 1. Push the code

```bash
cd ~/Desktop/CAHBIR/Reta/retarats-auto
python3 scripts/validate_config.py            # must print "Config OK."
python3 tests/test_curation.py && python3 tests/test_extractors.py \
  && python3 tests/test_site.py && python3 tests/test_sources.py   # all pass
git add -A && git commit -m "iCite + PubChem enrichment, deeper categorization, UI, docs" && git push
```

## 2. (Optional but recommended) clean corpus rebuild

Only if you want the tightened search rules + iCite applied from scratch:
**Actions → Caches → delete every `retarats-corpus-*` entry.** This also clears
the backfill checkpoint, so the next backfill starts fresh at the newest year.

## 3. Build the corpus — Historical backfill (auto)

**Actions → "Historical backfill (auto)" → Run workflow** with
`start_year 2026`, `min_year 2000`, `max_years 6`, `force` off. This single job now:
fetches the PubMed years **→ tops up citations (OpenAlex) → enriches with NIH iCite**
(RCR, APT, human/animal/molecular, triangle coords, clinical flags). Re-run it a
handful of times to walk 2026 → 2000 (or just let the 6-hourly schedule do it).

## 4. PubChem enrichment (local, needs network)

```bash
source .venv/bin/activate                     # REQUIRED: needs the venv (requests). Prompt shows (.venv).
python3 scripts/enrich_pubchem.py             # writes config/pubchem_cids.csv + pubchem_synonyms_suggested.csv
git add config/pubchem_cids.csv config/pubchem_synonyms_suggested.csv \
  && git commit -m "PubChem CIDs for Bioactives links" && git push
```
> If `ModuleNotFoundError: No module named 'requests'`: the venv isn't active (or missing).
> Run `source .venv/bin/activate`, or rebuild it with `bash setup.sh`, then re-run.
- `pubchem_cids.csv` powers the **"View on PubChem" link** on the Bioactives page.
- `pubchem_synonyms_suggested.csv` is a **review list** — do not bulk-merge into
  `SEARCH_RULES.csv` (the rules were deliberately tightened). Add only vetted
  synonyms for the rare, low-count molecules you want to broaden.

## 5. Trials + preprints — Registry sources

**Actions → "Registry sources (trials + preprints)" → Run workflow.** Now pulls
up to **100 studies/molecule** (completed + ongoing) and 100 preprints/molecule;
runs automatically twice weekly (Mon + Thu).

## 6. Publish — Update database and publish site

**Actions → "Update database and publish site" → Run workflow.** Rebuilds the
curated layer (now using iCite for impact/directness/classification and the
section-aware + landmark feed cap), copies the trials/preprints feeds, and deploys
to Pages. This is the step that makes new data appear on the live site.

## 7. Verify the data was retrieved AND used

- **Per-year coverage:** Actions → "Audit corpus coverage" → check 2000–present
  are populated (no `LOW`/zero years).
- **Rule counts:** `python3 scripts/audit_rule_counts.py --min-count 300` — sanity
  on search breadth.
- **Citations + iCite filled:** the backfill log prints citation/iCite fill counts;
  re-run backfill until `missing` trends to ~0.
- **On the live site:** Impact-percentile and APT sorts work; "Clinical articles
  only" toggle filters; capped molecules show "top N of M"; Bioactives cards show
  "View on PubChem"; Trials + Preprints tabs are populated.

## 8. Then it's automatic (set-and-forget)

| Workflow | Cadence | Does |
|---|---|---|
| Historical backfill (auto) | every 6 h | fetch years + citations + **iCite** (one job, one lock) |
| Backfill citations | daily 03:30 | standalone catch-up backstop |
| Update database and publish site | daily 06:00 | fetch new + rebuild + deploy |
| Registry sources | Mon + Thu 07:00 | trials + preprints refresh |

All share the `retarats-corpus` lock so they never corrupt the cache; the
recurring citation/iCite work lives inside the backfill job so nothing starves.

## Known follow-ups (not blocking)

- Rare-molecule broadening: apply *vetted* PubChem synonyms from step 4 to the
  low-count molecules' rules (keep them on-topic).
- Pre-2000 history: lower the backfill `min_year` once 2000→present is solid.
