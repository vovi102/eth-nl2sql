from __future__ import annotations

from pathlib import Path

import pytest

from eth_nl2sql.entity_linking_evaluation import (
    EvaluationError,
    evaluate,
    read_records,
    write_records,
)

USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
FAKE = "0x0000000000000000000000000000000000000002"


def record(qid: str, mentions: list[dict], properties=(), time=None, unsupported=(), lang="vi"):
    return {
        "question_id": qid,
        "question": "",
        "lang": lang,
        "mentions": mentions,
        "properties": list(properties),
        "time": time,
        "unsupported": list(unsupported),
        "warnings": [],
    }


def prop(sid: str, implicit: bool = False) -> dict:
    return {"sense_id": sid, "span": None if implicit else [0, 1], "text": None,
            "implicit": implicit}  # fmt: skip


SEPT_5 = {"start": "2026-09-05T00:00:00", "end": "2026-09-06T00:00:00", "span": [0, 1]}
GOLD = [
    record(
        "q1",
        [{"span": [25, 29], "text": "USDT", "type": "token", "target": USDT, "placeholder": "E1"}],
        [prop("transfer_count"), prop("of_token", True)],
        SEPT_5,
    ),
    record(
        "q2",
        [
            {
                "span": [21, 31],
                "text": "major DEXs",
                "type": "class",
                "target": "dex",
                "placeholder": "E1",
                "top_n": 5,
                "top_n_source": "default",
            }
        ],
        [prop("eth_amount"), prop("received_by"), prop("tx_success", True)],
        lang="en",
    ),  # fmt: skip
    record("q3", [{"span": [0, 5], "text": "Bybit", "type": "nil", "nil_reason": "not_in_kb"}]),
    record("q4", [], unsupported=[{"sense_id": "balance", "span": [0, 5], "text": "Số dư"}]),
]


def test_perfect_predictions_score_one() -> None:
    report = evaluate(GOLD, GOLD)
    assert report["mentions_strict"]["f1"] == 1.0
    assert report["entity_accuracy_at_1"]["accuracy"] == 1.0
    assert report["entity_recall_at_5"]["accuracy"] == 1.0
    assert report["nil_accuracy"]["accuracy"] == 1.0
    assert report["class_accuracy"]["accuracy"] == 1.0
    assert report["properties"]["f1"] == 1.0
    assert report["time_accuracy"]["accuracy"] == 1.0
    assert report["unsupported_accuracy"]["accuracy"] == 1.0
    assert set(report["by_lang"]) == {"en", "vi"}


def test_scores_each_layer() -> None:
    pred = [
        # wrong token first, right one second; span one character off; no implicit sense
        record(
            "q1",
            [
                {
                    "span": [25, 30],
                    "type": "token",
                    "target": FAKE,
                    "candidates": [
                        {"type": "token", "target": FAKE, "score": 1.2},
                        {"type": "token", "target": USDT, "score": 1.1},
                    ],
                }
            ],
            [prop("transfer_count")],
            {"start": "2026-09-05T00:00:00", "end": "2026-09-07T00:00:00"},
        ),  # fmt: skip
        # right class, wrong top_n
        record(
            "q2",
            [
                {
                    "span": [21, 31],
                    "type": "class",
                    "target": "dex",
                    "top_n": 3,
                    "candidates": [{"type": "class", "target": "dex", "score": 1.4}],
                }
            ],
            [prop("eth_amount"), prop("received_by"), prop("tx_success", True)],
            lang="en",
        ),  # fmt: skip
        record("q3", [{"span": [0, 5], "type": "nil", "nil_reason": "not_reviewed"}]),
        record("q4", [], [prop("tx_count")]),
    ]
    report = evaluate(GOLD, pred)
    assert report["mentions_strict"]["tp"] == 2  # class (top_n is not part of a mention) + nil
    assert report["mentions_loose"]["tp"] == 2  # the wrong token still misses
    assert report["entity_accuracy_at_1"] == {"accuracy": 0.5, "hits": 1, "total": 2}
    assert report["entity_recall_at_5"]["hits"] == 2
    assert report["nil_accuracy"]["hits"] == 1
    assert report["nil_reason_accuracy"]["hits"] == 0
    assert report["class_accuracy"]["hits"] == 0
    assert report["properties"]["tp"] == 4 and report["properties"]["gold"] == 5
    assert report["properties_explicit"]["gold"] == 3
    assert report["properties_explicit"]["recall"] == 1.0
    assert report["time_accuracy"]["hits"] == 3  # q1 wrong; q2-q4 both null
    assert report["unsupported_accuracy"]["hits"] == 3


def test_missing_prediction_is_an_error() -> None:
    with pytest.raises(EvaluationError, match="no prediction"):
        evaluate(GOLD, GOLD[:2])


def test_test_split_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "test.jsonl"
    write_records(path, GOLD)
    with pytest.raises(EvaluationError, match="final evaluation"):
        read_records(path)
    assert read_records(path, allow_test=True) == GOLD
