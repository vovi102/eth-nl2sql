"""The benchmark questions with the frozen runs of every configuration, for the page to browse.

Read only: data/benchmark/<split>.jsonl (gold), data/linking/<split>.predictions.jsonl (the
linker's records, which the predictions were run with), and per configuration the run and its
`ex-details` (evaluate-sql --details). The test split loads only with `allow_test`, as
everywhere else (`read_records`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from eth_nl2sql import paths
from eth_nl2sql.api.models import ModelConfig
from eth_nl2sql.linking.evaluation import read_records
from eth_nl2sql.sql.evaluation import expected_decision


def normalize_question(text: str) -> str:
    """The key a question typed on the page is matched with: NFC, single spaces, no case."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


def _by_id(path: Path, allow_test: bool) -> dict[str, dict]:
    if not path.is_file():
        return {}
    return {r["question_id"]: r for r in read_records(path, allow_test=allow_test)}


@dataclass
class Split:
    name: str
    gold: dict[str, dict]
    linking: dict[str, dict]
    runs: dict[str, dict[str, dict]] = field(default_factory=dict)  # model id -> qid -> line
    details: dict[str, dict[str, dict]] = field(default_factory=dict)  # model id -> qid -> EX

    def summary(self, qid: str) -> dict:
        g = self.gold[qid]
        return {
            "question_id": qid,
            "question": g["question"],
            "lang": g.get("lang"),
            "template_id": g.get("template_id"),
            "expected": expected_decision(g),
            "results": {
                mid: {"kind": d[qid]["kind"], "correct": d[qid]["correct"]}
                for mid, d in self.details.items()
                if qid in d
            },
        }

    def detail(self, qid: str) -> dict:
        return self.summary(qid) | {
            "gold": self.gold[qid],
            "linking": self.linking.get(qid),
            "predictions": {mid: run[qid] for mid, run in self.runs.items() if qid in run},
        }


class Examples:
    def __init__(self, splits: dict[str, Split]) -> None:
        self.splits = splits
        self.by_question: dict[str, tuple[str, str]] = {}
        for split in splits.values():
            for qid, g in split.gold.items():
                self.by_question.setdefault(normalize_question(g["question"]), (split.name, qid))

    @classmethod
    def load(
        cls,
        models: list[ModelConfig],
        *,
        allow_test: bool = False,
        benchmark_dir: Path = paths.BENCHMARK_DIR,
        linking_dir: Path = paths.LINKING_DIR,
    ) -> Examples:
        splits = {}
        for name in ("dev", "test") if allow_test else ("dev",):
            gold = _by_id(benchmark_dir / f"{name}.jsonl", allow_test)
            if not gold:
                continue
            split = Split(name, gold, _by_id(linking_dir / f"{name}.predictions.jsonl", allow_test))
            for m in models:
                run = _by_id(m.run_file(name), allow_test)
                if run:
                    split.runs[m.id] = run
                    split.details[m.id] = _by_id(m.details_file(name), allow_test)
            splits[name] = split
        return cls(splits)

    def find(self, question: str) -> tuple[Split, dict] | None:
        """The benchmark question with this text, if the page asks one."""
        hit = self.by_question.get(normalize_question(question))
        if hit is None:
            return None
        split = self.splits[hit[0]]
        return split, split.gold[hit[1]]

    def samples(self) -> list[dict]:
        """The first dev question of every template and language, in file order."""
        split = self.splits.get("dev")
        if split is None:
            return []
        seen, out = set(), []
        for qid, g in split.gold.items():
            key = (g.get("template_id"), g.get("lang"))
            if key not in seen:
                seen.add(key)
                out.append(split.summary(qid))
        return out
