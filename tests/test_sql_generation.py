"""The generation loop with a fake model, and the client against a stub llama-server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from eth_nl2sql.cli import main
from eth_nl2sql.entity_linking import EntityLinker
from eth_nl2sql.entity_linking_resources import load_resources
from eth_nl2sql.execution_evaluation import score
from eth_nl2sql.prompt_builder import PromptBuilder, load_schema
from eth_nl2sql.sql_execution import SqlRunner
from eth_nl2sql.sql_generation import (
    OpenAICompatibleClient,
    generate,
    generate_one,
    missing_placeholders,
    shared_prefix,
    summary,
)

RIGHT = "```sql\nSELECT SUM(value) / 1e18 FROM transactions WHERE to_address IN {E1}\n```"
OWN_ADDRESS = (
    "```sql\nSELECT SUM(value) / 1e18 FROM transactions "
    "WHERE to_address IN ('0xdac17f958d2ee523a2206206994597c13d831ec7')\n```"
)
WRONG = "```sql\nSELECT SUM(nope) FROM transactions WHERE to_address IN {E1}\n```"


class FakeClient:
    """Replies in order; a token is a character of the messages."""

    def __init__(self, replies: list[str], *, n_ctx: int | None = None) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict] | str] = []  # chats, and "prime" for a priming
        self.n_ctx = n_ctx
        self.max_tokens = 64

    def chat(self, messages: list[dict]) -> dict:
        self.calls.append(messages)
        return {"reply": self.replies.pop(0), "prompt_tokens": None, "completion_tokens": 7}

    def prompt_token_ids(self, messages: list[dict]) -> list[int]:
        return [ord(c) for m in messages for c in m["content"]]

    def prime(self, tokens: list[int]) -> None:
        self.calls.append("prime")

    def context_size(self) -> int | None:
        return self.n_ctx


@pytest.fixture
def linker(db: Path, knowledge_base_dir: Path) -> EntityLinker:
    return EntityLinker(load_resources(db, knowledge_base_dir))


@pytest.fixture
def case(db: Path, linker: EntityLinker):
    """A prompt of the fixture database, its linking record, and a runner."""
    record = linker.link("Swapper nhận bao nhiêu ETH?", "q1")
    prompt = PromptBuilder(linker, load_schema(db)).build(record)
    with SqlRunner(db, timeout=5) as runner:
        yield prompt, record, runner


def test_a_right_reply_is_asked_once(case, linker: EntityLinker) -> None:
    prompt, record, runner = case
    client = FakeClient([RIGHT])
    out = generate_one(prompt, client, runner, linker.placeholder_addresses(record))
    assert out["decision"] == "sql" and "{E1}" in out["sql"]
    assert [a["error_kind"] for a in out["attempts"]] == [None]
    assert client.calls == [prompt["messages"]]
    attempt = out["attempts"][0]
    assert attempt["reply"] == RIGHT and attempt["sql"] == out["sql"]
    assert attempt["prompt_tokens"] == len(client.prompt_token_ids(prompt["messages"]))


def test_a_failed_query_is_repaired_once(case, linker: EntityLinker) -> None:
    prompt, record, runner = case
    client = FakeClient([WRONG, RIGHT])
    out = generate_one(prompt, client, runner, linker.placeholder_addresses(record))
    assert [a["error_kind"] for a in out["attempts"]] == ["execution", None]
    assert out["sql"] == out["attempts"][1]["sql"]
    repair = client.calls[1]
    assert repair[:2] == prompt["messages"] and repair[2] == {"role": "assistant",
                                                              "content": WRONG}  # fmt: skip
    assert "nope" in repair[3]["content"]


def test_no_sql_twice_gives_up(case, linker: EntityLinker) -> None:
    prompt, record, runner = case
    client = FakeClient(["I cannot answer.", "Sorry."])
    out = generate_one(prompt, client, runner, linker.placeholder_addresses(record))
    assert out["sql"] is None and len(client.calls) == 2
    assert [a["error_kind"] for a in out["attempts"]] == ["no_sql", "no_sql"]


def test_a_query_without_the_placeholder_is_repaired(case, linker: EntityLinker) -> None:
    prompt, record, runner = case
    addresses = linker.placeholder_addresses(record)
    client = FakeClient([OWN_ADDRESS, RIGHT])
    out = generate_one(prompt, client, runner, addresses)
    assert [a["error_kind"] for a in out["attempts"]] == ["missing_placeholder", None]
    assert "{E1}" in client.calls[1][3]["content"]
    # Without the check the query ran, so B8 has nothing to repair.
    client = FakeClient([OWN_ADDRESS])
    out = generate_one(prompt, client, runner, addresses, placeholder_check=False)
    assert [a["error_kind"] for a in out["attempts"]] == [None]


def test_missing_placeholders_only_counts_those_of_the_prompt() -> None:
    messages = [{"role": "system", "content": "a placeholder such as {E3}"},
                {"role": "user", "content": "{E1} = Swapper; {E2} = USDT"}]  # fmt: skip
    assert missing_placeholders("SELECT 1 WHERE a IN {E2}", messages) == ["E1"]
    literal = [{"role": "user", "content": "Swapper = ('0x00')"}]
    assert missing_placeholders("SELECT 1", literal) == []


@pytest.mark.parametrize("decision", ["nil", "unsupported"])
def test_an_abstention_never_calls_the_model(case, decision: str) -> None:
    _, _, runner = case
    client = FakeClient([])
    prompt = {"question_id": "q2", "decision": decision, "answer": "..."}
    out = generate_one(prompt, client, runner, {})
    assert out == {"question_id": "q2", "decision": decision, "sql": None, "attempts": []}
    assert client.calls == []


def test_a_prompt_longer_than_the_context_is_skipped(case, linker: EntityLinker) -> None:
    prompt, record, runner = case
    client = FakeClient([RIGHT], n_ctx=100)
    out = generate_one(prompt, client, runner, linker.placeholder_addresses(record), n_ctx=100)
    assert out["attempts"] == [] and out["skipped"].startswith("context:")
    assert client.calls == []


def test_generate_takes_the_addresses_of_the_linking_record(case, linker: EntityLinker) -> None:
    prompt, record, runner = case
    nil = {"question_id": "q2", "decision": "nil", "answer": "..."}
    client = FakeClient([WRONG, RIGHT])
    out = list(generate([prompt, nil], {"q1": record}, linker, runner, client))
    assert [p["question_id"] for p in out] == ["q1", "q2"]
    stats = summary(out)
    assert stats["decisions"] == {"sql": 1, "nil": 1}
    assert stats["repaired"] == 1 and stats["repair_ran"] == 1
    assert stats["first_attempt"] == {"execution": 1} and stats["final"] == {"ran": 1}


def test_the_prefix_is_primed_before_each_question_not_before_a_repair(
    case, linker: EntityLinker
) -> None:
    prompt, record, runner = case
    system = prompt["messages"][0]
    other = {**prompt, "question_id": "q3",
             "messages": [system, {"role": "user", "content": "Other?"}]}  # fmt: skip
    client = FakeClient([WRONG, RIGHT, RIGHT])
    prefix = shared_prefix(client, [prompt, {"decision": "nil"}, other])
    assert prefix == client.prompt_token_ids([system])
    linking = {"q1": record, "q3": record}
    list(generate([prompt, other], linking, linker, runner, client, prefix=prefix))
    shape = ["prime" if c == "prime" else len(c) for c in client.calls]
    assert shape == ["prime", "prime", 2, 4, "prime", 2]


def test_a_skipped_prediction_scores_wrong() -> None:
    gold = {"question_id": "q1", "mentions": [], "unsupported": [], "gold_sql": "SELECT 1"}
    assert score(gold, {"decision": "sql", "skipped": "context"}, None, None)["kind"] == "skipped"
    assert score(gold, None, None, None)["kind"] == "missing"


# ---- the client, against a stub of llama-server -------------------------------------------


class StubServer(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []
    replies: list[str] = []

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/props":
            self._send({"model_path": "/models/fake.gguf", "build_info": "b1-test",
                        "default_generation_settings": {"n_ctx": 8192}})  # fmt: skip
        else:
            self._send({"data": [{"id": "fake.gguf"}]})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, body))
        if self.path == "/apply-template":
            text = "".join(m["content"] for m in body["messages"])
            self._send({"prompt": text})
        elif self.path == "/tokenize":
            self._send({"tokens": list(range(len(body["content"].split())))})
        elif self.path == "/completion":
            self._send({"content": "", "tokens_cached": len(body["prompt"])})
        else:
            reply = self.replies.pop(0)
            usage = {"prompt_tokens": 11, "completion_tokens": 5,
                     "prompt_tokens_details": {"cached_tokens": 3}}  # fmt: skip
            self._send({"choices": [{"message": {"content": reply}}], "usage": usage})

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server():
    StubServer.requests, StubServer.replies = [], []
    httpd = HTTPServer(("127.0.0.1", 0), StubServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_client_sends_greedy_requests(server: str) -> None:
    StubServer.replies = [RIGHT]
    client = OpenAICompatibleClient(server, max_tokens=99)
    messages = [{"role": "user", "content": "how many blocks"}]
    assert client.chat(messages) == {"reply": RIGHT, "prompt_tokens": 11, "cached_tokens": 3,
                                     "completion_tokens": 5}  # fmt: skip
    path, body = StubServer.requests[-1]
    assert path == "/v1/chat/completions"
    assert body == {"messages": messages, "temperature": 0.0, "max_tokens": 99, "seed": 0}
    assert client.prompt_tokens(messages) == 3
    client.prime([1, 2])
    assert StubServer.requests[-1] == (
        "/completion",
        {"prompt": [1, 2], "n_predict": 0, "cache_prompt": True},
    )
    assert client.context_size() == 8192
    assert client.server_info()["build_info"] == "b1-test"


def test_generate_sql_command(server: str, case, db: Path, knowledge_base_dir: Path,
                              tmp_path: Path) -> None:  # fmt: skip
    prompt, record, _ = case
    linking = tmp_path / "dev.linking.jsonl"
    linking.write_text(json.dumps(record, ensure_ascii=False) + "\n")
    nil = {"question_id": "q2", "decision": "nil", "answer": "..."}
    prompts = tmp_path / "dev.prompts.jsonl"
    prompts.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in (prompt, nil)))
    meta = {"input": str(linking), "options": {"evidence": True}}
    (tmp_path / "dev.prompts.jsonl.meta.json").write_text(json.dumps(meta))
    out = tmp_path / "run.jsonl"
    StubServer.replies = [WRONG, RIGHT]
    argv = ["generate-sql", "--prompts", str(prompts), "--output", str(out), "--server", server,
            "--db", str(db), "--knowledge-base-dir", str(knowledge_base_dir)]  # fmt: skip
    assert main(argv) == 0
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [(p["question_id"], len(p["attempts"])) for p in lines] == [("q1", 2), ("q2", 0)]
    assert lines[0]["sql"].endswith("IN {E1}")
    run_meta = json.loads((tmp_path / "run.jsonl.meta.json").read_text())
    assert run_meta["model"] == "fake" and run_meta["model_sha256"] is None
    assert run_meta["linking"] == str(linking) and len(run_meta["prompts_sha256"]) == 64
    assert run_meta["generation"]["temperature"] == 0.0
    assert run_meta["generation"]["placeholder_check"] is True
    assert run_meta["generation"]["fixed_prefix_tokens"] > 0
    assert [path for path, _ in StubServer.requests].count("/completion") == 2  # run, q1
    assert run_meta["summary"]["repair_ran"] == 1
