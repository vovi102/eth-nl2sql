from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from conftest import (
    ALICE,
    BOB,
    FAKE,
    ROUTER,
    TOKEN,
    UINT256_MAX,
    WEI_24,
    addr,
    h,
    month_tables,
    ts,
    write_month,
    write_sums,
)

from eth_nl2sql.ingest.bigquery import Window
from eth_nl2sql.ingest.duckdb_build import (
    DatabaseError,
    load_knowledge_base,
    report_path,
    verify_database,
)
from eth_nl2sql.knowledge import entities as knowledge_base


def query(db: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.sql(sql).fetchall()


def test_build_writes_database_and_report(db: Path) -> None:
    report = json.loads(report_path(db).read_text())
    assert {t: info["rows"] for t, info in report["tables"].items()} == {
        "blocks": 3,
        "transactions": 3,
        "token_transfers": 4,
        "contracts": 2,
        "tokens": 2,
        "amended_tokens": 2,
    }
    assert all(n == 0 for n in report["foreign_key_violations"].values())
    assert "transactions/transactions_2026-09-01.parquet" in report["sources"]
    assert not db.with_name(db.name + ".building").exists()


def test_main_keeps_bigquery_columns_and_values(db: Path) -> None:
    types = dict(
        query(
            db,
            "SELECT table_name || '.' || column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = current_database() AND schema_name = 'main'",
        )
    )
    assert types["transactions.value"] == "DECIMAL(38,0)"
    assert types["transactions.block_timestamp"] == "TIMESTAMP"
    assert types["token_transfers.value"] == "VARCHAR"  # as in BigQuery: up to 78 digits
    assert types["tokens.decimals"] == "VARCHAR"
    assert query(db, "SELECT value FROM token_transfers ORDER BY block_number, log_index") == [
        ("0",),
        ("71650676",),
        (UINT256_MAX,),
        ("5",),
    ]
    assert query(db, f"SELECT value FROM transactions WHERE hash = '{h(2)}'") == [
        (Decimal(WEI_24),)
    ]
    # UTC wall-clock time, whatever the time zone of the machine
    assert query(db, "SELECT min(block_timestamp) FROM transactions") == [(datetime(2026, 9, 1),)]
    assert query(db, "SELECT count(*) FROM transactions WHERE to_address IS NULL") == [(1,)]


def test_link_holds_only_reviewed_rows(db: Path) -> None:
    assert query(db, "SELECT entity_id, classes FROM link.entities") == [("swapper", ["dex"])]
    assert query(db, "SELECT address, is_contract FROM link.entity_addresses") == [(ROUTER, True)]
    aliases = query(db, "SELECT alias, target_type FROM link.entity_aliases ORDER BY alias")
    assert aliases == [("DEX", "class"), ("Swapper", "entity"), ("USDT", "token")]
    # The real token is called by two wallets, the copycat by none.
    assert query(db, "SELECT address, callers, transfers FROM link.token_stats") == [
        (TOKEN, 2, 4),
        (FAKE, 0, 0),
    ]
    assert query(db, "SELECT sense_id, table_name FROM link.property_senses") == [
        ("balance", []),
        ("eth_amount", ["transactions"]),
        ("received_by", ["transactions", "token_transfers"]),
        ("token_amount", ["token_transfers"]),
    ]
    assert query(db, "SELECT phrase_norm, sense_id FROM link.property_phrases") == [
        ("bao nhieu eth", "eth_amount"),
        ("nhan", "received_by"),
        ("so du", "balance"),
    ]


def test_meta_describes_every_column(db: Path) -> None:
    n_columns = query(
        db,
        "SELECT count(*) FROM duckdb_columns() "
        "WHERE database_name = current_database() AND schema_name = 'main'",
    )
    assert query(db, "SELECT count(*) FROM meta.columns") == n_columns
    assert query(
        db,
        "SELECT description_source FROM meta.columns "
        "WHERE table_name = 'transactions' AND column_name = 'blob_count'",
    ) == [("project",)]
    assert query(
        db,
        "SELECT examples FROM meta.columns WHERE table_name = 'tokens' AND column_name = 'symbol'",
    ) == [(["USDT"],)]
    assert query(
        db,
        "SELECT comment FROM duckdb_columns() "
        "WHERE table_name = 'token_transfers' AND column_name = 'value'",
    ) == [("BigQuery says: token_transfers.value",)]
    assert dict(query(db, "SELECT key, value FROM meta.build"))["window_start"] == "2026-09-01"


def test_verify_passes_then_catches_a_changed_value(db: Path, month_dir: Path) -> None:
    verify_database(db, month_dir)
    with duckdb.connect(str(db)) as con:
        # Same number of digits, so only the fingerprint can tell.
        con.execute(f"UPDATE transactions SET value = value + 1 WHERE hash = '{h(3)}'")
    with pytest.raises(DatabaseError, match="transactions: fingerprint differs"):
        verify_database(db, month_dir)


def test_build_refuses_changed_parquet(month_dir: Path, build, tmp_path: Path) -> None:
    path = month_dir / "tokens" / "tokens_2026-09.parquet"
    pq.write_table(pq.read_table(path).slice(0, 1), path)
    with pytest.raises(DatabaseError, match="SHA256 mismatch"):
        build(month_dir, tmp_path / "x.duckdb")
    assert not (tmp_path / "x.duckdb").exists()


def test_build_refuses_data_that_differs_from_bigquery(
    month_dir: Path, build, tmp_path: Path
) -> None:
    parity = json.loads((month_dir / "parity.json").read_text())
    parity["tables"]["transactions"]["2026-09-02"]["row_count"] += 1
    (month_dir / "parity.json").write_text(json.dumps(parity))
    with pytest.raises(DatabaseError, match=r"transactions: parity .* \['2026-09-02'\]"):
        build(month_dir, tmp_path / "x.duckdb")


def test_build_refuses_dangling_transfers(build, tmp_path: Path) -> None:
    month_dir = write_month(tmp_path / "raw", month_tables(extra_transfer=True))
    with pytest.raises(DatabaseError, match="transaction_hash -> transactions.hash: 1 dangling"):
        build(month_dir, tmp_path / "x.duckdb")


def test_build_needs_a_description_for_every_column(
    month_dir: Path, schema_file: Path, build, tmp_path: Path
) -> None:
    schema = json.loads(schema_file.read_text())
    del schema["tables"]["blocks"]["columns"]["miner"]
    schema_file.write_text(json.dumps(schema))
    with pytest.raises(DatabaseError, match=r"no description for \['blocks.miner'\]"):
        build(month_dir, tmp_path / "x.duckdb")


def test_load_knowledge_base_reloads_only_link(
    db: Path, knowledge_base_dir: Path, property_senses_dir: Path
) -> None:
    path = knowledge_base_dir / knowledge_base.ENTITIES.file
    rows = knowledge_base.read_sheet(path)
    for r in rows:
        if r["entity_id"] == "maybe":
            r.update(status="approved", address_set="open")
    knowledge_base.write_sheet(path, knowledge_base.ENTITIES.columns, rows)

    counts = load_knowledge_base(db, knowledge_base_dir, property_senses_dir)
    assert counts["entities"] == 2
    assert query(db, f"SELECT entity_id FROM link.entity_addresses WHERE address = '{BOB}'") == [
        ("maybe",)
    ]
    assert json.loads(report_path(db).read_text())["link"]["entities"] == 2
    assert query(db, "SELECT count(*) FROM transactions") == [(3,)]


# ---- several months --------------------------------------------------------------------

NEXT_WINDOW = Window(date(2026, 9, 3), date(2026, 9, 5))  # follows the fixture's days 1-2
NEW_TOKEN = addr(3)


def next_month_tables(router_is_erc20: bool = False) -> dict[str, dict]:
    """Days 3-4, after the fixture month: blocks 103-104 and two transactions. The extract
    sees TOKEN and ROUTER again (same contract rows, TOKEN with a newer total supply), and a
    new token; the copycat FAKE is not transferred."""
    t = month_tables()

    def put(table: str, n: int, **values: list) -> None:
        for col, (typ, old) in t[table].items():
            t[table][col] = (typ, values.get(col, old[:n]))

    put(
        "blocks",
        2,
        number=[103, 104],
        hash=[h(103), h(104)],
        parent_hash=[h(102), h(103)],
        timestamp=[ts(3), ts(4)],
        transaction_count=[1, 1],
    )
    put(
        "transactions",
        2,
        hash=[h(4), h(5)],
        from_address=[ALICE, BOB],
        to_address=[ROUTER, TOKEN],
        receipt_contract_address=[None, None],
        block_number=[103, 104],
        block_timestamp=[ts(3), ts(4)],
    )
    put(
        "token_transfers",
        2,
        transaction_hash=[h(4), h(5)],
        log_index=[0, 0],
        token_address=[NEW_TOKEN, TOKEN],
        from_address=[ROUTER, BOB],
        to_address=[ALICE, ALICE],
        value=["9", "7"],
        block_number=[103, 104],
        block_timestamp=[ts(3), ts(4)],
    )
    created = t["contracts"]["block_timestamp"][1][:2] + [datetime(2025, 1, 1, tzinfo=timezone.utc)]
    put(
        "contracts",
        3,
        address=[TOKEN, ROUTER, NEW_TOKEN],
        is_erc20=[True, router_is_erc20, True],
        is_erc721=[False] * 3,
        block_number=[4634748, 100, 50],
        block_timestamp=created,
    )
    first = {col: values[0] for col, (_, values) in t["tokens"].items()}
    rows = [
        first | {"total_supply": "200000000000000"},
        first | {"address": NEW_TOKEN, "symbol": "NEW", "name": "New"},
    ]
    put("tokens", 2, **{col: [r[col] for r in rows] for col in first})
    put(
        "amended_tokens",
        2,
        address=[TOKEN, NEW_TOKEN],
        symbol=["USDT", "NEW"],
        name=["Tether USD", "New"],
        decimals=["6", "18"],
    )
    return t


@pytest.fixture
def next_month_dir(tmp_path: Path) -> Path:
    return write_month(tmp_path / "raw-next", next_month_tables(), NEXT_WINDOW)


def test_two_months_build_one_database(
    month_dir: Path, next_month_dir: Path, build, tmp_path: Path
) -> None:
    db = tmp_path / "two.duckdb"
    report = build([next_month_dir, month_dir], db)  # any order: sorted by window
    assert {t: info["rows"] for t, info in report["tables"].items()} == {
        "blocks": 5,
        "transactions": 5,
        "token_transfers": 6,
        "contracts": 3,  # TOKEN and ROUTER are seen by both extracts
        "tokens": 3,
        "amended_tokens": 3,
    }
    assert "raw-next/tokens/tokens_2026-09.parquet" in report["sources"]
    assert "raw/tokens/tokens_2026-09.parquet" in report["sources"]
    # A token seen by both extracts keeps the newest row; the copycat keeps its only one.
    assert query(db, "SELECT address, total_supply FROM tokens ORDER BY address") == [
        (TOKEN, "200000000000000"),
        (FAKE, "1"),
        (NEW_TOKEN, "100000000000000"),
    ]
    build_rows = dict(query(db, "SELECT key, value FROM meta.build"))
    assert (build_rows["window_start"], build_rows["window_end_exclusive"]) == (
        "2026-09-01",
        "2026-09-05",
    )
    assert query(db, "SELECT description FROM meta.tables WHERE table_name = 'tokens'") == [
        (
            "Metadata of every token with a transfer in September 2026, as of the latest "
            "extraction (tokens_latest).",
        )
    ]
    verify_database(db, [month_dir, next_month_dir])


def test_months_must_follow_each_other(month_dir: Path, build, tmp_path: Path) -> None:
    later = write_month(
        tmp_path / "later", next_month_tables(), Window(date(2026, 9, 4), date(2026, 9, 6))
    )
    with pytest.raises(DatabaseError, match="months must follow each other"):
        build([month_dir, later], tmp_path / "x.duckdb")


def test_contract_rows_that_differ_between_months_stop_the_build(
    month_dir: Path, build, tmp_path: Path
) -> None:
    clash = write_month(tmp_path / "clash", next_month_tables(router_is_erc20=True), NEXT_WINDOW)
    with pytest.raises(DatabaseError, match="contracts: 1 keys with different rows"):
        build([month_dir, clash], tmp_path / "x.duckdb")
    assert not (tmp_path / "x.duckdb").exists()


def test_one_month_in_a_list_builds_the_same_tables(
    month_dir: Path, db: Path, build, tmp_path: Path
) -> None:
    again = build([month_dir], tmp_path / "again.duckdb")
    assert again["tables"] == json.loads(report_path(db).read_text())["tables"]
    assert "transactions/transactions_2026-09-01.parquet" in again["sources"]


def test_checksums_cover_every_file(month_dir: Path) -> None:
    (month_dir / "SHA256SUMS").write_text("")
    write_sums(month_dir)
    lines = (month_dir / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 1 + 2 + 2 + 1 + 1 + 1
