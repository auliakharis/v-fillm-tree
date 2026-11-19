# Compiler pipeline — overview

## Layout

```
configs/                       (repo root) JSON templates: companies, column definitions, concept metadata
output/                        (repo root, git-ignored) everything the pipeline generates
  synthetic/                   from 1-generate-data.py and its questions
  real/                        from 1-fetch-real-data.py (SEC EDGAR) and its questions
  counterfactual/              from 1-perturb-real-data.py (real companies, perturbed numbers) and its questions
script/generator-pipeline/
  run.sh                       RUN THIS: step 1 + 2-generate-questions.py in one go (--real for SEC data)
  1-generate-data.py           entry point 1: synthetic spreadsheet + schema + atoms
  1-fetch-real-data.py         entry point 1 (alternative): real spreadsheet from SEC EDGAR
  1-perturb-real-data.py       counterfactual spreadsheet: real one with every number moved 10-30%
  2-generate-questions.py      entry point 2: sample, bind, analyze, evaluate, render, write questions
  core/                        library code, never run directly
    metrics.py                 --mode metrics: questions about financial metrics (configs/financial_metrics.json)
    paths.py                   repo-anchored paths to configs/ and output/ (scripts work from any cwd)
    tree.py                    expression-tree model (nodes, leaves, derived concepts, sampling, binding)
    semantic_analyzer.py       walks a bound tree and builds human-readable meanings at each node
    evaluator.py               computes the numeric answer for a bound tree
    question_renderer.py       turns the root meaning into a natural-language question
    utils.py                   small shared helpers
script/benchmark/              LLM evaluation; reads the generator output by default
```

---

## How to run

Requires Python >= 3.11. From the repo root:

```bash
pip install -r requirements.txt

# everything in one go (extra args go to 2-generate-questions.py)
script/generator-pipeline/run.sh --n 90 --seed 42

# or step by step
python script/generator-pipeline/1-generate-data.py         # --seed, --factor
python script/generator-pipeline/2-generate-questions.py    # --n, --seed, --depth-min/max, ...
```

Outputs in `output/synthetic/`:

| File | Written by | Used by |
|---|---|---|
| `synthetic_company_data.csv` | `1-generate-data.py` | `2-generate-questions.py` |
| `synthetic_company_data.json` | `1-generate-data.py` | benchmark (spreadsheet context given to the LLM) |
| `schema.json`, `atoms.json` | `1-generate-data.py` | inspection; `tree.compile_tree_file` |
| `synthetic_questions.csv` / `.json` (+ `_multiturn.json` in metrics mode) | `2-generate-questions.py` | benchmark (`90q` dataset) |

Same `--seed` gives the same output. Useful flags of `2-generate-questions.py`:

- `--n` number of questions (default 90)
- `--depth-min` / `--depth-max` expression depth range (default 1–4)
- `--derived-prob-min` / `--derived-prob-max` how often named derived concepts (gross profit, net income, ...) appear
- `--ratio-root-prob` share of questions asking for a ratio or growth rate instead of an amount (default 0.2)

Then evaluate, e.g. `python script/benchmark/run_llm_eval.py --datasets 90q`
(`--questions-90q` / `--sheet-90q` point it at other files).

---

## Financial-metric questions (`--mode metrics`)

Instead of random operator trees, questions ask about real financial metrics (net margin, ROE, DIO,
DuPont ROE, ...) from `configs/financial_metrics.json`, based on the tiers in `configs/economics_question.md`.

```bash
script/generator-pipeline/run.sh --mode metrics --n 200 --seed 42           # synthetic data
script/generator-pipeline/run.sh --real --mode metrics --n 200 --seed 42    # real data
```

Each question = a metric (or a plain spreadsheet amount) × a pattern: value in a year, change between two
years, growth, CAGR, average / highest / lowest over a window. (A "difference between two companies" pattern exists
but is off in `PATTERNS` in `core/metrics.py`: the benchmark only shows the model the first company's data.)

`--style` sets how the question is written (the answer is the same):
- `named` (default): only the metric name, so the model must know how to compute it. Metrics whose definition
  varies between textbooks get a short `convention` from the config (e.g. "use average inventory and a 365-day year"),
  never the formula.
- `expanded`: also lists the definitions down to spreadsheet columns. A control: only reading and arithmetic.
- `paired`: both, as two rows with the same `pair_id` (`--n` counts pairs). Named wrong but expanded right
  = the model does not know the formula.

The tier is picked first, so tiers 1–5 appear about equally often. Depth comes from the metric formula,
so there is no depth limit by default; `--depth-min/--depth-max` still filter. Extra output columns:
`metric`, `pattern`, `style`, `tier`, `pair_id`.

**Multi-turn version.** Metrics mode also writes `<output>_multiturn.json` (e.g. `output/real/real_questions_multiturn.json`):
the same questions as conversations with one turn per intermediate value, bottom-up, each with its own `ground_truth`
(gross profit → operating income → … → net income → ROE), ending with the original question. Expanded-style turns
give only the one-level formula, since the parts were asked in earlier turns. `--turns leaves` also adds a turn for
every spreadsheet value read (long: 40+ turns for deep questions). Evaluate with
`run_llm_eval.py --datasets mt --questions-mt output/real/real_questions_multiturn.json --sheet-mt output/real/real_company_data.json`;
it scores every turn and reports the first turn that went wrong. Add `--gold-history` to put the correct answer of each turn in
the history instead of the model's own, so a wrong turn does not drag the next ones down; run with and without it
and compare. Results carry `"history": "gold"` or `"model"`.

Adding a metric: add a line to `configs/financial_metrics.json`. A formula can use spreadsheet columns,
other metrics, numbers, `+ - * / **`, and `name[-1]` for the previous year. Unknown names are reported at start-up.
Metrics needing data the spreadsheet does not have (debt, operating cash flow, D&A, interest, payables,
retained earnings) are not in the catalogue yet. Price-based metrics (P/E, P/B, dividend yield, market cap)
only work on synthetic data, since the real data has no stock price.

---

## Counterfactual data (memorization test)

```bash
script/generator-pipeline/run.sh --counterfactual --mode metrics --style paired --n 45 --seed 42
```

Fetches the real data, then `1-perturb-real-data.py` multiplies every number by 1 ± 10–30% (own random factor per
cell, so ratios change too; company names, years and blank cells stay). Questions are generated on the perturbed
sheet with `--reference-csv` pointing at the real one, so each row has `answer` (from the sheet the model sees) and
`reference_answer` (from the real data); multi-turn turns have `reference_ground_truth` too. A prediction close to
`reference_answer` but not `answer` means the model used remembered figures instead of reading the sheet. Some
questions (mostly growth rates) have both answers within tolerance of each other; leave those out of that count.
Benchmark: `--sheet-90q output/counterfactual/counterfactual_company_data.json --questions-90q output/counterfactual/counterfactual_questions.json`.

---

## Real data (SEC EDGAR)

`1-fetch-real-data.py` replaces the synthetic step 1 with numbers from real 10-K filings
(companies and CIKs in `configs/real_companies.json`). It writes the same spreadsheet format, so step 2 is unchanged.

```bash
export SEC_USER_AGENT="your-project your-email@example.org"   # SEC requires a contact
script/generator-pipeline/run.sh --real --n 90 --seed 42
# benchmark: --sheet-90q output/real/real_company_data.json --questions-90q output/real/real_questions.json
```

Outputs in `output/real/`: `real_company_data.csv/.json` and `real_questions.csv/.json`. Data is downloaded fresh on every run.
For checking numbers, `python 1-fetch-real-data.py --sources` also writes `real_company_sources.csv`
(XBRL tag, filing number and date behind every value).

Things to know:
- Values are in millions (shares in millions); `year` is the calendar year the fiscal year ends in.
- Cells a company does not report stay blank; step 2 skips them. `stock_price` and `employees` are always blank
  (not in XBRL), and `credit_rating` is not included.
- `operating_expenses`, `non_operating_expenses` and the `income_tax` rate are computed so that the pipeline's
  `operating_income`, `pretax_income` and `net_income` (income from continuing operations) match the filing.
  `current_assets` is still cash + receivables + inventories + short-term investments, which is lower than reported.
- LLMs may have memorized these companies' numbers; keep that in mind when reading benchmark results.
- To add a company: look up its CIK on sec.gov/edgar and add a line to `configs/real_companies.json`.
  If a column is blank, open `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`, search for the tag the company uses
  and add it to `RAW_ITEMS` in the script.

---

## The pipeline

### 0. Data prep (`1-generate-data.py`)

Run when you need a new synthetic dataset. This step:

1. Loads **`configs/`** (companies, column definitions, concept metadata).
2. Generates **`output/synthetic/synthetic_company_data.csv`** — one row per company per year with financial fields.
3. Writes **`output/synthetic/synthetic_company_data.json`** (same rows) and **`output/synthetic/schema.json`** describing columns.
4. Builds **`output/synthetic/atoms.json`** — one atom per spreadsheet cell (entity, period, concept, value, units, etc.).

`2-generate-questions.py` can build atoms in memory from the CSV + concept metadata, so you do not have to run `1-generate-data.py` before every question run if the CSV already exists.

### 1. Load data and build atoms (`2-generate-questions.py`)

Reads the spreadsheet CSV and concept metadata, then builds an in-memory `Atom` index used by binding, analysis, and evaluation.

### 2. Sample a symbolic tree (`core/tree.py`)

For each question, randomly samples a **template** expression tree (depth, derived-concept probability, operators) with rejection until the tree is valid (no forbidden duplicate derived forms, etc.). The root is an amount tree, or with probability `--ratio-root-prob` a ratio tree (ratio of two amounts, or year-over-year growth of one metric).

### 3. Bind the tree to real data (`core/tree.py`)

**`Expr.instantiate_typed_tree`** maps template leaves to concrete atoms (company, year, concept), expands derived concepts, and instantiates time aggregates (min / max / avg over a window of periods). The result is a fully **bound** expression tree tied to the spreadsheet.

### 4. Semantic analysis (`core/semantic_analyzer.py`)

**`SemanticAnalyzer.analyze`** walks the bound tree and produces an **`AnalysisResult`** at each node: natural-language **meanings** (e.g. “revenue for Apple in 2021”, “sum of …”) that describe what the expression represents.

### 5. Evaluation (`core/evaluator.py`)

**`Evaluator.eval`** recursively computes the numeric **answer** from atom values and operators (sum, diff, ratio, mul, growth, min, max).

### 6. Question rendering (`core/question_renderer.py`)

**`QuestionRenderer.render`** picks a phrasing template and formats the root meaning into an English **question** string.

### 7. Write dataset row

Each successful question becomes one row in the output CSV: question text, answer, expression JSON/string, template metadata, seeds, and per-leaf atom details (`leaf_1_*`, `leaf_2_*`, …).

Samples that fail for expected reasons (semantic incompatibility, division by zero, no matching data) are retried, up to 1000 attempts per question. Any other exception is a bug and stops the run.

---
