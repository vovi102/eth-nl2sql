from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from conftest import PHRASES, SENSES, write_property_senses

from eth_nl2sql import property_senses
from eth_nl2sql.build_duckdb import DatabaseError, load_knowledge_base


def test_check_accepts_the_fixture(property_senses_dir: Path) -> None:
    assert property_senses.check(property_senses_dir) == []
    use = property_senses.usable(property_senses_dir)
    assert {s["sense_id"] for s in use.senses} == {
        "eth_amount",
        "received_by",
        "token_amount",
        "balance",
    }
    assert "kích thước" not in {p["phrase"] for p in use.phrases}  # its sense is pending


def test_check_reports_bad_rows(tmp_path: Path) -> None:
    senses = SENSES + [
        {"sense_id": "eth_amount", "kind": "measure", "table_name": "transactions",
         "expression": "value"},  # duplicate id
        {"sense_id": "odd", "kind": "guess", "table_name": "receipts",
         "expression": "x IN {ADDRESSES}"},
        {"sense_id": "no_sql", "kind": "filter", "table_name": "blocks"},
    ]  # fmt: skip
    d = write_property_senses(tmp_path / "s", senses, PHRASES + [("lạ", "missing")])
    errors = property_senses.check(d)
    assert "property_senses.csv: sense_id 'eth_amount' appears 2 times" in errors
    assert "property_senses.csv: odd: unknown kind 'guess'" in errors
    assert "property_senses.csv: odd: unknown table 'receipts'" in errors
    assert "property_senses.csv: odd: unknown placeholders ['ADDRESSES']" in errors
    assert any(e.startswith("property_senses.csv: no_sql: an expression is needed") for e in errors)
    assert "property_phrases.csv: 'lạ': unknown sense 'missing'" in errors


def test_reload_refuses_an_expression_that_does_not_bind(
    db: Path, tmp_path: Path, knowledge_base_dir: Path
) -> None:
    broken = SENSES + [
        {"sense_id": "fee", "kind": "measure", "table_name": "transactions",
         "expression": "receipt_gas_usd * receipt_effective_gas_price"},  # typo
    ]  # fmt: skip
    d = write_property_senses(tmp_path / "broken", broken, PHRASES)
    with pytest.raises(DatabaseError, match="fee on transactions"):
        load_knowledge_base(db, knowledge_base_dir, d)
    # The link schema loaded by the build is left as it was.
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM link.property_senses").fetchone() == (4,)
