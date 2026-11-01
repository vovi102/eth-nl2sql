"""Rendering templates into linking records: spans, placeholders, implicit senses, time."""

from __future__ import annotations

import pytest

from eth_nl2sql.benchmark_templates import (
    Fill,
    assign_placeholders,
    implicit_senses,
    render,
    time_condition,
    time_fill,
)

UNSUPPORTED = {"balance", "usd_value", "internal_eth"}
TORNADO = Fill("Tornado Cash", {"type": "entity", "target": "tornado-cash"})


def test_render_reproduces_contract_example_1() -> None:
    """docs/linking-labels.md §7, example 1: every span comes out of the markup."""
    r = render(
        "{target} [nhận](received_by) [bao nhiêu ETH](eth_amount) {time}?",
        {"target": TORNADO, "time": time_fill("week", "vi", 1)},
        UNSUPPORTED,
    )
    assert r.question == "Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9?"
    assert r.mentions == [
        {"span": [0, 12], "text": "Tornado Cash", "type": "entity", "target": "tornado-cash"}
    ]
    assert r.slots == ["target"]
    assert [(p["sense_id"], p["span"], p["text"]) for p in r.properties] == [
        ("received_by", [13, 17], "nhận"),
        ("eth_amount", [18, 31], "bao nhiêu ETH"),
    ]
    assert r.time == {
        "start": "2026-09-01T00:00:00",
        "end": "2026-09-08T00:00:00",
        "span": [38, 54],
        "text": "tuần đầu tháng 9",
    }
    assert implicit_senses(r.properties, r.mentions, r.unsupported) == [
        {"sense_id": "tx_success", "span": None, "text": None, "implicit": True}
    ]


def test_render_reproduces_contract_example_3() -> None:
    """A class mention: "the" stays outside the span, the size word inside."""
    dex = Fill(
        "major DEXs",
        {"type": "class", "target": "dex", "top_n": 5, "top_n_source": "default"},
        prefix="the ",
    )
    r = render(
        "[How much ETH](eth_amount) did {target} [receive](received_by) {time}?",
        {"target": dex, "time": time_fill("day", "en", 5)},
        UNSUPPORTED,
    )
    assert r.question == "How much ETH did the major DEXs receive on September 5?"
    assert r.mentions[0]["span"] == [21, 31]
    assert [p["span"] for p in r.properties] == [[0, 12], [32, 39]]
    assert r.time["span"] == [40, 54]


def test_render_without_time_and_capitalization() -> None:
    wallet = Fill("Coinbase 10", {"type": "address", "target": "0xa9"}, prefix="ví ")
    surface = "{target} [nhận](received_by) bao nhiêu ETH {time}?"
    r = render(surface, {"target": wallet, "time": None}, UNSUPPORTED)
    assert r.question == "Ví Coinbase 10 nhận bao nhiêu ETH?"  # the prefix starts the sentence
    assert r.mentions[0]["span"] == [3, 14] and r.time is None
    lower = Fill("ether.fi", {"type": "entity", "target": "ether-fi"})
    r = render("{target} [nhận](received_by) gì?", {"target": lower}, UNSUPPORTED)
    assert r.question.startswith("ether.fi")  # a name keeps its own case


def test_unsupported_senses_are_kept_apart() -> None:
    r = render("[Số dư](balance) của {target} là bao nhiêu?", {"target": TORNADO}, UNSUPPORTED)
    assert r.properties == []
    assert r.unsupported == [{"sense_id": "balance", "span": [0, 5], "text": "Số dư"}]


@pytest.mark.parametrize(
    ("said", "mention_types", "unsupported", "expected"),
    [
        ({"eth_amount", "received_by"}, ["entity"], [], ["tx_success"]),
        ({"eth_amount", "sent_by"}, ["literal_address"], [], ["tx_success"]),
        ({"eth_amount", "received_by"}, ["nil"], [], []),  # NIL is not a linked mention
        ({"eth_amount", "received_by", "tx_failed"}, ["entity"], [], []),  # said explicitly
        ({"eth_amount"}, ["entity"], [], []),  # no direction
        ({"tx_count", "contract_called"}, ["entity"], [], []),
        ({"transfer_count"}, ["token"], [], ["of_token"]),
        ({"transfer_count", "of_token"}, ["token"], [], []),
        ({"eth_amount", "received_by"}, ["entity", "token"], [], ["tx_success", "of_token"]),
        ({"eth_amount", "received_by"}, ["entity"], [{"sense_id": "usd_value"}], []),
    ],
)
def test_implicit_senses(said, mention_types, unsupported, expected) -> None:
    properties = [{"sense_id": s} for s in sorted(said)]
    mentions = [{"type": t} for t in mention_types]
    assert [p["sense_id"] for p in implicit_senses(properties, mentions, unsupported)] == expected


def test_placeholders_follow_order_of_appearance_and_share_targets() -> None:
    mentions = [
        {"span": [30, 34], "type": "token", "target": "0xusdt"},
        {"span": [0, 12], "type": "entity", "target": "tornado-cash"},
        {"span": [40, 42], "type": "literal_address", "target": "0xabc"},
        {"span": [50, 62], "type": "entity", "target": "tornado-cash"},
    ]
    assign_placeholders(mentions)
    assert [m.get("placeholder") for m in mentions] == ["E2", "E1", None, "E1"]


@pytest.mark.parametrize(
    ("kind", "lang", "args", "text", "prefix", "window"),
    [
        ("month", "vi", (), "tháng 9", "trong ", ("2026-09-01", "2026-10-01")),
        ("day", "vi", (5,), "ngày 5/9", "", ("2026-09-05", "2026-09-06")),
        ("week", "en", (2,), "in the second week of September", "", ("2026-09-08", "2026-09-15")),
        ("week", "vi", (4,), "tuần thứ 4 tháng 9", "trong ", ("2026-09-22", "2026-09-29")),
        ("last_week", "en", (), "in the last week of September", "", ("2026-09-24", "2026-10-01")),
        ("half", "vi", (), "nửa đầu tháng 9", "trong ", ("2026-09-01", "2026-09-16")),
        ("range", "vi", (10, 15), "từ 10/9 đến 15/9", "", ("2026-09-10", "2026-09-16")),
    ],
)
def test_time_fill(kind, lang, args, text, prefix, window) -> None:
    fill = time_fill(kind, lang, *args)
    assert (fill.text, fill.prefix) == (text, prefix)
    assert tuple(str(d) for d in fill.time) == window


def test_time_condition_is_half_open() -> None:
    fill = time_fill("day", "en", 5)
    assert time_condition(fill.time) == (
        "block_timestamp >= TIMESTAMP '2026-09-05 00:00:00' "
        "AND block_timestamp < TIMESTAMP '2026-09-06 00:00:00'"
    )
    assert time_condition(None) == ""
    assert time_condition(fill.time, "timestamp").startswith("timestamp >= ")
