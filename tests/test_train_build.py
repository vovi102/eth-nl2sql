"""The phase-2 train builder: the held-out entities, variant counts, token_out, the L1 filter,
and runs on the fixture database."""

from __future__ import annotations

import functools
import hashlib
import json
from collections import Counter
from pathlib import Path

import duckdb
import pytest
from conftest import ALICE, ROUTER, TOKEN, addr

from eth_nl2sql import benchmark_build as bb
from eth_nl2sql import entity_knowledge_base as kb
from eth_nl2sql import train_build as tb
from eth_nl2sql.benchmark_build import Fillers, LinkTables
from eth_nl2sql.benchmark_templates import Variant
from eth_nl2sql.benchmark_validate import check_record
from eth_nl2sql.build_duckdb import load_knowledge_base
from eth_nl2sql.entity_linking import EntityLinker
from eth_nl2sql.entity_linking_resources import load_resources
from eth_nl2sql.prompt_builder import PromptBuilder, load_schema
from eth_nl2sql.sql_execution import extract_sql
from eth_nl2sql.train_templates import TRAIN_TEMPLATES_BY_ID as T

REPO = Path(__file__).parent.parent
SCHEMA = REPO / "data/schema"


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ---- pure functions -----------------------------------------------------------------------


def test_holdout_is_seeded_and_bounded() -> None:
    ids = [f"e{i:02d}" for i in range(99)]
    held = tb.holdout_entities(ids, 20, seed=1)
    assert len(held) == 20 and held == sorted(held) and set(held) <= set(ids)
    assert tb.holdout_entities(list(reversed(ids)), 20, seed=1) == held  # order does not matter
    assert tb.holdout_entities(ids, 20, seed=2) != held
    assert tb.holdout_entities(ids[:3], 20, seed=1) == ids[:3]


def test_variant_counts_follow_the_generator_cycle() -> None:
    """benchmark_build.generate takes one VI/EN pair per variant in turn."""
    assert tb.variant_counts(68, 1) == [68]
    assert tb.variant_counts(64, 2) == [32, 32]
    assert tb.variant_counts(66, 3) == [22, 22, 22]
    assert tb.variant_counts(4, 3) == [2, 2, 0]


def test_token_out_is_filled_like_token_in() -> None:
    v = Variant({"target": "entity", "token": "token"}, {}, "SELECT 1", {"target": "token_out"})
    filled, out = tb.shadow(v)
    assert out and filled.filters == {"target": "token_in"}
    plain = Variant({"target": "entity"}, {}, "SELECT 1", {"target": "calls"})
    assert tb.shadow(plain) == (plain, False)


def test_names_are_matched_as_whole_words() -> None:
    names = {"vault finance", "ens"}
    assert tb.names_any(names, "Có bao nhiêu giao dịch gọi Vault-Finance?")
    assert tb.names_any(names, '-- "ENS": {E1} = all 3 addresses of ENS (protocol)')
    assert not tb.names_any(names, "How many tokens did Vault Financial receive?")
    assert not tb.names_any(set(), "anything")


def toy() -> tuple[Fillers, LinkTables]:
    """swap: a DEX (class member) with a child; vault, and a name built on vault's; curve, a
    common word; a named wallet of vault, one named after it, and a token named after it."""

    def entity(name, parent=None, classes=()):  # fmt: skip
        return {"name": name, "parent": parent, "kind": "protocol", "classes": set(classes),
                "address_set": "closed"}  # fmt: skip

    entities = {
        "swap": entity("Swap", classes=("dex",)), "swap-v2": entity("Swap V2", parent="swap"),
        "vault": entity("Vault Finance"), "vault-pro": entity("Vault Finance Pro"),
        "curve": entity("Curve"),
    }  # fmt: skip
    aliases = [
        {"alias_norm": kb.normalize_alias(n), "target_type": "entity", "target_id": eid}
        for eid, n in [("swap", "Swap"), ("swap-v2", "Swap V2"), ("vault", "Vault Finance"),
                       ("vault", "VLT"), ("vault-pro", "Vault Finance Pro"), ("curve", "Curve")]
    ]  # fmt: skip
    link = LinkTables(
        entities=entities,
        children={"swap": {"swap-v2"}},
        addresses={"vault": [{"address": "0xv1"}], "swap": [{"address": "0xs1"}]},
        aliases=aliases,
        tokens={"0xt1": {"name": "Vault Finance Token", "symbol": "VFT"},
                "0xt2": {"name": "Tether USD", "symbol": "USDT"}},
        calls={}, senses={},
    )  # fmt: skip
    fillers = Fillers(
        entities=[{"entity_id": eid, "aliases": [(e["name"], "any")], "addresses": []}
                  for eid, e in entities.items()],
        addresses=[{"address": "0xv1", "alias": "Hot Wallet 1", "lang": "any", "open": False},
                   {"address": "0xs1", "alias": "VLT Router", "lang": "any", "open": False},
                   {"address": "0xs2", "alias": "Swap: Router", "lang": "any", "open": False}],
        tokens=[{"address": "0xt1", "alias": "VFT", "lang": "any", "decimals": 18},
                {"address": "0xt2", "alias": "USDT", "lang": "any", "decimals": 6}],
        classes={"dex": [("swap", ["0xs1"])]}, class_names={}, literals=[], nil_entities=[],
        nil_tokens=[], activity={}, token_receipts={},
    )  # fmt: skip
    return fillers, link


def test_holdout_candidates_are_never_named_by_a_train_prompt() -> None:
    fillers, link = toy()
    # swap: listed in the dex class; swap-v2: its parent is a filler; curve: a common word
    assert tb.holdout_candidates(fillers, link) == ["vault", "vault-pro"]


def test_hold_out_removes_every_filler_that_names_a_held_out_entity() -> None:
    fillers, link = toy()
    kept, removed = tb.hold_out(fillers, link, ["vault"])
    assert [e["entity_id"] for e in kept.entities] == ["swap", "swap-v2", "curve"]
    assert removed == {
        "entities": ["vault", "vault-pro"],  # held out; its name in the other's
        "addresses": ["0xs1", "0xv1"],  # named after an alias (VLT); owned by vault
        "tokens": ["0xt1"],  # "Vault Finance Token"
    }
    assert tb.hold_out(fillers, link, []) == (fillers, {"entities": [], "addresses": [],
                                                       "tokens": []})  # fmt: skip


def test_drop_cause_tells_the_known_linker_errors() -> None:
    entity = {"span": [0, 7], "type": "entity", "target": "pendle", "placeholder": "E1"}
    token = {"span": [0, 7], "type": "token", "target": "0xpendle", "placeholder": "E1"}
    assert tb.drop_cause({"mentions": [entity]}, {"mentions": [token]}) == "entity read as token"
    assert tb.drop_cause({"mentions": [token]}, {"mentions": [entity]}) == "token read as entity"
    other = entity | {"target": "other"}
    assert tb.drop_cause({"mentions": [entity]}, {"mentions": [other]}) is None


# ---- the L1 filter on the fixture database ----------------------------------------------


class StubLinker:
    """Placeholder addresses that may disagree with the gold's link tables."""

    def __init__(self, addresses: dict) -> None:
        self.addresses = addresses

    def placeholder_addresses(self, record: dict) -> dict:
        return self.addresses


def test_every_l1_reason(db: Path) -> None:
    swapper = {"span": [0, 7], "text": "Swapper", "type": "entity", "target": "swapper",
               "placeholder": "E1"}  # fmt: skip
    gold = {"mentions": [swapper], "gold_sql": "SELECT COUNT(*) FROM transactions "
            "WHERE from_address IN {E1}"}  # fmt: skip
    with duckdb.connect(str(db), read_only=True) as con:
        link = bb.load_link_tables(con)  # swapper -> [ROUTER], which sent nothing

        def reason(linked=gold, decision="sql", evidence="", addresses=None):
            stub = StubLinker({"E1": [ROUTER]} if addresses is None else addresses)
            return tb.l1_reason(gold, linked, {"decision": decision}, evidence, link, stub, con, 5)

        assert reason() is None
        assert reason(decision="nil") == "decision nil"
        assert reason(linked={"mentions": [swapper | {"target": "maybe"}]}) == "placeholders differ"
        assert reason(linked={"mentions": [swapper | {"placeholder": "E2"}]}) == (
            "placeholders differ"
        )
        assert reason(evidence='-- "sent": from_address IN {E}') == "bare {E} in evidence"
        assert reason(addresses={"E1": [addr(99)]}) is None  # other addresses, the same rows
        assert reason(addresses={"E1": [ALICE]}) == "linker addresses: other rows"
        assert reason(addresses={}) == "linker addresses: query failed"


# ---- builds on the fixture database --------------------------------------------------------


@pytest.fixture
def hermetic(tmp_path: Path, monkeypatch) -> dict:
    """No benchmark: an empty split and no dev question; data/schema read from the repo."""
    monkeypatch.chdir(REPO)
    (tmp_path / "bench.json").write_text('{"templates": {}}')
    (tmp_path / "dev.jsonl").write_text("")
    return {"benchmark_manifest": tmp_path / "bench.json", "dev": tmp_path / "dev.jsonl"}


def add_rows(sheet, knowledge_base_dir: Path, rows: list[dict]) -> None:
    path = knowledge_base_dir / sheet.file
    blank = dict.fromkeys(sheet.columns, "")
    kb.write_sheet(path, sheet.columns,
                   kb.read_sheet(path, sheet.columns) + [blank | r for r in rows])  # fmt: skip


def address_row(entity_id: str, address: str, name: str, is_contract: str) -> dict:
    return {"entity_id": entity_id, "address": address, "name_tag": name, "role": name,
            "is_contract": is_contract, "status": "auto"}  # fmt: skip


def alias_row(alias: str, target_type: str, target: str) -> dict:
    return {"alias": alias, "alias_norm": kb.normalize_alias(alias), "lang": "any",
            "alias_type": "name", "target_type": target_type, "target_id": target,
            "status": "auto"}  # fmt: skip


@pytest.fixture
def fillable(monkeypatch):
    """Fillers without the activity floors of the real data (the fixture is two days long)."""
    monkeypatch.setattr(bb, "load_fillers", functools.partial(
        bb.load_fillers, min_entity_calls=0, min_address_transactions=0))  # fmt: skip


def test_build_on_an_enriched_fixture(
    db: Path, knowledge_base_dir: Path, tmp_path: Path, hermetic: dict, fillable
) -> None:  # fmt: skip
    """Swapper also owns ALICE (a named wallet) and TOKEN (the only called address), so entity
    and address templates fill: each kept example is the generate-sql prompt for its question,
    B8 reads its answer back as the gold, and the manifest adds up."""
    add_rows(kb.ENTITY_ADDRESSES, knowledge_base_dir, [
        address_row("swapper", ALICE, "Swapper: Hot Wallet", "false"),
        address_row("swapper", TOKEN, "Swapper: Token", "true"),
    ])  # fmt: skip
    add_rows(kb.ALIASES, knowledge_base_dir, [alias_row("Swapper: Hot Wallet", "address", ALICE)])
    load_knowledge_base(db, knowledge_base_dir, SCHEMA)
    out = tmp_path / "train"
    templates = (T["entity_involving_tx_count"], T["entity_gas_used"], T["address_fees_spent"],
                 T["avg_base_fee"])  # fmt: skip
    m = tb.build(db, knowledge_base_dir, SCHEMA, out, count_scale=0.03, timeout=5, holdout=20,
                 templates=templates, **hermetic)  # fmt: skip
    kept, sft, dropped = (lines(out / f) for f in ("train.jsonl", "sft.jsonl", "dropped.jsonl"))
    assert {"entity_involving_tx_count", "entity_gas_used", "address_fees_spent",
            "avg_base_fee"} == {r["template_id"] for r in kept}  # fmt: skip
    assert {"entity", "address"} <= {x["type"] for r in kept for x in r["mentions"]}
    assert m["holdout_entities"] == [] and m["holdout"]["candidates"] == []  # swapper: a DEX
    linker = EntityLinker(load_resources(db, knowledge_base_dir))
    builder = PromptBuilder(linker, load_schema(db))
    with duckdb.connect(str(db), read_only=True) as con:
        senses = dict(con.execute("SELECT sense_id, kind FROM link.property_senses").fetchall())
    for r, e in zip(kept, sft, strict=True):
        assert check_record(r, senses) == []
        assert (e["question_id"], e["template_id"], e["lang"], e["split"]) == (
            r["question_id"], r["template_id"], r["lang"], r["split"])  # fmt: skip
        assert e["decision"] == "sql" and [x["role"] for x in e["messages"]] == [
            "system", "user", "assistant"]  # fmt: skip
        assert e["messages"][:2] == builder.build(linker.link(r["question"]))["messages"]
        assert extract_sql(e["messages"][2]["content"]) == r["gold_sql"]  # B8 reads it back
        assert "{E}" not in e["messages"][1]["content"]
    for name, info in m["files"].items():
        assert info["sha256"] == hashlib.sha256((out / name).read_bytes()).hexdigest()
    assert m["totals"]["generated"] == len(kept) + len(dropped)
    assert sum(m["by_lang"].values()) == m["totals"]["kept"] == len(kept)
    assert m["dropped"] == dict(Counter(r["dropped"] for r in dropped))
    assert m["totals"]["senses_mismatch"] == sum(not r["linked_senses_match"] for r in kept)


def test_a_held_out_entity_is_never_named(
    db: Path, knowledge_base_dir: Path, tmp_path: Path, hermetic: dict, fillable
) -> None:  # fmt: skip
    """Vault owns TOKEN and is no class member: it is the one candidate, so it is held out.
    No kept question or evidence names it, and its named contract fills no question."""
    add_rows(kb.ENTITIES, knowledge_base_dir, [
        {"entity_id": "vault", "name": "Vault Finance", "kind": "protocol",
         "address_set": "closed", "status": "approved"},
    ])  # fmt: skip
    add_rows(kb.ENTITY_ADDRESSES, knowledge_base_dir, [
        address_row("swapper", ALICE, "Swapper: Hot Wallet", "false"),
        address_row("vault", TOKEN, "Vault Finance: Token", "true"),
    ])  # fmt: skip
    add_rows(kb.ALIASES, knowledge_base_dir, [
        alias_row("Vault Finance", "entity", "vault"),
        alias_row("Swapper: Hot Wallet", "address", ALICE),
        alias_row("Vault Finance: Token", "address", TOKEN),
    ])  # fmt: skip
    load_knowledge_base(db, knowledge_base_dir, SCHEMA)
    out = tmp_path / "train"
    templates = (T["entity_involving_tx_count"], T["address_fees_spent"],
                 T["token_transfers_to_address"], T["avg_base_fee"])  # fmt: skip
    m = tb.build(db, knowledge_base_dir, SCHEMA, out, count_scale=0.03, timeout=5, holdout=1,
                 templates=templates, **hermetic)  # fmt: skip
    assert m["holdout_entities"] == ["vault"] and m["holdout"]["candidates"] == ["vault"]
    assert m["holdout"]["removed"]["addresses"] == [TOKEN]
    names = set(m["holdout"]["names"])
    assert names == {"vault finance"}
    kept, sft = lines(out / "train.jsonl"), lines(out / "sft.jsonl")
    assert kept, "the fixture keeps some questions"
    for r in kept:
        assert not {x.get("target") for x in r["mentions"]} & {"vault", TOKEN}
    for e in sft:
        visible = e["messages"][1]["content"].split("### Evidence\n", 1)[1]
        assert not tb.names_any(names, visible), visible
