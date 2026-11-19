"""Repo-anchored paths so scripts work regardless of the current working directory."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "configs"
OUTPUT_DIR = REPO_ROOT / "output"
SYNTHETIC_DIR = OUTPUT_DIR / "synthetic"  # 1-generate-data.py and its questions
REAL_DIR = OUTPUT_DIR / "real"            # 1-fetch-real-data.py (SEC EDGAR) and its questions
COUNTERFACTUAL_DIR = OUTPUT_DIR / "counterfactual"  # 1-perturb-real-data.py: real companies, perturbed numbers

COMPANIES_FILE = CONFIG_DIR / "companies.json"
CATEGORICAL_COLS_FILE = CONFIG_DIR / "categorical_cols.json"
YEARLY_NUMERIC_COLS_FILE = CONFIG_DIR / "yearly_numeric_cols.json"
CONCEPT_METADATA_FILE = CONFIG_DIR / "concept_metadata.json"
METRICS_FILE = CONFIG_DIR / "financial_metrics.json"

SPREADSHEET_CSV = SYNTHETIC_DIR / "synthetic_company_data.csv"
SPREADSHEET_JSON = SYNTHETIC_DIR / "synthetic_company_data.json"
SCHEMA_JSON = SYNTHETIC_DIR / "schema.json"
ATOMS_JSON = SYNTHETIC_DIR / "atoms.json"
QUESTIONS_CSV = SYNTHETIC_DIR / "synthetic_questions.csv"

# Real data from SEC EDGAR (1-fetch-real-data.py)
REAL_COMPANIES_FILE = CONFIG_DIR / "real_companies.json"
REAL_SPREADSHEET_CSV = REAL_DIR / "real_company_data.csv"
REAL_SPREADSHEET_JSON = REAL_DIR / "real_company_data.json"
REAL_SOURCES_CSV = REAL_DIR / "real_company_sources.csv"
REAL_QUESTIONS_CSV = REAL_DIR / "real_questions.csv"

# Counterfactual data (1-perturb-real-data.py)
COUNTERFACTUAL_SPREADSHEET_CSV = COUNTERFACTUAL_DIR / "counterfactual_company_data.csv"
COUNTERFACTUAL_SPREADSHEET_JSON = COUNTERFACTUAL_DIR / "counterfactual_company_data.json"

