"""Repo-anchored paths so scripts work regardless of the current working directory."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "configs"
OUTPUT_DIR = REPO_ROOT / "output"

COMPANIES_FILE = CONFIG_DIR / "companies.json"
CATEGORICAL_COLS_FILE = CONFIG_DIR / "categorical_cols.json"
YEARLY_NUMERIC_COLS_FILE = CONFIG_DIR / "yearly_numeric_cols.json"
CONCEPT_METADATA_FILE = CONFIG_DIR / "concept_metadata.json"

SPREADSHEET_CSV = OUTPUT_DIR / "synthetic_company_data.csv"
SPREADSHEET_JSON = OUTPUT_DIR / "synthetic_company_data.json"
SCHEMA_JSON = OUTPUT_DIR / "schema.json"
ATOMS_JSON = OUTPUT_DIR / "atoms.json"
QUESTIONS_CSV = OUTPUT_DIR / "random_questions.csv"
