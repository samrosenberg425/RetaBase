#!/bin/bash
set -u

cd "$(dirname "$0")" || exit 1

if [ -x "/opt/anaconda3/envs/research/bin/python" ]; then
  PYTHON="/opt/anaconda3/envs/research/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  PYTHON="$(command -v python)"
fi

clear
echo "RetaRats PubMed Pipeline"
echo
echo "Choose a run:"
echo "  1) Smoke test: retatrutide, last 30 days, 5 records per rule"
echo "  2) Daily update: all molecules, last 7 days"
echo "  3) Full 2026 refresh: all molecules, refresh existing rows"
echo "  4) Postprocess only: use existing local database"
echo "  5) Enrichment smoke test: offline, CSV-only, 25 records"
echo "  6) Enriched postprocess only: update enriched fields from existing DB"
echo "  7) PMC live smoke: basic rows, CSV-only, 2 PMC attempts"
echo "  8) Open exports folder"
echo
read -r -p "Enter 1, 2, 3, 4, 5, 6, 7, or 8: " choice
echo

case "$choice" in
  1)
    "$PYTHON" scripts/run_full_local_pipeline.py --mode daily --daily-days 30 --molecule retatrutide --max-records-per-rule 5
    ;;
  2)
    "$PYTHON" scripts/run_full_local_pipeline.py --mode daily --daily-days 7
    ;;
  3)
    "$PYTHON" scripts/run_full_local_pipeline.py --mode backfill --start-year 2026 --refresh-seen
    ;;
  4)
    "$PYTHON" scripts/run_full_local_pipeline.py --skip-fetch
    ;;
  5)
    "$PYTHON" scripts/run_enrichment_pipeline.py --db data/retarats_pubmed.sqlite --offline --csv-only --max-records 25
    ;;
  6)
    "$PYTHON" scripts/run_postprocessing_pipeline_enriched.py --db data/retarats_pubmed.sqlite --config-mode inputs --role-rules config/ROLE_RULES.csv --review-slices config/REVIEW_SLICES.csv --offline-enrichment
    ;;
  7)
    "$PYTHON" scripts/run_enrichment_pipeline.py --db data/retarats_pubmed.sqlite --mode basic --csv-only --max-records 100 --enable-pmc --pmc-max-records 2
    ;;
  8)
    mkdir -p exports
    open exports
    ;;
  *)
    echo "No valid option selected."
    ;;
esac

echo
echo "Done. If exports were created, start with:"
echo "  exports/evidence_review.csv"
echo "  exports/processing_routes_summary.csv"
echo "  exports/enriched/"
echo "  exports/review_queue/"
echo "  exports/prisma/review_slice_manifest.csv"
echo "  exports/review_slices/"
echo
read -r -p "Press Return to close this window."
