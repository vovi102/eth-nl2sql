from __future__ import annotations

import pytest

from eth_nl2sql.entity_linking_time import fold, parse_time


@pytest.mark.parametrize(
    ("question", "start", "end", "text"),
    [
        ("Tornado Cash nhận bao nhiêu ETH trong tuần đầu tháng 9?", 1, 8, "tuần đầu tháng 9"),
        ("Có bao nhiêu lượt chuyển USDT trong ngày 5/9?", 5, 6, "ngày 5/9"),
        ("How much ETH did the major DEXs receive on September 5?", 5, 6, "on September 5"),
        ("Binance nhận bao nhiêu ETH trong tháng 9?", 1, 31, "tháng 9"),
        ("tornado cash nhan bao nhieu eth trong tuan dau thang 9", 1, 8, "tuan dau thang 9"),
        ("mùng 5 tháng 9", 5, 6, "mùng 5 tháng 9"),
        ("từ ngày 3 đến ngày 7/9", 3, 8, "từ ngày 3 đến ngày 7/9"),
        ("from September 3 to 7", 3, 8, "from September 3 to 7"),
        ("tuần thứ 2 của tháng 9", 8, 15, "tuần thứ 2 của tháng 9"),
        ("tuần cuối tháng 9", 24, 31, "tuần cuối tháng 9"),
        ("in the last week of September", 24, 31, "in the last week of September"),
        ("nửa đầu tháng 9", 1, 16, "nửa đầu tháng 9"),
        ("first half of September", 1, 16, "first half of September"),
        ("10 ngày đầu tháng 9", 1, 11, "10 ngày đầu tháng 9"),
        ("the last 5 days of September", 26, 31, "the last 5 days of September"),
        ("on the 5th of September 2026", 5, 6, "on the 5th of September 2026"),
        ("Sept. 12", 12, 13, "Sept. 12"),
    ],
)
def test_parse_time(question: str, start: int, end: int, text: str) -> None:
    time = parse_time(question)
    to_day = {31: "2026-10-01T00:00:00"}
    assert time["start"] == f"2026-09-{start:02d}T00:00:00"
    assert time["end"] == to_day.get(end, f"2026-09-{end:02d}T00:00:00")
    assert time["text"] == text
    assert question[time["span"][0] : time["span"][1]] == text


@pytest.mark.parametrize(
    "question",
    [
        "How many transactions did 0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511 send?",
        "Số dư của ví Coinbase 10 là bao nhiêu?",
        "How much ETH may Uniswap receive?",  # 'may' is a verb here
        "ngày 31/9",  # no such day
    ],
)
def test_no_time(question: str) -> None:
    assert parse_time(question) is None


def test_fold_keeps_positions() -> None:
    question = "Đồng USD₮ tuần đầu"
    assert len(fold(question)) == len(question)
    assert fold(question) == "dong usd₮ tuan dau"
