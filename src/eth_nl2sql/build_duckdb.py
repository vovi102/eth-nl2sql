"""Build the DuckDB database from one month of Parquet and verify it against the extract.

Three schemas:

- main: the month's tables with the names, columns and values of the extract, i.e. of
  bigquery-public-data.crypto_ethereum. Only timestamps (UTC TIMESTAMP) and
  transactions.value (BigQuery NUMERIC, DECIMAL(38,0)) get native types; token amounts stay
  text, as in BigQuery, because they reach 78 digits. Gold SQL only touches these tables.
- link: the reviewed entity knowledge base (data/entity_knowledge_base), per-token activity
  and the property-sense dictionary (data/schema/property_*.csv), the input of linking. It
  can be reloaded without rebuilding main.
- meta: BigQuery's column descriptions, two example values per column, build provenance.

The build writes a new file and replaces the database only when every check passes:

- the Parquet files match SHA256SUMS;
- every parity.json aggregate, recomputed on the stored tables with the SQL BigQuery ran,
  equals BigQuery's value (row and key counts, sums, per day);
- each table's fingerprint (row count + sum of row hashes) equals its Parquet files', with
  numbers compared as the text they were extracted as, so no value changed, vanished or
  doubled on the way in;
- no dangling foreign keys: transactions -> blocks, token_transfers -> blocks and transactions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa

from eth_nl2sql import entity_knowledge_base as knowledge_base
from eth_nl2sql import property_senses
from eth_nl2sql.bigquery_schema import SOURCE_TABLES, TABLE_DESCRIPTIONS, column_descriptions
from eth_nl2sql.extract_bigquery import SPECS, Window, _files, duckdb_parity

log = logging.getLogger(__name__)

# Stored in this order so DuckDB's zone maps can skip row groups by block and time.
ORDER_BY = {
    "blocks": ("number",),
    "transactions": ("block_number", "transaction_index"),
    "token_transfers": ("block_number", "log_index"),
    "contracts": ("address", "block_number"),
    "tokens": ("address",),
    "amended_tokens": ("address",),
}
# BigQuery NUMERIC columns, extracted as text to keep every digit.
NUMERIC_COLUMNS = {("transactions", "value"): "DECIMAL(38,0)"}
FOREIGN_KEYS = (
    ("transactions", "block_number", "blocks", "number"),
    ("token_transfers", "block_number", "blocks", "number"),
)
EXAMPLE_ROWS = 200_000  # example values come from the first rows of each table


class DatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # stored type
    load: str  # SQL computing it from the Parquet column
    raw: str  # Parquet side of the fingerprint
    stored: str  # database side of the fingerprint


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _read(paths: list[Path]) -> str:
    return "read_parquet([" + ", ".join(_lit(str(p)) for p in paths) + "])"


def _connect(path: Path, memory_limit: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    # The session time zone decides how TIMESTAMP WITH TIME ZONE becomes TIMESTAMP.
    config = {"memory_limit": memory_limit, "TimeZone": "UTC"}
    return duckdb.connect(str(path), read_only=read_only, config=config)


def month_window(raw_dir: Path) -> Window:
    window = json.loads((Path(raw_dir) / "manifest.json").read_text())["window"]
    return Window(date.fromisoformat(window["start"]), date.fromisoformat(window["end_exclusive"]))


def _table_files(raw_dir: Path, window: Window) -> dict[str, list[Path]]:
    return {s.name: [p for _, p in sorted(_files(s, raw_dir, window).items())] for s in SPECS}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def check_sources(raw_dir: Path, window: Window) -> dict[str, dict]:
    """Every Parquet file of the month exists and matches SHA256SUMS."""
    expected = {}
    for line in (raw_dir / "SHA256SUMS").read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            expected[name.lstrip("*")] = digest
    paths = [p for files in _table_files(raw_dir, window).values() for p in files]
    names = [str(p.relative_to(raw_dir)) for p in paths]
    missing = [n for n, p in zip(names, paths, strict=True) if not p.is_file() or n not in expected]
    if missing:
        raise DatabaseError(f"missing from {raw_dir} or SHA256SUMS: {missing}")
    log.info("checksum %d files", len(paths))
    with ThreadPoolExecutor(8) as pool:
        digests = list(pool.map(_sha256, paths))
    bad = [n for n, d in zip(names, digests, strict=True) if d != expected[n]]
    if bad:
        raise DatabaseError(f"SHA256 mismatch: {bad}")
    return {
        n: {"bytes": p.stat().st_size, "sha256": d}
        for n, p, d in zip(names, paths, digests, strict=True)
    }


def _columns(con: duckdb.DuckDBPyConnection, table: str, source: str) -> list[Column]:
    columns = []
    for name, parquet_type, *_ in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall():
        q = _q(name)
        if (table, name) in NUMERIC_COLUMNS:
            t = NUMERIC_COLUMNS[table, name]
            # Compared as text: the stored number must print back to the extracted digits.
            columns.append(Column(name, t, f"CAST({q} AS {t})", q, f"CAST({q} AS VARCHAR)"))
        elif parquet_type == "TIMESTAMP WITH TIME ZONE":
            # UTC wall-clock time, which is what BigQuery's TIMESTAMP shows.
            ts = f"CAST({q} AS TIMESTAMP)"
            columns.append(Column(name, "TIMESTAMP", ts, ts, q))
        else:
            columns.append(Column(name, parquet_type, q, q, q))
    return columns


def _load(con: duckdb.DuckDBPyConnection, table: str, files: list[Path]) -> list[Column]:
    columns = _columns(con, table, _read(files))
    ddl = ", ".join(f"{_q(c.name)} {c.type}" for c in columns)
    exprs = ", ".join(f"{c.load} AS {_q(c.name)}" for c in columns)
    order = ", ".join(map(_q, ORDER_BY[table]))
    con.execute(f"CREATE TABLE main.{_q(table)} ({ddl})")
    for path in files:  # one day at a time keeps every sort small
        log.info("load %s", path.name)
        con.execute(
            f"INSERT INTO main.{_q(table)} SELECT {exprs} FROM {_read([path])} ORDER BY {order}"
        )
    return columns


def _verify(con: duckdb.DuckDBPyConnection, raw_dir: Path) -> dict:
    """Fingerprints, BigQuery parity and foreign keys of the main schema."""
    window = month_window(raw_dir)
    expected = json.loads((raw_dir / "parity.json").read_text())["tables"]
    problems: list[str] = []
    tables: dict[str, dict] = {}
    for spec in SPECS:
        log.info("verify %s", spec.name)
        files = _table_files(raw_dir, window)[spec.name]
        columns = _columns(con, spec.name, _read(files))
        raw = con.execute(
            f"SELECT count(*), sum(hash({', '.join(c.raw for c in columns)})::HUGEINT) "
            f"FROM {_read(files)}"
        ).fetchone()
        stored = con.execute(
            f"SELECT count(*), sum(hash({', '.join(c.stored for c in columns)})::HUGEINT) "
            f"FROM main.{_q(spec.name)}"
        ).fetchone()
        if raw != stored:
            problems.append(
                f"{spec.name}: fingerprint differs from its Parquet files "
                f"({stored[0]} rows stored, {raw[0]} in Parquet)"
            )

        # The parity SQL is the text BigQuery ran, so numbers go back to the extracted text.
        numeric = [c.name for c in columns if (spec.name, c.name) in NUMERIC_COLUMNS]
        source = f"main.{_q(spec.name)}"
        if numeric:
            replace = ", ".join(f"CAST({_q(n)} AS VARCHAR) AS {_q(n)}" for n in numeric)
            source = f"(SELECT * REPLACE ({replace}) FROM {source})"
        got = duckdb_parity(con, spec, source)
        if got != expected[spec.name]:
            days = sorted(
                d
                for d in set(got) | set(expected[spec.name])
                if got.get(d) != expected[spec.name].get(d)
            )
            problems.append(f"{spec.name}: parity aggregates differ from BigQuery on {days}")
        tables[spec.name] = {"rows": stored[0], "fingerprint": str(stored[1])}

    foreign_keys = {}
    for child, col, parent, pcol in FOREIGN_KEYS:
        foreign_keys[f"{child}.{col} -> {parent}.{pcol}"] = con.execute(
            f"SELECT count(*) FROM main.{_q(child)} c WHERE NOT EXISTS "
            f"(SELECT 1 FROM main.{_q(parent)} p WHERE p.{_q(pcol)} = c.{_q(col)})"
        ).fetchone()[0]
    # Day by day, so the join only ever holds one day of transactions.
    dangling = 0
    for day in window.days:
        dangling += con.execute(
            """SELECT count(*) FROM main.token_transfers tt
            WHERE CAST(tt.block_timestamp AS DATE) = ? AND NOT EXISTS (
              SELECT 1 FROM main.transactions tx
              WHERE CAST(tx.block_timestamp AS DATE) = ?
                AND tx.block_number = tt.block_number AND tx.hash = tt.transaction_hash)""",
            [day, day],
        ).fetchone()[0]
    foreign_keys["token_transfers.transaction_hash -> transactions.hash"] = dangling
    problems += [f"{fk}: {n} dangling rows" for fk, n in foreign_keys.items() if n]

    if problems:
        raise DatabaseError("verification failed:\n  " + "\n  ".join(problems))
    return {"tables": tables, "foreign_key_violations": foreign_keys}


def _register(con: duckdb.DuckDBPyConnection, name: str, rows: list[dict], columns: list[str]):
    data = {c: [r.get(c, "") for r in rows] for c in columns}
    con.register(name, pa.table(data, schema=pa.schema([(c, pa.string()) for c in columns])))


def _split(col: str) -> str:
    """A '|'-joined CSV cell as a list; an empty cell is an empty list."""
    return f"CASE WHEN {col} = '' THEN []::VARCHAR[] ELSE string_split({col}, '|') END AS {col}"


def _usable_senses(con: duckdb.DuckDBPyConnection, senses_dir: Path) -> property_senses.Senses:
    """Approved senses, refused unless every expression binds against the main tables."""
    problems = property_senses.check(senses_dir)
    use = property_senses.usable(senses_dir)
    for sense in use.senses:
        for table in filter(None, sense["table_name"].split("|")):
            try:  # EXPLAIN binds names and types without reading any data
                con.execute("EXPLAIN " + property_senses.probe_sql(sense, table))
            except duckdb.Error as e:
                problems.append(f"{sense['sense_id']} on {table}: {e}")
    if problems:
        raise DatabaseError("property senses:\n  " + "\n  ".join(problems))
    return use


def load_link(
    con: duckdb.DuckDBPyConnection, knowledge_base_dir: Path, property_senses_dir: Path
) -> dict[str, int]:
    """(Re)create the link schema from the reviewed knowledge base and property senses."""
    senses = _usable_senses(con, property_senses_dir)  # before dropping the old link schema
    use = knowledge_base.usable(knowledge_base_dir)
    con.execute("DROP SCHEMA IF EXISTS link CASCADE")
    con.execute("CREATE SCHEMA link")
    _register(
        con,
        "kb_classes",
        use.classes,
        ["class_id", "name_en", "name_vi", "eth_labels_slugs", "description"],
    )
    con.execute(
        f"""CREATE TABLE link.entity_classes AS SELECT class_id, name_en, name_vi,
          {_split("eth_labels_slugs")}, description FROM kb_classes ORDER BY class_id"""
    )
    _register(
        con,
        "kb_entities",
        use.entities,
        ["entity_id", "canonical_id", "name", "parent_id", "kind", "classes", "address_set"],
    )
    con.execute(
        f"""CREATE TABLE link.entities AS SELECT entity_id, canonical_id, name,
          nullif(parent_id, '') AS parent_id, kind, {_split("classes")}, address_set
        FROM kb_entities ORDER BY entity_id"""
    )
    _register(
        con,
        "kb_addresses",
        use.addresses,
        ["entity_id", "canonical_id", "address", "name_tag", "role", "classes", "is_contract"],
    )
    con.execute(
        f"""CREATE TABLE link.entity_addresses AS SELECT entity_id, canonical_id, address,
          name_tag, role, {_split("classes")},
          CASE is_contract WHEN 'true' THEN true WHEN 'false' THEN false END AS is_contract
        FROM kb_addresses ORDER BY entity_id, address"""
    )
    _register(
        con,
        "kb_aliases",
        use.aliases,
        ["alias", "alias_norm", "lang", "alias_type", "target_type", "target_id", "source"],
    )
    con.execute(
        """CREATE TABLE link.entity_aliases AS SELECT * FROM kb_aliases
        ORDER BY alias_norm, target_type, target_id"""
    )
    _register(
        con,
        "senses",
        senses.senses,
        ["sense_id", "kind", "applies_to", "table_name", "expression", "unit", "evidence_hint",
         "description_vi"],
    )  # fmt: skip
    con.execute(
        f"""CREATE TABLE link.property_senses AS SELECT sense_id, kind, applies_to,
          {_split("table_name")}, nullif(expression, '') AS expression, nullif(unit, '') AS unit,
          nullif(evidence_hint, '') AS evidence_hint, description_vi FROM senses
          ORDER BY sense_id"""
    )
    _register(con, "phrases", senses.phrases, ["phrase", "phrase_norm", "lang", "sense_id"])
    con.execute("CREATE TABLE link.property_phrases AS SELECT * FROM phrases ORDER BY phrase_norm")
    for name in ("kb_classes", "kb_entities", "kb_addresses", "kb_aliases", "senses", "phrases"):
        con.unregister(name)

    # Activity prior for ranking tokens that share a symbol: copycats are called by almost
    # nobody, the real token by millions of distinct wallets.
    log.info("link: token_stats")
    con.execute(
        """CREATE TABLE link.token_stats AS
        WITH calls AS (
          SELECT to_address AS address, count(*) AS calls, count(DISTINCT from_address) AS callers
          FROM main.transactions WHERE to_address IN (SELECT address FROM main.tokens)
          GROUP BY 1),
        transfers AS (
          SELECT token_address AS address, count(*) AS transfers
          FROM main.token_transfers GROUP BY 1)
        SELECT t.address, t.symbol, t.name, t.decimals_int AS decimals, t.token_standard,
          coalesce(c.callers, 0) AS callers, coalesce(c.calls, 0) AS calls,
          coalesce(x.transfers, 0) AS transfers
        FROM main.tokens t LEFT JOIN calls c USING (address) LEFT JOIN transfers x USING (address)
        ORDER BY callers DESC, address"""
    )
    tables = con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() "
        "AND schema_name = 'link' ORDER BY table_name"
    ).fetchall()
    return {t: con.execute(f"SELECT count(*) FROM link.{t}").fetchone()[0] for (t,) in tables}


def _load_meta(con: duckdb.DuckDBPyConnection, schema_file: Path) -> dict[str, int]:
    descriptions = column_descriptions(schema_file)
    columns = con.execute(
        """SELECT table_name, column_name, column_index, data_type FROM duckdb_columns()
        WHERE database_name = current_database() AND schema_name = 'main'
        ORDER BY table_name, column_index"""
    ).fetchall()
    missing = [f"{t}.{c}" for t, c, _, _ in columns if (t, c) not in descriptions]
    if missing:
        raise DatabaseError(f"no description for {missing} in {schema_file}")

    con.execute("CREATE SCHEMA meta")
    con.execute(
        "CREATE TABLE meta.tables (table_name VARCHAR, bigquery_source VARCHAR, "
        "description VARCHAR)"
    )
    con.execute(
        "CREATE TABLE meta.columns (table_name VARCHAR, column_name VARCHAR, ordinal INTEGER, "
        "data_type VARCHAR, description VARCHAR, description_source VARCHAR, "
        "examples VARCHAR[])"
    )
    for table in SOURCE_TABLES:
        description = TABLE_DESCRIPTIONS[table]
        con.execute(
            "INSERT INTO meta.tables VALUES (?, ?, ?)",
            [table, f"bigquery-public-data.crypto_ethereum.{SOURCE_TABLES[table]}", description],
        )
        con.execute(f"COMMENT ON TABLE main.{_q(table)} IS {_lit(description)}")
        order = ", ".join(map(_q, ORDER_BY[table]))
        con.execute(
            f"CREATE TEMP TABLE sample AS SELECT * FROM main.{_q(table)} "
            f"ORDER BY {order} LIMIT {EXAMPLE_ROWS}"
        )
        for t, column, ordinal, data_type in columns:
            if t != table:
                continue
            text, source = descriptions[table, column]
            # The two most frequent values among the first rows; ties broken by value.
            examples = [
                v
                for (v,) in con.execute(
                    f"""SELECT CAST({_q(column)} AS VARCHAR) AS v FROM sample
                    WHERE {_q(column)} IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC, v LIMIT 2"""
                ).fetchall()
            ]
            con.execute(
                "INSERT INTO meta.columns VALUES (?, ?, ?, ?, ?, ?, ?)",
                [table, column, ordinal, data_type, text, source, examples],
            )
            con.execute(f"COMMENT ON COLUMN main.{_q(table)}.{_q(column)} IS {_lit(text)}")
        con.execute("DROP TABLE sample")
    return {"tables": len(SOURCE_TABLES), "columns": len(columns)}


def report_path(db_path: Path) -> Path:
    return Path(db_path).with_suffix(".report.json")


def build_database(
    raw_dir: Path,
    db_path: Path,
    *,
    knowledge_base_dir: Path,
    schema_file: Path,
    property_senses_dir: Path,
    verify_checksums: bool = True,
    memory_limit: str = "4GB",
) -> dict:
    """Load one month into a fresh DuckDB file; it replaces db_path only if every check passes."""
    raw_dir, db_path = Path(raw_dir), Path(db_path)
    window = month_window(raw_dir)
    sources = check_sources(raw_dir, window) if verify_checksums else {}

    db_path.parent.mkdir(parents=True, exist_ok=True)
    building = db_path.with_name(db_path.name + ".building")
    building.unlink(missing_ok=True)
    try:
        with _connect(building, memory_limit) as con:
            for table, files in _table_files(raw_dir, window).items():
                _load(con, table, files)
            report = _verify(con, raw_dir)
            report["meta"] = _load_meta(con, schema_file)
            report["link"] = load_link(con, knowledge_base_dir, property_senses_dir)
            con.execute("CREATE TABLE meta.build (key VARCHAR, value VARCHAR)")
            provenance = {
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "duckdb_version": duckdb.__version__,
                "raw_dir": str(raw_dir),
                "window_start": window.start.isoformat(),
                "window_end_exclusive": window.end.isoformat(),
            }
            con.executemany("INSERT INTO meta.build VALUES (?, ?)", list(provenance.items()))
    except BaseException:
        building.unlink(missing_ok=True)
        raise
    os.replace(building, db_path)

    report = {**provenance, "sources": sources, **report}
    report_path(db_path).write_text(json.dumps(report, indent=2) + "\n")
    return report


def verify_database(db_path: Path, raw_dir: Path, *, memory_limit: str = "4GB") -> dict:
    """Re-run the fingerprint, parity and foreign-key checks against an existing database."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise DatabaseError(f"no database at {db_path}")
    with _connect(db_path, memory_limit, read_only=True) as con:
        return _verify(con, Path(raw_dir))


def load_knowledge_base(
    db_path: Path,
    knowledge_base_dir: Path,
    property_senses_dir: Path,
    *,
    memory_limit: str = "4GB",
) -> dict[str, int]:
    """Reload only the link schema, after another review batch."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise DatabaseError(f"no database at {db_path}")
    with _connect(db_path, memory_limit) as con:
        counts = load_link(con, knowledge_base_dir, property_senses_dir)
    path = report_path(db_path)
    if path.is_file():
        report = json.loads(path.read_text())
        report["link"] = counts
        report["link_loaded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.write_text(json.dumps(report, indent=2) + "\n")
    return counts
