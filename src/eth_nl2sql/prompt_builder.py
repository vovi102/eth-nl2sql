"""B6 and B7: the prompt of the SQL model, built from a linking record (proposed-method.md §4-5).

The input is a record in the format of linking-labels.md, from the linker or from the gold
(oracle linking), so every ablation of proposed-method.md §7 runs from the same function:

- `evidence=False`: schema and question only (no linking).
- `placeholders=False`: the evidence names the addresses literally instead of {E1}.
- `schema="linked"`: only the tables the linked property senses touch (B6), else all six.

A record with an unsupported sense or a NIL mention gets no prompt: the pipeline answers that
the data cannot tell, or that it does not know the name (linking-labels.md §5, example 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from eth_nl2sql.bigquery_schema import SOURCE_TABLES
from eth_nl2sql.entity_linking import EntityLinker, substitute_placeholders

SCHEMA_MODES = ("full", "linked")
ADDRESS_MENTIONS = ("entity", "address", "class", "literal_address")

SYSTEM_PROMPT = """\
You write one DuckDB SQL query that answers a question about Ethereum mainnet data of \
September 2026.
Rules:
- Use only the tables of the schema, without a schema prefix.
- Timestamps are UTC.
- Never filter tokens or entities by symbol or name; use the addresses the evidence gives.
- Use standard SQL: no QUALIFY, no EXCLUDE, no list functions.
- Reply with the query in a ```sql code block and nothing else."""

PLACEHOLDER_RULE = (
    "- Write a placeholder such as {E1} exactly as the evidence gives it, where a list of "
    "addresses goes (to_address IN {E1}); it is replaced by the addresses after generation."
)

# What an implicit sense adds, since the question does not say it (linking-labels.md §3).
IMPLICIT_TEXT = {
    "tx_success": "only successful transactions",
    "of_token": "only transfers of that token",
}
# property_senses.unit is written in Vietnamese; the evidence is English. Units are shown for
# measures only: a count says its unit itself.
UNITS_EN = {"đơn vị token": "token units", "giao dịch": "transactions"}


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    description: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class Schema:
    tables: dict[str, str]  # table -> description, in the order of SOURCE_TABLES
    columns: dict[str, tuple[Column, ...]]  # table -> columns in table order

    def ddl(self, tables: list[str]) -> str:
        """CREATE TABLE statements with BigQuery's description and 2 examples per column."""
        blocks = []
        for table in tables:
            lines = [f"-- {self.tables[table]}", f"CREATE TABLE {table} ("]
            columns = self.columns[table]
            for k, c in enumerate(columns):
                comma = "," if k < len(columns) - 1 else ""
                note = c.description.rstrip(".") + "."
                if c.examples:
                    note += " Examples: " + ", ".join(repr(e) for e in c.examples)
                lines.append(f"  {c.name} {c.data_type}{comma}  -- {note}")
            lines.append(");")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def load_schema(db_path: Path) -> Schema:
    """meta.tables and meta.columns of the database (built by build-db)."""
    with duckdb.connect(str(db_path), read_only=True) as con:
        tables = dict(con.execute("SELECT table_name, description FROM meta.tables").fetchall())
        rows = con.execute(
            "SELECT table_name, column_name, data_type, description, examples "
            "FROM meta.columns ORDER BY table_name, ordinal"
        ).fetchall()
    columns: dict[str, list[Column]] = {t: [] for t in tables}
    for table, name, data_type, description, examples in rows:
        columns[table].append(Column(name, data_type, description, tuple(examples)))
    order = [t for t in SOURCE_TABLES if t in tables]
    return Schema({t: tables[t] for t in order}, {t: tuple(columns[t]) for t in order})


@dataclass(frozen=True)
class PromptOptions:
    evidence: bool = True
    placeholders: bool = True
    schema: str = "full"

    def __post_init__(self) -> None:
        if self.schema not in SCHEMA_MODES:
            raise ValueError(f"schema must be one of {SCHEMA_MODES}, not {self.schema!r}")


class PromptBuilder:
    def __init__(self, linker: EntityLinker, schema: Schema) -> None:
        self.linker = linker
        self.schema = schema
        res = linker.resources
        self.address_names = {
            a["address"]: (a["name_tag"], eid, a["is_contract"])
            for eid, rows in res.entity_addresses.items()
            for a in rows
        }

    def build(self, record: dict, options: PromptOptions | None = None) -> dict:
        """{question_id, decision, messages | answer, tables, chars} for one linking record."""
        options = options or PromptOptions()
        out = {"question_id": record.get("question_id"), "question": record["question"]}
        if options.evidence:
            abstain = self._abstain(record)
            if abstain:
                return out | {"decision": abstain[0], "answer": abstain[1]}

        tables = self._tables(record, options)
        system = SYSTEM_PROMPT
        if options.evidence and options.placeholders:
            system += "\n" + PLACEHOLDER_RULE
        parts = [f"### Schema\n{self.schema.ddl(tables)}"]
        if options.evidence:
            evidence = "\n".join(self.evidence(record))
            if not options.placeholders:
                addresses = self.linker.placeholder_addresses(record)
                evidence = substitute_placeholders(evidence, addresses)
            parts.append(f"### Evidence\n{evidence}")
        parts.append(f"### Question\n{record['question']}")
        user = "\n\n".join(parts)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return out | {
            "decision": "sql",
            "messages": messages,
            "tables": tables,
            "chars": len(system) + len(user),
        }

    # ---- B6: tables ------------------------------------------------------------------

    def _sense_tables(self, record: dict) -> list[set[str]]:
        senses = self.linker.resources.senses
        return [
            set(senses[p["sense_id"]]["table_name"])
            for p in record["properties"]
            if senses.get(p["sense_id"], {}).get("table_name")
        ]

    def main_table(self, record: dict) -> str | None:
        """The one table every linked sense can use: token_transfers for "users of USDT"."""
        sets = self._sense_tables(record)
        common = set.intersection(*sets) if sets else set()
        return next(iter(common)) if len(common) == 1 else None

    def _tables(self, record: dict, options: PromptOptions) -> list[str]:
        everything = list(self.schema.tables)
        if options.schema == "full":
            return everything
        main = self.main_table(record)
        wanted = {main} if main else set().union(*self._sense_tables(record))
        return [t for t in everything if t in wanted] or everything

    # ---- B7: evidence ----------------------------------------------------------------

    @staticmethod
    def _abstain(record: dict) -> tuple[str, str] | None:
        if record["unsupported"]:
            senses = ", ".join(u["sense_id"] for u in record["unsupported"])
            return "unsupported", f"The data cannot answer this ({senses})."
        nil = [m["text"] for m in record["mentions"] if m["type"] == "nil"]
        if nil:
            return "nil", f"Unknown name: {', '.join(nil)}."
        return None

    def evidence(self, record: dict) -> list[str]:
        """The lines of the evidence block (proposed-method.md §5), with placeholders."""
        res = self.linker.resources
        main = self.main_table(record)
        lines = [f"-- Table: {main}"] if main else []
        seen = set()
        for m in record["mentions"]:
            key = m.get("placeholder") or m["target"]
            if m["type"] != "nil" and key not in seen:
                seen.add(key)
                lines.append(f'-- "{m["text"]}": {self._describe(m)}')

        addressed = [m for m in record["mentions"] if m["type"] in ADDRESS_MENTIONS]
        tokens = [m for m in record["mentions"] if m["type"] == "token"]
        decimals = res.tokens[tokens[0]["target"]]["decimals"] if tokens else None
        for p in record["properties"]:
            sense = res.senses[p["sense_id"]]
            target = tokens if sense["applies_to"] in ("token", "nft") else addressed
            expression = _bind(sense["expression"], target[0] if target else None, decimals)
            notes = []
            if sense["kind"] == "measure" and sense["unit"]:
                notes.append(f"unit: {UNITS_EN.get(sense['unit'], sense['unit'])}")
            if sense.get("evidence_hint"):
                notes.append(sense["evidence_hint"])
            if notes:
                expression += f" ({'; '.join(notes)})"
            if p["implicit"]:
                said = IMPLICIT_TEXT.get(p["sense_id"], p["sense_id"])
                lines.append(f"-- (implied: {said}) {expression}")
            else:
                lines.append(f'-- "{p["text"]}": {expression}')

        column = "timestamp" if main == "blocks" else "block_timestamp"
        time = record["time"]
        if time:
            start, end = (time[k].replace("T", " ") for k in ("start", "end"))
            lines.append(
                f"-- \"{time['text']}\": {column} >= TIMESTAMP '{start}' "
                f"AND {column} < TIMESTAMP '{end}'"
            )
        else:
            lines.append("-- No time range: use all rows (the data holds September 2026 only).")
        for w in record["warnings"]:
            if w.startswith("open_address_set:"):
                lines.append(
                    "-- Warning: only the known addresses of this entity are listed; "
                    "the answer is a lower bound."
                )
        return lines

    def _describe(self, m: dict) -> str:
        res = self.linker.resources
        ph = "{" + m["placeholder"] + "}" if m.get("placeholder") else None
        if m["type"] == "literal_address":
            return "an address typed in the question, used as a literal"
        if m["type"] == "token":
            t = res.tokens[m["target"]]
            return (
                f"{ph} = the contract of the token {t['name']} ({t['symbol']}), "
                f"decimals {t['decimals']}"
            )
        if m["type"] == "address":
            name, owner, is_contract = self.address_names.get(m["target"], (m["text"], None, None))
            kind = "contract" if is_contract else "wallet"
            of = f" of {res.entities[owner]['name']}" if owner in res.entities else ""
            return f'{ph} = the {kind} named "{name}"{of}'
        if m["type"] == "entity":
            e = res.entities[m["target"]]
            n = len(res.addresses_of(m["target"]))
            classes = f"; {', '.join(e['classes'])}" if e["classes"] else ""
            closed = "all" if e["address_set"] == "closed" else "the known"
            return f"{ph} = {closed} {n} addresses of {e['name']} ({e['kind']}{classes})"
        c = res.classes[m["target"]]
        members = self.linker.class_members(m["target"], m["top_n"])
        names = ", ".join(res.entities[eid]["name"] for eid, _ in members)
        n = len({a for _, addresses in members for a in addresses})
        why = (
            "N = 5 since the question gives no number"
            if m.get("top_n_source") == "default"
            else f"N = {m['top_n']} as asked"
        )
        return (
            f"{ph} = the {n} addresses of the top {m['top_n']} members of the class "
            f'"{c["name_en"]}" by calls in September ({why}): {names}'
        )


def _bind(expression: str, mention: dict | None, decimals: int | None) -> str:
    """Fill {E} and {decimals} of a sense; a typed address is compared with =."""
    if decimals is not None:
        expression = expression.replace("{decimals}", str(decimals))
    if mention is None:
        return expression
    if mention["type"] == "literal_address":
        return expression.replace("IN {E}", f"= '{mention['target']}'")
    return expression.replace("{E}", "{" + mention["placeholder"] + "}")
