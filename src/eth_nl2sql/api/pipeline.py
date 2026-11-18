"""One question through the pipeline, step by step, for the page.

The steps are the functions of `generate-sql`, called the same way: `EntityLinker.link`,
`PromptBuilder.build`, then `generate_one` (B8 with the placeholder check and one repair) after
the fixed prefix is primed as `generate` primes it (once before the question, once inside
`generate_one`). What changes is only what the page sees: each step is emitted as an event
as soon as it is done, the reply token by token.

Events (name, data): linking, prompt, answer (no SQL: nil, unsupported, skipped), attempt
(a call of the model starts; a repair carries the message that asks for it), token, executing
(B8 runs the attempt's SQL), attempts (every attempt of `generate_one`'s line), result (the
rows of the last query), gold (only for a question of the benchmark: EX against the gold, as
evaluate-sql scores it), timing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from eth_nl2sql.api import serialize
from eth_nl2sql.api.examples import Examples
from eth_nl2sql.api.llama import Cancelled, StreamingClient
from eth_nl2sql.linking.linker import EntityLinker
from eth_nl2sql.sql.evaluation import expected_decision, score
from eth_nl2sql.sql.execution import Execution, SqlRunner
from eth_nl2sql.sql.generation import generate_one
from eth_nl2sql.sql.prompt import PromptBuilder

Emit = Callable[[str, dict], None]
CANDIDATES_SHOWN = 5


class RecordingRunner:
    """A SqlRunner that keeps its last Execution, so the page gets the rows of the query
    `generate_one` ran without running it again; `on_run` hears of each query first."""

    def __init__(self, runner: SqlRunner, on_run: Callable[[str | None], None] | None = None):
        self.runner = runner
        self.on_run = on_run
        self.last: Execution | None = None

    def run(self, sql: str | None, addresses: dict[str, list[str]]) -> Execution:
        if self.on_run:
            self.on_run(sql)
        self.last = self.runner.run(sql, addresses)
        return self.last


@dataclass
class Engine:
    """What a question needs, loaded once."""

    linker: EntityLinker
    builder: PromptBuilder
    runner: SqlRunner
    examples: Examples
    max_rows: int = 200

    # ---- names for the page --------------------------------------------------------

    def label(self, kind: str, target: str | None) -> str | None:
        res = self.linker.resources
        if target is None:
            return None
        if kind == "entity" and target in res.entities:
            return res.entities[target]["name"]
        if kind == "class" and target in res.classes:
            c = res.classes[target]
            return c.get("name_vi") or c.get("name_en") or target
        if kind == "token" and target in res.tokens:
            t = res.tokens[target]
            return " · ".join(x for x in (t.get("symbol"), t.get("name")) if x) or None
        if kind in ("address", "literal_address", "token"):
            named = self.builder.address_names.get(target)
            return named[0] if named else None
        return None

    def describe_linking(self, record: dict, addresses: dict[str, list[str]]) -> dict:
        res = self.linker.resources
        mentions = []
        for m in record["mentions"]:
            out = {k: m.get(k) for k in ("span", "text", "type", "target", "placeholder", "top_n")}
            out["label"] = self.label(m["type"], m.get("target"))
            out["candidates"] = [
                c | {"label": self.label(c["type"], c["target"])}
                for c in m.get("candidates", [])[:CANDIDATES_SHOWN]
            ]
            if m["type"] == "class":
                out["members"] = [
                    {"id": eid, "label": self.label("entity", eid), "addresses": len(a)}
                    for eid, a in self.linker.class_members(m["target"], m["top_n"])
                ]
            mentions.append(out)
        properties = [
            p | {
                "description": res.senses.get(p["sense_id"], {}).get("description_vi"),
                "kind": res.senses.get(p["sense_id"], {}).get("kind"),
            }
            for p in record["properties"]
        ]  # fmt: skip
        unsupported = [
            u | {"description": res.senses.get(u["sense_id"], {}).get("description_vi")}
            for u in record["unsupported"]
        ]
        return {
            "question": record["question"],
            "lang": record["lang"],
            "mentions": mentions,
            "properties": properties,
            "time": record["time"],
            "unsupported": unsupported,
            "warnings": record["warnings"],
            "placeholders": {
                name: {"count": len(values), "sample": values[:3]}
                for name, values in addresses.items()
            },
        }

    # ---- the question --------------------------------------------------------------

    def ask(
        self,
        question: str,
        client: StreamingClient,
        emit: Emit,
        *,
        prefix: list[int] | None = None,
        cancel=None,
        gold: bool = True,
    ) -> dict:
        """The line generate-sql would write for this question, emitting every step; with
        `gold`, a benchmark question is also scored against its gold SQL."""
        started = time.monotonic()
        hit = self.examples.find(question)
        qid = hit[1]["question_id"] if hit else "live"

        record = self.linker.link(question, qid)
        addresses = self.linker.placeholder_addresses(record)
        emit("linking", self.describe_linking(record, addresses)
             | {"seconds": round(time.monotonic() - started, 3)})  # fmt: skip

        prompt = self.builder.build(record)
        emit("prompt", {
            "decision": prompt["decision"],
            "answer": prompt.get("answer"),
            "tables": prompt.get("tables"),
            "evidence": self.builder.evidence(record) if prompt["decision"] == "sql" else [],
            "messages": prompt.get("messages", []),
            "chars": prompt.get("chars"),
        })  # fmt: skip

        pred_run = None
        if prompt["decision"] != "sql":
            prediction = {"question_id": qid, "decision": prompt["decision"], "sql": None,
                          "attempts": []}  # fmt: skip
            emit("answer", {"decision": prompt["decision"], "answer": prompt["answer"]})
        else:
            recorder = RecordingRunner(self.runner, lambda sql: emit("executing", {"sql": sql}))
            calls = 0

            def on_chat(messages: list[dict]) -> None:
                nonlocal calls
                calls += 1
                emit("attempt", {"n": calls, "repair": messages[-1]["content"] if calls > 1
                                 else None})  # fmt: skip

            client.on_chat = on_chat
            client.on_token = lambda text: emit("token", {"text": text})
            if prefix:
                client.prime(prefix)
            prediction = generate_one(prompt, client, recorder, addresses,
                                      client.context_size(), prefix=prefix)  # fmt: skip
            emit("attempts", {"attempts": prediction["attempts"], "sql": prediction["sql"]})
            pred_run = recorder.last
            if "skipped" in prediction:
                emit("answer", {"decision": "skipped", "answer": prediction["skipped"]})
            elif pred_run is not None:
                final = prediction["attempts"][-1]["error_kind"]
                emit("result", serialize.execution(pred_run, addresses, self.max_rows)
                     | {"final_error_kind": final})  # fmt: skip

        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if hit and gold:
            emit("gold", self.gold_check(hit, prediction, pred_run))
        emit("timing", {"seconds": round(time.monotonic() - started, 3)})
        return prediction

    def gold_check(self, hit, prediction: dict, pred_run: Execution | None) -> dict:
        """EX of the live answer, as `run_questions` scores it, and the frozen runs' EX."""
        split, gold = hit
        gold_run = gold_addresses = None
        if (
            expected_decision(gold) == "sql"
            and prediction["decision"] == "sql"
            and "skipped" not in prediction
            and pred_run is not None
        ):
            gold_addresses = self.linker.placeholder_addresses(gold)
            gold_run = self.runner.run(gold["gold_sql"], gold_addresses)
        result = score(gold, prediction, gold_run, pred_run)
        return {
            "split": split.name,
            "question_id": gold["question_id"],
            "template_id": gold.get("template_id"),
            "expected": result["expected"],
            "kind": result["kind"],
            "correct": result["correct"],
            "gold_sql": gold.get("gold_sql"),
            "gold_result": None
            if gold_run is None
            else serialize.execution(gold_run, gold_addresses, self.max_rows),
            "frozen": split.summary(gold["question_id"])["results"],
        }

    def run_stored(self, sql: str | None, record: dict, runner: SqlRunner) -> dict:
        """A stored query (gold or a frozen prediction) with the addresses of its record."""
        addresses = self.linker.placeholder_addresses(record)
        return serialize.execution(runner.run(sql, addresses), addresses, self.max_rows)
