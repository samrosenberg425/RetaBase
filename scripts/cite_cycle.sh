#!/usr/bin/env bash
# Lazily fill citation counts for the whole database, then rebuild the site.
# Safe to run in the background and safe to re-run: it only touches papers that
# still lack a citation count, saves after every chunk (resumable), and is gentle
# on the APIs. After the first full pass, later runs are quick (only new papers).
#
#   Run once in the background:   nohup ./scripts/cite_cycle.sh > cite_cycle.log 2>&1 &
#   Watch progress:               tail -f cite_cycle.log
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

DB="data/retarats_pubmed.sqlite"

echo "[$(date)] citation fill starting"
python scripts/run_impact_backfill.py --db "$DB" --all --newest-first --max-records 3000 --sleep 2

echo "[$(date)] rebuilding curated layer + site so ranking picks up citations"
python scripts/run_curation_pipeline.py --db "$DB"

echo "[$(date)] done"
