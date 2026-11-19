# Compiler pipeline — overview

## Layout

```
configs/                       (repo root) JSON templates: companies, column definitions, concept metadata
output/                        (repo root, git-ignored) everything the pipeline generates
script/generator-pipeline/
  run.sh                       RUN THIS: 1-generate-data.py + 2-generate-questions.py in one go
  1-generate-data.py           entry point 1: synthetic spreadsheet + schema + atoms
  2-generate-questions.py      entry point 2: sample, bind, analyze, evaluate, render, write questions
  core/                        library code, never run directly
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

Outputs in `output/`:

| File | Written by | Used by |
|---|---|---|
| `synthetic_company_data.csv` | `1-generate-data.py` | `2-generate-questions.py` |
| `synthetic_company_data.json` | `1-generate-data.py` | benchmark (spreadsheet context given to the LLM) |
| `schema.json`, `atoms.json` | `1-generate-data.py` | inspection; `tree.compile_tree_file` |
| `random_questions.csv` / `.json` | `2-generate-questions.py` | benchmark (`90q` dataset) |

Same `--seed` gives the same output. Useful flags of `2-generate-questions.py`:

- `--n` number of questions (default 90)
- `--depth-min` / `--depth-max` expression depth range (default 1–4)
- `--derived-prob-min` / `--derived-prob-max` how often named derived concepts (gross profit, net income, ...) appear
- `--ratio-root-prob` share of questions asking for a ratio or growth rate instead of an amount (default 0.2)

Then evaluate, e.g. `python script/benchmark/run_llm_eval.py --datasets 90q`
(`--questions-90q` / `--sheet-90q` point it at other files).

---

## The pipeline

### 0. Data prep (`1-generate-data.py`)

Run when you need a new synthetic dataset. This step:

1. Loads **`configs/`** (companies, column definitions, concept metadata).
2. Generates **`output/synthetic_company_data.csv`** — one row per company per year with financial fields.
3. Writes **`output/synthetic_company_data.json`** (same rows) and **`output/schema.json`** describing columns.
4. Builds **`output/atoms.json`** — one atom per spreadsheet cell (entity, period, concept, value, units, etc.).

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
