#!/usr/bin/env bash
# Generate data and questions into <repo>/output/<synthetic|real|counterfactual>/. Extra args go to
# 2-generate-questions.py, e.g.
#   ./run.sh --mode metrics --n 200 --seed 42                   synthetic data
#   ./run.sh --real --mode metrics --n 200 --seed 42            real 10-K data from SEC EDGAR (needs SEC_USER_AGENT)
#   ./run.sh --counterfactual --mode metrics --n 200 --seed 42  real companies with perturbed numbers; questions get
#                                                               reference_answer = the answer on the real data
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
OUT=../../output
case "${1:-}" in
    --real)
        shift
        "$PYTHON" 1-fetch-real-data.py
        "$PYTHON" 2-generate-questions.py --csv $OUT/real/real_company_data.csv --output $OUT/real/real_questions.csv "$@"
        ;;
    --counterfactual)
        shift
        "$PYTHON" 1-fetch-real-data.py
        "$PYTHON" 1-perturb-real-data.py
        "$PYTHON" 2-generate-questions.py --csv $OUT/counterfactual/counterfactual_company_data.csv \
            --reference-csv $OUT/real/real_company_data.csv \
            --output $OUT/counterfactual/counterfactual_questions.csv "$@"
        ;;
    *)
        "$PYTHON" 1-generate-data.py
        "$PYTHON" 2-generate-questions.py "$@"
        ;;
esac
