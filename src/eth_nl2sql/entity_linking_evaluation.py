"""Score linking records against gold records (docs/linking-labels.md §9).

Both sides are JSON Lines in the format of linking-labels.md; predictions also carry
`candidates`. Records are paired by question_id. data/benchmark/test.jsonl is for the final
evaluation only: `read_records` refuses it unless allow_test is set.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from eth_nl2sql.property_senses import PHRASES_FILE, SENSES_FILE

KNOWLEDGE_BASE_FILES = (
    "entity_classes.csv",
    "entities.csv",
    "entity_addresses.csv",
    "entity_aliases.csv",
)


class EvaluationError(RuntimeError):
    pass


def read_records(path: Path, *, allow_test: bool = False) -> list[dict]:
    path = Path(path)
    if path.stem.startswith("test") and not allow_test:
        raise EvaluationError(f"{path} is the test split, kept for the final evaluation only")
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_records(path: Path, records: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def knowledge_base_version(knowledge_base_dir: Path, property_senses_dir: Path) -> dict[str, str]:
    """SHA256 of the CSVs a linking record depends on (linking-labels.md §8)."""
    files = [Path(knowledge_base_dir) / f for f in KNOWLEDGE_BASE_FILES]
    files += [Path(property_senses_dir) / f for f in (SENSES_FILE, PHRASES_FILE)]
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "" for p in files
    }


def _overlap(a: list[int], b: list[int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def _prf(tp: int, n_pred: int, n_gold: int) -> dict:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_gold if n_gold else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": p, "recall": r, "f1": f1, "tp": tp, "pred": n_pred, "gold": n_gold}


def _ratio(hits: int, total: int) -> dict:
    return {"accuracy": hits / total if total else 0.0, "hits": hits, "total": total}


def _ranked_targets(mention: dict) -> list[str]:
    if mention.get("candidates"):
        return [c["target"] for c in mention["candidates"]]
    return [mention["target"]] if mention.get("target") else []


class _Counts:
    def __init__(self) -> None:
        self.c: Counter = Counter()

    def add(self, gold: dict, pred: dict) -> None:
        c = self.c
        gold_mentions, pred_mentions = gold["mentions"], pred["mentions"]

        def key(m: dict) -> tuple:
            return (tuple(m["span"]), m["type"], m.get("target"))

        strict = Counter(map(key, gold_mentions)) & Counter(map(key, pred_mentions))
        c["strict_tp"] += sum(strict.values())
        c["mentions_pred"] += len(pred_mentions)
        c["mentions_gold"] += len(gold_mentions)
        used: set[int] = set()
        for g in gold_mentions:
            for k, p in enumerate(pred_mentions):
                same = (p["type"], p.get("target")) == (g["type"], g.get("target"))
                if k not in used and same and _overlap(g["span"], p["span"]):
                    used.add(k)
                    c["loose_tp"] += 1
                    break

        for g in gold_mentions:
            # the predicted mention that overlaps the gold one most, if any
            p = max(
                (p for p in pred_mentions if _overlap(g["span"], p["span"])),
                key=lambda p: _overlap(g["span"], p["span"]),
                default=None,
            )
            ranked = _ranked_targets(p) if p and p["type"] != "nil" else []
            if g["type"] == "nil":
                c["nil_total"] += 1
                c["nil_hits"] += bool(p and p["type"] == "nil")
                c["nil_reason_hits"] += bool(
                    p and p["type"] == "nil" and p.get("nil_reason") == g.get("nil_reason")
                )
                continue
            c["linked_total"] += 1
            c[f"linked_total:{g['type']}"] += 1
            hit1 = ranked[:1] == [g["target"]]
            c["at1_hits"] += hit1
            c[f"at1_hits:{g['type']}"] += hit1
            c["at5_hits"] += g["target"] in ranked[:5]
            if g["type"] == "class":
                c["class_total"] += 1
                c["class_hits"] += bool(
                    p
                    and p["type"] == "class"
                    and p.get("target") == g["target"]
                    and p.get("top_n") == g.get("top_n")
                )

        for suffix, keep in (("", lambda p: True), ("_explicit", lambda p: not p["implicit"])):
            gs = {p["sense_id"] for p in gold["properties"] if keep(p)}
            ps = {p["sense_id"] for p in pred["properties"] if keep(p)}
            c["prop_tp" + suffix] += len(gs & ps)
            c["prop_pred" + suffix] += len(ps)
            c["prop_gold" + suffix] += len(gs)

        def interval(t: dict | None) -> tuple | None:
            return (t["start"], t["end"]) if t else None

        c["questions"] += 1
        c["time_hits"] += interval(gold["time"]) == interval(pred["time"])
        c["unsupported_hits"] += {u["sense_id"] for u in gold["unsupported"]} == {
            u["sense_id"] for u in pred["unsupported"]
        }

    def report(self) -> dict:
        c = self.c
        by_type = {
            t.split(":", 1)[1]: _ratio(c[f"at1_hits:{t.split(':', 1)[1]}"], n)
            for t, n in sorted(c.items())
            if t.startswith("linked_total:")
        }
        return {
            "questions": c["questions"],
            "mentions_strict": _prf(c["strict_tp"], c["mentions_pred"], c["mentions_gold"]),
            "mentions_loose": _prf(c["loose_tp"], c["mentions_pred"], c["mentions_gold"]),
            "entity_accuracy_at_1": _ratio(c["at1_hits"], c["linked_total"]),
            "entity_accuracy_at_1_by_type": by_type,
            "entity_recall_at_5": _ratio(c["at5_hits"], c["linked_total"]),
            "nil_accuracy": _ratio(c["nil_hits"], c["nil_total"]),
            "nil_reason_accuracy": _ratio(c["nil_reason_hits"], c["nil_total"]),
            "class_accuracy": _ratio(c["class_hits"], c["class_total"]),
            "properties": _prf(c["prop_tp"], c["prop_pred"], c["prop_gold"]),
            "properties_explicit": _prf(
                c["prop_tp_explicit"], c["prop_pred_explicit"], c["prop_gold_explicit"]
            ),
            "time_accuracy": _ratio(c["time_hits"], c["questions"]),
            "unsupported_accuracy": _ratio(c["unsupported_hits"], c["questions"]),
        }


def evaluate(gold: list[dict], predictions: list[dict]) -> dict:
    """Overall scores, and the same scores per language."""
    by_id = {p["question_id"]: p for p in predictions}
    missing = [g["question_id"] for g in gold if g["question_id"] not in by_id]
    if missing:
        raise EvaluationError(f"{len(missing)} gold questions have no prediction: {missing[:5]}")
    overall, by_lang = _Counts(), defaultdict(_Counts)
    for g in gold:
        overall.add(g, by_id[g["question_id"]])
        by_lang[g["lang"]].add(g, by_id[g["question_id"]])
    return overall.report() | {"by_lang": {k: v.report() for k, v in sorted(by_lang.items())}}


def format_report(report: dict) -> str:
    """The headline numbers, one line each."""
    lines = []
    for name, value in report.items():
        if name in ("by_lang", "entity_accuracy_at_1_by_type"):
            continue
        if isinstance(value, dict) and "f1" in value:
            lines.append(
                f"{name:<26} P {value['precision']:.3f}  R {value['recall']:.3f}  "
                f"F1 {value['f1']:.3f}  ({value['tp']}/{value['pred']} pred, {value['gold']} gold)"
            )
        elif isinstance(value, dict):
            lines.append(f"{name:<26} {value['accuracy']:.3f}  ({value['hits']}/{value['total']})")
        else:
            lines.append(f"{name:<26} {value}")
    for t, v in report.get("entity_accuracy_at_1_by_type", {}).items():
        lines.append(f"  accuracy@1 {t:<14} {v['accuracy']:.3f}  ({v['hits']}/{v['total']})")
    return "\n".join(lines)
