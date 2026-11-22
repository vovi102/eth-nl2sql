"""The state of the app, apart from HTTP: the loaded pipeline, llama-server, the GPU lock.

llama-server has one slot, so one question runs at a time (`gpu` lock); asking while another
question runs or while a model loads is refused (`Busy`), not queued. Browsing the frozen runs
uses its own read-only DuckDB connection, so it works during a question.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from eth_nl2sql import paths
from eth_nl2sql.api.examples import Examples
from eth_nl2sql.api.llama import DEFAULT_BINARY, Cancelled, LlamaManager, StreamingClient
from eth_nl2sql.api.models import MODELS_DIR, ModelConfig, registry
from eth_nl2sql.api.pipeline import Emit, Engine
from eth_nl2sql.linking.evaluation import read_records
from eth_nl2sql.linking.linker import EntityLinker
from eth_nl2sql.linking.resources import load_resources
from eth_nl2sql.sql.execution import SqlRunner
from eth_nl2sql.sql.generation import ClientError
from eth_nl2sql.sql.prompt import PromptBuilder, load_schema

log = logging.getLogger(__name__)


class Busy(RuntimeError):
    pass


class Unavailable(RuntimeError):
    pass


@dataclass
class Settings:
    db: Path = paths.DB
    knowledge_base_dir: Path = paths.KNOWLEDGE_BASE_DIR
    memory_limit: str = "4GB"
    sql_timeout: float = 30.0  # as generate-sql and evaluate-sql
    llama: bool = True  # False: browse the frozen runs only, no GPU
    llama_bin: Path = DEFAULT_BINARY
    llama_port: int = 8080
    default_model: str = "3b-lora"
    allow_test: bool = False
    # The page replays the September runs and check-serve compares its prompts with theirs:
    # they ranked class members by calls in all the data (docs/multi-month-plan.md D3).
    class_rank: str = "data"
    # The fixed prefix is computed from this file, as generate-sql computes it for dev.
    prefix_prompts: Path = paths.PROMPTS_DIR / "dev.predictions.prompts.jsonl"
    models_dir: Path = MODELS_DIR
    predictions_dir: Path = paths.PREDICTIONS_DIR
    # llama-server logs and the saved prefix KV (~550 MB for the 4 models): not in /tmp, which
    # a restart of WSL empties, so that the prefix is computed once per machine.
    log_dir: Path = field(default_factory=lambda: _cache_dir() / "eth-nl2sql-serve")
    max_rows: int = 200


def _cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


ClientFactory = Callable[[ModelConfig, threading.Event], StreamingClient]


class Service:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        models: list[ModelConfig],
        llama: LlamaManager | None,
        browse_runner: SqlRunner,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.models = {m.id: m for m in models}
        self.llama = llama
        self.browse_runner = browse_runner
        self.client_factory = client_factory or self._client
        self.gpu = threading.Lock()
        self.browse = threading.Lock()
        self.busy: str | None = None  # loading | asking
        self.warming: dict | None = None  # {model, done, total} while `warm` runs
        self.cancel_event: threading.Event | None = None

    @classmethod
    def load(cls, settings: Settings) -> Service:
        """The pipeline from disk: linker and schema first, then the read-only connections
        (as the CLI opens them)."""
        s = settings
        models = registry(s.models_dir, s.predictions_dir)
        if s.default_model not in {m.id for m in models}:
            raise ValueError(f"unknown model {s.default_model!r}")
        resources = load_resources(s.db, s.knowledge_base_dir, memory_limit=s.memory_limit)
        linker = EntityLinker(resources, class_rank=s.class_rank)
        builder = PromptBuilder(linker, load_schema(s.db))
        runner = SqlRunner(s.db, timeout=s.sql_timeout, memory_limit=s.memory_limit)
        browse = SqlRunner(s.db, timeout=s.sql_timeout, memory_limit=s.memory_limit)
        examples = Examples.load(models, allow_test=s.allow_test)
        llama = None
        if s.llama:
            llama = LlamaManager(s.llama_bin, port=s.llama_port, log_dir=s.log_dir,
                                 prefix_prompts=s.prefix_prompts)  # fmt: skip
        engine = Engine(linker, builder, runner, examples, max_rows=s.max_rows)
        return cls(s, engine, models, llama, browse)

    def _client(self, model: ModelConfig, cancel: threading.Event) -> StreamingClient:
        return StreamingClient(self.llama.url, cancel=cancel)

    def close(self) -> None:
        if self.llama is not None:
            self.llama.stop()
        self.engine.runner.close()
        self.browse_runner.close()

    # ---- models ----------------------------------------------------------------------

    def model(self, model_id: str) -> ModelConfig:
        if model_id not in self.models:
            raise KeyError(model_id)
        return self.models[model_id]

    def public_models(self) -> list[dict]:
        out = []
        for m in self.models.values():
            loaded = self.llama is not None and self.llama.serves(m)
            out.append(m.public() | {"loaded": loaded})
        return out

    def status(self) -> dict:
        return {
            "llama": None if self.llama is None else self.llama.status(),
            "busy": self.busy,
            "allow_test": self.settings.allow_test,
            "splits": list(self.engine.examples.splits),
            "default_model": self.settings.default_model,
            "warming": self.warming,
        }

    def _acquire(self, reason: str) -> None:
        if not self.gpu.acquire(blocking=False):
            raise Busy(self.busy or "busy")
        self.busy = reason

    def _release(self) -> None:
        self.busy = None
        self.gpu.release()

    def select(self, model_id: str) -> bool:
        """Load the model in the background; False when it is served already."""
        model = self.model(model_id)
        if self.llama is None:
            raise Unavailable("llama-server is off (--no-llama)")
        if self.llama.serves(model):
            return False
        self._acquire("loading")

        def load() -> None:
            try:
                self.llama.ensure(model)
            except Exception:
                log.exception("llama-server for %s", model.id)
            finally:
                self._release()

        threading.Thread(target=load, daemon=True).start()
        return True

    def warm(self) -> None:
        """In the background: save the prefix of every configuration that has none yet (each
        costs a start and ~30 s once), then load the default model."""
        if self.llama is None:
            return
        default = self.model(self.settings.default_model)
        todo = [m for m in self.models.values()
                if m is not default and not m.missing_files()
                and self.llama.cached_prefix(m) is None]  # fmt: skip
        todo.append(default)
        self._acquire("loading")

        def run() -> None:
            try:
                for i, model in enumerate(todo):
                    self.warming = {"model": model.id, "done": i, "total": len(todo)}
                    try:
                        self.llama.ensure(model)
                    except Exception:
                        log.exception("llama-server for %s", model.id)
            finally:
                self.warming = None
                self._release()

        threading.Thread(target=run, daemon=True).start()

    # ---- questions -------------------------------------------------------------------

    def begin(self, model_id: str) -> threading.Event:
        """Take the GPU for one question; the caller then runs `ask_stream`, which frees it."""
        self.model(model_id)
        if self.llama is None:
            raise Unavailable("llama-server is off (--no-llama)")
        self._acquire("asking")
        self.cancel_event = threading.Event()
        return self.cancel_event

    def ask_stream(self, question: str, model_id: str, emit: Emit,
                   cancel: threading.Event) -> None:  # fmt: skip
        """One question after `begin`, every step emitted; always ends with a `done` event.

        If llama-server dies during the question (a CUDA error), it is started again and the
        question asked once more from the start (a `retry` event tells the page)."""
        try:
            try:
                self._ask(question, self.model(model_id), emit, cancel)
            except (ClientError, OSError) as e:
                if not self.llama.died():
                    raise
                log.warning("llama-server died during a question: %s", e)
                emit("retry", {"message": "llama-server dừng đột ngột; khởi động lại và hỏi lại"})
                self._ask(question, self.model(model_id), emit, cancel)
        except Cancelled:
            emit("cancelled", {})
        except Exception as e:
            log.exception("question failed")
            emit("error", {"type": type(e).__name__, "message": str(e)})
        finally:
            self.cancel_event = None
            self._release()
            emit("done", {})

    def _ask(self, question: str, model: ModelConfig, emit: Emit, cancel: threading.Event,
             *, gold: bool = True) -> dict:  # fmt: skip
        emit("start", {"model": model.id, "label": model.label, "phase": model.phase})
        if not self.llama.serves(model):
            cached = self.llama.cached_prefix(model) is not None
            emit("model_loading", {"model": model.id, "label": model.label, "cached": cached})
            self.busy = "loading"
            try:
                self.llama.ensure(model)
            finally:
                self.busy = "asking"
            emit("model_ready", {"model": model.id, "seconds": self.llama.load_seconds,
                                 "prefix": self.llama.prefix_source})  # fmt: skip
        client = self.client_factory(model, cancel)
        return self.engine.ask(question, client, emit, prefix=self.llama.prefix, cancel=cancel,
                               gold=gold)  # fmt: skip

    def cancel(self) -> bool:
        """Stop the running question: the reply stream closes, a running query is interrupted."""
        event = self.cancel_event
        if event is None:
            return False
        event.set()
        self.engine.runner.con.interrupt()
        return True

    # ---- the frozen runs -------------------------------------------------------------

    def run_stored(self, split_name: str, qid: str, source: str) -> dict:
        split = self.engine.examples.splits.get(split_name)
        if split is None or qid not in split.gold:
            raise KeyError(f"{split_name}/{qid}")
        if source == "gold":
            gold = split.gold[qid]
            if not gold.get("gold_sql"):
                raise ValueError("this question has no gold SQL (the right answer is to abstain)")
            sql, record = gold["gold_sql"], gold
        else:
            prediction = split.runs.get(source, {}).get(qid)
            if prediction is None:
                raise KeyError(f"no frozen run of {source} for {qid}")
            if prediction["decision"] != "sql" or not prediction.get("sql"):
                raise ValueError(f"{source} wrote no SQL for {qid} ({prediction['decision']})")
            sql, record = prediction["sql"], split.linking[qid]
        if not self.browse.acquire(blocking=False):
            raise Busy("running")
        try:
            return self.engine.run_stored(sql, record, self.browse_runner)
        finally:
            self.browse.release()


def check_reproduction(service: Service, question_ids: list[str], out=print) -> bool:
    """Do live answers equal the frozen dev runs? Each configuration answers every question;
    then the first questions once more with the configurations alternating, so that each
    answer follows a new process that restored the saved prefix. Same prompt, same reply of
    every attempt, same SQL."""
    split = service.engine.examples.splits["dev"]
    prompts = {p["question_id"]: p for p in read_records(service.settings.prefix_prompts)}
    models = list(service.models.values())
    plan = [(m, qid) for m in models for qid in question_ids]
    plan += [(m, qid) for qid in question_ids[:3] for m in models]
    same = {m.id: 0 for m in models}
    asked = {m.id: 0 for m in models}
    ok = True
    for i, (model, qid) in enumerate(plan, 1):
        events: list[tuple[str, dict]] = []
        service._acquire("asking")
        try:
            live = service._ask(split.gold[qid]["question"], model,
                                lambda e, d, got=events: got.append((e, d)),
                                threading.Event(), gold=False)  # fmt: skip
        finally:
            service._release()
        frozen = split.runs[model.id][qid]
        messages = next(d["messages"] for e, d in events if e == "prompt")
        checks = {
            "prompt": messages == prompts[qid].get("messages", []),
            "replies": [a["reply"] for a in live["attempts"]]
            == [a["reply"] for a in frozen["attempts"]],
            "sql": live["sql"] == frozen["sql"],
        }
        asked[model.id] += 1
        loaded = next((d for e, d in events if e == "model_ready"), None)
        note = f" (new process, prefix {loaded['prefix']})" if loaded else ""
        if all(checks.values()):
            same[model.id] += 1
            out(f"  [{i}/{len(plan)}] {model.id} {qid} same{note}")
        else:
            ok = False
            bad = ", ".join(k for k, v in checks.items() if not v)
            out(f"  [{i}/{len(plan)}] {model.id} {qid} DIFFERENT: {bad}{note}")
    for m in models:
        out(f"{m.id}: {same[m.id]}/{asked[m.id]} identical to the frozen dev run")
    return ok
