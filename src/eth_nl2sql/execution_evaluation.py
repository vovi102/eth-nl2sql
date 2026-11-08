"""EX: execution accuracy of generated SQL against the gold (proposed-method.md §7).

A prediction is {question_id, decision, sql}: `decision` is sql, nil or unsupported (the same
values as data/prompts/), `sql` keeps the placeholders. Both queries go through B8 with the
addresses of their own linking record: the gold's, and the record the prompt was built from.

The rules, the convention of BIRD:

- Rows are compared as a multiset: ORDER BY decides which rows a LIMIT keeps, so a top-k
  answer is still checked, but ties ranked differently are no error.
- Column names do not count, column order does not either. `ex` asks for exactly the gold's
  columns; `ex_extra_columns_ok` also accepts a query that adds columns.
- Numbers match with a relative tolerance: SUM(value / 1e18) and SUM(value) / 1e18 differ in
  the last digits. A timestamp at midnight equals the date (date_trunc('day') vs CAST AS DATE).
- A question without gold SQL is right when the decision is the expected nil or unsupported;
  an answerable question is wrong when the system abstains.
- A question without a prediction, or whose prompt was too long for the model (`skipped`), is
  wrong; `evaluate-sql --only-predicted` leaves them out and counts them instead.

With the manifest of the phase-2 train data, `train_exposure` adds the split of R8
(phase2-plan.md §3): is a target of the question one that no train prompt names?
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from itertools import permutations

from eth_nl2sql.entity_linking import EntityLinker
from eth_nl2sql.sql_execution import Execution, SqlRunner

REL_TOL = 1e-6
MAX_COLUMN_MAPPINGS = 5_000


class GoldError(RuntimeError):
    pass


def expected_decision(gold: dict) -> str:
    """unsupported wins over nil, as in PromptBuilder._abstain."""
    if gold["unsupported"]:
        return "unsupported"
    if any(m["type"] == "nil" for m in gold["mentions"]):
        return "nil"
    return "sql"


def _cell(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, datetime):
        if value.time() == datetime.min.time() and value.tzinfo is None:
            return value.date()
        return value
    if isinstance(value, date):
        return value
    return str(value)


def _sort_key(row: tuple) -> tuple:
    """Order rows so equal ones line up; floats by 6 significant digits."""
    return tuple(
        (0, "") if v is None
        else (1, f"{v:.6e}") if isinstance(v, float)
        else (2, str(v))
        for v in row
    )  # fmt: skip


def _same_cell(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=1e-12)
    return a == b and type(a) is type(b)


def _same_rows(gold: list[tuple], pred: list[tuple]) -> bool:
    if len(gold) != len(pred):
        return False
    gold, pred = sorted(gold, key=_sort_key), sorted(pred, key=_sort_key)
    return all(
        all(_same_cell(a, b) for a, b in zip(g, p, strict=True))
        for g, p in zip(gold, pred, strict=True)
    )


def same_rows(gold: list[tuple], pred: list[tuple]) -> bool:
    """Same multiset of rows, columns in the same order, with the tolerance of EX."""
    return _same_rows(
        [tuple(_cell(v) for v in row) for row in gold],
        [tuple(_cell(v) for v in row) for row in pred],
    )


def results_match(gold: Execution, pred: Execution, *, extra_columns: bool = False) -> bool:
    """Do some len(gold.columns) columns of pred, in some order, hold the gold rows?"""
    if not (gold.ok and pred.ok) or pred.truncated or len(gold.rows) != len(pred.rows):
        return False
    k, m = len(gold.columns), len(pred.columns)
    if m < k or (m > k and not extra_columns):
        return False
    gold_rows = [tuple(_cell(v) for v in row) for row in gold.rows]
    pred_rows = [tuple(_cell(v) for v in row) for row in pred.rows]
    for n, mapping in enumerate(permutations(range(m), k)):
        if n >= MAX_COLUMN_MAPPINGS:
            return False
        if _same_rows(gold_rows, [tuple(row[i] for i in mapping) for row in pred_rows]):
            return True
    return False


def score(
    gold: dict, prediction: dict | None, gold_run: Execution | None, pred_run: Execution | None
) -> dict:
    """{question_id, kind, correct, correct_extra_columns} of one question."""
    expected = expected_decision(gold)
    decision = prediction.get("decision", "sql") if prediction else "sql"
    out = {"question_id": gold["question_id"], "expected": expected, "decision": decision}
    if prediction is None or "skipped" in prediction:
        kind = "missing" if prediction is None else "skipped"
        return out | {"kind": kind, "correct": False, "correct_extra_columns": False}
    if expected != "sql" or decision != "sql":
        right = decision == expected
        kind = "correct" if right else "abstained" if expected == "sql" else "wrong_decision"
        return out | {"kind": kind, "correct": right, "correct_extra_columns": right}
    if not pred_run.ok:
        return out | {"kind": pred_run.error_kind, "error": pred_run.error, "correct": False,
                      "correct_extra_columns": False}  # fmt: skip
    strict = results_match(gold_run, pred_run)
    loose = strict or results_match(gold_run, pred_run, extra_columns=True)
    kind = "correct" if strict else "extra_columns" if loose else "wrong_result"
    return out | {"kind": kind, "correct": strict, "correct_extra_columns": loose,
                  "postgres_parses": pred_run.postgres_parses}  # fmt: skip


def run_questions(
    gold_records: list[dict],
    predictions: list[dict],
    linking_records: list[dict],
    linker: EntityLinker,
    runner: SqlRunner,
) -> list[dict]:
    """Run gold and predicted SQL of every question and score them."""
    by_id = {p["question_id"]: p for p in predictions}
    linking = {r["question_id"]: r for r in linking_records}
    results = []
    for gold in gold_records:
        qid = gold["question_id"]
        prediction = by_id.get(qid)
        gold_run = pred_run = None
        if (
            expected_decision(gold) == "sql"
            and prediction
            and prediction["decision"] == "sql"
            and "skipped" not in prediction
        ):
            gold_run = runner.run(gold["gold_sql"], linker.placeholder_addresses(gold))
            if not gold_run.ok:
                raise GoldError(f"{qid}: gold SQL fails: {gold_run.error}")
            if qid not in linking:
                raise GoldError(f"{qid}: no linking record for the prediction")
            addresses = linker.placeholder_addresses(linking[qid])
            pred_run = runner.run(prediction.get("sql"), addresses)
        results.append(score(gold, prediction, gold_run, pred_run))
    return results


NAMED_TYPES = ("entity", "address", "token", "class")


def held_out_targets(train_manifest: dict) -> dict[str, set[str]]:
    """What no train prompt names (decision 27): the held-out entities, and the named addresses
    and tokens left out of the fillers because their names carry one of them."""
    removed = train_manifest["holdout"]["removed"]
    return {
        "entity": set(train_manifest["holdout_entities"]),
        "address": set(removed["addresses"]),
        "token": set(removed["tokens"]),
    }


def train_exposure(
    gold: dict,
    held_out: dict[str, set[str]],
    class_members: Callable[[str, int], list[tuple[str, list[str]]]],
) -> str:
    """held_out, not_held_out or no_named_target, from the gold mentions of the question.

    A class counts through its members (`EntityLinker.class_members`): the held-out entities are
    no top-5 member of a class, but a question may ask for more than 5.
    """
    named = [m for m in gold["mentions"] if m["type"] in NAMED_TYPES]
    if not named:
        return "no_named_target"
    for m in named:
        if m["type"] == "class":
            members = {entity for entity, _ in class_members(m["target"], m["top_n"])}
            if members & held_out["entity"]:
                return "held_out"
        elif m["target"] in held_out[m["type"]]:
            return "held_out"
    return "not_held_out"


def _ex(results: list[dict]) -> dict:
    n = len(results)
    hits = sum(r["correct"] for r in results)
    loose = sum(r["correct_extra_columns"] for r in results)
    return {"ex": hits / n if n else 0.0, "ex_extra_columns_ok": loose / n if n else 0.0,
            "correct": hits, "questions": n}  # fmt: skip


def report(
    results: list[dict],
    gold_records: list[dict],
    manifest: dict | None,
    exposure: dict[str, str] | None = None,
) -> dict:
    """EX overall, on answerable and unanswerable questions, by language, group, phenomenon.

    `discriminating` is EX on the questions where a mistake (manifest `discriminates`: status,
    direction, time) changes the answer, so that a correct answer shows the mistake was avoided.
    `exposure` maps question_id to its `train_exposure`, for the slice `by_train_exposure`.
    """
    gold = {g["question_id"]: g for g in gold_records}
    templates = (manifest or {}).get("templates", {})
    phenomena = (manifest or {}).get("phenomena", {})
    checks = (manifest or {}).get("discriminates", {})
    slices: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        g = gold[r["question_id"]]
        slices["by_lang"][g["lang"]].append(r)
        group = templates.get(g.get("template_id"), {}).get("group")
        if group:
            slices["by_group"][group].append(r)
        for p in phenomena.get(r["question_id"], []):
            slices["by_phenomenon"][p].append(r)
        for check, differs in checks.get(r["question_id"], {}).items():
            if differs:
                slices["discriminating"][check].append(r)
        if exposure is not None:
            slices["by_train_exposure"][exposure[r["question_id"]]].append(r)
    answerable = [r for r in results if r["expected"] == "sql"]
    executed = [r for r in answerable if r.get("postgres_parses") is not None]
    return {
        "overall": _ex(results),
        "answerable": _ex(answerable),
        "unanswerable": _ex([r for r in results if r["expected"] != "sql"]),
        "kinds": dict(Counter(r["kind"] for r in results).most_common()),
        "postgres_parses": (
            sum(r["postgres_parses"] for r in executed) / len(executed) if executed else None
        ),
        **{
            name: {key: _ex(rs) for key, rs in sorted(groups.items())}
            for name, groups in slices.items()
        },
    }


def format_report(rep: dict) -> str:
    def line(name: str, s: dict) -> str:
        return (f"  {name:<22} EX {s['ex']:.3f}  (+extra columns {s['ex_extra_columns_ok']:.3f})"
                f"  {s['correct']}/{s['questions']}")  # fmt: skip

    lines = [line(k, rep[k]) for k in ("overall", "answerable", "unanswerable")]
    lines.append(f"  kinds: {rep['kinds']}")
    for name in ("by_lang", "by_group", "by_phenomenon", "discriminating", "by_train_exposure"):
        if name not in rep:
            continue
        lines.append(f"  {name}:")
        lines += ["  " + line(k, s) for k, s in rep.get(name, {}).items()]
    return "\n".join(lines)


def gold_as_predictions(gold_records: list[dict]) -> list[dict]:
    """The self-check of the scorer: the gold SQL must score EX 1.0."""
    return [
        {"question_id": g["question_id"], "decision": expected_decision(g), "sql": g["gold_sql"]}
        for g in gold_records
    ]
