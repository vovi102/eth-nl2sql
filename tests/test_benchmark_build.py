"""Class members and placeholders as the gold sees them, the template catalog, the split."""

from __future__ import annotations

import json
from collections import Counter
from itertools import product
from pathlib import Path

import duckdb
import pytest
from conftest import ROUTER, TOKEN

from eth_nl2sql import property_senses
from eth_nl2sql.benchmark_build import (
    LinkTables,
    build,
    build_record,
    class_members,
    entity_addresses,
    knowledge_base_version,
    load_link_tables,
    placeholder_addresses,
    split_templates,
    ties_at_limit,
)
from eth_nl2sql.benchmark_templates import GROUPS, TEMPLATES, Fill, time_fill
from eth_nl2sql.benchmark_validate import check_record, check_sql, substitute_placeholders
from eth_nl2sql.build_duckdb import load_knowledge_base

REPO = Path(__file__).parent.parent


def entity(classes: str = "", parent: str | None = None, kind: str = "protocol") -> dict:
    return {"name": "", "parent": parent, "kind": kind, "address_set": "closed",
            "classes": set(filter(None, classes.split("|")))}  # fmt: skip


def address(a: str, classes: str = "") -> dict:
    return {"address": a, "name_tag": a, "classes": classes.split("|") if classes else []}


@pytest.fixture
def link() -> LinkTables:
    """Uniswap-like tree with a classless child, a Circle-like issuer and a group."""
    entities = {
        "swap": entity("dex"),
        "swap-v2": entity("dex", parent="swap"),
        "swap-permit": entity("", parent="swap"),  # Permit2: in the entity, not in the class
        "pool": entity("dex"),
        "issuer": entity("stablecoin"),
        "issuer-bridge": entity("bridge", parent="issuer"),  # rolls up only if parent is a bridge
        "bots": entity("", kind="group"),
    }
    addresses = {
        "swap": [address("0xs1"), address("0xstoken")],
        "swap-v2": [address("0xs2")],
        "swap-permit": [address("0xpermit")],
        "pool": [address("0xp1")],
        "issuer": [address("0xusdc")],
        "issuer-bridge": [address("0xcctp")],
        # a group has no tree, but its token contracts are left out too (decision c)
        "bots": [address("0xb1", "dex"), address("0xb2"), address("0xbtoken", "dex")],
    }
    return LinkTables(
        entities=entities,
        children={"swap": {"swap-v2", "swap-permit"}, "issuer": {"issuer-bridge"}},
        addresses=addresses,
        aliases=[],
        tokens={"0xstoken": {}, "0xusdc": {}, "0xbtoken": {}},
        calls={
            "0xs1": 5,
            "0xs2": 5,
            "0xstoken": 1000,
            "0xpermit": 1000,
            "0xp1": 20,
            "0xb1": 1,
            "0xbtoken": 500,  # counted, it would put the group first
            "0xusdc": 10**6,
            "0xcctp": 3,
        },  # fmt: skip
        senses={},
    )


def test_class_members_follow_the_contract(link: LinkTables) -> None:
    # swap: its own address and the dex child, no token, no classless child -> 10 calls
    assert class_members(link, "dex") == [
        ("pool", ["0xp1"]),
        ("swap", ["0xs1", "0xs2"]),
        ("bots", ["0xb1"]),
    ]
    # the parent is no bridge: the child is a member of its own, without the parent's token
    assert class_members(link, "bridge") == [("issuer-bridge", ["0xcctp"])]


def test_entity_mentions_use_every_address(link: LinkTables) -> None:
    assert entity_addresses(link, "swap") == ["0xpermit", "0xs1", "0xs2", "0xstoken"]


def test_placeholder_addresses(link: LinkTables) -> None:
    record = {"mentions": [
        {"type": "class", "target": "dex", "top_n": 2, "placeholder": "E1"},
        {"type": "token", "target": "0xusdc", "placeholder": "E2"},
        {"type": "literal_address", "target": "0xabc"},
        {"type": "entity", "target": "issuer", "placeholder": "E3"},
    ]}  # fmt: skip
    assert placeholder_addresses(link, record) == {
        "E1": ["0xp1", "0xs1", "0xs2"],
        "E2": ["0xusdc"],
        "E3": ["0xcctp", "0xusdc"],
    }


def test_split_keeps_templates_apart_and_groups_on_both_sides() -> None:
    splits = split_templates(TEMPLATES, seed=1)
    assert splits == split_templates(TEMPLATES, seed=1)
    assert set(splits) == {t.template_id for t in TEMPLATES}
    for group in GROUPS:
        sides = Counter(splits[t.template_id] for t in TEMPLATES if t.group == group)
        assert sides["dev"] >= 1 and sides["test"] >= 1, group
    assert 0.25 <= Counter(splits.values())["dev"] / len(TEMPLATES) <= 0.4


def test_knowledge_base_version_ignores_the_directory() -> None:
    a = {"x/entities.csv": "1", "x/property_senses.csv": "2"}
    b = {"y/entities.csv": "1", "y/property_senses.csv": "2"}
    assert knowledge_base_version(a) == knowledge_base_version(b)
    assert knowledge_base_version(a) != knowledge_base_version(a | {"x/entities.csv": "3"})


# ---- the catalog: every surface of every template --------------------------------------


def dummy_fill(kind: str, lang: str) -> Fill:
    return {
        "entity": Fill("Swapper", {"type": "entity", "target": "swapper"}),
        "address": Fill("Swapper: Router", {"type": "address", "target": ROUTER},
                        "contract " if lang == "vi" else ""),
        "token": Fill("USDT", {"type": "token", "target": TOKEN}),
        "class": Fill("DEX lớn" if lang == "vi" else "major DEXs",
                      {"type": "class", "target": "dex", "top_n": 5, "top_n_source": "default"},
                      "các " if lang == "vi" else "the "),
        "literal": Fill(ROUTER, {"type": "literal_address", "target": ROUTER}),
        "nil_entity": Fill("Bybit", {"type": "nil", "nil_reason": "not_in_kb"}),
        "nil_token": Fill("ZQRT", {"type": "nil", "nil_reason": "not_in_kb"}),
    }[kind]  # fmt: skip


def test_every_surface_gives_a_valid_record_and_bindable_sql(db: Path) -> None:
    """All surfaces, slots and time kinds of the catalog, against the fixture's tables."""
    senses = {s["sense_id"]: s["kind"] for s in property_senses.usable(REPO / "data/schema").senses}
    problems, n = [], 0
    with duckdb.connect(str(db), read_only=True) as con:
        for template in TEMPLATES:
            for variant in template.variants:
                for lang, surfaces in variant.surfaces.items():
                    for surface, kind in product(surfaces, template.times):
                        fills = {slot: dummy_fill(k, lang) for slot, k in variant.slots.items()}
                        fills["time"] = time_fill(kind, lang, 2, 4)
                        extra = {"scale": "1e6", "literal": f"'{ROUTER}'"}
                        r = build_record(template, variant, lang, surface, fills, extra, senses)
                        r["question_id"] = "q00001"
                        n += 1
                        errors = check_record(r, senses)
                        if (r["gold_sql"] is None) != (template.group in ("nil", "unsupported")):
                            errors.append("gold_sql presence")
                        if r["gold_sql"] is not None:
                            errors += check_sql(r["gold_sql"])
                            names = {m["placeholder"] for m in r["mentions"] if "placeholder" in m}
                            sql = substitute_placeholders(
                                r["gold_sql"], dict.fromkeys(names, [TOKEN])
                            )
                            try:  # binds every table and column without reading the data
                                con.execute("EXPLAIN " + sql)
                            except duckdb.Error as e:
                                errors.append(str(e).splitlines()[0])
                        if errors:
                            problems.append((r["template_id"], r["question"], errors))
    assert n > 400
    assert problems == []


def test_build_on_the_fixture_database(
    db: Path, knowledge_base_dir: Path, property_senses_dir: Path, tmp_path: Path
) -> None:
    """Most templates find no filler in a two-day fixture; block producers do.

    The fixture's link schema is reloaded with the real property senses the catalog uses.
    """
    load_knowledge_base(db, knowledge_base_dir, REPO / "data/schema")
    out = tmp_path / "benchmark"
    manifest = build(db, knowledge_base_dir, property_senses_dir, out, count_scale=0.2)
    with duckdb.connect(str(db), read_only=True) as con:
        senses = dict(con.execute("SELECT sense_id, kind FROM link.property_senses").fetchall())
        link = load_link_tables(con)
    assert link.entities["swapper"]["classes"] == {"dex"}
    records = [
        json.loads(line)
        for split in ("dev", "test")
        for line in (out / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert manifest["templates"]["top_block_producers"]["questions"] > 0
    assert sum(info["questions"] for info in manifest["files"].values()) == len(records)
    assert len({r["question_id"] for r in records}) == len(records)
    for r in records:
        assert r["knowledge_base_version"] == manifest["knowledge_base_version"]
        assert check_record(r, senses) == []
    answerable = {r["question_id"] for r in records if r["gold_sql"] is not None}
    assert set(manifest["discriminates"]) == answerable
    for checks in manifest["discriminates"].values():
        assert set(checks) <= {"status", "direction", "time"}
        assert all(v in (True, False, None) for v in checks.values())


def test_ties_at_limit() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('a', 3), ('b', 2), ('c', 2)) v(a, n)")
    top = "SELECT a, n FROM t ORDER BY n DESC NULLS LAST, a ASC NULLS LAST LIMIT {k}"
    no_link = LinkTables  # a record without mentions never looks at the link tables
    assert ties_at_limit({"gold_sql": top.format(k=2), "mentions": []}, no_link, con, 5)
    assert not ties_at_limit({"gold_sql": top.format(k=1), "mentions": []}, no_link, con, 5)
    assert not ties_at_limit({"gold_sql": top.format(k=3), "mentions": []}, no_link, con, 5)
    assert not ties_at_limit({"gold_sql": None, "mentions": []}, no_link, con, 5)
