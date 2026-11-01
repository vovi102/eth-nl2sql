"""Build the benchmark: fill templates, label them, verify the gold SQL, split by template.

    uv run python -m eth_nl2sql.benchmark_build

Writes data/benchmark/dev.jsonl, test.jsonl and manifest.json (docs/benchmark.md). Every
question is a linking record of docs/linking-labels.md plus `gold_sql`, `template_id` and
`knowledge_base_version`. Fillers come from the reviewed `link` schema; a question is kept only
when its gold SQL passes the checks of benchmark_validate and returns an informative result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from eth_nl2sql import benchmark_validate as validate
from eth_nl2sql.benchmark_templates import (
    GROUPS,
    TEMPLATES,
    Fill,
    Template,
    Variant,
    assign_placeholders,
    implicit_senses,
    render,
    time_condition,
    time_fill,
)
from eth_nl2sql.entity_knowledge_base import ALIASES, ENTITIES, normalize_alias, read_sheet

log = logging.getLogger(__name__)

DEFAULT_DB = Path("data/db/ethereum.duckdb")
DEFAULT_KNOWLEDGE_BASE_DIR = Path("data/entity_knowledge_base")
DEFAULT_PROPERTY_SENSES_DIR = Path("data/schema")
DEFAULT_OUT_DIR = Path("data/benchmark")
SEED = 20261101
QUERY_TIMEOUT = 10.0
ATTEMPTS_PER_QUESTION = 25
DEV_SHARE = 1 / 3
MAX_TOKEN_DIGITS = 38  # token_transfers.value beyond DECIMAL(38,0) cannot be summed
MIN_TOKEN_CALLERS = 1000
MIN_ENTITY_CALLS = 50
MIN_ADDRESS_TRANSACTIONS = 100  # named wallets and contracts people actually use
POPULAR_TOKENS = 30  # half of the token questions use one of the most called tokens
DEFAULT_TOP_N = 5

# Classes asked about (docs/benchmark.md §1, decision 2) and how a question names them. Each
# name must be an approved class alias; English plurals drop their final "s" to match.
CLASS_NAMES = {
    "dex": {"vi": ("DEX", "sàn phi tập trung"), "en": ("DEXs", "decentralized exchanges")},
    "dex-aggregator": {"vi": ("DEX aggregator", "bộ tổng hợp DEX"), "en": ("DEX aggregators",)},
    "bridge": {"vi": ("bridge", "cầu nối chuỗi"), "en": ("bridges",)},
    "lending": {"vi": ("giao thức cho vay",), "en": ("lending protocols",)},
    "layer-2": {"vi": ("L2", "layer 2"), "en": ("L2s",)},
}
# Candidate NIL names; only those matching no alias (any status) and no token are used.
NOT_IN_KB_NAMES = (
    "Bybit", "Hyperliquid", "Robinhood", "Upbit", "Bitpanda", "BtcTurk", "WhiteBIT", "Zaif",
    "Kinesis Money", "Bitso", "Luno", "CoinDCX", "Paribu", "Independent Reserve",
)  # fmt: skip
NIL_TOKEN_SYMBOLS = ("HYPEX", "ZQRT", "VNDC2", "PLUMX", "KAITOX", "SOLVQ", "MOVEZ", "BERAQ")


# ---- the reviewed knowledge base, as the gold sees it -------------------------------------


@dataclass
class LinkTables:
    entities: dict[str, dict]  # canonical_id -> name, parent (canonical), kind, classes, set
    children: dict[str, set[str]]
    addresses: dict[str, list[dict]]  # canonical_id -> address rows
    aliases: list[dict]
    tokens: dict[str, dict]  # token address -> token_stats row
    calls: dict[str, int]  # September transactions per to_address (knowledge base addresses)
    senses: dict[str, str]  # sense_id -> kind


def load_link_tables(con: duckdb.DuckDBPyConnection) -> LinkTables:
    def rows(sql: str) -> list[dict]:
        cur = con.execute(sql)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    entity_rows = rows("SELECT * FROM link.entities")
    canonical = {e["entity_id"]: e["canonical_id"] for e in entity_rows}
    entities = {}
    for e in entity_rows:
        if e["entity_id"] == e["canonical_id"]:
            parent = canonical.get(e["parent_id"] or "")
            entities[e["entity_id"]] = {
                "name": e["name"],
                "parent": parent if parent != e["entity_id"] else None,
                "kind": e["kind"],
                "classes": set(e["classes"]),
                "address_set": e["address_set"],
            }
    children: dict[str, set[str]] = defaultdict(set)
    for eid, e in entities.items():
        if e["parent"] in entities:
            children[e["parent"]].add(eid)
    addresses: dict[str, list[dict]] = defaultdict(list)
    for a in rows("SELECT canonical_id, address, name_tag, classes FROM link.entity_addresses"):
        addresses[a["canonical_id"]].append(a)
    calls = dict(
        con.execute(
            "SELECT to_address, count(*) FROM main.transactions WHERE to_address IN "
            "(SELECT address FROM link.entity_addresses) GROUP BY 1"
        ).fetchall()
    )
    return LinkTables(
        entities=entities,
        children=dict(children),
        addresses=dict(addresses),
        aliases=rows("SELECT * FROM link.entity_aliases"),
        tokens={t["address"]: t for t in rows("SELECT * FROM link.token_stats")},
        calls=calls,
        senses=dict(con.execute("SELECT sense_id, kind FROM link.property_senses").fetchall()),
    )


def subtree(link: LinkTables, entity_id: str) -> list[str]:
    """The entity and its descendants."""
    out, todo = [], [entity_id]
    while todo:
        eid = todo.pop()
        if eid not in out:
            out.append(eid)
            todo.extend(sorted(link.children.get(eid, ())))
    return out


def entity_addresses(link: LinkTables, entity_id: str) -> list[str]:
    """Every address of an entity and its children, tokens included (§2, last paragraph)."""
    return sorted({a["address"] for e in subtree(link, entity_id) for a in link.addresses[e]})


def class_members(link: LinkTables, class_id: str) -> list[tuple[str, list[str]]]:
    """Members of a class with their addresses, ranked (docs/linking-labels.md §2).

    A child rolls up to its highest ancestor that has the class too. A member's addresses are
    those of the entities in its tree that have the class, without token contracts. Groups have
    no reviewed class: their addresses that carry the class count. Ranked by September calls,
    ties by id.
    """
    members: dict[str, set[str]] = defaultdict(set)
    for eid, e in link.entities.items():
        if e["kind"] == "group":
            found = {
                a["address"]
                for a in link.addresses.get(eid, [])
                if class_id in a["classes"] and a["address"] not in link.tokens
            }
            if found:
                members[eid] |= found
        elif class_id in e["classes"]:
            top = eid
            while link.entities.get(top, {}).get("parent") in link.entities and (
                class_id in link.entities[link.entities[top]["parent"]]["classes"]
            ):
                top = link.entities[top]["parent"]
            members[top] |= {
                a["address"] for a in link.addresses.get(eid, []) if a["address"] not in link.tokens
            }
    ranked = sorted(members.items(), key=lambda m: (-sum(link.calls.get(a, 0) for a in m[1]), m[0]))
    return [(eid, sorted(addresses)) for eid, addresses in ranked]


def placeholder_addresses(link: LinkTables, record: dict) -> dict[str, list[str]]:
    """{E1: [addresses]} for the gold mentions of a record."""
    out: dict[str, list[str]] = {}
    for m in record["mentions"]:
        name = m.get("placeholder")
        if not name or name in out:
            continue
        if m["type"] == "entity":
            out[name] = entity_addresses(link, m["target"])
        elif m["type"] == "class":
            top = class_members(link, m["target"])[: m["top_n"]]
            out[name] = sorted({a for _, addresses in top for a in addresses})
        else:
            out[name] = [m["target"]]
    return out


# ---- fillers ------------------------------------------------------------------------------


@dataclass
class Fillers:
    entities: list[dict]  # canonical_id, aliases [(alias, lang)], addresses
    addresses: list[dict]  # address, alias, open (a wallet of an open entity), lang
    tokens: list[dict]  # address, alias, lang, decimals
    classes: dict[str, list[tuple[str, list[str]]]]  # class_id -> ranked members
    class_names: dict[str, dict[str, list[str]]]  # class_id -> lang -> approved alias forms
    literals: list[str]
    nil_entities: list[tuple[str, str]]  # (name, nil_reason)
    nil_tokens: list[str]
    activity: dict[str, Counter]  # address -> September counts (calls, eth_in, failed, ...)
    token_receipts: dict[str, set[str]]  # receiver address -> tokens it received


def _alias_targets(link: LinkTables) -> dict[str, set[tuple[str, str]]]:
    targets: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for a in link.aliases:
        targets[a["alias_norm"]].add((a["target_type"], a["target_id"]))
    return targets


def _activity(con: duckdb.DuckDBPyConnection, addresses: set[str]) -> dict[str, Counter]:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE benchmark_addresses AS "
        "SELECT unnest(?::VARCHAR[]) AS address",
        [sorted(addresses)],
    )
    activity: dict[str, Counter] = defaultdict(Counter)
    for address, calls, eth_in, failed in con.execute(
        """SELECT to_address, count(*), count(*) FILTER (WHERE receipt_status = 1 AND value > 0),
          count(*) FILTER (WHERE receipt_status = 0)
        FROM main.transactions WHERE to_address IN (SELECT address FROM benchmark_addresses)
        GROUP BY 1"""
    ).fetchall():
        activity[address].update(calls=calls, eth_in=eth_in, failed_calls=failed)
    for address, sends, eth_out in con.execute(
        """SELECT from_address, count(*), count(*) FILTER (WHERE receipt_status = 1 AND value > 0)
        FROM main.transactions WHERE from_address IN (SELECT address FROM benchmark_addresses)
        GROUP BY 1"""
    ).fetchall():
        activity[address].update(sends=sends, eth_out=eth_out)
    return activity


def _token_receipts(
    con: duckdb.DuckDBPyConnection, tokens: list[str]
) -> dict[str, set[str]]:  # fmt: skip
    con.execute(
        "CREATE OR REPLACE TEMP TABLE benchmark_tokens AS SELECT unnest(?::VARCHAR[]) AS address",
        [tokens],
    )
    receipts: dict[str, set[str]] = defaultdict(set)
    for to_address, token in con.execute(
        """SELECT DISTINCT to_address, token_address FROM main.token_transfers
        WHERE token_address IN (SELECT address FROM benchmark_tokens)
          AND to_address IN (SELECT address FROM benchmark_addresses)"""
    ).fetchall():
        receipts[to_address].add(token)
    return receipts


def load_fillers(
    con: duckdb.DuckDBPyConnection,
    link: LinkTables,
    knowledge_base_dir: Path,
    *,
    min_token_callers: int = MIN_TOKEN_CALLERS,
    min_entity_calls: int = MIN_ENTITY_CALLS,
    min_address_transactions: int = MIN_ADDRESS_TRANSACTIONS,
) -> Fillers:
    targets = _alias_targets(link)

    def unambiguous(alias_norm: str) -> bool:  # tokens are ranked at link time, not ambiguous
        return len({t for t in targets[alias_norm] if t[0] != "token"}) == 1

    by_entity: dict[str, list[tuple[str, str]]] = defaultdict(list)
    named: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for a in link.aliases:
        if a["target_type"] == "entity" and unambiguous(a["alias_norm"]):
            by_entity[a["target_id"]].append((a["alias"], a["lang"]))
        elif (
            a["target_type"] == "address"
            and unambiguous(a["alias_norm"])
            and "0x" not in a["alias"].lower()
        ):
            named[a["target_id"]].append((a["alias"], a["lang"]))
    owner = {a["address"]: eid for eid, rows in link.addresses.items() for a in rows}

    # Activity of every named address, entity address and typed-address candidate.
    literal_candidates = [
        r[0]
        for r in con.execute(
            """SELECT address FROM (
              SELECT to_address AS address, count(*) AS n FROM main.transactions
              WHERE block_timestamp >= TIMESTAMP '2026-09-15 00:00:00'
                AND block_timestamp < TIMESTAMP '2026-09-16 00:00:00' GROUP BY 1
              UNION ALL
              SELECT from_address, count(*) FROM main.transactions
              WHERE block_timestamp >= TIMESTAMP '2026-09-15 00:00:00'
                AND block_timestamp < TIMESTAMP '2026-09-16 00:00:00' GROUP BY 1)
            WHERE address IS NOT NULL GROUP BY address ORDER BY sum(n) DESC, address LIMIT 600"""
        ).fetchall()
        if r[0] not in owner and r[0] not in link.tokens
    ][:200]
    all_addresses = set(owner) | set(literal_candidates)
    activity = _activity(con, all_addresses)

    entities = []
    for eid, e in sorted(link.entities.items()):
        if e["address_set"] != "closed" or e["kind"] == "group" or not by_entity.get(eid):
            continue
        addresses = entity_addresses(link, eid)
        if sum(link.calls.get(a, 0) for a in addresses) >= min_entity_calls:
            entities.append({"entity_id": eid, "aliases": by_entity[eid], "addresses": addresses})

    named_addresses = []
    for address, aliases in sorted(named.items()):
        eid = owner.get(address)
        used = activity.get(address, Counter())
        if (
            eid is None
            or link.entities[eid]["kind"] == "group"
            or used["calls"] + used["sends"] < min_address_transactions
        ):
            continue
        for alias, lang in aliases:
            named_addresses.append(
                {"address": address, "alias": alias, "lang": lang,
                 "open": link.entities[eid]["address_set"] == "open"}
            )  # fmt: skip

    long_valued = {
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT token_address FROM main.token_transfers "
            f"WHERE length(value) > {MAX_TOKEN_DIGITS}"
        ).fetchall()
    }
    best: dict[str, str] = {}  # alias_norm -> the most called token with that alias
    for a in link.aliases:
        if a["target_type"] == "token" and a["target_id"] in link.tokens:
            current = best.get(a["alias_norm"])
            callers = link.tokens[a["target_id"]]["callers"]
            if current is None or (callers, a["target_id"]) > (
                link.tokens[current]["callers"],
                current,
            ):
                best[a["alias_norm"]] = a["target_id"]
    tokens = []
    for a in link.aliases:
        t = link.tokens.get(a["target_id"])
        if (
            a["target_type"] == "token"
            and best.get(a["alias_norm"]) == a["target_id"]
            and t["decimals"] is not None
            and t["token_standard"] != "ERC721"
            and t["callers"] >= min_token_callers
            and a["target_id"] not in long_valued
        ):
            tokens.append(
                {"address": a["target_id"], "alias": a["alias"], "lang": a["lang"],
                 "decimals": t["decimals"]}
            )  # fmt: skip
    tokens.sort(key=lambda t: (t["alias"], t["address"]))
    token_receipts = _token_receipts(con, sorted({t["address"] for t in tokens}))

    class_norms = {
        (a["target_id"], a["alias_norm"]) for a in link.aliases if a["target_type"] == "class"
    }
    classes, class_names = {}, {}
    for cid, names in CLASS_NAMES.items():
        approved = {
            lang: [
                name
                for name in candidates
                if {(cid, normalize_alias(name)), (cid, re.sub(r"s$", "", normalize_alias(name)))}
                & class_norms
            ]
            for lang, candidates in names.items()
        }
        members = class_members(link, cid)
        if members and all(approved.values()):
            classes[cid], class_names[cid] = members, approved

    nil_entities, nil_tokens = _nil_names(link, knowledge_base_dir)
    return Fillers(
        entities=entities,
        addresses=named_addresses,
        tokens=tokens,
        classes=classes,
        class_names=class_names,
        literals=literal_candidates,
        nil_entities=nil_entities,
        nil_tokens=nil_tokens,
        activity=activity,
        token_receipts=token_receipts,
    )


def _nil_names(
    link: LinkTables, knowledge_base_dir: Path
) -> tuple[list[tuple[str, str]], list[str]]:
    """NIL names: `not_in_kb` match no alias of any status; `not_reviewed` name an entity that is
    still pending. Neither may match a token symbol or name, which the linker ranks too."""
    every_alias = {a["alias_norm"] for a in read_sheet(knowledge_base_dir / ALIASES.file)}
    token_norms = set()
    for t in link.tokens.values():
        token_norms |= {normalize_alias(t["symbol"] or ""), normalize_alias(t["name"] or "")}
    usable = {a["alias_norm"] for a in link.aliases}
    taken = every_alias | token_norms
    nil = [(n, "not_in_kb") for n in NOT_IN_KB_NAMES if normalize_alias(n) not in taken]

    entities = read_sheet(knowledge_base_dir / ENTITIES.file, ENTITIES.columns)
    pending = {
        e["entity_id"]: int(e["rank"] or 10**9) for e in entities if e["status"] == "pending"
    }
    seen = set()
    for a in sorted(
        read_sheet(knowledge_base_dir / ALIASES.file, ALIASES.columns),
        key=lambda a: pending.get(a["target_id"], 10**9),
    ):
        norm = a["alias_norm"]
        if (
            a["target_type"] == "entity"
            and a["target_id"] in pending
            and pending[a["target_id"]] <= 400
            and a["alias_type"] == "name"
            and norm not in usable
            and norm not in token_norms
            and norm not in seen
            and re.fullmatch(r"[A-Za-z][\w .]*", a["alias"])
        ):
            seen.add(norm)
            nil.append((a["alias"], "not_reviewed"))
    nil_tokens = [s for s in NIL_TOKEN_SYMBOLS if normalize_alias(s) not in taken]
    return nil, nil_tokens


# ---- generation ---------------------------------------------------------------------------


class Rejected(Exception):
    pass


def _choice(rng: random.Random, pool: list, what: str):
    if not pool:
        raise Rejected(f"no {what} to fill in")
    return rng.choice(pool)


def _pick_alias(rng: random.Random, aliases: list[tuple[str, str]], lang: str) -> str:
    allowed = [
        a for a, alang in aliases if alang in ("any", lang) or (lang == "vi" and alang == "en")
    ]
    if not allowed:
        raise Rejected("no alias in this language")
    return rng.choice(sorted(allowed))


def _time(rng: random.Random, template: Template, lang: str) -> Fill | None:
    kind = rng.choice(template.times)
    if kind == "range":
        first = rng.randint(1, 24)
        return time_fill(kind, lang, first, first + rng.randint(2, 6))
    return time_fill(kind, lang, rng.randint(1, 30) if kind == "day" else rng.randint(1, 4))


def _active(fillers: Fillers, addresses: list[str], need: str | None) -> bool:
    if need is None or need == "token_in":
        return True
    return sum(fillers.activity.get(a, Counter())[need] for a in addresses) > 0


def _fill_slots(
    rng: random.Random, variant: Variant, lang: str, fillers: Fillers, link: LinkTables
) -> tuple[dict[str, Fill], dict]:
    """Choose a filler for every slot; returns the fills and the token decimals, if any."""
    fills: dict[str, Fill] = {}
    extra: dict = {}
    receivers: list[str] = []
    for slot, kind in variant.slots.items():
        need = variant.filters.get(slot)
        if kind == "entity":
            pool = [e for e in fillers.entities if _active(fillers, e["addresses"], need)]
            e = _choice(rng, pool, "entity")
            alias = _pick_alias(rng, e["aliases"], lang)
            fills[slot] = Fill(alias, {"type": "entity", "target": e["entity_id"]})
            receivers = e["addresses"]
        elif kind == "address":
            pool = [
                a for a in fillers.addresses
                if a["lang"] in ("any", lang) and _active(fillers, [a["address"]], need)
            ]  # fmt: skip
            wallets = [a for a in pool if a["open"]]
            a = _choice(rng, wallets if wallets and rng.random() < 0.5 else pool, "address")
            prefix = ("ví " if a["open"] else "contract ") if lang == "vi" else ""
            fills[slot] = Fill(a["alias"], {"type": "address", "target": a["address"]}, prefix)
            receivers = [a["address"]]
        elif kind == "class":
            cid = _choice(rng, sorted(fillers.classes), "class")
            members = fillers.classes[cid]
            name = rng.choice(fillers.class_names[cid][lang])
            if rng.random() < 0.5 or len(members) < 3:
                n, source = DEFAULT_TOP_N, "default"
                text = f"{name} lớn" if lang == "vi" else f"major {name}"
                prefix = "các " if lang == "vi" else "the "
            else:
                n, source = rng.randint(2, min(DEFAULT_TOP_N, len(members) - 1)), "explicit"
                text = f"{n} {name} lớn nhất" if lang == "vi" else f"top {n} {name}"
                prefix = "" if lang == "vi" else "the "
            mention = {"type": "class", "target": cid, "top_n": n, "top_n_source": source}
            fills[slot] = Fill(text, mention, prefix)
            receivers = sorted({a for _, addresses in members[:n] for a in addresses})
        elif kind == "literal":
            pool = [a for a in fillers.literals if _active(fillers, [a], need)]
            a = _choice(rng, pool, "typed address")
            fills[slot] = Fill(a, {"type": "literal_address", "target": a})
            extra["literal"] = f"'{a}'"
        elif kind == "nil_entity":
            reason = rng.choice(("not_in_kb", "not_reviewed"))  # the pools differ in size
            pool = [n for n in fillers.nil_entities if n[1] == reason]
            name, reason = _choice(rng, pool, f"{reason} name")
            fills[slot] = Fill(name, {"type": "nil", "nil_reason": reason})
        elif kind == "nil_token":
            fills[slot] = Fill(
                _choice(rng, fillers.nil_tokens, "NIL token"),
                {"type": "nil", "nil_reason": "not_in_kb"},
            )
        elif kind != "token":
            raise ValueError(kind)
    if variant.slots.get("token") == "token":  # after the receiver: a token it received
        pool = [t for t in fillers.tokens if t["lang"] in ("any", lang)]
        if receivers and any(v == "token_in" for v in variant.filters.values()):
            received = set().union(*(fillers.token_receipts.get(a, set()) for a in receivers))
            pool = [t for t in pool if t["address"] in received]
        if not pool:
            raise Rejected("no token received by this target")
        by_callers = sorted(
            {t["address"] for t in pool}, key=lambda a: (-link.tokens[a]["callers"], a)
        )
        popular = [t for t in pool if t["address"] in set(by_callers[:POPULAR_TOKENS])]
        t = rng.choice(popular if rng.random() < 0.5 else pool)
        fills["token"] = Fill(t["alias"], {"type": "token", "target": t["address"]})
        extra["scale"] = f"1e{t['decimals']}"
    return fills, extra


def _gold_sql(variant: Variant, rendered, extra: dict) -> str:
    placeholders = {
        slot: "{" + m["placeholder"] + "}"
        for slot, m in zip(rendered.slots, rendered.mentions, strict=True)
        if "placeholder" in m
    }
    window = None
    if rendered.time is not None:
        window = (
            date.fromisoformat(rendered.time["start"][:10]),
            date.fromisoformat(rendered.time["end"][:10]),
        )
    condition = time_condition(window)
    block_condition = time_condition(window, "timestamp")
    return variant.sql.format(
        **placeholders,
        **extra,
        time_and=f" AND {condition}" if condition else "",
        time_where=f" WHERE {condition}" if condition else "",
        block_time_where=f" WHERE {block_condition}" if block_condition else "",
    )


MENTION_ORDER = ("span", "text", "type", "target", "placeholder", "top_n", "top_n_source")


def make_record(
    rng: random.Random, template: Template, variant: Variant, lang: str, fillers: Fillers,
    link: LinkTables,
) -> dict:  # fmt: skip
    surface = rng.choice(variant.surfaces[lang])
    fills, extra = _fill_slots(rng, variant, lang, fillers, link)
    fills["time"] = _time(rng, template, lang) if "{time}" in surface else None
    return build_record(template, variant, lang, surface, fills, extra, link.senses)


def build_record(
    template: Template, variant: Variant, lang: str, surface: str, fills: dict[str, Fill | None],
    extra: dict, senses: dict[str, str],
) -> dict:  # fmt: skip
    """The labeled record of one filled surface, with its gold SQL when answerable.

    `senses` maps sense_id -> kind; an implicit sense is added only when it exists, as the
    linker does.
    """
    rendered = render(surface, fills, {s for s, k in senses.items() if k == "unsupported"})
    assign_placeholders(rendered.mentions)
    implicit = implicit_senses(rendered.properties, rendered.mentions, rendered.unsupported)
    properties = rendered.properties + [p for p in implicit if p["sense_id"] in senses]
    answerable = not rendered.unsupported and all(m["type"] != "nil" for m in rendered.mentions)
    gold_sql = _gold_sql(variant, rendered, extra) if answerable and variant.sql else None
    mentions = [
        {k: m[k] for k in MENTION_ORDER if k in m} | {k: m[k] for k in m if k not in MENTION_ORDER}
        for m in sorted(rendered.mentions, key=lambda m: m["span"][0])
    ]
    return {
        "question_id": None,
        "question": rendered.question,
        "lang": lang,
        "mentions": mentions,
        "properties": properties,
        "time": rendered.time,
        "unsupported": rendered.unsupported,
        "warnings": [],
        "gold_sql": gold_sql,
        "template_id": template.template_id,
    }


def verify(record: dict, link: LinkTables, con: duckdb.DuckDBPyConnection, timeout: float) -> dict:
    """Run every check; returns the measured result, or raises Rejected."""
    problems = validate.check_record(record, link.senses)
    if problems:
        raise ValueError(f"{record['template_id']}: {record['question']!r}: {problems}")
    if record["gold_sql"] is None:
        return {}
    problems = validate.check_sql(record["gold_sql"])
    if problems:
        raise ValueError(f"{record['template_id']}: {record['gold_sql']}: {problems}")
    sql = validate.substitute_placeholders(record["gold_sql"], placeholder_addresses(link, record))
    try:
        rows, seconds = validate.run_sql(con, sql, timeout)
    except validate.GoldQueryError as e:
        raise Rejected(f"query: {e}") from e
    if not validate.informative(rows):
        raise Rejected("empty result")
    return {"rows": len(rows), "seconds": round(seconds, 2)}


def generate(
    template: Template, fillers: Fillers, link: LinkTables, con: duckdb.DuckDBPyConnection,
    rng: random.Random, count: int, timeout: float, rejected: Counter,
) -> list[dict]:  # fmt: skip
    out, seen = [], set()
    for _ in range(count * ATTEMPTS_PER_QUESTION):
        if len(out) == count:
            break
        lang = ("vi", "en")[len(out) % 2]
        variant = template.variants[len(out) // 2 % len(template.variants)]
        try:
            record = make_record(rng, template, variant, lang, fillers, link)
            if record["question"] in seen:
                raise Rejected("duplicate question")
            verify(record, link, con, timeout)
        except Rejected as e:
            rejected[str(e)] += 1
            continue
        seen.add(record["question"])
        out.append(record)
    if len(out) < count:
        log.warning("%s: only %d of %d questions", template.template_id, len(out), count)
    return out


# ---- split, phenomena, output -------------------------------------------------------------


def split_templates(templates: tuple[Template, ...], seed: int) -> dict[str, str]:
    """Dev or test per template: about DEV_SHARE of the templates of every group go to dev."""
    rng = random.Random(seed)
    out = {}
    for group in GROUPS:
        ids = sorted(t.template_id for t in templates if t.group == group)
        rng.shuffle(ids)
        n_dev = max(1, round(len(ids) * DEV_SHARE)) if len(ids) > 1 else 0
        for i, tid in enumerate(ids):
            out[tid] = "dev" if i < n_dev else "test"
    return out


def phenomena(record: dict, link: LinkTables, group: str) -> list[str]:
    """The blockchain features a question exercises (docs/benchmark.md §5)."""
    out = set()
    shared_symbol = Counter(normalize_alias(t["symbol"] or "") for t in link.tokens.values())
    for m in record["mentions"]:
        if m["type"] == "entity":
            if len(entity_addresses(link, m["target"])) >= 2:
                out.add("multi_address")
            if normalize_alias(m["text"]) != normalize_alias(link.entities[m["target"]]["name"]):
                out.add("alias")
        elif m["type"] == "token" and shared_symbol[normalize_alias(m["text"])] > 1:
            out.add("alias")
        elif m["type"] == "class":
            out.add("class")
    if group == "ambiguous":
        out.add("ambiguous_property")
    return sorted(out)


def knowledge_base_files(knowledge_base_dir: Path, property_senses_dir: Path) -> dict[str, str]:
    files = sorted(Path(knowledge_base_dir).glob("*.csv")) + sorted(
        Path(property_senses_dir).glob("property_*.csv")
    )
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def knowledge_base_version(files: dict[str, str]) -> str:
    lines = "".join(f"{Path(name).name} {sha}\n" for name, sha in sorted(files.items()))
    return hashlib.sha256(lines.encode()).hexdigest()


def build(
    db: Path, knowledge_base_dir: Path, property_senses_dir: Path, out_dir: Path, *,
    seed: int = SEED, timeout: float = QUERY_TIMEOUT, count_scale: float = 1.0,
    memory_limit: str = "4GB",
) -> dict:  # fmt: skip
    files = knowledge_base_files(knowledge_base_dir, property_senses_dir)
    version = knowledge_base_version(files)
    con = duckdb.connect(str(db), read_only=True)
    con.execute(f"SET memory_limit = '{memory_limit}'")
    link = load_link_tables(con)
    fillers = load_fillers(con, link, knowledge_base_dir)
    log.info(
        "fillers: %d entities, %d named addresses, %d tokens, %d literals, %d NIL names",
        len(fillers.entities), len(fillers.addresses), len(fillers.tokens),
        len(fillers.literals), len(fillers.nil_entities),
    )  # fmt: skip
    splits = split_templates(TEMPLATES, seed)
    records: dict[str, list[dict]] = {"dev": [], "test": []}
    rejected: dict[str, Counter] = {}
    rng = random.Random(seed)
    for template in TEMPLATES:
        rejected[template.template_id] = Counter()
        count = max(1, round(template.count * count_scale))
        made = generate(
            template, fillers, link, con, rng, count, timeout, rejected[template.template_id]
        )
        log.info("%-28s %3d questions", template.template_id, len(made))
        records[splits[template.template_id]] += made
    con.close()

    groups = {t.template_id: t.group for t in TEMPLATES}
    question_phenomena = {}
    n = 0
    for split in ("dev", "test"):
        for r in records[split]:
            n += 1
            r["question_id"] = f"q{n:05d}"
            r["knowledge_base_version"] = version
            question_phenomena[r["question_id"]] = phenomena(r, link, groups[r["template_id"]])
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_files = {}
    for split, rows in records.items():
        path = out_dir / f"{split}.jsonl"
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        manifest_files[path.name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "questions": len(rows),
        }
    all_records = records["dev"] + records["test"]
    manifest = {
        "created": date.today().isoformat(),
        "seed": seed,
        "knowledge_base_version": version,
        "knowledge_base_files": files,
        "files": manifest_files,
        "templates": {
            t.template_id: {
                "group": t.group,
                "split": splits[t.template_id],
                "questions": sum(r["template_id"] == t.template_id for r in all_records),
                "rejected": dict(rejected[t.template_id]),
            }
            for t in TEMPLATES
        },
        "counts": {
            "by_split_lang": dict(Counter(f"{s}/{r['lang']}" for s in records for r in records[s])),
            "by_group": dict(Counter(groups[r["template_id"]] for r in all_records)),
            "by_phenomenon": dict(Counter(p for ps in question_phenomena.values() for p in ps)),
        },
        "class_members": {
            cid: [[eid, len(addresses)] for eid, addresses in members[:10]]
            for cid, members in fillers.classes.items()
        },
        "phenomena": question_phenomena,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eth_nl2sql.benchmark_build")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--knowledge-base-dir", type=Path, default=DEFAULT_KNOWLEDGE_BASE_DIR)
    parser.add_argument("--property-senses-dir", type=Path, default=DEFAULT_PROPERTY_SENSES_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--timeout", type=float, default=QUERY_TIMEOUT, help="seconds per query")
    parser.add_argument("--count-scale", type=float, default=1.0, help="e.g. 0.1 for a dry run")
    parser.add_argument("--memory-limit", default="4GB")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    manifest = build(
        args.db, args.knowledge_base_dir, args.property_senses_dir, args.out_dir,
        seed=args.seed, timeout=args.timeout, count_scale=args.count_scale,
        memory_limit=args.memory_limit,
    )  # fmt: skip
    for name, info in manifest["files"].items():
        print(f"  {name:<12} {info['questions']:>5} questions")
    print(f"  by group: {manifest['counts']['by_group']}")
    print(f"  by phenomenon: {manifest['counts']['by_phenomenon']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
