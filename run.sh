#!/usr/bin/env bash
# Start the dashboard. Run from the repo root: ./run.sh
set -euo pipefail
cd "$(dirname "$0")/backend"

if [[ ! -f .env ]]; then
  echo "backend/.env is missing. Copy backend/.env.example to backend/.env and fill it in." >&2
  exit 1
fi

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
