"""B8 (reply -> rows) and the EX scorer, on the tiny fixture database."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import ALICE, BOB, TOKEN

from eth_nl2sql.execution_evaluation import (
    expected_decision,
    report,
    results_match,
    score,
)
from eth_nl2sql.sql_execution import Execution, SqlRunner, extract_sql, repair_messages


@pytest.mark.parametrize(
    ("reply", "sql"),
    [
        ("```sql\nSELECT 1;\n```", "SELECT 1"),
        ("Here:\n```SQL\nSELECT 1\n```\nand ```sql\nSELECT 2\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("```sql\nSELECT 1 FROM t WHERE", "SELECT 1 FROM t WHERE"),  # cut off by max tokens
        ("The answer is SELECT COUNT(*) FROM blocks;", "SELECT COUNT(*) FROM blocks"),
        ("I cannot answer this.", None),
        ("```sql\n\n```", None),
    ],
)
def test_extract_sql(reply: str, sql: str | None) -> None:
    assert extract_sql(reply) == sql


@pytest.fixture
def runner(db: Path):
    with SqlRunner(db, timeout=5) as r:
        yield r


def test_runs_with_placeholders(runner: SqlRunner) -> None:
    run = runner.run(
        "SELECT from_address, COUNT(*) AS n FROM token_transfers "
        "WHERE token_address IN {E1} GROUP BY from_address",
        {"E1": [TOKEN]},
    )
    assert run.ok and run.columns == ["from_address", "n"]
    assert sorted(run.rows) == [(ALICE, 3), (BOB, 1)]
    assert run.postgres_parses is True


def test_an_empty_placeholder_matches_nothing(runner: SqlRunner) -> None:
    run = runner.run("SELECT COUNT(*) FROM transactions WHERE to_address IN {E1}", {"E1": []})
    assert run.rows == [(0,)]


ERRORS = [
    (None, "no_sql"),
    ("SELECT * FROM transactions WHERE to_address IN {E2}", "unknown_placeholder"),
    ("SELECT * FROM transactions WHERE to_address IN {E}", "unknown_placeholder"),
    ("SELEC * FROM transactions", "parse"),
    ("SELECT 1; SELECT 2", "not_a_query"),
    ("DELETE FROM transactions", "not_a_query"),
    ("SELECT no_such_column FROM transactions", "execution"),
    ("SELECT * FROM read_csv('/etc/hostname')", "execution"),  # no file access
    ("SELECT COUNT(*) FROM range(100000000000) a, range(100000000000) b", "timeout"),
]


def test_errors(db: Path) -> None:
    """One database for every case: building the fixture is the slow part."""
    with SqlRunner(db, timeout=0.5) as runner:
        for sql, kind in ERRORS:
            run = runner.run(sql, {"E1": [TOKEN]})
            assert (run.ok, run.error_kind) == (False, kind), sql
        run = runner.run("SELECT nope FROM transactions WHERE to_address IN {E1}",
                         {"E1": [ALICE] * 9})  # fmt: skip
        assert ALICE not in run.error and len(run.error) <= 300


def test_repair_messages() -> None:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    failed = Execution("SELECT x", "execution", 'Binder Error: column "x" not found')
    out = repair_messages(messages, "```sql\nSELECT x\n```", failed)
    assert out[:2] == messages and [m["role"] for m in out[2:]] == ["assistant", "user"]
    assert 'column "x" not found' in out[3]["content"]


# ---- EX -----------------------------------------------------------------------------------


def ran(columns: list[str], rows: list[tuple]) -> Execution:
    return Execution("SELECT", columns=columns, rows=rows)


GOLD = ran(["from_address", "tx_count"], [("0xa", 3), ("0xb", 1)])


@pytest.mark.parametrize(
    ("pred", "strict", "loose"),
    [
        (ran(["a", "b"], [("0xb", 1), ("0xa", 3)]), True, True),  # row order, names
        (ran(["n", "who"], [(3, "0xa"), (1, "0xb")]), True, True),  # column order
        (ran(["a", "b"], [("0xa", Decimal("3.0000001")), ("0xb", 1.0)]), True, True),
        (ran(["a", "b"], [("0xa", 3.01), ("0xb", 1)]), False, False),
        (ran(["a", "b", "c"], [("0xa", 3, 0), ("0xb", 1, 0)]), False, True),
        (ran(["a"], [("0xa",), ("0xb",)]), False, False),  # a gold column is missing
        (ran(["a", "b"], [("0xa", 3)]), False, False),
        (ran(["a", "b"], [("0xa", 3), ("0xa", 3)]), False, False),  # multiset, not set
        (ran(["a", "b"], [("0xa", "3"), ("0xb", "1")]), False, False),  # text is no number
    ],
)
def test_results_match(pred: Execution, strict: bool, loose: bool) -> None:
    assert results_match(GOLD, pred) is strict
    assert results_match(GOLD, pred, extra_columns=True) is loose


def test_midnight_timestamp_is_the_date() -> None:
    gold = ran(["day", "n"], [(date(2026, 9, 1), 5)])
    assert results_match(gold, ran(["d", "n"], [(datetime(2026, 9, 1), 5)]))
    assert not results_match(gold, ran(["d", "n"], [(datetime(2026, 9, 1, 1), 5)]))


def test_a_failed_or_truncated_prediction_never_matches() -> None:
    assert not results_match(GOLD, Execution("SELECT", "timeout", "slow"))
    truncated = ran(["a", "b"], GOLD.rows)
    truncated.truncated = True
    assert not results_match(GOLD, truncated)


def gold_record(qid: str, *, nil: bool = False, unsupported: bool = False) -> dict:
    mention = {"type": "nil", "text": "Bybit"} if nil else {"type": "entity", "text": "X"}
    return {
        "question_id": qid,
        "lang": "vi",
        "template_id": "t",
        "mentions": [mention],
        "unsupported": [{"sense_id": "balance"}] if unsupported else [],
        "gold_sql": None if nil or unsupported else "SELECT 1",
    }


def test_decisions() -> None:
    nil, unsupported, sql = gold_record("q1", nil=True), gold_record("q2", unsupported=True), \
        gold_record("q3")  # fmt: skip
    assert [expected_decision(g) for g in (nil, unsupported, sql)] == ["nil", "unsupported", "sql"]
    assert score(nil, {"decision": "nil"}, None, None)["kind"] == "correct"
    assert score(nil, {"decision": "unsupported"}, None, None)["kind"] == "wrong_decision"
    assert score(nil, {"decision": "sql", "sql": "SELECT 1"}, None, None)["correct"] is False
    assert score(sql, {"decision": "nil"}, None, None)["kind"] == "abstained"
    failed = Execution("SELECT", "parse", "bad")
    assert score(sql, {"decision": "sql"}, GOLD, failed)["kind"] == "parse"
    assert score(sql, {"decision": "sql"}, GOLD, GOLD)["kind"] == "correct"


def test_report_slices() -> None:
    golds = [gold_record("q1"), gold_record("q2", nil=True)]
    results = [score(golds[0], {"decision": "sql"}, GOLD, GOLD),
               score(golds[1], {"decision": "sql"}, None, None)]  # fmt: skip
    manifest = {"templates": {"t": {"group": "entity"}}, "phenomena": {"q1": ["alias"]},
                "discriminates": {"q1": {"status": True, "time": False}}}  # fmt: skip
    rep = report(results, golds, manifest)
    assert rep["discriminating"] == {"status": rep["answerable"]}
    assert rep["overall"]["correct"] == 1 and rep["overall"]["questions"] == 2
    assert rep["answerable"]["ex"] == 1.0 and rep["unanswerable"]["ex"] == 0.0
    assert rep["by_group"]["entity"]["questions"] == 2
    assert rep["by_phenomenon"] == {"alias": rep["answerable"]}
    assert rep["kinds"] == {"correct": 1, "wrong_decision": 1}
