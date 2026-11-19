from __future__ import annotations

from dataclasses import dataclass, field
import argparse
import csv
import json
import random
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from core.evaluator import Evaluator
from core.metrics import MetricQuestion, MetricQuestionBuilder, load_metrics, safe_eval
from core.paths import CONCEPT_METADATA_FILE, METRICS_FILE, QUESTIONS_CSV, SPREADSHEET_CSV
from core.question_renderer import QuestionRenderer
from core.semantic_analyzer import SemanticAnalyzer
from core.tree import Atom, BindEnv, DERIVED_CONCEPTS, Expr, SemanticError, Store

T = TypeVar("T")

# Expected ways a random sample can fail to become a valid question (incompatible meanings,
# division by zero, no matching atoms, ...). Anything else is a real bug and must surface.
REJECTABLE_ERRORS = (SemanticError, ZeroDivisionError, ValueError, RuntimeError)
MAX_ATTEMPTS_PER_QUESTION = 1000
METRIC_COLUMNS = ["metric", "pattern", "style", "tier", "pair_id", "reference_answer"]


@dataclass
class RowData:
    i: int
    expr: Expr
    question: str
    answer: float
    expr_json: dict[str, Any]
    expr_str: str
    tree_payload: Expr
    tree_seed: int
    bind_seed: int
    master_seed: int
    derived_prob: float
    depth: int
    template_stats: dict[str, int]
    leaf_keys: list[str]
    atoms: dict[str, Atom]
    does_expr_contain_derived: bool
    metric_info: dict[str, Any] = field(default_factory=dict)  # METRIC_COLUMNS values (--mode metrics)


def validate_args(args: argparse.Namespace) -> None:
    if args.n <= 0:
        raise ValueError("--n must be > 0.")
    if args.depth_min < 0 or args.depth_max < 0:
        raise ValueError("--depth-min/--depth-max must be >= 0.")
    if args.depth_min > args.depth_max:
        raise ValueError("--depth-min must be <= --depth-max.")
    if not (
        0.0 <= args.derived_prob_min <= 1.0 and 0.0 <= args.derived_prob_max <= 1.0
    ):
        raise ValueError("--derived-prob-min and --derived-prob-max must be in [0, 1].")
    if args.derived_prob_min > args.derived_prob_max:
        raise ValueError("--derived-prob-min must be <= --derived-prob-max.")
    if not 0.0 <= args.ratio_root_prob <= 1.0:
        raise ValueError("--ratio-root-prob must be in [0, 1].")


def load_concept_metadata(path: Path) -> dict[str, dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    concepts = payload.get("define")
    if not isinstance(concepts, dict) or not concepts:
        raise ValueError(f"Invalid concept metadata payload in {path}")
    return concepts


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header row: {path}")
        return list(reader.fieldnames), list(reader)


def resolve_entity_column(fieldnames: Sequence[str]) -> str:
    for candidate in ("company_name", "ticker", "entity"):
        if candidate in fieldnames:
            return candidate
    raise ValueError("CSV must contain one of: company_name, ticker, entity.")


def normalize_value(value: Any) -> float:
    if value is None:
        raise ValueError("Missing value in spreadsheet.")
    if isinstance(value, str) and not value.strip():
        raise ValueError("Missing value in spreadsheet.")
    return float(value)


def build_atoms_from_dataframe(
    fieldnames: Sequence[str],
    rows: Sequence[dict[str, str]],
    concept_metadata: dict[str, dict[str, Any]],
) -> dict[str, Atom]:
    required = {"year", *concept_metadata.keys()}
    missing = sorted(required.difference(fieldnames))
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    entity_column = resolve_entity_column(fieldnames)
    atoms: dict[str, Atom] = {}

    for idx, row in enumerate(rows):
        for concept, meta in concept_metadata.items():
            # Blank cell = not reported (real data). No atom, so questions never bind to it.
            if not str(row[concept] or "").strip():
                continue
            atom = Atom(
                key=f"{idx}_{concept}",
                concept=concept,
                semantic_type=meta["semantic_type"],
                label=concept.replace("_", " "),
                entity=str(row[entity_column]),
                period=str(row["year"]),
                unit=meta["unit"],
                value=normalize_value(row[concept]),
                depth=0,
                parent_concept=meta.get("parent_concept"),
                role=meta.get("role"),
            )
            atoms[atom.key] = atom

    return atoms


def check_same_layout(atoms: dict[str, Atom], reference_atoms: dict[str, Atom], path: Path) -> None:
    """Atom keys are row index + column, so both spreadsheets must list the same company-years in the same order."""
    for key, atom in atoms.items():
        ref = reference_atoms.get(key)
        if ref is None or (ref.entity, ref.period) != (atom.entity, atom.period):
            raise ValueError(f"{path} does not have the same rows as --csv (first mismatch at {key}).")


def flatten_leaf_keys(expr: Expr) -> list[str]:
    return [leaf.key for leaf in expr.flatten_leaves() if leaf.key is not None]


def with_random_seed(seed: int, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    previous_state = random.getstate()
    random.seed(seed)
    try:
        return fn(*args, **kwargs)
    finally:
        random.setstate(previous_state)


def build_row(data: RowData) -> dict[str, Any]:
    expr = data.expr
    row: dict[str, Any] = {
        "question_id": data.i,
        "depth": expr.expr_depth(),
        "question": data.question,
        "expression": data.expr_str,
        "expression_json": json.dumps(data.expr_json, ensure_ascii=False),
        "template_expression": json.dumps(
            data.tree_payload.expr_to_json(), ensure_ascii=False
        ),
        "answer": data.answer,
        "template_depth_requested": data.depth,
        "template_actual_depth": Expr.actual_tree_depth(data.tree_payload),
        "bound_expression_depth": expr.expr_depth(),
        "tree_seed": data.tree_seed,
        "binding_seed": data.bind_seed,
        "master_seed": data.master_seed,
        "derived_prob": data.derived_prob,
        "template_internal_nodes": data.template_stats["internal_nodes"],
        "template_leaf_slots": data.template_stats["leaves"],
        "bound_leaf_count": len(data.leaf_keys),
        **{col: data.metric_info.get(col, "") for col in METRIC_COLUMNS},
    }

    for j, leaf_key in enumerate(data.leaf_keys, start=1):
        atom = data.atoms[leaf_key]
        row[f"leaf_{j}_key"] = atom.key
        row[f"leaf_{j}_concept"] = atom.concept
        row[f"leaf_{j}_label"] = atom.label
        row[f"leaf_{j}_entity"] = atom.entity
        row[f"leaf_{j}_period"] = atom.period
        row[f"leaf_{j}_unit"] = atom.unit
        row[f"leaf_{j}_value"] = atom.value

    return row


def generate_one(
    *,
    master_rng: random.Random,
    args: argparse.Namespace,
    need_derived: bool,
    store: Store,
    analyzer: SemanticAnalyzer,
    evaluator: Evaluator,
    renderer: QuestionRenderer,
) -> tuple[Expr, Expr, str, float, int, int, int, float]:
    """Sample until one tree binds, analyzes, evaluates, and meets the depth/derived targets."""
    last_reason = "no attempt made"
    for _ in range(MAX_ATTEMPTS_PER_QUESTION):
        tree_seed = master_rng.randint(0, 10**9)
        bind_seed = master_rng.randint(0, 10**9)
        depth = master_rng.randint(args.depth_min, args.depth_max)
        derived_prob = round(
            master_rng.uniform(args.derived_prob_min, args.derived_prob_max), 3
        )
        try:
            tree_payload, _ = with_random_seed(
                tree_seed,
                Expr.sample_tree_with_rejection,
                max_depth=depth,
                derived_prob=derived_prob,
                derived_registry=DERIVED_CONCEPTS,
                ratio_root_prob=args.ratio_root_prob,
            )
            expr = with_random_seed(
                bind_seed,
                Expr.instantiate_typed_tree,
                tree_payload,
                store,
                BindEnv(),
                DERIVED_CONCEPTS,
            )
            analysis = analyzer.analyze(expr)
            answer = evaluator.eval(expr)
            question = with_random_seed(bind_seed, renderer.render, analysis)
        except REJECTABLE_ERRORS as err:
            last_reason = f"{type(err).__name__}: {err}"
            continue

        if need_derived and not expr.contains_a_derived_concept():
            last_reason = "needed a derived concept"
            continue
        if not (args.depth_min <= expr.expr_depth() <= args.depth_max):
            last_reason = f"bound depth {expr.expr_depth()} outside requested range"
            continue
        return tree_payload, expr, question, answer, tree_seed, bind_seed, depth, derived_prob

    raise RuntimeError(
        f"No valid question after {MAX_ATTEMPTS_PER_QUESTION} attempts. Last reason: {last_reason}"
    )


def generate_one_metric(
    *,
    master_rng: random.Random,
    args: argparse.Namespace,
    builder: MetricQuestionBuilder,
    evaluator: Evaluator,
) -> tuple[MetricQuestion, float, int]:
    """Sample metric questions until one fits the data and the depth range."""
    last_reason = "no attempt made"
    for _ in range(MAX_ATTEMPTS_PER_QUESTION):
        seed = master_rng.randint(0, 10**9)
        try:
            q = with_random_seed(seed, builder.sample)
            answer = evaluator.eval(q.expr)
        except (ValueError, ZeroDivisionError) as err:
            last_reason = f"{type(err).__name__}: {err}"
            continue
        if not (args.depth_min <= q.expr.expr_depth() <= args.depth_max):
            last_reason = f"depth {q.expr.expr_depth()} outside requested range"
            continue
        return q, answer, seed
    raise RuntimeError(
        f"No valid question after {MAX_ATTEMPTS_PER_QUESTION} attempts. Last reason: {last_reason}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate random financial questions from the synthetic spreadsheet."
    )
    parser.add_argument("--csv", type=Path, default=SPREADSHEET_CSV, help="Input spreadsheet CSV")
    parser.add_argument(
        "--concept-metadata",
        type=Path,
        default=CONCEPT_METADATA_FILE,
        help="Concept metadata JSON used to build atoms from the CSV",
    )
    parser.add_argument(
        "--mode",
        choices=["random", "metrics"],
        default="random",
        help="random: random operator trees; metrics: financial metrics from --metrics (net margin, ROE, ...)",
    )
    parser.add_argument("--metrics", type=Path, default=METRICS_FILE, help="Metric catalogue for --mode metrics")
    parser.add_argument(
        "--style",
        choices=["named", "expanded", "paired"],
        default="named",
        help="--mode metrics: named = metric name only (tests if the model knows the formula); "
        "expanded = definitions given (control); paired = both, two rows per question with the same pair_id",
    )
    parser.add_argument(
        "--turns",
        choices=["metrics", "leaves"],
        default="metrics",
        help="--mode metrics: steps in <output>_multiturn.json. metrics = one turn per intermediate metric value; "
        "leaves = also one turn per spreadsheet value read",
    )
    parser.add_argument(
        "--reference-csv",
        type=Path,
        default=None,
        help="--mode metrics: a spreadsheet with the same rows as --csv (e.g. the real data behind a counterfactual "
        "one); adds reference_answer = the answer on that spreadsheet",
    )
    parser.add_argument("--n", type=int, default=90, help="Number of questions to generate (pairs with --style paired)")
    parser.add_argument("--depth-min", type=int, default=None, help="Minimum tree depth (default: 1, or 0 in metrics mode)")
    parser.add_argument("--depth-max", type=int, default=None, help="Maximum tree depth (default: 4, or no limit in metrics mode)")
    parser.add_argument(
        "--derived-prob-min", type=float, default=0.10, help="Minimum derived concept probability"
    )
    parser.add_argument(
        "--derived-prob-max", type=float, default=0.60, help="Maximum derived concept probability"
    )
    parser.add_argument(
        "--ratio-root-prob",
        type=float,
        default=0.2,
        help="Probability that a question asks for a ratio or growth rate instead of an amount",
    )
    parser.add_argument("--seed", type=int, default=None, help="Master seed; default is random")
    parser.add_argument(
        "--output",
        type=Path,
        default=QUESTIONS_CSV,
        help="Output CSV path; a JSON copy is written next to it with the same stem",
    )
    args = parser.parse_args()
    if args.depth_min is None:
        args.depth_min = 0 if args.mode == "metrics" else 1
    if args.depth_max is None:
        args.depth_max = 100 if args.mode == "metrics" else 4
    validate_args(args)

    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames, csv_rows = read_csv_rows(args.csv)
    concept_metadata = load_concept_metadata(args.concept_metadata)
    atoms = build_atoms_from_dataframe(fieldnames, csv_rows, concept_metadata)
    store = Store.store_from_atoms(atoms)
    analyzer = SemanticAnalyzer(atoms)
    evaluator = Evaluator(atoms)
    reference = None
    if args.reference_csv is not None:
        reference_atoms = build_atoms_from_dataframe(*read_csv_rows(args.reference_csv), concept_metadata)
        check_same_layout(atoms, reference_atoms, args.reference_csv)
        reference = Evaluator(reference_atoms)
    renderer = QuestionRenderer()
    builder = None
    if args.mode == "metrics":
        builder = MetricQuestionBuilder(atoms, load_metrics(args.metrics, set(concept_metadata)))

    master_seed = (
        args.seed if args.seed is not None else random.SystemRandom().randint(0, 10**9)
    )
    master_rng = random.Random(master_seed)
    target_derived_ratio = (args.derived_prob_min + args.derived_prob_max) / 2

    rows: list[RowData] = []
    multiturn: list[dict[str, Any]] = []  # --mode metrics: the same questions, decomposed into turns
    derived_count = 0
    for i in range(1, args.n + 1):
        if builder is not None:
            # Metric questions have no separate template: the bound tree stands in for it.
            q, answer, seed = generate_one_metric(
                master_rng=master_rng, args=args, builder=builder, evaluator=evaluator
            )
            expr = q.expr
            tree_payload, tree_seed, bind_seed, depth, derived_prob = expr, seed, seed, expr.expr_depth(), 0.0
            styles = ["named", "expanded"] if args.style == "paired" else [args.style]
            texts = {"named": q.named, "expanded": q.expanded}
            variants = [
                (texts[style], {"metric": q.metric, "pattern": q.pattern, "style": style, "tier": q.tier, "pair_id": i,
                                "reference_answer": safe_eval(reference, expr) if reference else ""})
                for style in styles
            ]
        else:
            need_derived = derived_count / i < target_derived_ratio
            tree_payload, expr, question, answer, tree_seed, bind_seed, depth, derived_prob = generate_one(
                master_rng=master_rng,
                args=args,
                need_derived=need_derived,
                store=store,
                analyzer=analyzer,
                evaluator=evaluator,
                renderer=renderer,
            )
            variants = [(question, {})]
        contains_derived = expr.contains_a_derived_concept()
        derived_count += contains_derived
        for question, metric_info in variants:
            rows.append(
                RowData(
                    i=len(rows) + 1,
                    expr=expr,
                    question=question,
                    answer=answer,
                    expr_json=expr.expr_to_json(),
                    expr_str=expr.show_expr(),
                    tree_payload=tree_payload,
                    tree_seed=tree_seed,
                    bind_seed=bind_seed,
                    master_seed=master_seed,
                    derived_prob=derived_prob,
                    depth=depth,
                    template_stats=Expr.count_nodes(tree_payload),
                    leaf_keys=flatten_leaf_keys(expr),
                    atoms=atoms,
                    does_expr_contain_derived=contains_derived,
                    metric_info=metric_info,
                )
            )
            if builder is not None:
                record = builder.multiturn_record(
                    q, metric_info["style"], answer, include_lookups=args.turns == "leaves", reference=reference
                )
                multiturn.append({"question_id": len(rows), **record, **metric_info})

    rows_to_write = [build_row(row) for row in rows]
    max_leaf_count = max(len(row.leaf_keys) for row in rows)

    fieldnames = [
        "question_id",
        "depth",
        "question",
        "expression",
        "expression_json",
        "template_expression",
        "answer",
        "template_depth_requested",
        "template_actual_depth",
        "bound_expression_depth",
        "tree_seed",
        "binding_seed",
        "master_seed",
        "derived_prob",
        "template_internal_nodes",
        "template_leaf_slots",
        "bound_leaf_count",
        *METRIC_COLUMNS,
    ]
    for j in range(1, max_leaf_count + 1):
        fieldnames.extend(
            f"leaf_{j}_{field}"
            for field in ("key", "concept", "label", "entity", "period", "unit", "value")
        )

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_to_write)

    json_path = output_path.with_suffix(".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows_to_write, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(rows_to_write)} questions to {output_path} and {json_path}")
    if multiturn:
        multiturn_path = output_path.with_name(output_path.stem + "_multiturn.json")
        with open(multiturn_path, "w", encoding="utf-8") as f:
            json.dump(multiturn, f, indent=2, ensure_ascii=False)
        turns = [r["num_turns"] for r in multiturn]
        print(f"Saved multi-turn version to {multiturn_path} ({min(turns)}-{max(turns)} turns per question)")
    print(f"Master seed: {master_seed}")
    print(f"Max bound leaf count: {max_leaf_count}")


if __name__ == "__main__":
    main()
