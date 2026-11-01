"""Extract one calendar month of Ethereum from bigquery-public-data.crypto_ethereum.

Work is split into one query per table and day (one per table for the small ones), and
every finished piece is recorded, so an interrupted run resumes where it stopped:

1. All pending queries are dry-run first and the run is refused if they could push the
   bytes billed past the budget; each job also carries maximum_bytes_billed.
2. A query's result lands in BigQuery's anonymous result table. Aggregates of that
   table go to parity.json; the SQL is written in the subset BigQuery and DuckDB share,
   so the same text runs on both.
3. The result table is streamed to Parquet with the Storage Read API. Numeric columns
   wider than INT64 are cast to STRING in BigQuery, so values arrive exactly as stored.
4. The aggregates are recomputed on the Parquet files with DuckDB and must match.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

SOURCE = "bigquery-public-data.crypto_ethereum"
ROW_GROUP_ROWS = 500_000


class ExtractError(RuntimeError):
    pass


@dataclass(frozen=True)
class Spec:
    name: str
    # Placeholders: {src}; {start}, {end} bound the chunk; {month_end}; {month_tx} and
    # {month_tt} are "`table` WHERE <in month>" for transactions and token_transfers.
    sql: str
    key: str  # row identity, as SQL valid in BigQuery and DuckDB; {q} quotes identifiers
    time_column: str | None = None  # parity is per day of this column; None = whole table
    # One query and file per day. Off for blocks: dry-runs of that view ignore partition
    # pruning (6 GB per day estimated, ~60 MB per month billed) and it is small anyway.
    daily: bool = True
    aggs: dict[str, str] = field(default_factory=dict)  # parity aggregates, portable SQL


SPECS: tuple[Spec, ...] = (
    Spec(
        "blocks",
        """
        SELECT number, `hash`, parent_hash, timestamp, miner, size, gas_limit, gas_used,
          transaction_count, base_fee_per_gas, blob_gas_used, excess_blob_gas
        FROM `{src}.blocks`
        WHERE timestamp >= TIMESTAMP '{start}' AND timestamp < TIMESTAMP '{end}'
        """,
        key="number",
        time_column="timestamp",
        daily=False,
        aggs={
            "min_number": "MIN(number)",
            "max_number": "MAX(number)",
            "sum_transaction_count": "SUM(transaction_count)",
            "sum_gas_used": "SUM(gas_used)",
        },
    ),
    Spec(
        "transactions",
        """
        SELECT `hash`, nonce, transaction_index, from_address, to_address,
          CAST(value AS STRING) AS value, gas, gas_price, max_fee_per_gas,
          max_priority_fee_per_gas, transaction_type, receipt_status, receipt_gas_used,
          receipt_cumulative_gas_used, receipt_effective_gas_price, receipt_contract_address,
          max_fee_per_blob_gas, receipt_blob_gas_price, receipt_blob_gas_used,
          ARRAY_LENGTH(blob_versioned_hashes) AS blob_count, block_number, block_timestamp
        FROM `{src}.transactions`
        WHERE block_timestamp >= TIMESTAMP '{start}' AND block_timestamp < TIMESTAMP '{end}'
        """,
        key="{q}hash{q}",
        time_column="block_timestamp",
        aggs={
            "sum_block_number": "SUM(block_number)",
            "sum_receipt_gas_used": "SUM(receipt_gas_used)",
            "sum_value_length": "SUM(LENGTH(value))",
            "count_to_address": "COUNT(to_address)",
        },
    ),
    Spec(
        "token_transfers",
        """
        SELECT transaction_hash, log_index, token_address, from_address, to_address, value,
          block_number, block_timestamp
        FROM `{src}.token_transfers`
        WHERE block_timestamp >= TIMESTAMP '{start}' AND block_timestamp < TIMESTAMP '{end}'
        """,
        key="CONCAT(transaction_hash, ':', CAST(log_index AS STRING))",
        time_column="block_timestamp",
        aggs={
            "sum_block_number": "SUM(block_number)",
            "sum_log_index": "SUM(log_index)",
            "sum_value_length": "SUM(LENGTH(value))",
            "tokens": "COUNT(DISTINCT token_address)",
        },
    ),
    # Contracts the month touched: called, created, or emitting token transfers. An address
    # can be deployed many times (CREATE2 after selfdestruct), one row per deployment.
    Spec(
        "contracts",
        """
        SELECT address, is_erc20, is_erc721, block_number, block_timestamp
        FROM `{src}.contracts`
        WHERE block_timestamp < TIMESTAMP '{month_end}' AND address IN (
          SELECT to_address FROM {month_tx}
          UNION DISTINCT SELECT receipt_contract_address FROM {month_tx}
          UNION DISTINCT SELECT token_address FROM {month_tt})
        """,
        key="CONCAT(address, ':', CAST(block_number AS STRING))",
        aggs={
            "sum_block_number": "SUM(block_number)",
            "erc20": "COUNT(CASE WHEN is_erc20 THEN 1 END)",
        },
    ),
    Spec(
        "tokens",
        """
        SELECT address, symbol, name, decimals, decimals_int, total_supply, token_standard,
          ARRAY_TO_STRING(token_standards, '|') AS token_standards,
          ARRAY_TO_STRING(protocols, '|') AS protocols, is_erc20, is_erc721, is_erc1155,
          implementation_address, proxy_standard, creator_address, creation_transaction_hash,
          block_number, block_timestamp
        FROM `{src}.tokens_latest`
        WHERE address IN (SELECT token_address FROM {month_tt})
        """,
        key="address",
        aggs={
            "count_decimals_int": "COUNT(decimals_int)",
            "sum_decimals_int": "SUM(decimals_int)",
            "count_symbol": "COUNT(symbol)",
        },
    ),
    Spec(
        "amended_tokens",
        """
        SELECT address, symbol, name, decimals
        FROM `{src}.amended_tokens`
        WHERE address IN (SELECT token_address FROM {month_tt})
        """,
        key="address",
        aggs={"count_decimals": "COUNT(decimals)", "count_symbol": "COUNT(symbol)"},
    ),
)

SPEC_BY_NAME = {s.name: s for s in SPECS}


@dataclass(frozen=True)
class Window:
    start: date
    end: date  # exclusive

    @classmethod
    def month(cls, month: str) -> Window:
        start = datetime.strptime(month, "%Y-%m").date()
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return cls(start, end)

    @property
    def days(self) -> list[str]:
        n = (self.end - self.start).days
        return [(self.start + timedelta(days=i)).isoformat() for i in range(n)]

    @property
    def tag(self) -> str:
        return self.start.strftime("%Y%m")


def _parity_sql(spec: Spec, table: str, quote: str) -> str:
    """quote: identifier quote of the engine, '`' for BigQuery and '"' for DuckDB."""
    key = spec.key.format(q=quote)
    day = f"CAST(CAST({spec.time_column} AS DATE) AS STRING)" if spec.time_column else "'all'"
    aggs = "".join(f", {expr} AS {name}" for name, expr in spec.aggs.items())
    return (
        f"SELECT {day} AS day, COUNT(*) AS row_count, COUNT(DISTINCT {key}) AS key_count"
        f"{aggs} FROM {table} GROUP BY 1 ORDER BY 1"
    )


def _check_parity(name: str, spec: Spec, window: Window, rows: dict[str, dict]) -> list[str]:
    """Sanity checks on one table's aggregates that hold wherever they were computed."""
    problems = []
    expected_days = set(window.days) if spec.time_column else {"all"}
    if spec.time_column and set(rows) != expected_days:
        missing, extra = expected_days - set(rows), set(rows) - expected_days
        problems.append(f"{name}: missing days {sorted(missing)}, unexpected {sorted(extra)}")
    for day, r in rows.items():
        if r["row_count"] != r["key_count"]:
            problems.append(f"{name} {day}: {r['row_count'] - r['key_count']} duplicate keys")
    if name == "blocks" and rows:
        lo = min(r["min_number"] for r in rows.values())
        hi = max(r["max_number"] for r in rows.values())
        total = sum(r["row_count"] for r in rows.values())
        if hi - lo + 1 != total:
            problems.append(f"blocks: {hi - lo + 1 - total} missing block numbers in {lo}..{hi}")
    return problems


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.replace(path)


def _files(spec: Spec, out_dir: Path, window: Window) -> dict[str, Path]:
    """Self-describing names: <table>/<table>_<YYYY-MM-DD>.parquet, or _<YYYY-MM> for 'all'."""
    days = window.days if spec.time_column and spec.daily else ["all"]
    month = window.start.strftime("%Y-%m")
    return {
        day: out_dir / spec.name / f"{spec.name}_{month if day == 'all' else day}.parquet"
        for day in days
    }


class Extractor:
    def __init__(self, out_dir: Path, window: Window, project: str | None = None):
        from google.cloud import bigquery

        self.out_dir = Path(out_dir)
        self.window = window
        self.bq = bigquery.Client(project=project)
        self.project = self.bq.project
        self.manifest_path = self.out_dir / "manifest.json"
        self.parity_path = self.out_dir / "parity.json"
        self.manifest = _read_json(self.manifest_path)
        self.parity = _read_json(self.parity_path).get("tables", {})
        start = self.manifest.get("window", {}).get("start")
        if start not in (None, window.start.isoformat()):
            raise ExtractError(f"{self.out_dir} holds a different month; use another --out")
        self._lock = threading.Lock()

    def chunks(self) -> list[tuple[Spec, str]]:
        return [(spec, day) for spec in SPECS for day in _files(spec, self.out_dir, self.window)]

    def render(self, spec: Spec, day: str | None = None) -> str:
        """The query for one day of a time-partitioned table, else for the whole month."""
        start, end = self.window.start, self.window.end
        if day not in (None, "all"):
            start = date.fromisoformat(day)
            end = start + timedelta(days=1)
        month_start = f"{self.window.start} 00:00:00+00"
        month_end = f"{self.window.end} 00:00:00+00"
        in_month = (
            f"WHERE block_timestamp >= TIMESTAMP '{month_start}' "
            f"AND block_timestamp < TIMESTAMP '{month_end}'"
        )
        sql = spec.sql.format(
            src=SOURCE,
            start=f"{start} 00:00:00+00",
            end=f"{end} 00:00:00+00",
            month_end=month_end,
            month_tx=f"`{SOURCE}.transactions` {in_month}",
            month_tt=f"`{SOURCE}.token_transfers` {in_month}",
        )
        return "\n".join(line.strip() for line in sql.strip().splitlines())

    def dry_run(self, sql: str) -> int:
        from google.cloud import bigquery

        job = self.bq.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
        return job.total_bytes_processed

    def plan(self) -> list[tuple[str, int]]:
        """Bytes each table's month query would process (an upper bound for views)."""
        return [(spec.name, self.dry_run(self.render(spec))) for spec in SPECS]

    def billed_so_far(self) -> int:
        return sum(
            j["bytes_billed"] + j["parity_bytes_billed"]
            for j in self.manifest.get("jobs", {}).values()
        )

    def _done(self, spec: Spec, day: str) -> bool:
        path = _files(spec, self.out_dir, self.window)[day]
        if f"{spec.name}/{day}" not in self.manifest.get("jobs", {}) or not path.is_file():
            return False
        return _parquet_rows(path) == self._expected_rows(spec.name, day)

    def _expected_rows(self, name: str, day: str) -> int:
        days = self.parity.get(name, {})
        return sum(r["row_count"] for d, r in days.items() if day == "all" or d == day)

    def run(self, budget_bytes: int, workers: int = 4) -> None:
        from google.cloud import bigquery_storage_v1

        pending = [(s, d) for s, d in self.chunks() if not self._done(s, d)]
        log.info("%d of %d files to extract; dry-running", len(pending), len(self.chunks()))
        with ThreadPoolExecutor(workers) as pool:
            estimates = list(pool.map(lambda c: self.dry_run(self.render(*c)), pending))
        billed = self.billed_so_far()
        if billed + sum(estimates) > budget_bytes:
            raise ExtractError(
                f"remaining queries may process {sum(estimates) / 1e9:.1f} GB, over the budget "
                f"({billed / 1e9:.1f} of {budget_bytes / 1e9:.1f} GB billed so far)"
            )
        log.info("estimate %.1f GB (upper bound)", sum(estimates) / 1e9)

        self._save()
        storage = bigquery_storage_v1.BigQueryReadClient()
        with ThreadPoolExecutor(workers) as pool:
            futures = [
                pool.submit(self._extract_one, storage, spec, day, estimate)
                for (spec, day), estimate in zip(pending, estimates, strict=True)
            ]
            for fut in futures:
                fut.result()

        problems = []
        for spec in SPECS:
            problems += _check_parity(spec.name, spec, self.window, self.parity.get(spec.name, {}))
        if problems:
            raise ExtractError("extracted data failed checks:\n  " + "\n  ".join(problems))

    def _extract_one(self, storage, spec: Spec, day: str, estimate: int) -> None:
        from google.cloud import bigquery

        sql = self.render(spec, day)
        cap = int(estimate * 1.1) + (100 << 20)
        job = self.bq.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap))
        job.result()
        # Results land in an anonymous table; query and stream it rather than staging our own
        # copy, which would not fit the free storage of a sandbox project.
        dest = job.destination
        table = f"{dest.project}.{dest.dataset_id}.{dest.table_id}"
        parity_job = self.bq.query(_parity_sql(spec, f"`{table}`", "`"))
        parity = {r["day"]: {k: v for k, v in r.items() if k != "day"} for r in parity_job}
        if day != "all" and set(parity) - {day}:
            raise ExtractError(f"{spec.name} {day}: query returned rows of {sorted(parity)}")
        expected = sum(r["row_count"] for r in parity.values())
        path = _files(spec, self.out_dir, self.window)[day]
        _download(storage, self.project, table, path, expected)

        with self._lock:
            self.manifest.setdefault("jobs", {})[f"{spec.name}/{day}"] = {
                "job_id": job.job_id,
                "bytes_processed": job.total_bytes_processed,
                "bytes_billed": job.total_bytes_billed,
                "parity_job_id": parity_job.job_id,
                "parity_bytes_billed": parity_job.total_bytes_billed or 0,
                "rows": expected,
                "finished_at": job.ended.isoformat(),
            }
            self.parity.setdefault(spec.name, {}).update(parity)
            self._save()
        log.info("extracted %s/%s (%d rows)", spec.name, day, expected)

    def finish(self) -> None:
        sums = []
        for spec, day in self.chunks():
            path = _files(spec, self.out_dir, self.window)[day]
            sums.append(f"{_sha256(path)}  {path.relative_to(self.out_dir)}")
        (self.out_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        self.manifest["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._save()

    def _save(self) -> None:
        self.manifest.setdefault("source", SOURCE)
        self.manifest.setdefault(
            "window",
            {"start": self.window.start.isoformat(), "end_exclusive": self.window.end.isoformat()},
        )
        self.manifest.setdefault("project", self.project)
        self.manifest["queries"] = {spec.name: self.render(spec) for spec in SPECS}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.manifest_path, self.manifest)
        tables = {name: dict(sorted(days.items())) for name, days in sorted(self.parity.items())}
        _write_json(self.parity_path, {"engine": "bigquery", "tables": tables})


def _download(storage, project: str, table: str, path: Path, expected: int) -> None:
    """Stream a BigQuery table to Parquet; the file appears only if the row count matches."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from google.cloud.bigquery_storage_v1 import types

    p, d, t = table.split(".")
    session = storage.create_read_session(
        parent=f"projects/{project}",
        read_session=types.ReadSession(
            table=f"projects/{p}/datasets/{d}/tables/{t}", data_format=types.DataFormat.ARROW
        ),
        max_stream_count=1,
    )
    schema = pa.ipc.read_schema(pa.py_buffer(session.arrow_schema.serialized_schema))

    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    rows = 0
    with pq.ParquetWriter(part, schema, compression="zstd") as writer:
        pending: list[pa.RecordBatch] = []
        for stream in session.streams:
            for page in storage.read_rows(stream.name).rows().pages:
                pending.append(page.to_arrow())
                if sum(b.num_rows for b in pending) >= ROW_GROUP_ROWS:
                    table_ = pa.Table.from_batches(pending, schema)
                    writer.write_table(table_)
                    rows += table_.num_rows
                    pending = []
        if pending:
            table_ = pa.Table.from_batches(pending, schema)
            writer.write_table(table_)
            rows += table_.num_rows
    if rows != expected:
        part.unlink()
        raise ExtractError(f"{path}: got {rows} rows, BigQuery has {expected}")
    part.replace(path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def _parquet_rows(path: Path) -> int:
    import pyarrow.parquet as pq

    return pq.ParquetFile(path).metadata.num_rows


def duckdb_parity(con: duckdb.DuckDBPyConnection, spec: Spec, source: str) -> dict[str, dict]:
    cur = con.execute(_parity_sql(spec, source, '"'))
    names = [d[0] for d in cur.description][1:]
    return {
        r[0]: {n: None if v is None else int(v) for n, v in zip(names, r[1:], strict=True)}
        for r in cur.fetchall()
    }


def verify_extract(out_dir: Path, memory_limit: str = "4GB") -> dict[str, int]:
    """Recompute parity.json's aggregates on the Parquet files; return rows per table."""
    out_dir = Path(out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text())
    window = Window(
        date.fromisoformat(manifest["window"]["start"]),
        date.fromisoformat(manifest["window"]["end_exclusive"]),
    )
    expected = json.loads((out_dir / "parity.json").read_text())["tables"]
    problems: list[str] = []
    totals: dict[str, int] = {}
    with duckdb.connect(config={"memory_limit": memory_limit, "TimeZone": "UTC"}) as con:
        for spec in SPECS:
            log.info("verify %s", spec.name)
            files = list(_files(spec, out_dir, window).values())
            missing = [str(p) for p in files if not p.is_file()]
            if missing:
                problems.append(f"{spec.name}: missing {missing}")
                continue
            source = "read_parquet([" + ", ".join(f"'{p}'" for p in files) + "])"
            got = duckdb_parity(con, spec, source)
            if got != expected[spec.name]:
                diff = sorted(
                    day
                    for day in set(got) | set(expected[spec.name])
                    if got.get(day) != expected[spec.name].get(day)
                )
                problems.append(f"{spec.name}: aggregates differ from BigQuery on {diff}")
            problems += _check_parity(spec.name, spec, window, got)
            totals[spec.name] = sum(r["row_count"] for r in got.values())
    if problems:
        raise ExtractError("verification failed:\n  " + "\n  ".join(problems))
    return totals
