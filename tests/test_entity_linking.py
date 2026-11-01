"""Linking on the tiny fixture database, and the gold examples on the real one.

The examples of docs/linking-labels.md §7 are the contract: the linker's output must equal
them, `candidates` aside. They need data/db/ethereum.duckdb and are skipped without it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FAKE, ROUTER, TOKEN

from eth_nl2sql import entity_knowledge_base as knowledge_base
from eth_nl2sql.entity_linking import (
    EntityLinker,
    singular,
    stem,
    substitute_placeholders,
    tokenize,
)
from eth_nl2sql.entity_linking_resources import load_resources

REAL_DB = Path(__file__).parent.parent / "data/db/ethereum.duckdb"
REAL_KNOWLEDGE_BASE = Path(__file__).parent.parent / "data/entity_knowledge_base"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
COINBASE_10 = "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43"


def without_candidates(record: dict) -> dict:
    return record | {
        "mentions": [{k: v for k, v in m.items() if k != "candidates"} for m in record["mentions"]]
    }


def test_tokenize_keeps_positions_and_alias_form() -> None:
    question = "Tornado.Cash nhận USD₮ ngày 5/9?"
    words = tokenize(question)
    assert [w.norm for w in words] == ["tornado", "cash", "nhan", "usd₮", "ngay", "5", "9"]
    assert all(question[w.start : w.end] == w.text for w in words)


@pytest.mark.parametrize(
    ("word", "expected"),
    [("dexs", "dex"), ("dexes", "dex"), ("bridges", "bridge"), ("addresses", "address"),
     ("address", "address"), ("gas", "gas"), ("usdt", "usdt"), ("l2s", "l2"), ("l2", "l2")],
)  # fmt: skip
def test_singular(word: str, expected: str) -> None:
    assert singular(word) == expected


def test_stem_joins_verb_forms() -> None:
    assert stem("receive") == stem("received") == stem("receives") == stem("receiving")
    assert stem("send") == stem("sent") == stem("sends")
    assert stem("transactions") == stem("transaction")
    assert stem("nhieu") == "nhieu"


# ---- the fixture database (tests/conftest.py) --------------------------------------------


@pytest.fixture
def linker(db: Path, knowledge_base_dir: Path) -> EntityLinker:
    return EntityLinker(load_resources(db, knowledge_base_dir))


def test_links_entity_properties_and_implicit_success(linker: EntityLinker) -> None:
    record = linker.link("Swapper nhận bao nhiêu ETH?", "q1")
    assert record["question_id"] == "q1" and record["lang"] == "vi"
    assert without_candidates(record)["mentions"] == [
        {"span": [0, 7], "text": "Swapper", "type": "entity", "target": "swapper",
         "placeholder": "E1"},
    ]  # fmt: skip
    assert [(p["sense_id"], p["implicit"]) for p in record["properties"]] == [
        ("received_by", False),
        ("eth_amount", False),
    ]  # tx_success is not an approved sense of the fixture, so it is not added
    assert record["time"] is None and record["warnings"] == []


def test_token_prior_picks_the_real_token(linker: EntityLinker) -> None:
    mention = linker.link("Lượng token USDT nhận được?")["mentions"][0]
    assert (mention["type"], mention["target"]) == ("token", TOKEN)
    assert [c["target"] for c in mention["candidates"]] == [TOKEN, FAKE]


def test_unreviewed_and_unknown_names_are_nil(linker: EntityLinker) -> None:
    mentions = linker.link("Maybe và Bybit nhận bao nhiêu ETH?")["mentions"]
    assert [(m["text"], m["type"], m["nil_reason"]) for m in mentions] == [
        ("Maybe", "nil", "not_reviewed"),
        ("Bybit", "nil", "not_in_kb"),
    ]
    assert all("placeholder" not in m for m in mentions)


def test_type_word_around_an_unreviewed_name(db: Path, knowledge_base_dir: Path) -> None:
    """ "Swapper Protocol" is the reviewed Swapper plus a type word, even if an unreviewed
    entity also carries that alias; "Maybe Labs" is only the unreviewed one."""
    path = knowledge_base_dir / knowledge_base.ALIASES.file
    rows = knowledge_base.read_sheet(path)
    for alias in ("Swapper Protocol", "Maybe Labs"):
        rows.append(rows[0] | {"alias": alias, "alias_norm": knowledge_base.normalize_alias(alias),
                               "target_type": "entity", "target_id": "maybe"})  # fmt: skip
    knowledge_base.write_sheet(path, knowledge_base.ALIASES.columns, rows)
    linker = EntityLinker(load_resources(db, knowledge_base_dir))
    first = linker.link("Swapper Protocol nhận bao nhiêu ETH?")["mentions"][0]
    assert (first["text"], first["type"], first["target"]) == ("Swapper", "entity", "swapper")
    first = linker.link("Maybe Labs nhận bao nhiêu ETH?")["mentions"][0]
    assert (first["text"], first["nil_reason"]) == ("Maybe Labs", "not_reviewed")


def test_exact_unreviewed_name_beats_a_close_reviewed_one(
    db: Path, knowledge_base_dir: Path
) -> None:
    """ "Swappers" is an unreviewed entity, not a typo of the reviewed "Swapper"."""
    path = knowledge_base_dir / knowledge_base.ALIASES.file
    rows = knowledge_base.read_sheet(path)
    rows.append(rows[0] | {"alias": "Swappers", "alias_norm": "swappers", "target_id": "maybe"})
    knowledge_base.write_sheet(path, knowledge_base.ALIASES.columns, rows)
    linker = EntityLinker(load_resources(db, knowledge_base_dir))
    (mention,) = linker.link("Có bao nhiêu giao dịch gọi Swappers?")["mentions"]
    assert (mention["text"], mention["nil_reason"]) == ("Swappers", "not_reviewed")


def test_class_mention_and_its_addresses(linker: EntityLinker) -> None:
    record = linker.link("Top 3 DEXs received how much ETH?")
    (mention,) = record["mentions"]
    assert mention["text"] == "Top 3 DEXs" and mention["target"] == "dex"
    assert (mention["top_n"], mention["top_n_source"]) == (3, "explicit")
    assert linker.placeholder_addresses(record) == {"E1": [ROUTER]}
    default = linker.link("các DEX lớn nhận bao nhiêu ETH")["mentions"][0]
    assert (default["text"], default["top_n"], default["top_n_source"]) == ("DEX lớn", 5, "default")


def test_unsupported_sense(linker: EntityLinker) -> None:
    record = linker.link("Số dư của Swapper là bao nhiêu?")
    assert record["unsupported"] == [{"sense_id": "balance", "span": [0, 5], "text": "Số dư"}]
    assert record["properties"] == []


def test_placeholders_follow_order_and_share_targets(linker: EntityLinker) -> None:
    record = linker.link(f"USDT gửi tới Swapper, rồi Swapper gửi tới {ROUTER}")
    assert [(m["text"], m.get("placeholder")) for m in record["mentions"]] == [
        ("USDT", "E1"),
        ("Swapper", "E2"),
        ("Swapper", "E2"),
        (ROUTER, None),
    ]
    addresses = linker.placeholder_addresses(record)
    assert addresses == {"E1": [TOKEN], "E2": [ROUTER]}
    sql = substitute_placeholders("to_address IN {E2} AND token_address IN {E1}", addresses)
    assert sql == f"to_address IN ('{ROUTER}') AND token_address IN ('{TOKEN}')"


# ---- the real database --------------------------------------------------------------------

real = pytest.mark.skipif(not REAL_DB.is_file(), reason="needs data/db/ethereum.duckdb")


@pytest.fixture(scope="module")
def real_linker() -> EntityLinker:
    return EntityLinker(load_resources(REAL_DB, REAL_KNOWLEDGE_BASE))


def implicit(sid: str) -> dict:
    return {"sense_id": sid, "span": None, "text": None, "implicit": True}


def explicit(sid: str, start: int, end: int, text: str) -> dict:
    return {"sense_id": sid, "span": [start, end], "text": text, "implicit": False}


def time(start: str, end: str, span: list[int], text: str) -> dict:
    return {"start": f"2026-{start}T00:00:00", "end": f"2026-{end}T00:00:00", "span": span,
            "text": text}  # fmt: skip


# docs/linking-labels.md §7, examples 1-8
GOLD_EXAMPLES = [
    {
        "question": "Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9?", "lang": "vi",
        "mentions": [{"span": [0, 12], "text": "Tornado Cash", "type": "entity",
                      "target": "tornado-cash", "placeholder": "E1"}],
        "properties": [explicit("received_by", 13, 17, "nhận"),
                       explicit("eth_amount", 18, 31, "bao nhiêu ETH"), implicit("tx_success")],
        "time": time("09-01", "09-08", [38, 54], "tuần đầu tháng 9"),
        "unsupported": [], "warnings": [],
    },
    {
        "question": "Có bao nhiêu lượt chuyển USDT trong ngày 5/9?", "lang": "vi",
        "mentions": [{"span": [25, 29], "text": "USDT", "type": "token", "target": USDT,
                      "placeholder": "E1"}],
        "properties": [explicit("transfer_count", 3, 24, "bao nhiêu lượt chuyển"),
                       implicit("of_token")],
        "time": time("09-05", "09-06", [36, 44], "ngày 5/9"),
        "unsupported": [], "warnings": [],
    },
    {
        "question": "How much ETH did the major DEXs receive on September 5?", "lang": "en",
        "mentions": [{"span": [21, 31], "text": "major DEXs", "type": "class", "target": "dex",
                      "placeholder": "E1", "top_n": 5, "top_n_source": "default"}],
        "properties": [explicit("eth_amount", 0, 12, "How much ETH"),
                       explicit("received_by", 32, 39, "receive"), implicit("tx_success")],
        "time": time("09-05", "09-06", [40, 54], "on September 5"),
        "unsupported": [], "warnings": [],
    },
    {
        "question": "Ví Coinbase 10 nhận bao nhiêu ETH ngày 1/9?", "lang": "vi",
        "mentions": [{"span": [3, 14], "text": "Coinbase 10", "type": "address",
                      "target": COINBASE_10, "placeholder": "E1"}],
        "properties": [explicit("received_by", 15, 19, "nhận"),
                       explicit("eth_amount", 20, 33, "bao nhiêu ETH"), implicit("tx_success")],
        "time": time("09-01", "09-02", [34, 42], "ngày 1/9"),
        "unsupported": [], "warnings": [],
    },
    {
        "question": "How many transactions did 0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511 send?",
        "lang": "en",
        "mentions": [{"span": [26, 68], "text": "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511",
                      "type": "literal_address",
                      "target": "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511"}],
        "properties": [explicit("tx_count", 0, 21, "How many transactions"),
                       explicit("sent_by", 69, 73, "send")],
        "time": None, "unsupported": [], "warnings": [],
    },
    {
        "question": "Binance nhận bao nhiêu ETH trong tháng 9?", "lang": "vi",
        "mentions": [{"span": [0, 7], "text": "Binance", "type": "entity", "target": "binance",
                      "placeholder": "E1"}],
        "properties": [explicit("received_by", 8, 12, "nhận"),
                       explicit("eth_amount", 13, 26, "bao nhiêu ETH"), implicit("tx_success")],
        "time": time("09-01", "10-01", [33, 40], "tháng 9"),
        "unsupported": [], "warnings": ["open_address_set:binance"],
    },
    {
        "question": "Bybit nhận bao nhiêu ETH trong tháng 9?", "lang": "vi",
        "mentions": [{"span": [0, 5], "text": "Bybit", "type": "nil", "nil_reason": "not_in_kb"}],
        "properties": [explicit("received_by", 6, 10, "nhận"),
                       explicit("eth_amount", 11, 24, "bao nhiêu ETH")],
        "time": time("09-01", "10-01", [31, 38], "tháng 9"),
        "unsupported": [], "warnings": [],
    },
    {
        "question": "Số dư của ví Coinbase 10 là bao nhiêu?", "lang": "vi",
        "mentions": [{"span": [13, 24], "text": "Coinbase 10", "type": "address",
                      "target": COINBASE_10, "placeholder": "E1"}],
        "properties": [], "time": None,
        "unsupported": [{"sense_id": "balance", "span": [0, 5], "text": "Số dư"}],
        "warnings": [],
    },
]  # fmt: skip


def same_record(got: dict, expected: dict) -> None:
    got = without_candidates(got)
    key = lambda p: p["sense_id"]  # noqa: E731  the order of properties does not matter
    assert sorted(got.pop("properties"), key=key) == sorted(expected["properties"], key=key)
    assert got == {k: v for k, v in expected.items() if k != "properties"}


@real
@pytest.mark.parametrize("expected", GOLD_EXAMPLES, ids=lambda e: e["question"][:30])
def test_gold_examples(real_linker: EntityLinker, expected: dict) -> None:
    same_record(real_linker.link(expected["question"]), expected)


def first(linker: EntityLinker, question: str) -> tuple:
    m = linker.link(question)["mentions"][0]
    return m["text"], m["type"], m.get("target") or m.get("nil_reason")


@real
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # Tether: the company receives ETH, the token is transferred
        ("Tether nhận bao nhiêu ETH trong tháng 9?", ("Tether", "entity", "tether")),
        ("Có bao nhiêu lượt chuyển Tether ngày 5/9?", ("Tether", "token", USDT)),
        ("Công ty Tether gửi bao nhiêu ETH?", ("Tether", "entity", "tether")),
        ("Đồng Tether có bao nhiêu lượt chuyển?", ("Tether", "token", USDT)),
        # Uniswap: the protocol or the UNI token
        ("How much ETH did Uniswap receive in September?", ("Uniswap", "entity", "uniswap")),
        ("How many transfers of the Uniswap token on September 2?", ("Uniswap", "token", UNI)),
        ("Giao thức Uniswap nhận bao nhiêu ETH?", ("Uniswap", "entity", "uniswap")),
        # 'Uniswap Protocol' is all of Uniswap; its Permit2 child is called by name (review)
        ("Uniswap Protocol received how much ETH?", ("Uniswap Protocol", "entity", "uniswap")),
        ("Permit2 được gọi bao nhiêu lần?", ("Permit2", "entity", "uniswap-protocol")),
        ("Có bao nhiêu lượt chuyển UNI ngày 3/9?", ("UNI", "token", UNI)),
        # homoglyph copycat: never the real USDT
        ("Có bao nhiêu lượt chuyển ꒤5DT trong ngày 5/9?",
         ("꒤5DT", "token", "0xdd44e894d8a7d765e45553347309c55a3c639a5f")),
        # Vietnamese without diacritics, lower case
        ("tornado cash nhan bao nhieu eth trong tuan dau thang 9",
         ("tornado cash", "entity", "tornado-cash")),
        ("vi coinbase 10 nhan bao nhieu eth ngay 1/9", ("coinbase 10", "address", COINBASE_10)),
        ("may tron tornado nhan bao nhieu eth", ("may tron tornado", "entity", "tornado-cash")),
        # typos, wallets that do not exist, entities not reviewed yet
        ("Tornado Cassh nhận bao nhiêu ETH?", ("Tornado Cassh", "entity", "tornado-cash")),
        ("Coinbase 99 nhận bao nhiêu ETH?", ("Coinbase 99", "nil", "not_in_kb")),
        ("sàn bybit nhận bao nhiêu ETH?", ("bybit", "nil", "not_in_kb")),
        # classes
        ("Các sàn lớn nhận bao nhiêu ETH?", ("sàn lớn", "class", "cex")),
        ("5 DEX lớn nhất nhận bao nhiêu ETH ngày 5/9?", ("5 DEX lớn nhất", "class", "dex")),
        ("Các stablecoin lớn nhất nhận bao nhiêu ETH?",
         ("stablecoin lớn nhất", "class", "stablecoin")),
    ],
)  # fmt: skip
def test_hard_cases(real_linker: EntityLinker, question: str, expected: tuple) -> None:
    assert first(real_linker, question) == expected


@real
def test_pending_entity_is_not_reviewed(real_linker: EntityLinker) -> None:
    """Any entity still pending in the CSV (which one changes as review goes on)."""
    pending = [
        e["name"]
        for e in knowledge_base.read_sheet(REAL_KNOWLEDGE_BASE / knowledge_base.ENTITIES.file)
        if e["status"] == "pending"
        and e["name"].isalpha()
        and e["name"][0].isupper()
        and knowledge_base.normalize_alias(e["name"]) not in real_linker.resources.aliases
    ]
    if not pending:
        pytest.skip("every entity is reviewed")
    assert first(real_linker, f"{pending[0]} nhận bao nhiêu ETH?") == (
        pending[0],
        "nil",
        "not_reviewed",
    )


@real
@pytest.mark.parametrize(
    "question",
    [
        "How many transactions did the top 3 bridges send on September 5?",
        "Did anyone send ETH in September?",  # 'send', 'one' are token symbols
        "Có bao nhiêu block trong ngày 5/9?",  # 'Block' is a token name
    ],
)
def test_common_words_are_no_tokens(real_linker: EntityLinker, question: str) -> None:
    types = {m["type"] for m in real_linker.link(question)["mentions"]}
    assert "token" not in types and "nil" not in types


@real
def test_class_members_roll_up_to_the_parent(real_linker: EntityLinker) -> None:
    dex = dict(real_linker.class_members("dex", 5))
    assert "uniswap" in dex and not {"uniswap-v2", "uniswap-v3", "uniswap-v4"} & set(dex)
    v3 = set(real_linker.resources.addresses_of("uniswap-v3")) - set(real_linker.resources.tokens)
    assert v3 <= set(dex["uniswap"])
    # a child reviewed without the class (Uniswap Protocol: Permit2) is not counted
    classless = [
        e
        for e in real_linker.resources.subtree("uniswap")
        if "dex" not in real_linker.resources.entities[e]["classes"]
    ]
    for e in classless:
        assert not set(real_linker.resources.addresses_of(e)) & set(dex["uniswap"])
    bridges = dict(real_linker.class_members("bridge", 20))
    assert "circle-cctp" in bridges and "circle" not in bridges  # Circle is no bridge


@real
def test_class_members_hold_no_token_contract(real_linker: EntityLinker) -> None:
    """Decision c of linking-labels.md: Circle CCTP without USDC, Sky without DAI."""
    res = real_linker.resources
    for class_id in res.classes:
        for _, addresses in real_linker.class_members(class_id, 10**6):
            assert not set(addresses) & set(res.tokens), class_id


def mention_of(record: dict, text: str) -> tuple:
    (m,) = [m for m in record["mentions"] if m["text"] == text]
    return m["type"], m.get("target") or m.get("nil_reason")


# Errors seen on data/benchmark/dev.jsonl (2026-11-01), each fixed at its cause.
@real
@pytest.mark.parametrize(
    ("question", "text", "expected"),
    [
        ("What total ETH amount did the major L2s receive on September 18?", "major L2s",
         ("class", "layer-2")),
        # a quantity word or a transfer verb right before a name makes it a token
        ("Ví Coinbase Prime 2 nhận bao nhiêu Ondo ngày 30/9?", "Ondo",
         ("token", "0xfaba6f8e4a5e8ab82f62fe7c39859fa577269be3")),
        ("Số ví hoạt động chuyển SUSHI là bao nhiêu?", "SUSHI",
         ("token", "0x6b3595068778dd592e39a122f4f5a5cf09c90fe2")),
        ("Số ví hoạt động chuyển Tether Gold từ 7/9 đến 9/9 là bao nhiêu?", "Tether Gold",
         ("token", "0x68749665ff8d2d112fa859aa293f07a622782f38")),
        # a title-case token name of common words; the phrase "giá trị USD" gives way
        ("Tổng giá trị USD Coin mà ví Shakepay 9 nhận là bao nhiêu?", "USD Coin",
         ("token", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")),
        ("Which 5 addresses called null address the most from September 21 to 24?",
         "null address", ("entity", "null")),
        # a typo does not add a word to a name
        ("What total transaction fee did callers of jaredfromsubway.eth pay in September?",
         "jaredfromsubway.eth", ("entity", "jaredfromsubway")),
    ],
)  # fmt: skip
def test_dev_regressions(real_linker: EntityLinker, question: str, text: str, expected) -> None:
    assert mention_of(real_linker.link(question), text) == expected


@real
def test_amount_of_a_token_and_no_stray_nil(real_linker: EntityLinker) -> None:
    record = real_linker.link("Tổng giá trị USD Coin mà ví Shakepay 9 nhận là bao nhiêu?")
    assert record["unsupported"] == []
    assert {"token_amount", "received_by", "of_token"} <= {
        p["sense_id"] for p in record["properties"]
    }
    record = real_linker.link("Ví Bitget 6 nhận bao nhiêu FET trong tháng 9?")
    (amount,) = [p for p in record["properties"] if p["sense_id"] == "token_amount"]
    assert amount["text"] == "bao nhiêu" and not amount["implicit"]
    record = real_linker.link("Across Protocol đã chuyển đi bao nhiêu ETH ngày 5/9?")
    assert [m["type"] for m in record["mentions"]] == ["entity"]
    # one word, one phrase: "transfers of" is part of "number of transfers"
    record = real_linker.link("What was the daily number of transfers of SHIB in September?")
    of_token = [p for p in record["properties"] if p["sense_id"] == "of_token"]
    assert [p["implicit"] for p in of_token] == [True]
    # ... but an overlapping phrase that adds a content word stays: "giao dịch blob"
    record = real_linker.link("Số giao dịch blob trong ngày 3/9")
    assert {p["sense_id"] for p in record["properties"]} == {"tx_count", "tx_blob"}
    assert record["mentions"] == []
