"""A two-day month in the layout of data/raw/2026-09, seeded with edge cases.

Every table has the columns and Parquet types of the real extract. parity.json is computed
by DuckDB on the files themselves (BigQuery computes it for a real month), so a correct
build has to reproduce it exactly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eth_nl2sql import entity_knowledge_base as knowledge_base
from eth_nl2sql import property_senses
from eth_nl2sql.bigquery_schema import SOURCE_TABLES
from eth_nl2sql.build_duckdb import build_database
from eth_nl2sql.extract_bigquery import SPECS, Window, _files, duckdb_parity

WINDOW = Window(date(2026, 9, 1), date(2026, 9, 3))
UINT256_MAX = str(2**256 - 1)  # 78 digits: token amounts stay text
WEI_24 = "123456789012345678901234"  # the widest ETH value of the month has 24 digits


def ts(day: int, second: int = 0) -> datetime:
    return datetime(2026, 9, day, 0, 0, second, tzinfo=timezone.utc)


def h(i: int) -> str:
    return "0x" + f"{i:064x}"


def addr(i: int) -> str:
    return "0x" + f"{i:040x}"


TOKEN, FAKE, ALICE, BOB, ROUTER = addr(1), addr(2), addr(10), addr(11), addr(20)
STR, INT, BOOL, TS = pa.string(), pa.int64(), pa.bool_(), pa.timestamp("us", tz="UTC")


def _transactions() -> dict:
    n = 3
    return {
        "hash": (STR, [h(1), h(2), h(3)]),
        "nonce": (INT, [0, 1, 0]),
        "transaction_index": (INT, [0, 1, 0]),
        "from_address": (STR, [ALICE, ALICE, BOB]),
        "to_address": (STR, [TOKEN, None, TOKEN]),  # None: contract creation
        "value": (STR, ["0", WEI_24, "1000000000000000000"]),
        "gas": (INT, [84000, 21000, 60000]),
        "gas_price": (INT, [2064002940, 72169328, 1]),
        "max_fee_per_gas": (INT, [3000000000, None, 2]),
        "max_priority_fee_per_gas": (INT, [1000000, None, 1]),
        "transaction_type": (INT, [2, 0, 2]),
        "receipt_status": (INT, [1, 0, 1]),
        "receipt_gas_used": (INT, [29000, 21000, 45000]),
        "receipt_cumulative_gas_used": (INT, [29000, 50000, 45000]),
        "receipt_effective_gas_price": (INT, [2064002940, 72169328, 1]),
        "receipt_contract_address": (STR, [None, ROUTER, None]),
        "max_fee_per_blob_gas": (INT, [None] * n),
        "receipt_blob_gas_price": (INT, [None] * n),
        "receipt_blob_gas_used": (INT, [None] * n),
        "blob_count": (INT, [0] * n),
        "block_number": (INT, [100, 100, 102]),
        "block_timestamp": (TS, [ts(1), ts(1), ts(2)]),
    }


def _token_transfers() -> dict:
    values = ["0", "71650676", UINT256_MAX, "5"]
    return {
        "transaction_hash": (STR, [h(1), h(1), h(1), h(3)]),
        "log_index": (INT, [0, 1, 2, 0]),
        "token_address": (STR, [TOKEN] * 4),
        "from_address": (STR, [ALICE, ALICE, ALICE, BOB]),
        "to_address": (STR, [BOB, BOB, BOB, ALICE]),
        "value": (STR, values),
        "block_number": (INT, [100, 100, 100, 102]),
        "block_timestamp": (TS, [ts(1), ts(1), ts(1), ts(2)]),
    }


def _tokens() -> dict:
    return {
        "address": (STR, [TOKEN, FAKE]),
        "symbol": (STR, ["USDT", "USDT"]),  # the copycat shares the symbol
        "name": (STR, ["Tether USD", "Tether USD"]),
        "decimals": (STR, ["6", "6"]),
        "decimals_int": (INT, [6, 6]),
        "total_supply": (STR, ["100000000000000", "1"]),
        "token_standard": (STR, ["ERC20", None]),
        "token_standards": (STR, ["ERC20", ""]),
        "protocols": (STR, ["erc20", ""]),
        "is_erc20": (BOOL, [True, False]),
        "is_erc721": (BOOL, [False, False]),
        "is_erc1155": (BOOL, [False, False]),
        "implementation_address": (STR, [None, None]),
        "proxy_standard": (STR, [None, None]),
        "creator_address": (STR, [ALICE, BOB]),
        "creation_transaction_hash": (STR, [h(9), h(8)]),
        "block_number": (INT, [4634748, 25000000]),
        "block_timestamp": (TS, [datetime(2017, 11, 28, tzinfo=timezone.utc)] * 2),
    }


def month_tables(extra_transfer: bool = False) -> dict[str, dict]:
    tables = {
        "blocks": {
            "number": (INT, [100, 101, 102]),
            "hash": (STR, [h(100), h(101), h(102)]),
            "parent_hash": (STR, [h(99), h(100), h(101)]),
            "timestamp": (TS, [ts(1), ts(1, 12), ts(2)]),
            "miner": (STR, [addr(30)] * 3),
            "size": (INT, [1000, 500, 900]),
            "gas_limit": (INT, [60000000] * 3),
            "gas_used": (INT, [50000, 0, 45000]),
            "transaction_count": (INT, [2, 0, 1]),
            "base_fee_per_gas": (INT, [1, 1, 1]),
            "blob_gas_used": (INT, [0, 0, 0]),
            "excess_blob_gas": (INT, [0, 0, 0]),
        },
        "transactions": _transactions(),
        "token_transfers": _token_transfers(),
        "contracts": {
            "address": (STR, [TOKEN, ROUTER]),
            "is_erc20": (BOOL, [True, False]),
            "is_erc721": (BOOL, [False, False]),
            "block_number": (INT, [4634748, 100]),
            "block_timestamp": (TS, [datetime(2017, 11, 28, tzinfo=timezone.utc), ts(1)]),
        },
        "tokens": _tokens(),
        "amended_tokens": {
            "address": (STR, [TOKEN, FAKE]),
            "symbol": (STR, ["USDT", "USDT"]),
            "name": (STR, ["Tether USD", "Tether USD"]),
            "decimals": (STR, ["6", None]),
        },
    }
    if extra_transfer:  # a transfer whose transaction is missing
        for col, (typ, values) in tables["token_transfers"].items():
            extra = {"transaction_hash": h(77), "log_index": 9}.get(col, values[-1])
            tables["token_transfers"][col] = (typ, values + [extra])
    return tables


def write_month(raw_dir: Path, tables: dict[str, dict]) -> Path:
    for spec in SPECS:
        cols = tables[spec.name]
        schema = pa.schema([(c, typ) for c, (typ, _) in cols.items()])
        data = pa.table({c: v for c, (_, v) in cols.items()}, schema=schema)
        for day, path in _files(spec, raw_dir, WINDOW).items():
            rows = data
            if day != "all":
                times = data.column(spec.time_column).to_pylist()
                keep = [t.date().isoformat() == day for t in times]
                rows = data.filter(pa.array(keep))
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(rows, path)

    window = {"start": WINDOW.start.isoformat(), "end_exclusive": WINDOW.end.isoformat()}
    (raw_dir / "manifest.json").write_text(json.dumps({"window": window}))
    parity = {}
    with duckdb.connect(config={"TimeZone": "UTC"}) as con:
        for spec in SPECS:
            files = list(_files(spec, raw_dir, WINDOW).values())
            source = "read_parquet([" + ", ".join(f"'{p}'" for p in files) + "])"
            parity[spec.name] = duckdb_parity(con, spec, source)
    (raw_dir / "parity.json").write_text(json.dumps({"engine": "test", "tables": parity}))
    write_sums(raw_dir)
    return raw_dir


def write_sums(raw_dir: Path) -> None:
    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(raw_dir)}"
        for p in sorted(raw_dir.glob("*/*.parquet"))
    ]
    (raw_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n")


@pytest.fixture
def month_dir(tmp_path: Path) -> Path:
    return write_month(tmp_path / "raw", month_tables())


@pytest.fixture
def schema_file(tmp_path: Path) -> Path:
    tables = {
        name: {
            "source": f"bigquery-public-data.crypto_ethereum.{view}",
            "columns": {
                c: {"type": "", "mode": "", "description": f"BigQuery says: {name}.{c}"}
                for c in month_tables()[name]
            },
        }
        for name, view in SOURCE_TABLES.items()
    }
    path = tmp_path / "bigquery_schema.json"
    path.write_text(json.dumps({"tables": {"fetched_at": "2026-11-01", **tables}}))
    return path


@pytest.fixture
def knowledge_base_dir(tmp_path: Path) -> Path:
    d = tmp_path / "entity_knowledge_base"
    d.mkdir()
    knowledge_base.write_sheet(
        d / knowledge_base.CLASSES_FILE,
        ("class_id", "name_en", "name_vi", "eth_labels_slugs", "description", "status", "note"),
        [
            {
                "class_id": "dex",
                "name_en": "decentralized exchange",
                "name_vi": "sàn phi tập trung",
                "eth_labels_slugs": "dex",
                "status": "approved",
            },
        ],
    )
    entity = dict.fromkeys(knowledge_base.ENTITIES.columns, "")
    knowledge_base.write_sheet(
        d / knowledge_base.ENTITIES.file,
        knowledge_base.ENTITIES.columns,
        [
            entity
            | {
                "entity_id": "swapper",
                "name": "Swapper",
                "kind": "protocol",
                "classes": "dex",
                "address_set": "closed",
                "status": "approved",
            },
            entity | {"entity_id": "maybe", "name": "Maybe", "status": "pending"},
        ],
    )
    address = dict.fromkeys(knowledge_base.ENTITY_ADDRESSES.columns, "")
    knowledge_base.write_sheet(
        d / knowledge_base.ENTITY_ADDRESSES.file,
        knowledge_base.ENTITY_ADDRESSES.columns,
        [
            address
            | {
                "entity_id": "swapper",
                "address": ROUTER,
                "name_tag": "Swapper: Router",
                "role": "Router",
                "classes": "dex",
                "is_contract": "true",
                "status": "auto",
            },
            address
            | {
                "entity_id": "maybe",
                "address": BOB,
                "name_tag": "Maybe 1",
                "role": "Maybe 1",
                "is_contract": "false",
                "status": "auto",
            },
        ],
    )
    alias = dict.fromkeys(knowledge_base.ALIASES.columns, "")
    knowledge_base.write_sheet(
        d / knowledge_base.ALIASES.file,
        knowledge_base.ALIASES.columns,
        [
            alias
            | {
                "alias": a,
                "alias_norm": knowledge_base.normalize_alias(a),
                "lang": "any",
                "alias_type": "name",
                "target_type": tt,
                "target_id": t,
                "status": s,
            }
            for a, tt, t, s in [
                ("Swapper", "entity", "swapper", "auto"),
                ("Maybe", "entity", "maybe", "auto"),  # entity not approved: not loaded
                ("USDT", "token", TOKEN, "auto"),
                ("DEX", "class", "dex", "approved"),
            ]
        ],
    )
    return d


def write_property_senses(d: Path, senses: list[dict], phrases: list[tuple[str, str]]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    blank = dict.fromkeys(property_senses.SENSE_COLUMNS, "")
    knowledge_base.write_sheet(
        d / property_senses.SENSES_FILE,
        property_senses.SENSE_COLUMNS,
        [blank | {"status": "approved"} | s for s in senses],
    )
    knowledge_base.write_sheet(
        d / property_senses.PHRASES_FILE,
        property_senses.PHRASE_COLUMNS,
        [
            {
                "phrase": phrase,
                "phrase_norm": knowledge_base.normalize_alias(phrase),
                "lang": "vi",
                "sense_id": sid,
                "status": "approved",
            }
            for phrase, sid in phrases
        ],
    )
    return d


SENSES = [
    {"sense_id": "eth_amount", "kind": "measure", "applies_to": "eth",
     "table_name": "transactions", "expression": "value / 1e18", "unit": "ETH"},
    {"sense_id": "received_by", "kind": "filter", "applies_to": "address",
     "table_name": "transactions|token_transfers", "expression": "to_address IN {E}",
     "evidence_hint": "from_address is the sender"},
    {"sense_id": "token_amount", "kind": "measure", "applies_to": "token",
     "table_name": "token_transfers",
     "expression": "CAST(value AS DECIMAL(38,0)) / 1e{decimals}"},
    {"sense_id": "balance", "kind": "unsupported", "applies_to": "address"},
    {"sense_id": "draft", "kind": "measure", "table_name": "blocks", "expression": "size",
     "status": "pending"},
]  # fmt: skip
PHRASES = [
    ("bao nhiêu ETH", "eth_amount"),
    ("nhận", "received_by"),
    ("số dư", "balance"),
    ("kích thước", "draft"),  # sense not approved: not loaded
]


@pytest.fixture
def property_senses_dir(tmp_path: Path) -> Path:
    return write_property_senses(tmp_path / "schema", SENSES, PHRASES)


@pytest.fixture
def build(knowledge_base_dir: Path, schema_file: Path, property_senses_dir: Path):
    def run(month_dir: Path, db: Path) -> dict:
        return build_database(
            month_dir,
            db,
            knowledge_base_dir=knowledge_base_dir,
            schema_file=schema_file,
            property_senses_dir=property_senses_dir,
        )

    return run


@pytest.fixture
def db(month_dir: Path, build, tmp_path: Path) -> Path:
    path = tmp_path / "db" / "ethereum.duckdb"
    build(month_dir, path)
    return path
