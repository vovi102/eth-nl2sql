"""Prompts (B6/B7) on the tiny fixture database, and against the dev gold on the real one."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import ALICE, ROUTER, TOKEN

from eth_nl2sql.entity_linking import EntityLinker
from eth_nl2sql.entity_linking_resources import load_resources
from eth_nl2sql.prompt_builder import PromptBuilder, PromptOptions, load_schema

REAL_DB = Path(__file__).parent.parent / "data/db/ethereum.duckdb"
REAL_KNOWLEDGE_BASE = Path(__file__).parent.parent / "data/entity_knowledge_base"
DEV = Path(__file__).parent.parent / "data/benchmark/dev.jsonl"


@pytest.fixture
def builder(db: Path, knowledge_base_dir: Path) -> PromptBuilder:
    return PromptBuilder(EntityLinker(load_resources(db, knowledge_base_dir)), load_schema(db))


def user_part(prompt: dict, title: str) -> str:
    user = prompt["messages"][1]["content"]
    return user.split(f"### {title}\n", 1)[1].split("\n\n###", 1)[0]


def test_prompt_with_evidence_and_placeholders(builder: PromptBuilder) -> None:
    record = builder.linker.link("Swapper nhận bao nhiêu ETH?", "q1")
    prompt = builder.build(record)
    assert prompt["decision"] == "sql" and prompt["question_id"] == "q1"
    system, user = (m["content"] for m in prompt["messages"])
    assert "{E1} exactly" in system
    assert user.count("CREATE TABLE") == 6 and prompt["tables"][0] == "blocks"
    assert "BigQuery says: transactions.value." in user
    assert user_part(prompt, "Evidence").splitlines() == [
        "-- Table: transactions",
        '-- "Swapper": {E1} = all 1 addresses of Swapper (protocol; dex)',
        '-- "nhận": to_address IN {E1} (from_address is the sender)',
        '-- "bao nhiêu ETH": value / 1e18 (unit: ETH)',
        "-- No time range: use all rows (the data holds September 2026 only).",
    ]
    assert user.endswith("### Question\nSwapper nhận bao nhiêu ETH?")
    assert prompt["chars"] == len(system) + len(user)


def test_linked_schema_keeps_the_main_table(builder: PromptBuilder) -> None:
    record = builder.linker.link("Swapper nhận bao nhiêu ETH?")
    prompt = builder.build(record, PromptOptions(schema="linked"))
    assert prompt["tables"] == ["transactions"]
    assert prompt["messages"][1]["content"].count("CREATE TABLE") == 1


def test_literal_ablation_writes_the_addresses(builder: PromptBuilder) -> None:
    record = builder.linker.link("Swapper nhận bao nhiêu ETH?")
    prompt = builder.build(record, PromptOptions(placeholders=False))
    evidence = user_part(prompt, "Evidence")
    assert "{E1}" not in evidence and f"to_address IN ('{ROUTER}')" in evidence
    assert "{E1}" not in prompt["messages"][0]["content"]


def test_without_linking_there_is_no_evidence_and_no_abstention(builder: PromptBuilder) -> None:
    record = builder.linker.link("Số dư của Swapper là bao nhiêu?")
    assert builder.build(record)["decision"] == "unsupported"
    prompt = builder.build(record, PromptOptions(evidence=False))
    assert prompt["decision"] == "sql"
    assert "### Evidence" not in prompt["messages"][1]["content"]


def test_nil_mention_abstains(builder: PromptBuilder) -> None:
    prompt = builder.build(builder.linker.link("Bybit nhận bao nhiêu ETH?"))
    assert (prompt["decision"], prompt["answer"]) == ("nil", "Unknown name: Bybit.")
    assert "messages" not in prompt


def token_record() -> dict:
    """A gold record (oracle linking): the token fixes the table and its decimals."""
    record = {
        "question": f"Lượng USDT {ALICE} nhận ngày 1/9?",
        "mentions": [
            {"span": [6, 10], "text": "USDT", "type": "token", "target": TOKEN,
             "placeholder": "E1"},
            {"span": [11, 53], "text": ALICE, "type": "literal_address", "target": ALICE},
        ],
        "properties": [
            {"sense_id": "token_amount", "span": [0, 10], "text": "Lượng USDT", "implicit": False},
            {"sense_id": "received_by", "span": [54, 58], "text": "nhận", "implicit": False},
        ],
        "time": {"start": "2026-09-01T00:00:00", "end": "2026-09-02T00:00:00",
                 "span": [59, 67], "text": "ngày 1/9"},
        "unsupported": [], "warnings": [],
    }  # fmt: skip
    return record


def test_token_evidence(builder: PromptBuilder) -> None:
    lines = builder.evidence(token_record())
    assert lines == [
        "-- Table: token_transfers",
        '-- "USDT": {E1} = the contract of the token Tether USD (USDT), decimals 6',
        f'-- "{ALICE}": an address typed in the question, used as a literal',
        '-- "Lượng USDT": CAST(value AS DECIMAL(38,0)) / 1e6',
        f"-- \"nhận\": to_address = '{ALICE}' (from_address is the sender)",
        "-- \"ngày 1/9\": block_timestamp >= TIMESTAMP '2026-09-01 00:00:00' "
        "AND block_timestamp < TIMESTAMP '2026-09-02 00:00:00'",
    ]


def test_schema_mode_is_checked() -> None:
    with pytest.raises(ValueError):
        PromptOptions(schema="some")


# ---- the real database --------------------------------------------------------------------


@pytest.mark.skipif(
    not (REAL_DB.is_file() and DEV.is_file()), reason="needs data/db/ethereum.duckdb"
)
def test_dev_gold_predicates_are_in_the_evidence() -> None:
    """Every WHERE predicate of a dev gold query is written, verbatim, in its evidence."""
    builder = PromptBuilder(
        EntityLinker(load_resources(REAL_DB, REAL_KNOWLEDGE_BASE)), load_schema(REAL_DB)
    )
    for line in DEV.read_text(encoding="utf-8").splitlines():
        gold = json.loads(line)
        prompt = builder.build(gold)
        if gold["gold_sql"] is None:
            assert prompt["decision"] in ("nil", "unsupported")
            continue
        evidence = "\n".join(builder.evidence(gold))
        where = re.split(r"\bWHERE\b", gold["gold_sql"])[1:]
        clause = re.split(r"\b(?:GROUP|ORDER) BY\b", where[0])[0] if where else ""
        for predicate in filter(None, (p.strip() for p in clause.split(" AND "))):
            assert predicate in evidence, (gold["question_id"], predicate)
