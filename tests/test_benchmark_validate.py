"""The checks a benchmark question must pass: record shape, SQL dialect, execution."""

from __future__ import annotations

import duckdb
import pytest

from eth_nl2sql.benchmark_validate import (
    GoldQueryError,
    check_record,
    check_sql,
    informative,
    run_sql,
    substitute_placeholders,
)

SENSES = {
    "eth_amount": "measure",
    "received_by": "filter",
    "tx_success": "filter",
    "balance": "unsupported",
}
GOLD = (
    "SELECT SUM(value) / 1e18 AS eth_received FROM transactions WHERE to_address IN {E1} "
    "AND receipt_status = 1 AND block_timestamp >= TIMESTAMP '2026-09-01 00:00:00' "
    "AND block_timestamp < TIMESTAMP '2026-09-08 00:00:00'"
)


def example_1() -> dict:
    """docs/linking-labels.md §7, example 1, with its gold SQL."""
    return {
        "question_id": "q00001",
        "question": "Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9?",
        "lang": "vi",
        "mentions": [
            {
                "span": [0, 12],
                "text": "Tornado Cash",
                "type": "entity",
                "target": "tornado-cash",
                "placeholder": "E1",
            }
        ],  # fmt: skip
        "properties": [
            {"sense_id": "received_by", "span": [13, 17], "text": "nhận", "implicit": False},
            {
                "sense_id": "eth_amount",
                "span": [18, 31],
                "text": "bao nhiêu ETH",
                "implicit": False,
            },
            {"sense_id": "tx_success", "span": None, "text": None, "implicit": True},
        ],  # fmt: skip
        "time": {
            "start": "2026-09-01T00:00:00",
            "end": "2026-09-08T00:00:00",
            "span": [38, 54],
            "text": "tuần đầu tháng 9",
        },  # fmt: skip
        "unsupported": [],
        "warnings": [],
        "gold_sql": GOLD,
        "template_id": "entity_eth_received",
    }


def test_contract_example_passes() -> None:
    assert check_record(example_1(), SENSES) == []
    assert check_sql(GOLD) == []


def test_record_problems_are_found() -> None:
    r = example_1()
    r["mentions"][0]["span"] = [0, 11]
    assert "is 'Tornado Cas', not 'Tornado Cash'" in check_record(r, SENSES)[0]

    r = example_1()
    r["mentions"][0]["placeholder"] = "E2"
    assert any("placeholder E2 should be E1" in e for e in check_record(r, SENSES))

    r = example_1()
    r["mentions"] = [{"span": [0, 12], "text": "Tornado Cash", "type": "nil",
                      "nil_reason": "not_in_kb"}]  # fmt: skip
    errors = check_record(r, SENSES)
    assert "gold_sql must exist exactly when the question is answerable" in errors

    r = example_1()
    r["time"]["end"] = "2026-10-02T00:00:00"
    assert any("outside September 2026" in e for e in check_record(r, SENSES))

    r = example_1()
    r["properties"].append({"sense_id": "balance", "span": None, "text": None, "implicit": True})
    assert any("not a supported sense" in e for e in check_record(r, SENSES))


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        ("SELECT a FROM t QUALIFY row_number() OVER () = 1", "dialect-specific construct"),
        ("SELECT * EXCLUDE (a) FROM t", "dialect-specific construct"),
        ("SELECT list_sum(a) FROM t", "dialect-specific construct"),
        ("SELECT a FROM t LIMIT 5", "LIMIT without ORDER BY"),
        ("SELECT a, count(*) AS n FROM t GROUP BY a ORDER BY n DESC, a ASC", "needs NULLS"),
        ("SELECT a, count(*) AS n FROM t GROUP BY a ORDER BY n DESC NULLS LAST LIMIT 5",
         "tie-breaking column"),
        ("SELECT FROM WHERE", "does not parse"),
    ],
)  # fmt: skip
def test_sql_problems_are_found(sql: str, problem: str) -> None:
    assert any(problem in e for e in check_sql(sql)), check_sql(sql)


def test_order_by_keys_split_outside_parentheses() -> None:
    sql = (
        "SELECT transaction_hash, log_index FROM token_transfers WHERE token_address IN {E1} "
        "ORDER BY CAST(value AS DECIMAL(38,0)) DESC NULLS LAST, transaction_hash ASC NULLS LAST, "
        "log_index ASC NULLS LAST LIMIT 1"
    )
    assert check_sql(sql) == []


def test_substitute_placeholders() -> None:
    sql = "SELECT 1 FROM t WHERE a IN {E1} AND b IN {E12}"
    assert substitute_placeholders(sql, {"E1": ["0x1"], "E12": ["0x2", "0x3"]}) == (
        "SELECT 1 FROM t WHERE a IN ('0x1') AND b IN ('0x2', '0x3')"
    )
    with pytest.raises(GoldQueryError, match="E12"):
        substitute_placeholders(sql, {"E1": ["0x1"]})


def test_informative() -> None:
    assert not informative([])
    assert not informative([(None,)])
    assert not informative([(0,)])
    assert informative([(3,)])
    assert informative([("0xabc", 0)])  # a row of a ranking is an answer


def test_run_sql_reports_errors_and_timeouts() -> None:
    con = duckdb.connect()
    rows, seconds = run_sql(con, "SELECT 42", timeout=5)
    assert rows == [(42,)] and seconds >= 0
    with pytest.raises(GoldQueryError):
        run_sql(con, "SELECT * FROM missing_table", timeout=5)
    with pytest.raises(GoldQueryError, match="slower than"):
        run_sql(con, "SELECT sum(i * i) FROM range(100000000000) t(i)", timeout=0.2)
