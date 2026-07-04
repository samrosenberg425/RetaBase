#!/usr/bin/env bash
# Fetch papers, backfill citations, and rebuild the curated layer + site.
# Usage:   ./run_local.sh [START_YEAR] [MIN_YEAR] [TARGET_GB]
# Example: ./run_local.sh 2025 2022        (fetch 2022-2025, small trial)
#          ./run_local.sh 2025 1975 10      (full historical fill, up to 10 GB)
set -euo pipefail
cd "$(dirname "$0")"

START="${1:-2025}"
MIN="${2:-2022}"
TARGET_GB="${3:-10}"
DB="data/retarats_pubmed.sqlite"

# Use the venv if present.
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "==> 1/3 Fetching PubMed papers ${START} -> ${MIN} (newest first), rebuilding as it goes"
python scripts/run_backfill.py --start-year "$START" --min-year "$MIN" --target-gb "$TARGET_GB" --rebuild

echo "==> 2/3 Backfilling citation counts (OpenAlex -> Semantic Scholar), newest first"
python scripts/run_impact_backfill.py --db "$DB" --newest-first --max-records 5000

echo "==> 3/3 Rebuilding curated layer + site so citations feed the ranking"
python scripts/run_curation_pipeline.py --db "$DB"

echo
echo "==> Done. Database size:"
du -h "$DB" 2>/dev/null || true
echo "==> Open the dashboard:  open exports/site/index.html"
