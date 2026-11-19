"""
Counterfactual spreadsheet: the real companies with every number moved by 10-30% up or down.

Used to test memorization: a model that reads the sheet answers with the counterfactual numbers;
a model that recalls the real figures (Apple's true revenue, ...) answers with the real ones.
2-generate-questions.py --reference-csv <real csv> stores the real-data answer next to each question.

Input:  output/real/real_company_data.csv          (from 1-fetch-real-data.py)
Output: output/counterfactual/counterfactual_company_data.csv / .json

Every numeric cell gets its own random factor, so ratios (margins, ROE, ...) change too, not only
amounts. Row order, companies, years and blank cells stay exactly as in the real file, so atom keys
(row index + column) point to the same company-year in both files. Same --seed = same output.
"""

import argparse
import csv
import json
import random
from pathlib import Path

from core.paths import COUNTERFACTUAL_SPREADSHEET_CSV, COUNTERFACTUAL_SPREADSHEET_JSON, REAL_SPREADSHEET_CSV

KEEP_COLUMNS = {"company_name", "ticker", "sector", "country", "exchange", "year"}


def perturb(value: float, low: float, high: float) -> float:
    """Multiply by 1 +/- a random 10-30% (by default), rounded like the real data."""
    change = random.uniform(low, high) * random.choice([-1, 1])
    new_value = value * (1 + change)
    return round(new_value, 4) if abs(value) < 1 else round(new_value, 1)  # rates keep 4 decimals


def main():
    parser = argparse.ArgumentParser(description="Perturb the real spreadsheet into a counterfactual one.")
    parser.add_argument("--input", type=Path, default=REAL_SPREADSHEET_CSV)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-change", type=float, default=0.10, help="Smallest relative change per cell.")
    parser.add_argument("--max-change", type=float, default=0.30, help="Largest relative change per cell.")
    args = parser.parse_args()
    random.seed(args.seed)

    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for col, value in row.items():
            if col not in KEEP_COLUMNS and value.strip():
                row[col] = perturb(float(value), args.min_change, args.max_change)

    COUNTERFACTUAL_SPREADSHEET_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(COUNTERFACTUAL_SPREADSHEET_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    # JSON copy for the benchmark, blank cells left out (same as the real JSON).
    json_rows = [{k: (int(v) if k == "year" else v) for k, v in row.items() if v != ""} for row in rows]
    with open(COUNTERFACTUAL_SPREADSHEET_JSON, "w", encoding="utf-8") as f:
        json.dump(json_rows, f, indent=2)
    print(f"Wrote {len(rows)} perturbed company-year rows to {COUNTERFACTUAL_SPREADSHEET_CSV}")


if __name__ == "__main__":
    main()
