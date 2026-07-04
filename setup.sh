#!/usr/bin/env bash
# One-time environment setup for the retarats pipeline (macOS/Linux).
# Creates an isolated virtualenv, installs dependencies, and runs an offline
# sanity check. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"
echo "==> Project: $(pwd)"

# 1) Pick a Python 3 interpreter.
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3 (e.g. 'brew install python') and re-run." >&2
  exit 1
fi
echo "==> Using $($PY --version) at $(command -v "$PY")"

# 2) Create + activate a local virtualenv (.venv is gitignored).
if [ ! -d ".venv" ]; then
  echo "==> Creating virtualenv .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 3) Install dependencies.
echo "==> Installing dependencies (this may take a minute)"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

# 4) Check the .env exists.
if [ ! -f ".env" ]; then
  echo "WARNING: no .env found. Copy .env.example to .env and add your NCBI key/email." >&2
else
  echo "==> Found .env"
fi

# 5) Offline sanity checks (no network needed): tests + a coverage report.
echo "==> Running offline tests"
python tests/test_curation.py | tail -1
python tests/test_extractors.py | tail -1
python tests/test_site.py | tail -1

echo "==> Citation-coverage snapshot (offline, no network):"
python scripts/run_impact_backfill.py --db data/retarats_pubmed.sqlite --offline | sed 's/^/    /'

cat <<'DONE'

==> Setup complete.

To do a small trial fetch (recommended first — gauges size/speed), run:
    source .venv/bin/activate
    ./run_local.sh 2025 2022        # fetch 2022-2025, then rebuild + citations

Then open exports/site/index.html in your browser.

For the full historical fill later:
    ./run_local.sh 2025 1975        # walks back to 1975 (long; resumable)
DONE
