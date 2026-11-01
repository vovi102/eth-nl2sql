from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eth_nl2sql.extract_bigquery import (
    SPECS,
    ExtractError,
    Window,
    _check_parity,
    _files,
    duckdb_parity,
    verify_extract,
)

WINDOW = Window(date(2026, 9, 1), date(2026, 9, 3))
UINT256_MAX = str(2**256 - 1)


def ts(day: int, second: int = 0) -> datetime:
    return datetime(2026, 9, day, 0, 0, second, tzinfo=timezone.utc)


def h(i: int) -> str:
    return "0x" + f"{i:064x}"


ROWS: dict[str, dict[str, list]] = {
    "blocks": {
        "number": [100, 101, 102],
        "hash": [h(100), h(101), h(102)],
        "timestamp": [ts(1), ts(1, 12), ts(2)],
        "transaction_count": [2, 0, 1],
        "gas_used": [50_000, 0, 21_000],
    },
    "transactions": {
        "hash": [h(1), h(2), h(3)],
        "to_address": ["0xtok", None, "0xaaa"],
        "value": ["0", "123456789012345678901234", "1"],
        "receipt_gas_used": [29_000, 21_000, 21_000],
        "block_number": [100, 100, 102],
        "block_timestamp": [ts(1), ts(1), ts(2)],
    },
    "token_transfers": {
        "transaction_hash": [h(1), h(1), h(3)],
        "log_index": [0, 1, 0],
        "token_address": ["0xtok", "0xtok", "0xtok"],
        "value": ["71650676", UINT256_MAX, "1"],
        "block_number": [100, 100, 102],
        "block_timestamp": [ts(1), ts(1), ts(2)],
    },
    "contracts": {
        "address": ["0xtok"],
        "is_erc20": [True],
        "block_number": [1],
    },
    "tokens": {
        "address": ["0xtok"],
        "symbol": ["TOK"],
        "decimals_int": [6],
    },
    "amended_tokens": {
        "address": ["0xtok"],
        "symbol": ["TOK"],
        "decimals": ["6"],
    },
}


def write_split(out: Path, name: str, columns: dict[str, list]) -> None:
    spec = next(s for s in SPECS if s.name == name)
    table = pa.table(columns)
    for day, path in _files(spec, out, WINDOW).items():
        part = table
        if day != "all":
            days = [t.date().isoformat() for t in columns[spec.time_column]]
            part = table.filter(pa.array([d == day for d in days]))
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(part, path)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    d = tmp_path / "2026-09"
    for name, columns in ROWS.items():
        write_split(d, name, columns)
    # Stand in for BigQuery: the parity SQL is portable, so DuckDB computes the reference.
    tables = {}
    with duckdb.connect(config={"TimeZone": "UTC"}) as con:
        for spec in SPECS:
            files = ", ".join(f"'{p}'" for p in _files(spec, d, WINDOW).values())
            tables[spec.name] = duckdb_parity(con, spec, f"read_parquet([{files}])")
    (d / "parity.json").write_text(json.dumps({"tables": tables}))
    (d / "manifest.json").write_text(
        json.dumps({"window": {"start": "2026-09-01", "end_exclusive": "2026-09-03"}})
    )
    return d


def test_month_window() -> None:
    w = Window.month("2026-09")
    assert (w.start, w.end, w.tag) == (date(2026, 9, 1), date(2026, 10, 1), "202609")
    assert len(w.days) == 30
    assert Window.month("2026-12").end == date(2027, 1, 1)


def test_verify_accepts_matching_files(out: Path) -> None:
    totals = verify_extract(out)
    assert totals == {name: len(next(iter(cols.values()))) for name, cols in ROWS.items()}
    parity = json.loads((out / "parity.json").read_text())["tables"]
    assert set(parity["transactions"]) == {"2026-09-01", "2026-09-02"}
    assert parity["token_transfers"]["2026-09-01"]["sum_value_length"] == 8 + 78


def test_verify_detects_changed_value(out: Path) -> None:
    columns = dict(ROWS["token_transfers"], value=["71650676", UINT256_MAX[:-1], "1"])
    write_split(out, "token_transfers", columns)
    with pytest.raises(ExtractError, match="token_transfers: aggregates differ .*2026-09-01"):
        verify_extract(out)


def test_verify_detects_missing_file(out: Path) -> None:
    (out / "transactions" / "transactions_2026-09-02.parquet").unlink()
    with pytest.raises(ExtractError, match="transactions: missing"):
        verify_extract(out)


def test_checks_catch_duplicates_gaps_and_missing_days() -> None:
    by_name = {s.name: s for s in SPECS}
    dup = {
        "2026-09-01": {"row_count": 3, "key_count": 2},
        "2026-09-02": {"row_count": 1, "key_count": 1},
    }
    assert _check_parity("transactions", by_name["transactions"], WINDOW, dup) == [
        "transactions 2026-09-01: 1 duplicate keys"
    ]
    gap = {
        "2026-09-01": {"row_count": 1, "key_count": 1, "min_number": 100, "max_number": 100},
        "2026-09-02": {"row_count": 1, "key_count": 1, "min_number": 102, "max_number": 102},
    }
    assert _check_parity("blocks", by_name["blocks"], WINDOW, gap) == [
        "blocks: 1 missing block numbers in 100..102"
    ]
    one_day = {"2026-09-01": {"row_count": 1, "key_count": 1}}
    assert (
        "missing days ['2026-09-02']"
        in _check_parity("transactions", by_name["transactions"], WINDOW, one_day)[0]
    )
