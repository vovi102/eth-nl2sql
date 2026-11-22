"""The web app's backend: the streaming client, the question's events, the HTTP routes."""

from __future__ import annotations

import json
import threading
import warnings
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from eth_nl2sql.api import serialize  # noqa: E402
from eth_nl2sql.api.app import create_app  # noqa: E402
from eth_nl2sql.api.examples import Examples, Split, normalize_question  # noqa: E402
from eth_nl2sql.api.llama import Cancelled, LlamaManager, StreamingClient  # noqa: E402
from eth_nl2sql.api.models import registry  # noqa: E402
from eth_nl2sql.api.pipeline import Engine  # noqa: E402
from eth_nl2sql.api.service import Busy, Service, Settings  # noqa: E402
from eth_nl2sql.linking.linker import EntityLinker  # noqa: E402
from eth_nl2sql.linking.resources import load_resources  # noqa: E402
from eth_nl2sql.sql.execution import SqlRunner  # noqa: E402
from eth_nl2sql.sql.generation import ClientError  # noqa: E402
from eth_nl2sql.sql.prompt import PromptBuilder, load_schema  # noqa: E402

with warnings.catch_warnings():  # starlette's TestClient warns about httpx on import
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

QUESTION = "Swapper nhận bao nhiêu ETH?"
GOLD_SQL = "SELECT SUM(value) / 1e18 FROM transactions WHERE to_address IN {E1}"
RIGHT = f"```sql\n{GOLD_SQL}\n```"
WRONG = "```sql\nSELECT SUM(nope) FROM transactions WHERE to_address IN {E1}\n```"


# ---- serialize ------------------------------------------------------------------------


def test_cells_keep_big_and_exact_numbers() -> None:
    assert serialize.cell(2**256 - 1) == str(2**256 - 1)  # a 78-digit token amount
    assert serialize.cell(2**53 - 1) == 2**53 - 1
    assert serialize.cell(Decimal("1.500000000")) == "1.500000000"
    assert serialize.cell(datetime(2026, 9, 5, 12, 0)) == "2026-09-05T12:00:00"
    assert serialize.cell(b"\x01\xff") == "0x01ff"
    assert serialize.cell(float("nan")) == "nan"
    assert serialize.cell(1.25) == 1.25 and serialize.cell(None) is None


def test_preview_shortens_address_lists() -> None:
    addresses = {"E1": [f"0x{i:040x}" for i in range(5)], "E10": ["0xabc"], "E2": []}
    sql = "x IN {E1} OR y IN {E10} OR z IN {E2}"
    preview = serialize.preview_sql(sql, addresses)
    assert "… +2)" in preview and "('0xabc')" in preview and "(NULL)" in preview
    assert serialize.preview_sql(None, addresses) is None


# ---- models ---------------------------------------------------------------------------


def test_registry_names_the_frozen_runs(tmp_path: Path) -> None:
    models = {m.id: m for m in registry(tmp_path / "models", tmp_path / "predictions")}
    assert list(models) == ["3b-lora", "3b", "1.5b-lora", "1.5b"]
    lora, base = models["3b-lora"], models["3b"]
    assert (lora.phase, base.phase, lora.label) == (2, 1, "Qwen2.5-Coder 3B + LoRA")
    assert lora.run_file("dev") == tmp_path / "predictions/phase2/dev" / (
        "dev.predictions.prompts.qwen2.5-coder-3b-instruct-q4_k_m.3b-e2.jsonl"
    )
    assert base.details_file("test").name == (
        "test.predictions.prompts.qwen2.5-coder-3b-instruct-q4_k_m.ex-details.jsonl"
    )
    assert lora.adapter == tmp_path / "models/lora/3b-run/gguf/3b-e2.gguf"
    assert len(lora.missing_files()) == 2 and len(base.missing_files()) == 1  # no adapter
    assert lora.ex("dev") is None
    report = base.report_file("dev")
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"overall": {"ex": 0.5, "correct": 1, "questions": 2,
                                              "ex_extra_columns_ok": 0.5}}))  # fmt: skip
    assert base.public()["ex"] == {"dev": {"ex": 0.5, "correct": 1, "questions": 2}, "test": None}


# ---- the streaming client and the saved prefix, against a stub llama-server -------------


class StubServer(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []
    chunks: list[str] = []

    def _send(self, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, body))
        if self.path == "/apply-template":
            self._send(json.dumps({"prompt": body["messages"][0]["content"]}).encode())
        elif self.path == "/tokenize":
            self._send(json.dumps({"tokens": [len(w) for w in body["content"].split()]}).encode())
        elif self.path == "/v1/chat/completions":
            events = [{"choices": [{"delta": {"role": "assistant", "content": None}}]}]
            events += [{"choices": [{"delta": {"content": c}}]} for c in self.chunks]
            usage = {"prompt_tokens": 9, "completion_tokens": 3,
                     "prompt_tokens_details": {"cached_tokens": 4}}  # fmt: skip
            events.append({"choices": [], "usage": usage})
            text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            self._send(text.encode(), "text/event-stream")
        else:
            self._send(b"{}")

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def stub() -> str:
    StubServer.requests, StubServer.chunks = [], ["```sql\nSELECT ", "1\n```"]
    httpd = HTTPServer(("127.0.0.1", 0), StubServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_streaming_client_joins_the_stream(stub: str) -> None:
    tokens: list[str] = []
    client = StreamingClient(stub, on_token=tokens.append, max_tokens=99)
    messages = [{"role": "user", "content": "q"}]
    out = client.chat(messages)
    assert out == {"reply": "```sql\nSELECT 1\n```", "prompt_tokens": 9, "cached_tokens": 4,
                   "completion_tokens": 3}  # fmt: skip
    assert tokens == StubServer.chunks
    path, body = StubServer.requests[-1]
    assert path == "/v1/chat/completions"
    assert body["stream"] is True and "lora" not in body  # the process's own adapter, if any
    assert (body["temperature"], body["max_tokens"], body["seed"]) == (0.0, 99, 0)


def test_streaming_client_stops_when_cancelled(stub: str) -> None:
    cancel = threading.Event()
    client = StreamingClient(stub, on_token=lambda t: cancel.set(), cancel=cancel)
    with pytest.raises(Cancelled):
        client.chat([{"role": "user", "content": "q"}])
    with pytest.raises(Cancelled):  # nothing is sent once cancelled
        client.chat([{"role": "user", "content": "q"}])


def test_the_prefix_is_computed_once_then_restored(stub: str, tmp_path: Path) -> None:
    model = registry(tmp_path / "models")[0]  # 3b-lora
    for f in (model.base, model.adapter, tmp_path / "llama-server"):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    prompts = tmp_path / "dev.prompts.jsonl"
    prompts.write_text("".join(
        json.dumps({"question_id": q, "decision": "sql",
                    "messages": [{"role": "user", "content": f"schema evidence {q}"}]}) + "\n"
        for q in ("q1", "question2")
    ))  # fmt: skip
    manager = LlamaManager(tmp_path / "llama-server", log_dir=tmp_path, prefix_prompts=prompts)
    manager.slot_dir.mkdir()  # start() makes it
    client = StreamingClient(stub)
    assert manager.command(model)[3:5] == ["--lora", str(model.adapter.resolve())]
    assert "--lora" not in manager.command(registry(tmp_path / "models")[1])  # 3b: base alone

    manager._load_prefix(model, client)
    assert (manager.prefix, manager.prefix_source) == ([6, 8], "computed")
    assert [p for p, _ in StubServer.requests][-2:] == ["/completion", "/slots/0?action=save"]
    assert StubServer.requests[-2][1]["prompt"] == [6, 8]
    assert manager.cached_prefix(model) is None  # the server writes the KV file, the stub not
    (manager.slot_dir / "prefix-3b-lora.bin").write_bytes(b"kv")
    assert manager.cached_prefix(model) == [6, 8]

    StubServer.requests.clear()
    manager._load_prefix(model, client)
    assert manager.prefix_source == "restored"
    assert StubServer.requests == [("/slots/0?action=restore", {"filename": "prefix-3b-lora.bin"})]
    prompts.write_text(prompts.read_text() + "\n")  # another prompt file: computed again
    assert manager.cached_prefix(model) is None


# ---- one question through the pipeline -------------------------------------------------


class FakeClient:
    """Replies in order, two chunks each, through the hooks the page listens to."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list = []
        self.max_tokens = 64
        self.on_token = self.on_chat = None

    def chat(self, messages: list[dict]) -> dict:
        self.on_chat(messages)
        reply = self.replies.pop(0)
        self.calls.append(messages)
        for part in (reply[:10], reply[10:]):
            self.on_token(part)
        return {"reply": reply, "prompt_tokens": None, "completion_tokens": 7}

    def prompt_token_ids(self, messages: list[dict]) -> list[int]:
        return [ord(c) for m in messages for c in m["content"]]

    def prime(self, tokens: list[int]) -> None:
        self.calls.append("prime")

    def context_size(self) -> int | None:
        return None


@pytest.fixture
def linker(db: Path, knowledge_base_dir: Path) -> EntityLinker:
    return EntityLinker(load_resources(db, knowledge_base_dir))


@pytest.fixture
def examples(linker: EntityLinker) -> Examples:
    record = linker.link(QUESTION, "q00001")
    gold = record | {"gold_sql": GOLD_SQL, "template_id": "entity_eth_received"}
    frozen = {"question_id": "q00001", "decision": "sql", "sql": GOLD_SQL,
              "attempts": [{"reply": RIGHT, "sql": GOLD_SQL, "error_kind": None}]}  # fmt: skip
    split = Split("dev", {"q00001": gold}, {"q00001": record}, runs={"3b": {"q00001": frozen}},
                  details={"3b": {"q00001": {"kind": "correct", "correct": True}}})  # fmt: skip
    return Examples({"dev": split})


@pytest.fixture
def engine(db: Path, linker: EntityLinker, examples: Examples):
    builder = PromptBuilder(linker, load_schema(db))  # before the runner, as Service.load
    with SqlRunner(db, timeout=5) as runner:
        yield Engine(linker, builder, runner, examples)


def collect(engine: Engine, question: str, client: FakeClient, **kwargs):
    events: list[tuple[str, dict]] = []
    out = engine.ask(question, client, lambda e, d: events.append((e, d)), **kwargs)
    return out, events


def test_a_benchmark_question_emits_every_step_and_its_gold(engine: Engine) -> None:
    client = FakeClient([RIGHT])
    out, events = collect(engine, "  swapper nhận  bao nhiêu ETH? ", client, prefix=[1, 2])
    names = [e for e, _ in events]
    assert names == ["linking", "prompt", "attempt", "token", "token", "executing", "attempts",
                     "result", "gold", "timing"]  # fmt: skip
    data = dict(events)
    assert out["question_id"] == "q00001" and out["sql"] == GOLD_SQL  # matched despite case
    assert client.calls[:2] == ["prime", "prime"]  # as generate(): before, and in generate_one
    mention = data["linking"]["mentions"][0]
    assert (mention["placeholder"], mention["label"]) == ("E1", "Swapper")
    assert data["linking"]["placeholders"]["E1"]["count"] == 1
    assert data["prompt"]["decision"] == "sql" and data["prompt"]["evidence"]
    assert data["result"]["ok"] and data["result"]["columns"]
    assert data["result"]["final_error_kind"] is None
    gold = data["gold"]
    assert (gold["kind"], gold["correct"], gold["question_id"]) == ("correct", True, "q00001")
    assert gold["gold_result"]["rows"] == data["result"]["rows"]
    assert gold["frozen"] == {"3b": {"kind": "correct", "correct": True}}


def test_a_failed_query_is_repaired_live(engine: Engine) -> None:
    client = FakeClient([WRONG, RIGHT])
    out, events = collect(engine, "Swapper nhận bao nhiêu ETH ngày 2/9?", client)
    attempts = [d for e, d in events if e == "attempt"]
    assert [a["n"] for a in attempts] == [1, 2]
    assert attempts[0]["repair"] is None and "The query failed" in attempts[1]["repair"]
    assert [a["error_kind"] for a in out["attempts"]] == ["execution", None]
    assert "gold" not in dict(events) and out["question_id"] == "live"


@pytest.mark.parametrize(
    ("question", "decision"),
    [("Bybit nhận bao nhiêu ETH?", "nil"), ("Số dư của Swapper là bao nhiêu?", "unsupported")],
)
def test_no_model_call_when_the_pipeline_abstains(engine: Engine, question: str,
                                                  decision: str) -> None:  # fmt: skip
    client = FakeClient([])
    out, events = collect(engine, question, client)
    assert client.calls == [] and out["decision"] == decision
    assert dict(events)["answer"]["decision"] == decision
    assert [e for e, _ in events] == ["linking", "prompt", "answer", "timing"]


def test_serve_keeps_the_rule_of_the_september_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page and check-serve replay the September runs, whose class members were ranked by
    calls in all the data (docs/multi-month-plan.md D3)."""
    from eth_nl2sql import cli

    seen: list[str] = []

    def load(settings: Settings) -> Service:
        seen.append(settings.class_rank)
        raise SystemExit(0)

    monkeypatch.setattr(Service, "load", staticmethod(load))
    for argv in (["check-serve"], ["check-serve", "--class-rank", "question"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
    assert seen == ["data", "question"]
    assert Settings().class_rank == "data"


def test_normalized_questions_match() -> None:
    assert normalize_question(" Swapper  NHẬN\tbao nhiêu ETH? ") == "swapper nhận bao nhiêu eth?"


# ---- the service and the HTTP routes ---------------------------------------------------


class FakeLlama:
    def __init__(self) -> None:
        self.model_id: str | None = "3b"
        self.started: list[str] = []
        self.prefix: list[int] = []
        self.url = "http://fake"
        self.load_seconds, self.prefix_source = 1.0, "restored"
        self.crashes = 0  # died() answers True this many times

    def serves(self, model) -> bool:
        return self.model_id == model.id

    def ensure(self, model) -> bool:
        self.started.append(model.id)
        self.model_id = model.id
        return True

    def cached_prefix(self, model) -> list[int] | None:
        return None

    def died(self) -> bool:
        self.crashes -= 1
        return self.crashes >= 0

    def status(self) -> dict:
        return {"state": "ready", "model": self.model_id}

    def stop(self) -> None:
        pass


@pytest.fixture
def service(db: Path, engine: Engine, tmp_path: Path):
    replies: list[str] = []
    llama = FakeLlama()
    with SqlRunner(db, timeout=5) as browse:
        svc = Service(Settings(), engine, registry(tmp_path), llama, browse,
                      client_factory=lambda model, cancel: FakeClient(replies))  # fmt: skip
        svc.replies = replies
        yield svc


def sse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_ask_streams_server_sent_events(service: Service) -> None:
    service.replies.append(RIGHT)
    with TestClient(create_app(service, load_default=False)) as http:
        with http.stream("POST", "/api/ask", json={"question": QUESTION, "model": "3b"}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            events = sse_events("".join(r.iter_text()))
        assert [e for e, _ in events][0] == "start" and events[-1][0] == "done"
        assert dict(events)["gold"]["correct"] is True
        assert http.get("/api/status").json()["busy"] is None  # the GPU is free again


def test_another_model_loads_first(service: Service) -> None:
    service.replies.append(RIGHT)
    events: list[tuple[str, dict]] = []
    cancel = service.begin("3b-lora")
    service.ask_stream(QUESTION, "3b-lora", lambda e, d: events.append((e, d)), cancel)
    names = [e for e, _ in events]
    assert names[:3] == ["start", "model_loading", "model_ready"] and names[-1] == "done"
    assert service.llama.started == ["3b-lora"]
    assert dict(events)["model_ready"]["prefix"] == "restored"


def test_a_crashed_server_is_restarted_and_asked_again(service: Service) -> None:
    service.replies.append(RIGHT)
    service.llama.crashes = 1
    calls = []

    class Crashing(FakeClient):
        def chat(self, messages):
            raise ClientError("/v1/chat/completions: Remote end closed connection")

    def factory(model, cancel):
        calls.append(model.id)
        return Crashing([]) if len(calls) == 1 else FakeClient(service.replies)

    service.client_factory = factory
    service.llama.model_id = None  # the crash: the next _ask starts the model again
    events: list[tuple[str, dict]] = []
    cancel = service.begin("3b")
    service.ask_stream(QUESTION, "3b", lambda e, d: events.append((e, d)), cancel)
    names = [e for e, _ in events]
    assert "retry" in names and "error" not in names and names[-1] == "done"
    assert dict(events)["result"]["ok"] and calls == ["3b", "3b"]


def test_warm_saves_every_prefix_then_loads_the_default(service: Service) -> None:
    for m in service.models.values():
        for f in (m.base, m.adapter):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
    service.warm()
    with service.gpu:  # free again once warm is done
        pass
    assert service.llama.started == ["3b", "1.5b-lora", "1.5b", "3b-lora"]
    assert service.warming is None and service.busy is None


def test_warm_skips_models_without_files(service: Service) -> None:
    service.warm()
    with service.gpu:
        pass
    assert service.llama.started == ["3b-lora"]  # the default, which then reports its error


def test_one_question_at_a_time(service: Service) -> None:
    with TestClient(create_app(service, load_default=False)) as http:
        service._acquire("asking")
        try:
            r = http.post("/api/ask", json={"question": QUESTION, "model": "3b"})
            assert r.status_code == 409 and "câu khác" in r.json()["detail"]
            with pytest.raises(Busy):
                service.select("1.5b")
        finally:
            service._release()
        assert http.post("/api/ask", json={"question": "x", "model": "7b"}).status_code == 404


def test_select_starts_only_another_model(service: Service) -> None:
    assert service.select("3b") is False  # served already
    assert service.select("1.5b") is True
    with service.gpu:  # free again once loaded
        pass
    assert service.llama.started == ["1.5b"]


def test_errors_end_the_stream(service: Service) -> None:
    events: list[tuple[str, dict]] = []
    cancel = service.begin("3b")  # no reply queued: the fake client fails
    service.ask_stream(QUESTION, "3b", lambda e, d: events.append((e, d)), cancel)
    assert [e for e, _ in events][-2:] == ["error", "done"]
    assert service.busy is None and not service.gpu.locked()


def test_browse_routes(service: Service) -> None:
    with TestClient(create_app(service, load_default=False)) as http:
        models = http.get("/api/models").json()
        assert models["default"] == "3b-lora" and len(models["models"]) == 4
        listing = http.get("/api/examples?split=dev").json()
        assert listing["models"] == ["3b"] and listing["questions"][0]["question"] == QUESTION
        assert http.get("/api/examples?split=test").status_code == 404
        detail = http.get("/api/examples/dev/q00001").json()
        assert detail["gold"]["gold_sql"] == GOLD_SQL and "3b" in detail["predictions"]
        assert http.get("/api/samples").json()["samples"][0]["question_id"] == "q00001"
        gold = http.post("/api/run-sql", json={"split": "dev", "question_id": "q00001",
                                               "source": "gold"}).json()  # fmt: skip
        frozen = http.post("/api/run-sql", json={"split": "dev", "question_id": "q00001",
                                                 "source": "3b"}).json()  # fmt: skip
        assert gold["ok"] and gold["rows"] == frozen["rows"]
        missing = http.post("/api/run-sql", json={"split": "dev", "question_id": "q00001",
                                                  "source": "1.5b"})  # fmt: skip
        assert missing.status_code == 404


def test_without_llama_only_browsing_works(service: Service) -> None:
    service.llama = None
    with TestClient(create_app(service, load_default=False)) as http:
        assert http.post("/api/ask", json={"question": QUESTION, "model": "3b"}).status_code == 503
        assert http.get("/api/status").json()["llama"] is None
        assert http.get("/api/examples").status_code == 200
