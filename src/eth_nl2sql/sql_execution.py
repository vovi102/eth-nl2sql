"""B8: from the model's reply to the rows of the main schema (proposed-method.md §4).

1. Take the SQL out of the reply (the first ```sql block, else the text from SELECT/WITH on).
2. Replace {E1}, {E2}, ... with the addresses of the linking record.
3. Parse it with SQLGlot as DuckDB: one read-only query. Whether it also parses as Postgres is
   recorded, not enforced: the DuckDB ∩ Postgres subset binds the gold SQL, not the model.
4. Run it on a read-only connection without file access, with a timeout.

A failed query gets one more try: `repair_messages` returns the conversation plus the error.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from eth_nl2sql.benchmark_validate import PLACEHOLDER_RE
from eth_nl2sql.entity_linking import substitute_placeholders

SQL_BLOCK_RE = re.compile(
    r"```[ \t]*(?:sql|duckdb|postgres(?:ql)?)?[ \t]*\n(.*?)(?:```|\Z)", re.S | re.I
)
BARE_SQL_RE = re.compile(r"(?is)\b(?:WITH|SELECT)\b.*")
MAX_ERROR_CHARS = 300

# Why a reply produced no rows; None when it ran.
ERROR_KINDS = ("no_sql", "unknown_placeholder", "parse", "not_a_query", "execution", "timeout")


@dataclass
class Execution:
    sql: str | None  # as the model wrote it, with placeholders
    error_kind: str | None = None
    error: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False  # more than max_rows rows
    postgres_parses: bool | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error_kind is None


def extract_sql(reply: str) -> str | None:
    """The first SQL code block of the reply (an unclosed one too), else bare SELECT/WITH."""
    block = SQL_BLOCK_RE.search(reply)
    if block:
        text = block.group(1)
    else:
        bare = BARE_SQL_RE.search(reply)
        text = bare.group(0) if bare else ""
    text = text.strip().rstrip(";").strip()
    return text or None


def _probe(sql: str) -> str:
    """The query with one address per placeholder: parsing 4,608 literals is slow and useless."""
    return PLACEHOLDER_RE.sub("('0x0000000000000000000000000000000000000000')", sql)


def _parses(sql: str, dialect: str) -> list[exp.Expression] | None:
    try:
        return [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except sqlglot.errors.SqlglotError:
        return None


class SqlRunner:
    """One read-only connection to the database, reused for every query."""

    def __init__(
        self,
        db_path: Path,
        *,
        timeout: float = 30.0,
        max_rows: int = 10_000,
        memory_limit: str = "4GB",
    ) -> None:
        config = {"memory_limit": memory_limit, "TimeZone": "UTC"}
        self.con = duckdb.connect(str(db_path), read_only=True, config=config)
        # Only now: TimeZone needs the icu extension loaded first. No read_csv('/etc/...')
        # from a generated query.
        self.con.execute("SET enable_external_access = false")
        self.timeout = timeout
        self.max_rows = max_rows

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> SqlRunner:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def check(self, sql: str, addresses: dict[str, list[str]]) -> Execution | None:
        """An Execution with the error when the query cannot run; None when it may."""
        missing = sorted(set(PLACEHOLDER_RE.findall(sql)) - set(addresses))
        if missing:
            return Execution(sql, "unknown_placeholder", f"no addresses for {missing}")
        if re.search(r"\{[^}]*\}", PLACEHOLDER_RE.sub("", sql)):
            return Execution(sql, "unknown_placeholder", "a {...} that is no placeholder E<n>")
        trees = _parses(_probe(sql), "duckdb")
        if trees is None:
            return Execution(sql, "parse", "SQLGlot cannot parse it as DuckDB")
        if len(trees) != 1 or not isinstance(trees[0], exp.Query):
            return Execution(sql, "not_a_query", "expected exactly one SELECT query")
        return None

    def run(self, sql: str | None, addresses: dict[str, list[str]]) -> Execution:
        """B8 on the SQL of one reply: placeholders, checks, execution."""
        if sql is None:
            return Execution(None, "no_sql", "no SQL in the reply")
        failed = self.check(sql, addresses)
        if failed:
            return failed
        postgres = _parses(_probe(sql), "postgres") is not None
        final = substitute_placeholders(sql, addresses)
        timer = threading.Timer(self.timeout, self.con.interrupt)
        started = time.monotonic()
        timer.start()
        try:
            cursor = self.con.execute(final)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(self.max_rows + 1)
        except duckdb.InterruptException:
            return Execution(sql, "timeout", f"slower than {self.timeout:g} s",
                             postgres_parses=postgres, seconds=self.timeout)  # fmt: skip
        except duckdb.Error as e:
            return Execution(sql, "execution", _short(str(e)), postgres_parses=postgres,
                             seconds=time.monotonic() - started)  # fmt: skip
        finally:
            timer.cancel()
        return Execution(
            sql,
            columns=columns,
            rows=rows[: self.max_rows],
            truncated=len(rows) > self.max_rows,
            postgres_parses=postgres,
            seconds=time.monotonic() - started,
        )


def _short(error: str) -> str:
    """The first line of a DuckDB error, without the thousands of addresses it may quote."""
    line = error.strip().splitlines()[0] if error.strip() else "error"
    line = re.sub(r"\('0x[0-9a-f]{40}'(?:, '0x[0-9a-f]{40}')+\)", "(...)", line)
    return line[:MAX_ERROR_CHARS]


def repair_messages(messages: list[dict], reply: str, execution: Execution) -> list[dict]:
    """The second and last try: the model sees its reply and why it failed."""
    return messages + [
        {"role": "assistant", "content": reply},
        {
            "role": "user",
            "content": f"The query failed ({execution.error_kind}): {execution.error}\n"
            "Reply with the corrected query in a ```sql code block and nothing else.",
        },
    ]
