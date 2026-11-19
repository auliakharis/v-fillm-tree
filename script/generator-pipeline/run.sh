#!/usr/bin/env bash
# Generate synthetic data and questions into <repo>/output/. Extra args go to 2-generate-questions.py,
# e.g.  ./run.sh --n 200 --seed 42
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" 1-generate-data.py
"$PYTHON" 2-generate-questions.py "$@"
