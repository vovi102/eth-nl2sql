"""JSON for the browser: result rows, and an execution of B8."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from eth_nl2sql.sql.execution import Execution

# JavaScript numbers are exact up to 2^53; a token amount has up to 78 digits.
MAX_SAFE_INTEGER = 2**53 - 1
ADDRESSES_SHOWN = 3


def cell(value):
    """A value of a result row as JSON: big or exact numbers as text, never rounded."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [cell(v) for v in value]
    if isinstance(value, dict):
        return {str(k): cell(v) for k, v in value.items()}
    return str(value)


def short_list(addresses: list[str], shown: int = ADDRESSES_SHOWN) -> str:
    """('0x…', '0x…', '0x…', … +N): the literal of B8, shortened for the page."""
    if not addresses:
        return "(NULL)"
    head = ", ".join(f"'{a}'" for a in addresses[:shown])
    rest = len(addresses) - shown
    return f"({head}, … +{rest:,})" if rest > 0 else f"({head})"


def preview_sql(sql: str | None, addresses: dict[str, list[str]]) -> str | None:
    """The SQL that runs, with each address list shortened (substitute_placeholders' order)."""
    if sql is None:
        return None
    for name, values in sorted(addresses.items(), key=lambda kv: -len(kv[0])):
        sql = sql.replace("{" + name + "}", short_list(values))
    return sql


def execution(run: Execution, addresses: dict[str, list[str]], max_rows: int) -> dict:
    """What the page shows of one run: the first max_rows rows, the error, the seconds."""
    return {
        "ok": run.ok,
        "error_kind": run.error_kind,
        "error": run.error,
        "sql": run.sql,
        "sql_preview": preview_sql(run.sql, addresses),
        "columns": list(run.columns),
        "rows": [[cell(v) for v in row] for row in run.rows[:max_rows]],
        "row_count": len(run.rows),
        "rows_shown": min(len(run.rows), max_rows),
        "truncated": run.truncated,
        "seconds": round(run.seconds, 3),
    }
