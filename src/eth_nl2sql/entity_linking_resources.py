"""The resources of entity linking, loaded once into memory.

Everything comes from the link schema of data/db/ethereum.duckdb, plus September's call counts
of every knowledge-base address (main.transactions, about half a second). The connection is
read-only and closed as soon as the tables are in memory, so another process can reload the
link schema (load-knowledge-base) between two runs. Unreviewed entities, needed only to tell
'not_reviewed' from 'not_in_kb', are read from the knowledge-base CSVs.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from eth_nl2sql.entity_knowledge_base import (
    ALIASES,
    ENTITIES,
    entity_name,
    normalize_alias,
    read_sheet,
    slug,
)


@dataclass(frozen=True)
class Target:
    type: str  # entity | address | token | class
    id: str  # canonical_id, address, token address or class_id


@dataclass
class LinkingResources:
    aliases: dict[str, set[Target]]  # alias_norm -> targets (usable aliases only)
    entities: dict[str, dict]  # canonical_id -> entity row (classes merged over same_as)
    entity_addresses: dict[str, list[dict]]  # canonical_id -> its own address rows
    children: dict[str, set[str]]  # canonical_id -> canonical ids of child entities
    address_owner: dict[str, set[str]]  # address -> canonical ids
    classes: dict[str, dict]  # class_id -> row
    tokens: dict[str, dict]  # token address -> token_stats row
    tokens_by_norm: dict[str, list[str]]  # normalized symbol or name -> token addresses
    senses: dict[str, dict]  # sense_id -> property_senses row
    phrases: list[dict]  # property_phrases rows
    address_calls: dict[str, int]  # September transactions whose to_address is the address
    unreviewed: dict[str, set[str]]  # alias_norm -> unreviewed (pending) entity ids
    _activity: dict[str, int] = field(default_factory=dict, repr=False)

    def subtree(self, entity_id: str) -> list[str]:
        """The entity and all its descendants, parents first."""
        out, todo = [], [entity_id]
        while todo:
            eid = todo.pop()
            if eid not in out:
                out.append(eid)
                todo.extend(sorted(self.children.get(eid, ()), reverse=True))
        return out

    def addresses_of(self, entity_id: str) -> list[str]:
        """Every address of an entity and of its child entities (linking-labels.md §2)."""
        return sorted(
            {a["address"] for e in self.subtree(entity_id) for a in self.entity_addresses[e]}
        )

    def activity(self, entity_id: str) -> int:
        """September calls (to_address) of every address of the entity."""
        if entity_id not in self._activity:
            self._activity[entity_id] = sum(
                self.address_calls.get(a, 0) for a in self.addresses_of(entity_id)
            )
        return self._activity[entity_id]

    def prior(self, target: Target) -> float:
        """log(1 + activity): token callers, entity or address calls; classes have none."""
        if target.type == "token":
            return math.log1p(self.tokens.get(target.id, {}).get("callers") or 0)
        if target.type == "entity":
            return math.log1p(self.activity(target.id))
        if target.type == "address":
            return math.log1p(self.address_calls.get(target.id, 0))
        return 0.0


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cursor = con.execute(sql)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _unreviewed(knowledge_base_dir: Path, usable_addresses: set[str]) -> dict[str, set[str]]:
    """Aliases (not rejected) of pending entities and of their addresses."""
    pending = {
        e["entity_id"]
        for e in read_sheet(knowledge_base_dir / ENTITIES.file, ENTITIES.columns)
        if e["status"] == "pending"
    }
    out: dict[str, set[str]] = defaultdict(set)
    for al in read_sheet(knowledge_base_dir / ALIASES.file, ALIASES.columns):
        if al["status"] == "rejected":
            continue
        if al["target_type"] == "entity" and al["target_id"] in pending:
            out[al["alias_norm"]].add(al["target_id"])
        elif al["target_type"] == "address" and al["target_id"] not in usable_addresses:
            eid = slug(entity_name(al["alias"]))
            if eid in pending:
                out[al["alias_norm"]].add(eid)
    return dict(out)


def load_resources(
    db_path: Path, knowledge_base_dir: Path, *, memory_limit: str = "4GB"
) -> LinkingResources:
    config = {"memory_limit": memory_limit, "TimeZone": "UTC"}
    with duckdb.connect(str(db_path), read_only=True, config=config) as con:
        alias_rows = _rows(
            con, "SELECT alias_norm, target_type, target_id FROM link.entity_aliases"
        )
        entity_rows = _rows(con, "SELECT * FROM link.entities")
        address_rows = _rows(con, "SELECT * FROM link.entity_addresses")
        class_rows = _rows(con, "SELECT * FROM link.entity_classes")
        token_rows = _rows(
            con, "SELECT address, symbol, name, decimals, callers, transfers FROM link.token_stats"
        )
        sense_rows = _rows(con, "SELECT * FROM link.property_senses")
        phrase_rows = _rows(con, "SELECT * FROM link.property_phrases")
        address_calls = dict(
            con.execute(
                """SELECT to_address, count(*) FROM main.transactions
                WHERE to_address IN (SELECT address FROM link.entity_addresses) GROUP BY 1"""
            ).fetchall()
        )

    canonical = {e["entity_id"]: e["canonical_id"] for e in entity_rows}
    entities: dict[str, dict] = {}
    for e in sorted(entity_rows, key=lambda e: e["entity_id"] != e["canonical_id"]):
        row = entities.setdefault(e["canonical_id"], dict(e) | {"classes": []})
        row["classes"] = sorted(set(row["classes"]) | set(e["classes"]))
    children: dict[str, set[str]] = defaultdict(set)
    for e in entity_rows:
        parent = canonical.get(e["parent_id"] or "")
        if parent and parent != e["canonical_id"]:
            children[parent].add(e["canonical_id"])
    entity_addresses: dict[str, list[dict]] = {eid: [] for eid in entities}
    address_owner: dict[str, set[str]] = defaultdict(set)
    for a in address_rows:
        entity_addresses[a["canonical_id"]].append(a)
        address_owner[a["address"]].add(a["canonical_id"])

    aliases: dict[str, set[Target]] = defaultdict(set)
    for al in alias_rows:
        aliases[al["alias_norm"]].add(Target(al["target_type"], al["target_id"]))
    tokens = {t["address"]: t for t in token_rows}
    tokens_by_norm: dict[str, list[str]] = defaultdict(list)
    for t in token_rows:
        for text in {normalize_alias(t["symbol"] or ""), normalize_alias(t["name"] or "")}:
            if text:
                tokens_by_norm[text].append(t["address"])

    return LinkingResources(
        aliases=dict(aliases),
        entities=entities,
        entity_addresses=entity_addresses,
        children=dict(children),
        address_owner=dict(address_owner),
        classes={c["class_id"]: c for c in class_rows},
        tokens=tokens,
        tokens_by_norm=dict(tokens_by_norm),
        senses={s["sense_id"]: s for s in sense_rows},
        phrases=phrase_rows,
        address_calls=address_calls,
        unreviewed=_unreviewed(Path(knowledge_base_dir), set(address_owner)),
    )
