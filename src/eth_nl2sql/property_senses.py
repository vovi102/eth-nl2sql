"""The property-sense dictionary: question phrases -> SQL expressions over the main schema.

data/schema/property_senses.csv holds one row per sense (an expression and the kind of entity
it applies to); property_phrases.csv maps VI/EN phrases to senses. A phrase such as "giá trị"
is resolved by the type of the linked entity, not by the phrase alone (docs/proposed-method.md).
Both files are reviewed like the entity knowledge base: only approved rows are loaded.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from eth_nl2sql.entity_knowledge_base import normalize_alias, read_sheet

SENSES_FILE = "property_senses.csv"
PHRASES_FILE = "property_phrases.csv"
SENSE_COLUMNS = (
    "sense_id", "kind", "applies_to", "table_name", "expression", "unit", "description_vi",
    "status", "note",
)  # fmt: skip
PHRASE_COLUMNS = ("phrase", "phrase_norm", "lang", "sense_id", "status", "note")
KINDS = ("measure", "filter", "aggregate", "dimension", "unsupported")
STATUSES = ("pending", "approved", "rejected")
TABLES = ("blocks", "transactions", "token_transfers", "contracts", "tokens", "amended_tokens")
# {E}: the address set from entity linking; {decimals}: the linked token's decimals.
PLACEHOLDERS = {"E", "decimals"}


@dataclass
class Senses:
    senses: list[dict]
    phrases: list[dict]


def usable(senses_dir: Path) -> Senses:
    """Approved senses, and approved phrases of approved senses."""
    senses = read_sheet(Path(senses_dir) / SENSES_FILE, SENSE_COLUMNS)
    phrases = read_sheet(Path(senses_dir) / PHRASES_FILE, PHRASE_COLUMNS)
    approved = [s for s in senses if s["status"] == "approved"]
    ids = {s["sense_id"] for s in approved}
    return Senses(
        approved, [p for p in phrases if p["status"] == "approved" and p["sense_id"] in ids]
    )


def check(senses_dir: Path) -> list[str]:
    """Problems that would make a sense unusable; executing the SQL is left to the build."""
    senses = read_sheet(Path(senses_dir) / SENSES_FILE, SENSE_COLUMNS)
    phrases = read_sheet(Path(senses_dir) / PHRASES_FILE, PHRASE_COLUMNS)
    errors = []
    for sid, n in Counter(s["sense_id"] for s in senses).items():
        if n > 1:
            errors.append(f"{SENSES_FILE}: sense_id {sid!r} appears {n} times")
    for s in senses:
        where = f"{SENSES_FILE}: {s['sense_id']}"
        if s["status"] not in STATUSES:
            errors.append(f"{where}: unknown status {s['status']!r}")
        if s["kind"] not in KINDS:
            errors.append(f"{where}: unknown kind {s['kind']!r}")
        if (s["kind"] == "unsupported") != (s["expression"] == ""):
            errors.append(f"{where}: an expression is needed exactly when the data can answer")
        for table in filter(None, s["table_name"].split("|")):
            if table not in TABLES:
                errors.append(f"{where}: unknown table {table!r}")
        unknown = set(re.findall(r"\{(\w+)\}", s["expression"])) - PLACEHOLDERS
        if unknown:
            errors.append(f"{where}: unknown placeholders {sorted(unknown)}")
    ids = {s["sense_id"] for s in senses}
    for p in phrases:
        where = f"{PHRASES_FILE}: {p['phrase']!r}"
        if p["status"] not in STATUSES:
            errors.append(f"{where}: unknown status {p['status']!r}")
        if p["sense_id"] not in ids:
            errors.append(f"{where}: unknown sense {p['sense_id']!r}")
        if p["phrase_norm"] != normalize_alias(p["phrase"]):
            errors.append(f"{where}: phrase_norm should be {normalize_alias(p['phrase'])!r}")
    return errors


def probe_sql(sense: dict, table: str) -> str:
    """A query that makes DuckDB bind the expression on `table` (run it under EXPLAIN)."""
    expr = sense["expression"].replace("{E}", "('0x0000000000000000000000000000000000000000')")
    expr = expr.replace("{decimals}", "18")
    if table == "blocks":
        expr = expr.replace("block_timestamp", "timestamp")
    return {
        "measure": f"SELECT SUM({expr}) FROM main.{table}",
        "filter": f"SELECT COUNT(*) FROM main.{table} WHERE {expr}",
        "aggregate": f"SELECT {expr} FROM main.{table}",
        "dimension": f"SELECT {expr}, COUNT(*) FROM main.{table} GROUP BY 1",
    }[sense["kind"]]
