# Taking Retarats online (free, live-updating)

## Recommended architecture (free, minimal ops)

Everything runs on GitHub — no server, no paid services:

```
                 ┌─────────────────────────────────────────────┐
                 │  GitHub repo (this project)                  │
                 │                                              │
  schedule ────► │  Actions: update.yml (weekly/daily)          │
  or manual      │    1. restore SQLite from Actions cache      │
                 │    2. incremental PubMed fetch (new papers)  │
                 │    3. rebuild curated layer + site_data.json │
                 │    4. deploy static site → GitHub Pages      │
                 │    5. save SQLite back to cache              │
                 └───────────────┬──────────────────────────────┘
                                 │
                       GitHub Pages (free static host)
                                 │
                    index.html  fetch()  site_data.json
                                 │
                          public dashboard
```

Why this stack:
- **GitHub Pages** hosts the dashboard for free at `https://<you>.github.io/<repo>/`.
- **GitHub Actions** runs the pipeline on a schedule — this is the "live update."
- **Actions cache (~10 GB free per repo)** holds the growing SQLite corpus, so the
  git repo itself stays small and the 10 GB target has a natural, free home.
- The site reads a compact **`site_data.json`** (built rank-sorted, most
  reliable/impactful first) rather than a giant inline blob.

Airtable / Google Sheets are **not required** for this. They can be added later as
an optional manual-curation layer, but Pages + Actions is the most efficient free
path and is fully version-controlled. (If you later want Airtable for editing,
the `curated_evidence.csv` / `site_data.json` map onto it directly.)

## Storage note (the 10 GB backfill)

A 10 GB **plain git repo is a bad idea** (GitHub warns >1 GB, struggles >5 GB).
So the raw corpus does **not** live in git. Options, cheapest first:
1. **Actions cache** (used by the workflows here) — free, ~10 GB, auto-restored
   each run. Best default.
2. **Git LFS** — if you want the SQLite versioned; 1 GB free then paid.
3. **A GitHub Release asset** — attach the `.sqlite` to a release (2 GB/file).

The *published* data (curated `site_data.json`, a few MB) is what goes to Pages.

## The backfill

`scripts/run_backfill.py` walks backwards from a start year, one year at a time,
resumable (checkpoint) and size-capped:

```
# locally, on your computer (needs network):
python3 scripts/run_backfill.py --start-year 2025 --min-year 1975 --target-gb 10 --rebuild
```

or run the **Historical backfill (manual)** Action. GitHub jobs cap at ~6h, so
re-run it until it reports the target size — the checkpoint resumes each time.

> The offline build sandbox has **no network to NCBI**, so the fetch/backfill
> cannot run there — it runs on your machine or the Actions runner.

## What I need from you to finish wiring this up

**Required**
1. **A GitHub repo** for this project (or confirm I should prepare everything so
   you just `git init && git push`). Then enable **Settings → Pages → Source:
   GitHub Actions**. I can't push on your behalf, but the workflows + site are
   ready to run the moment the repo exists.
2. **NCBI E-utilities API key** + the email tied to it — free from
   https://www.ncbi.nlm.nih.gov/account/ (Settings → API Key Management). Raises
   PubMed rate limits from 3→10 req/s, which matters a lot for a 10 GB backfill.
   Add them as repo secrets `NCBI_API_KEY` and `NCBI_EMAIL`.

**Optional but recommended (more papers / better "impact" ranking)**
3. **OpenAlex** (no key needed, just an email) or **Semantic Scholar API key**
   (free) — to backfill **citation counts**, which feed the `impact` axis of the
   ranking (currently 0 until this data exists). OpenAlex is the easiest free one.
4. **CORE** or **Semantic Scholar** — additional full-text/metadata sources
   beyond PubMed/PMC/EuropePMC (all of which need no key).
5. **Unpaywall email** (free, just an email) — open-access status/links.

**Not needed**
- Google Drive — not required for the GitHub path. Only relevant if you decide
  you want a Google-Sheets curation layer.
- Any paid service.

Give me the GitHub repo (or the go-ahead to prepare it turnkey) and the NCBI key,
and the scheduled live updates will run themselves.

## Credentials & one-time setup (safe handling)

**Never commit secrets.** `.env` is gitignored for local runs; for GitHub Actions
use **repo secrets** (Settings → Secrets and variables → Actions). If a token is
ever pasted somewhere shared, rotate it.

Local `.env` (copy from `.env.example`, gitignored):
```
NCBI_EMAIL=sr2007@rwjms.rutgers.edu
NCBI_API_KEY=<your NCBI key>
API_CONTACT_EMAIL=sr2007@rwjms.rutgers.edu   # OpenAlex/Crossref/Unpaywall polite pool
```

GitHub Actions repo secrets to add (names the workflows expect):
- `NCBI_API_KEY` — your NCBI E-utilities key (raises PubMed rate limit 3→10 rps).
- `NCBI_EMAIL` — sr2007@rwjms.rutgers.edu (also used as the OpenAlex/Unpaywall contact).

Notes:
- **OpenAlex needs no key** — just the contact email (polite pool). Citation
  counts feed the ranking's `impact` axis via `scripts/run_impact_backfill.py`.
- **Unpaywall** likewise needs only the email.
- Keep the **GitHub token** out of the repo; use it only for `git push`/`git remote`
  locally, or configure it as the Actions default `GITHUB_TOKEN` (automatic).

### First-run checklist
1. Create the GitHub repo; `git remote add origin <url>`; push this project.
2. Add the two repo secrets above.
3. Settings → Pages → Source: **GitHub Actions**.
4. Run **Historical backfill (manual)** (or `scripts/run_backfill.py` locally) to
   fill the corpus; re-run until it reports the target size.
5. Run `scripts/run_impact_backfill.py` (or let `update.yml` do it) for citations.
6. The weekly `update.yml` then fetches new papers, rebuilds, and redeploys Pages.

## Embedding the dashboard into another website

Once the site is live on GitHub Pages (`https://<you>.github.io/<repo>/`), the
simplest and most robust way to put it on another site (WordPress, Squarespace,
Wix, a lab site, a custom page) is an **iframe**:

```html
<iframe
  src="https://<you>.github.io/<repo>/"
  title="Peptide evidence database"
  style="width:100%; height:900px; border:0; border-radius:8px"
  loading="lazy"
  referrerpolicy="no-referrer">
</iframe>
```

Why iframe:
- **No CORS issues** — the page fetches `site_data.json` from its own Pages origin,
  not the host site, so embedding works from any domain.
- **Style isolation** — the dashboard's CSS can't clash with the host page.
- **Auto-updates** — because it points at the live Pages URL, it always shows the
  latest scheduled build; the host page needs no changes when data updates.
- GitHub Pages does not send `X-Frame-Options: DENY`, so it is embeddable.

Tips:
- Give the iframe a tall fixed height (the dashboard scrolls internally). If you
  want it to auto-size to the parent, that requires a small postMessage handshake
  (ask and it can be added).
- For a cleaner embed (hide the big title bar so it blends into the host page),
  an optional `?embed=1` mode can be added to the generator — say the word.

Alternatives (more work, only if you outgrow the iframe):
- **Reverse-proxy / subpath**: serve the Pages site under `yourdomain.com/evidence/`
  via your host's proxy so it looks first-party.
- **Web component**: package the dashboard as a mountable custom element for
  inline (non-iframe) embedding. Overkill for now.
