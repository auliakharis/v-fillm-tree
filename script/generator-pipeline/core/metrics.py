"""Questions about real financial metrics (configs/financial_metrics.json), used by --mode metrics.

A question = one metric (net margin, ROE, ...) + one pattern (its value in a year, its change between
two years, its average over a window, a comparison of two companies, ...). Each question is written in
two styles, with the same answer:
  named     only names the metric (plus its convention if the definition is ambiguous),
            so the model must know how to compute it -> tests financial understanding
  expanded  also lists the definitions, one per metric, down to spreadsheet columns
            -> control: tests only reading the sheet and doing the arithmetic

Formulas are parsed with Python's `ast` and turned into the normal expression tree
(Node / Leaf / Literal / DerivedExpr), so Evaluator and the CSV output work unchanged.

Multi-turn: every metric value (and, optionally, every spreadsheet value) bound while building a question is
recorded as a Step; multiturn_record() turns them into one user turn each, bottom-up, each with its own
ground truth, and ends with the original question. Format = what script/benchmark expects for --datasets mt.

Debugging: MetricQuestionBuilder(atoms, metrics).sample() builds one random question;
.bind("net_margin", "Apple Inc", 2023) gives the tree for one metric and company-year;
.definitions("net_margin") shows the definitions an expanded question would give.
"""
from __future__ import annotations

import ast
import json
import random
from dataclasses import dataclass
from pathlib import Path

from .evaluator import Evaluator
from .tree import Atom, DerivedExpr, Expr, Leaf, Literal, Node, Operation

AST_OPS = {ast.Add: Operation.sum, ast.Sub: Operation.diff, ast.Mult: Operation.mul,
           ast.Div: Operation.ratio, ast.Pow: Operation.pow}
OP_SYMBOLS = {ast.Add: "+", ast.Sub: "−", ast.Mult: "×", ast.Div: "÷", ast.Pow: "^"}

UNIT_HINTS = {
    "amount": " Answer in millions of USD.",
    "percent": " Answer as a decimal (e.g. 0.12 for 12%).",
    "multiple": "",
    "per_share": " Answer in USD per share.",
    "days": " Answer in days.",
}

# "compare" (two companies) is switched off: the benchmark gives the model only the first company's sheet,
# so those questions cannot be answered yet. Add it back here once script/benchmark passes every company named.
PATTERNS = ["value", "change", "growth", "cagr", "average", "maximum", "minimum"]
GROWABLE_UNITS = {"amount", "per_share"}  # growth / CAGR of a ratio is not a standard question


@dataclass
class Metric:
    name: str
    label: str
    unit: str
    tier: int
    formula: str
    convention: str = ""

    @property
    def tree(self) -> ast.expr:
        return ast.parse(self.formula, mode="eval").body


@dataclass
class Step:
    """One value bound while building a question: a metric or a spreadsheet lookup for one company-year."""
    kind: str  # "metric" or "lookup"
    name: str
    entity: str
    year: int
    expr: Expr


@dataclass
class MetricQuestion:
    expr: Expr
    named: str      # question text, named style
    expanded: str   # question text, expanded style (same as named for plain spreadsheet lookups)
    metric: str
    pattern: str
    tier: int
    steps: list[Step]  # bottom-up: every value is listed after the values it is computed from


def load_metrics(path: Path, columns: set[str]) -> dict[str, Metric]:
    """Read the metric catalogue and check that every name in every formula exists."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)["define"]
    metrics = {name: Metric(name=name, **spec) for name, spec in raw.items()}
    for metric in metrics.values():
        for node in ast.walk(metric.tree):
            if isinstance(node, ast.Name) and node.id not in metrics and node.id not in columns:
                raise ValueError(f"Metric {metric.name}: unknown name '{node.id}' in formula '{metric.formula}'")
        if metric.unit not in UNIT_HINTS:
            raise ValueError(f"Metric {metric.name}: unknown unit '{metric.unit}'")
    return metrics


def safe_eval(evaluator: Evaluator, expr: Expr) -> float | None:
    """Value of expr, or None if it cannot be computed (e.g. division by zero on the other spreadsheet)."""
    try:
        return evaluator.eval(expr)
    except (ValueError, ZeroDivisionError, KeyError):
        return None


def subscript_offset(node: ast.Subscript) -> tuple[str, int]:
    """name[-1] -> ("name", 1): how many years back."""
    if not isinstance(node.value, ast.Name):
        raise ValueError(f"Only name[-k] is supported, got {ast.unparse(node)}")
    offset = ast.literal_eval(node.slice)
    if not isinstance(offset, int) or offset >= 0:
        raise ValueError(f"Year offset must be negative, got {ast.unparse(node)}")
    return node.value.id, -offset


class MetricQuestionBuilder:
    def __init__(self, atoms: dict[str, Atom], metrics: dict[str, Metric]):
        self.metrics = metrics
        self.atoms = atoms
        self.evaluator = Evaluator(atoms)
        self.keys = {(a.entity, int(a.period), a.concept): a.key for a in atoms.values()}
        self.entities = sorted({a.entity for a in atoms.values()})
        self.years = sorted({int(a.period) for a in atoms.values()})
        # Plain spreadsheet amounts can be asked too ("revenue of Apple in 2023").
        self.lookups = sorted({a.concept for a in atoms.values() if a.unit == "M_USD"})
        self.steps: list[Step] = []  # filled by bind() while sample() builds a question

    # ------------------------------------------------------------------
    # Formula -> expression tree
    # ------------------------------------------------------------------

    def bind(self, name: str, entity: str, year: int) -> Expr:
        """Tree for a metric or spreadsheet column, for one company-year. Missing data -> ValueError."""
        if name in self.metrics:
            expr = DerivedExpr(name=name, expr=self.bind_formula(self.metrics[name].tree, entity, year))
            self.steps.append(Step("metric", name, entity, year, expr))
            return expr
        key = self.keys.get((entity, year, name))
        if key is None:
            raise ValueError(f"No {name} for {entity} in {year}")
        expr = Leaf(key=key)
        self.steps.append(Step("lookup", name, entity, year, expr))
        return expr

    def bind_formula(self, node: ast.expr, entity: str, year: int) -> Expr:
        if isinstance(node, ast.BinOp):
            return Node(op=AST_OPS[type(node.op)],
                        left=self.bind_formula(node.left, entity, year),
                        right=self.bind_formula(node.right, entity, year))
        if isinstance(node, ast.Constant):
            return Literal(float(node.value))
        if isinstance(node, ast.Name):
            return self.bind(node.id, entity, year)
        if isinstance(node, ast.Subscript):
            name, years_back = subscript_offset(node)
            return self.bind(name, entity, year - years_back)
        raise ValueError(f"Unsupported formula syntax: {ast.unparse(node)}")

    # ------------------------------------------------------------------
    # Formula -> words (expanded style)
    # ------------------------------------------------------------------

    def definitions(self, name: str) -> list[str]:
        """'label = formula' for the metric and every metric it uses, top-down, each listed once."""
        lines: list[str] = []
        seen: set[str] = set()

        def visit(metric_name: str) -> None:
            if metric_name in seen or metric_name not in self.metrics:
                return
            seen.add(metric_name)
            metric = self.metrics[metric_name]
            lines.append(f"{metric.label} = {self.words(metric.tree)[0]}")
            for node in ast.walk(metric.tree):
                if isinstance(node, ast.Name):
                    visit(node.id)

        visit(name)
        return lines

    def label_of(self, name: str) -> str:
        return self.metrics[name].label if name in self.metrics else name.replace("_", " ")

    def words(self, node: ast.expr) -> tuple[str, bool]:
        """One level of a formula in words: (text, is_compound). Compound parts get parentheses when nested."""
        if isinstance(node, ast.BinOp):
            left, left_compound = self.words(node.left)
            right, right_compound = self.words(node.right)
            left = f"({left})" if left_compound else left
            right = f"({right})" if right_compound else right
            return f"{left} {OP_SYMBOLS[type(node.op)]} {right}", True
        if isinstance(node, ast.Constant):
            return f"{node.value:g}", False
        if isinstance(node, ast.Subscript):
            name, years_back = subscript_offset(node)
            suffix = " of the previous year" if years_back == 1 else f" of {years_back} years earlier"
            return self.label_of(name) + suffix, False
        if isinstance(node, ast.Name):
            return self.label_of(node.id), False
        raise ValueError(f"Unsupported formula syntax: {ast.unparse(node)}")

    # ------------------------------------------------------------------
    # Sampling one question
    # ------------------------------------------------------------------

    def info(self, name: str) -> Metric:
        if name in self.metrics:
            return self.metrics[name]
        return Metric(name=name, label=name.replace("_", " "), unit="amount", tier=1, formula=name)

    def window(self) -> list[int]:
        """Random run of at least two consecutive years."""
        length = random.randint(2, len(self.years))
        start = random.randint(0, len(self.years) - length)
        return self.years[start:start + length]

    def require_positive(self, expr: Expr, why: str) -> None:
        if self.evaluator.eval(expr) <= 0:
            raise ValueError(f"{why}: base value is not positive")

    def sample(self) -> MetricQuestion:
        """One random question. Raises ValueError/ZeroDivisionError when the data does not allow it (caller retries)."""
        self.steps = []
        # Pick the tier first so the many simple tier-1 items do not dominate.
        candidates = list(self.metrics) + self.lookups
        tier = random.choice(sorted({self.info(n).tier for n in candidates}))
        name = random.choice([n for n in candidates if self.info(n).tier == tier])
        metric = self.info(name)
        patterns = [p for p in PATTERNS if metric.unit in GROWABLE_UNITS or p not in ("growth", "cagr")]
        pattern = random.choice(patterns)
        entity = random.choice(self.entities)
        label = metric.label
        years = self.window()
        y0, y1 = years[0], years[-1]
        unit = metric.unit

        match pattern:
            case "value":
                expr = self.bind(name, entity, y1)
                question = f"What was {entity}'s {label} in {y1}?"
            case "change":
                expr = Node(op=Operation.diff, left=self.bind(name, entity, y1), right=self.bind(name, entity, y0))
                question = f"By how much did {entity}'s {label} change from {y0} to {y1}?"
            case "growth":
                base = self.bind(name, entity, y0)
                self.require_positive(base, "growth")
                expr = Node(op=Operation.growth, left=self.bind(name, entity, y1), right=base)
                question = f"By what percentage did {entity}'s {label} grow from {y0} to {y1}?"
                unit = "percent"
            case "cagr":
                start, end = self.bind(name, entity, y0), self.bind(name, entity, y1)
                self.require_positive(start, "cagr")
                self.require_positive(end, "cagr")
                ratio = Node(op=Operation.ratio, left=end, right=start)
                expr = Node(op=Operation.diff,
                            left=Node(op=Operation.pow, left=ratio, right=Literal(1 / (y1 - y0))),
                            right=Literal(1.0))
                question = f"What was the compound annual growth rate of {entity}'s {label} from {y0} to {y1}?"
                unit = "percent"
            case "average":
                values = [self.bind(name, entity, y) for y in years]
                expr = Node(op=Operation.ratio, left=Expr.fold_narry(Operation.sum, values, balanced=True),
                            right=Literal(float(len(values))))
                question = f"What was {entity}'s average {label} over the years {y0} through {y1}?"
            case "maximum" | "minimum":
                values = [self.bind(name, entity, y) for y in years]
                op = Operation.max if pattern == "maximum" else Operation.min
                expr = Expr.fold_narry(op, values, balanced=True)
                word = "highest" if pattern == "maximum" else "lowest"
                question = f"What was {entity}'s {word} yearly {label} over the years {y0} through {y1}?"
            case "compare":
                other = random.choice([e for e in self.entities if e != entity])
                expr = Node(op=Operation.diff, left=self.bind(name, entity, y1), right=self.bind(name, other, y1))
                question = (f"How much higher was {entity}'s {label} than {other}'s in {y1}? "
                            f"(Give a negative number if it was lower.)")
            case _:
                raise ValueError(f"Unknown pattern {pattern}")

        named = question + (f" {metric.convention}" if metric.convention else "") + UNIT_HINTS[unit]
        expanded = named
        if name in self.metrics:
            expanded = question + " Use these definitions: " + "; ".join(self.definitions(name)) + "." + UNIT_HINTS[unit]
        return MetricQuestion(expr=expr, named=named, expanded=expanded, metric=name, pattern=pattern,
                              tier=metric.tier, steps=list(self.steps))

    # ------------------------------------------------------------------
    # Multi-turn decomposition
    # ------------------------------------------------------------------

    def step_question(self, step: Step, style: str) -> str:
        """The sub-question for one intermediate value."""
        question = f"What was {step.entity}'s {self.label_of(step.name)} in {step.year}?"
        if step.kind == "lookup":
            unit = self.atoms[step.expr.key].unit
            return question + {"M_USD": UNIT_HINTS["amount"], "ratio": UNIT_HINTS["percent"]}.get(unit, "")
        metric = self.metrics[step.name]
        if style == "expanded":
            # One level only: the metrics it uses were asked in earlier turns.
            question += f" Use {metric.label} = {self.words(metric.tree)[0]}."
        elif metric.convention:
            question += f" {metric.convention}"
        return question + UNIT_HINTS[metric.unit]

    def multiturn_record(
        self,
        q: MetricQuestion,
        style: str,
        answer: float,
        include_lookups: bool = False,
        reference: Evaluator | None = None,
    ) -> dict:
        """One question as a conversation: one turn per distinct intermediate value, then the original question.

        With `reference` (an Evaluator over another spreadsheet with the same layout, e.g. the real data behind a
        counterfactual one), every turn also gets `reference_ground_truth`: the answer on that spreadsheet.
        """
        final_question = q.named if style == "named" else q.expanded
        turns: list[dict] = []
        seen: set[tuple[str, str, int]] = set()
        for step in q.steps:
            key = (step.name, step.entity, step.year)
            if key in seen or (step.kind == "lookup" and not include_lookups):
                continue
            seen.add(key)
            turns.append({"op": step.name, "question": self.step_question(step, style),
                          "ground_truth": self.evaluator.eval(step.expr), "expr": step.expr})
        if q.pattern == "value" and turns and turns[-1]["op"] == q.metric:
            # The last step already is the asked value: ask it with the original wording instead of twice.
            turns[-1] = {"op": q.metric, "question": final_question, "ground_truth": answer, "expr": q.expr}
        else:
            turns.append({"op": q.pattern, "question": final_question, "ground_truth": answer, "expr": q.expr})
        for number, turn in enumerate(turns, start=1):
            turn["turn_number"] = number
            turn["is_final"] = number == len(turns)
            expr = turn.pop("expr")
            if reference is not None:
                turn["reference_ground_truth"] = safe_eval(reference, expr)
        return {
            "original_question": final_question,
            "answer": answer,
            "depth": q.expr.expr_depth(),
            "num_turns": len(turns),
            "messages": [{"role": "user", "content": t["question"]} for t in turns],
            "turns": turns,
        }
