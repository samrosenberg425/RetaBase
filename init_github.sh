#!/usr/bin/env bash
# Prepare this project as a clean git repo ready to push to GitHub.
# It does NOT push (you do that with your own credentials). Data, the venv, the
# .env, and generated exports are all gitignored, so only code/config/docs/
# workflows are committed — the repo stays small; the corpus lives in the
# Actions cache, and the site is built + deployed by GitHub Actions.
#
#   ./init_github.sh
# then follow the printed steps to add your remote and push.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Starting a clean git repo (removing any previous .git)"
rm -rf .git
git init -q
git add -A
git -c user.email="you@example.com" -c user.name="retarats" commit -q -m "Retarats peptide evidence pipeline + dashboard"

echo
echo "==> Committed. Files that will NOT be pushed (gitignored):"
echo "    data/  .venv/  exports/  .env  __pycache__/  *.xlsx"
echo
cat <<'NEXT'
==> Next steps (run these yourself, with your own GitHub account):

  1. Create an empty repo on github.com (e.g. "retarats"). Do NOT add a README.

  2. Point this project at it and push (uses your login/token when prompted):
       git branch -M main
       git remote add origin https://github.com/<YOUR_USER>/<YOUR_REPO>.git
       git push -u origin main

  3. On GitHub: Settings -> Secrets and variables -> Actions -> New repository secret
       NCBI_API_KEY = <your key>
       NCBI_EMAIL   = samuel.rosenberg@rutgers.edu

  4. Settings -> Pages -> Source: "GitHub Actions".

  5. Actions tab -> run "Historical backfill (manual)" (and/or it runs on schedule),
     then "Update database and publish site". Your dashboard goes live at:
       https://<YOUR_USER>.github.io/<YOUR_REPO>/

  6. Embed it anywhere (see docs/ONLINE_DEPLOYMENT.md "Embedding"):
       <iframe src="https://<YOUR_USER>.github.io/<YOUR_REPO>/"
               style="width:100%;height:900px;border:0" loading="lazy"></iframe>
NEXT
