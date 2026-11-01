"""Checks every benchmark question must pass (docs/benchmark.md §6).

- The record follows docs/linking-labels.md: spans match the text, placeholders are numbered in
  order of appearance, gold SQL exists exactly when the question is answerable.
- The gold SQL stays in the subset shared by DuckDB and Postgres and is deterministic.
- The gold SQL runs on the main schema, fast, and returns an informative result.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata

import duckdb
import sqlglot
from sqlglot import exp

PLACEHOLDER_RE = re.compile(r"\{(E\d+)\}")
FORBIDDEN_RE = re.compile(r"(?i)\b(QUALIFY|EXCLUDE|list_\w+)\b")
ORDER_BY_RE = re.compile(r"(?is)\bORDER BY\b(.*?)(?:\bLIMIT\b|$)")
NULLS_RE = re.compile(r"(?i)\bNULLS (FIRST|LAST)\b")
# Columns that make a row of a gold result unique, used to break ties in ORDER BY.
TIE_BREAKERS = {"hash", "transaction_hash", "log_index", "from_address", "to_address", "miner",
                "day", "address"}  # fmt: skip
DIALECTS = ("duckdb", "postgres")
MENTION_FIELDS = {
    "entity": {"span", "text", "type", "target", "placeholder"},
    "address": {"span", "text", "type", "target", "placeholder"},
    "token": {"span", "text", "type", "target", "placeholder"},
    "class": {"span", "text", "type", "target", "placeholder", "top_n", "top_n_source"},
    "literal_address": {"span", "text", "type", "target"},
    "nil": {"span", "text", "type", "nil_reason"},
}


class GoldQueryError(RuntimeError):
    pass


def substitute_placeholders(sql: str, addresses: dict[str, list[str]]) -> str:
    """{E1} -> ('0x…', '0x…'), the post-processing step B8."""
    missing = set(PLACEHOLDER_RE.findall(sql)) - set(addresses)
    if missing:
        raise GoldQueryError(f"no addresses for {sorted(missing)}")
    return PLACEHOLDER_RE.sub(
        lambda m: "(" + ", ".join(f"'{a}'" for a in addresses[m.group(1)]) + ")", sql
    )


def _check_span(item: dict, question: str, where: str) -> list[str]:
    start, end = item["span"]
    if not 0 <= start < end <= len(question):
        return [f"{where}: span {item['span']} outside the question"]
    if question[start:end] != item["text"]:
        return [f"{where}: span {item['span']} is {question[start:end]!r}, not {item['text']!r}"]
    return []


def check_record(record: dict, senses: dict[str, str]) -> list[str]:
    """Problems with a record against docs/linking-labels.md; `senses` maps sense_id -> kind."""
    errors = []
    question = record["question"]
    if unicodedata.normalize("NFC", question) != question:
        errors.append("question is not NFC")
    if record["lang"] not in ("vi", "en"):
        errors.append(f"lang {record['lang']!r}")
    names: dict[tuple, str] = {}
    for i, m in enumerate(sorted(record["mentions"], key=lambda m: m["span"][0])):
        where = f"mention {i} {m['text']!r}"
        errors += _check_span(m, question, where)
        expected = MENTION_FIELDS.get(m["type"])
        if expected is None:
            errors.append(f"{where}: unknown type {m['type']!r}")
            continue
        if set(m) != expected:
            errors.append(f"{where}: fields {sorted(m)} should be {sorted(expected)}")
        if "placeholder" in expected:
            key = (m["type"], m["target"], m.get("top_n"))
            names.setdefault(key, f"E{len(names) + 1}")
            if m.get("placeholder") != names[key]:
                errors.append(f"{where}: placeholder {m.get('placeholder')} should be {names[key]}")
        if m["type"] == "class" and m["top_n_source"] not in ("explicit", "default"):
            errors.append(f"{where}: top_n_source {m['top_n_source']!r}")
    for p in record["properties"]:
        where = f"property {p['sense_id']}"
        if senses.get(p["sense_id"]) in (None, "unsupported"):
            errors.append(f"{where}: not a supported sense")
        if p["implicit"]:
            if p["span"] is not None or p["text"] is not None:
                errors.append(f"{where}: an implicit sense has no span")
        else:
            errors += _check_span(p, question, where)
    for u in record["unsupported"]:
        if senses.get(u["sense_id"]) != "unsupported":
            errors.append(f"unsupported {u['sense_id']}: not an unsupported sense")
        errors += _check_span(u, question, f"unsupported {u['sense_id']}")
    if record["time"] is not None:
        t = record["time"]
        errors += _check_span(t, question, "time")
        if not "2026-09-01T00:00:00" <= t["start"] < t["end"] <= "2026-10-01T00:00:00":
            errors.append(f"time [{t['start']}, {t['end']}) outside September 2026")
    answerable = not record["unsupported"] and all(m["type"] != "nil" for m in record["mentions"])
    if answerable != (record["gold_sql"] is not None):
        errors.append("gold_sql must exist exactly when the question is answerable")
    if record["gold_sql"] is not None:
        used = set(PLACEHOLDER_RE.findall(record["gold_sql"]))
        if used != set(names.values()):
            errors.append(f"gold_sql uses {sorted(used)}, mentions give {sorted(names.values())}")
    return errors


def _top_level_split(text: str) -> list[str]:
    """Split on commas outside parentheses: CAST(value AS DECIMAL(38,0)) stays one key."""
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    return parts + [current.strip()]


def check_sql(sql: str) -> list[str]:
    """The gold SQL is in the DuckDB ∩ Postgres subset and orders its rows deterministically."""
    errors = []
    probe = PLACEHOLDER_RE.sub("('0x0000000000000000000000000000000000000000')", sql)
    if FORBIDDEN_RE.search(probe):
        errors.append(f"dialect-specific construct {FORBIDDEN_RE.search(probe).group(0)!r}")
    trees = {}
    for dialect in DIALECTS:
        try:
            trees[dialect] = sqlglot.parse_one(probe, read=dialect)
        except sqlglot.errors.ParseError as e:
            errors.append(f"does not parse as {dialect}: {e}")
    tree = trees.get("postgres") or trees.get("duckdb")
    if tree is None:
        return errors
    if tree.find(exp.Limit) and not tree.find(exp.Order):
        errors.append("LIMIT without ORDER BY")
    order = ORDER_BY_RE.search(probe)
    if order:
        keys = _top_level_split(order.group(1))
        for key in keys:
            if not NULLS_RE.search(key):
                errors.append(f"ORDER BY key {key!r} needs NULLS FIRST/LAST")
        last = keys[-1].split()[0]
        if last not in TIE_BREAKERS:
            errors.append(f"ORDER BY should end with a tie-breaking column, not {last!r}")
    return errors


def run_sql(con: duckdb.DuckDBPyConnection, sql: str, timeout: float) -> tuple[list, float]:
    """Rows of the query and the seconds it took; GoldQueryError when it fails or is too slow."""
    timer = threading.Timer(timeout, con.interrupt)
    started = time.monotonic()
    timer.start()
    try:
        rows = con.execute(sql).fetchall()
    except duckdb.InterruptException as e:
        raise GoldQueryError(f"slower than {timeout:g} s") from e
    except duckdb.Error as e:
        raise GoldQueryError(str(e).splitlines()[0]) from e
    finally:
        timer.cancel()
    return rows, time.monotonic() - started


def informative(rows: list) -> bool:
    """An empty result, or a single value that is NULL or 0, says nothing about the question."""
    if not rows:
        return False
    if len(rows) == 1 and len(rows[0]) == 1:
        return rows[0][0] not in (None, 0)
    return True
