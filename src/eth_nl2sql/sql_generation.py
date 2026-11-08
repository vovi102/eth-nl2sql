"""The SQL model on the prompts of B7, then B8 (proposed-method.md §4, §7).

A prompt file of data/prompts/ has one line per question. A `decision` other than sql is
written out as it is, without calling the model. For sql:

1. The model answers the chat messages, greedy (temperature 0), at most `max_tokens` tokens.
2. B8 takes the SQL out of the reply and runs it with the addresses of the linking record the
   prompt was built from (`EntityLinker.placeholder_addresses`).
3. A query that fails gets one more try with `repair_messages`. With `placeholder_check`
   (the default), so does a query that runs but leaves out a placeholder the prompt gives: the
   model wrote an address of its own instead of {E1} (`missing_placeholder`; every gold query
   of dev uses every placeholder of its record).

The prompt cache of llama-server makes a question cheap (the schema is the same in every
prompt), but it also makes greedy decoding depend on the question before: the server reuses
as many tokens as the two prompts share, and a different split between reused and computed
tokens gives slightly different numbers, now and then another token. With `fixed_prefix` (the
default), the cache is brought back before each question to the tokens every prompt of the
file shares (`prime`), so each question starts from the same state whatever runs before it.

The output line is {question_id, decision, sql, attempts}: `sql` is the last attempt's SQL,
still with placeholders, the format `evaluate-sql` reads. A prompt longer than the context of
the server is not sent (decision 7 of proposed-method.md §9): its line has `skipped`.

The client is apart from the loop, so that phase 2 (LoRA) runs through the same code with
another server: anything that speaks the OpenAI chat API. A LoRA adapter is loaded by
llama-server itself (`--lora`, `--lora-scaled`); `served_adapter` checks that the server applies
the adapter the run is named after, and no other one.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Protocol

from eth_nl2sql.benchmark_validate import PLACEHOLDER_RE
from eth_nl2sql.entity_linking import EntityLinker
from eth_nl2sql.sql_execution import Execution, SqlRunner, extract_sql, repair_messages

MAX_ATTEMPTS = 2  # the reply and one repair


class ClientError(RuntimeError):
    pass


class ChatClient(Protocol):
    max_tokens: int

    def chat(self, messages: list[dict]) -> dict:
        """{reply, prompt_tokens, completion_tokens} of one greedy completion."""
        ...

    def prompt_token_ids(self, messages: list[dict]) -> list[int] | None:
        """The messages after the chat template, tokenized; None when the server cannot say."""
        ...

    def prime(self, tokens: list[int]) -> None:
        """Leave exactly these tokens in the prompt cache of the server."""
        ...

    def context_size(self) -> int | None: ...


class OpenAICompatibleClient:
    """A chat completions server: llama.cpp's llama-server here, vLLM or another one later.

    Only `prompt_token_ids`, `prime` and `server_info` use endpoints of llama-server
    (/apply-template, /tokenize, /completion, /props); elsewhere they answer None or fail.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        model: str | None = None,
        max_tokens: int = 512,
        seed: int = 0,
        timeout: float = 600.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.seed = seed
        self.timeout = timeout
        self.api_key = api_key
        self._props: dict | None = None

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:500].decode(errors="replace")
            raise ClientError(f"{path}: HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise ClientError(f"{path}: {e.reason}") from e

    def generation_parameters(self) -> dict:
        return {"temperature": 0.0, "max_tokens": self.max_tokens, "seed": self.seed}

    def chat(self, messages: list[dict]) -> dict:
        payload = {"messages": messages, **self.generation_parameters()}
        if self.model:
            payload["model"] = self.model
        out = self._request("/v1/chat/completions", payload)
        usage = out.get("usage") or {}
        return {
            "reply": out["choices"][0]["message"]["content"] or "",
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }

    def props(self) -> dict | None:
        if self._props is None:
            try:
                self._props = self._request("/props")
            except ClientError:
                return None
        return self._props

    def prompt_token_ids(self, messages: list[dict]) -> list[int] | None:
        """The chat template with the generation prompt, tokenized by the model's tokenizer."""
        try:
            prompt = self._request("/apply-template", {"messages": messages})["prompt"]
            return self._request("/tokenize", {"content": prompt})["tokens"]
        except (ClientError, KeyError):
            return None

    def prompt_tokens(self, messages: list[dict]) -> int | None:
        ids = self.prompt_token_ids(messages)
        return None if ids is None else len(ids)

    def prime(self, tokens: list[int]) -> None:
        """A completion of the tokens alone: llama-server keeps the tokens a request shares
        with the cache, recomputes the last one and drops the rest."""
        self._request("/completion", {"prompt": tokens, "n_predict": 0, "cache_prompt": True})

    def context_size(self) -> int | None:
        props = self.props() or {}
        return (props.get("default_generation_settings") or {}).get("n_ctx")

    def lora_adapters(self) -> list[dict] | None:
        """[{id, path, scale}] of llama-server's GET /lora-adapters; None without the endpoint."""
        try:
            adapters = self._request("/lora-adapters")
        except ClientError:
            return None
        return adapters if isinstance(adapters, list) else None

    def server_info(self) -> dict:
        """What the run's meta file records about the model and the runtime."""
        props = self.props() or {}
        try:
            models = [m["id"] for m in self._request("/v1/models")["data"]]
        except (ClientError, KeyError):
            models = []
        return {
            "base_url": self.base_url,
            "models": models,
            "model_path": props.get("model_path"),
            "build_info": props.get("build_info"),
            "n_ctx": self.context_size(),
            "chat_template": props.get("chat_template"),
            "lora_adapters": self.lora_adapters(),
        }


def _same_file(served: str, adapter: Path) -> bool:
    """A path of /lora-adapters names `adapter`: the same absolute path, or the same file name
    when the server got a relative path (relative to its own working directory)."""
    path = Path(served)
    if path.is_absolute():
        return path.resolve() == adapter.resolve()
    return path.name == adapter.name


def served_adapter(adapters: list[dict] | None, adapter: Path | None) -> dict | None:
    """The /lora-adapters entry of `adapter`, or None for a run without adapter.

    Raises ClientError when the run would be named wrong: an adapter the server does not load,
    another adapter loaded beside it, or a loaded adapter that the run does not name (even at
    scale 0, so that the runtime check of phase2-plan.md §5.2 is named after its adapter).
    """
    if adapter is None:
        if adapters:
            paths = ", ".join(f"{a.get('path')} (scale {a.get('scale')})" for a in adapters)
            raise ClientError(f"the server loads LoRA adapters: {paths}; name it with --adapter")
        return None
    if adapters is None:
        raise ClientError("the server cannot list its LoRA adapters (GET /lora-adapters)")
    match = [a for a in adapters if _same_file(str(a.get("path", "")), adapter)]
    if len(match) != 1 or len(adapters) != 1:
        paths = ", ".join(str(a.get("path")) for a in adapters) or "none"
        raise ClientError(f"the server must load exactly {adapter}, it loads: {paths}")
    return match[0]


def adapter_tag(adapter: Path, scale: float) -> str:
    """The adapter's part of a prediction file name: its stem, and the scale unless it is 1."""
    return adapter.stem if scale == 1 else f"{adapter.stem}.scale{scale:g}"


def _fits(client: ChatClient, messages: list[dict], n_ctx: int | None) -> tuple[bool, int | None]:
    """Does the prompt leave room for max_tokens of reply?"""
    ids = client.prompt_token_ids(messages)
    tokens = None if ids is None else len(ids)
    if n_ctx is None or tokens is None:
        return True, tokens
    return tokens + client.max_tokens <= n_ctx, tokens


def shared_prefix(client: ChatClient, prompts: Iterable[dict]) -> list[int]:
    """The tokens every sql prompt starts with: the system prompt and, with the full schema,
    the schema. Empty when the server cannot tokenize."""
    prefix = None
    for prompt in prompts:
        if prompt["decision"] != "sql":
            continue
        ids = client.prompt_token_ids(prompt["messages"])
        if ids is None:
            return []
        if prefix is None:
            prefix = ids
            continue
        n = 0
        while n < min(len(prefix), len(ids)) and prefix[n] == ids[n]:
            n += 1
        prefix = prefix[:n]
    return prefix or []


def missing_placeholders(sql: str, messages: list[dict]) -> list[str]:
    """Placeholders of the evidence that the query does not use; none in a literal prompt.

    Only user messages count: the system prompt names {E1} as an example.
    """
    given = {
        name
        for m in messages
        if m["role"] == "user"
        for name in PLACEHOLDER_RE.findall(m["content"])
    }
    return sorted(given - set(PLACEHOLDER_RE.findall(sql)))


def generate_one(
    prompt: dict,
    client: ChatClient,
    runner: SqlRunner,
    addresses: dict[str, list[str]],
    n_ctx: int | None = None,
    *,
    placeholder_check: bool = True,
    prefix: list[int] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict:
    """{question_id, decision, sql, attempts} of one line of a prompt file; `prefix` is primed
    in the cache before the first attempt (a repair goes on from the attempt it repairs).
    `max_attempts` 1 is the reply alone, without repair (`generate-sql --no-repair`)."""
    out = {"question_id": prompt["question_id"], "decision": prompt["decision"], "sql": None,
           "attempts": []}  # fmt: skip
    if prompt["decision"] != "sql":
        return out
    messages = prompt["messages"]
    for _ in range(max_attempts):
        fits, tokens = _fits(client, messages, n_ctx)
        if not fits:
            if not out["attempts"]:
                out["skipped"] = f"context: {tokens} prompt tokens + {client.max_tokens} > {n_ctx}"
            break
        if prefix and not out["attempts"]:
            client.prime(prefix)
        started = time.monotonic()
        answer = client.chat(messages)
        seconds = time.monotonic() - started
        sql = extract_sql(answer["reply"])
        run = runner.run(sql, addresses)
        if run.ok and placeholder_check:
            missing = missing_placeholders(sql, prompt["messages"])
            if missing:
                names = ", ".join(f"{{{n}}}" for n in missing)
                run = Execution(sql, "missing_placeholder",
                                f"the evidence gives {names} but the query does not use it; "
                                "write the placeholder itself, not an address")  # fmt: skip
        out["sql"] = sql
        out["attempts"].append({
            "reply": answer["reply"],
            "sql": sql,
            "error_kind": run.error_kind,
            "error": run.error,
            "seconds": round(seconds, 3),
            "prompt_tokens": answer.get("prompt_tokens") or tokens,
            "cached_tokens": answer.get("cached_tokens"),
            "completion_tokens": answer.get("completion_tokens"),
        })  # fmt: skip
        if run.ok:
            break
        messages = repair_messages(messages, answer["reply"], run)
    return out


def generate(
    prompts: Iterable[dict],
    linking: dict[str, dict],
    linker: EntityLinker,
    runner: SqlRunner,
    client: ChatClient,
    *,
    progress: Callable[[int, dict], None] | None = None,
    placeholder_check: bool = True,
    prefix: list[int] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> Iterator[dict]:
    """Predictions for every prompt, in order; `linking` maps question_id to its record.

    With a `prefix` (`shared_prefix`), it is primed once before the run as well: the first
    priming computes all of it in one batch, every later one recomputes only its last token, so
    the first question would otherwise start from a slightly different cache than the others.
    """
    n_ctx = client.context_size()
    if prefix:
        client.prime(prefix)
    for i, prompt in enumerate(prompts, 1):
        addresses = {}
        if prompt["decision"] == "sql":
            record = linking.get(prompt["question_id"])
            if record is None:
                raise ClientError(f"{prompt['question_id']}: no linking record for the prompt")
            addresses = linker.placeholder_addresses(record)
        prediction = generate_one(
            prompt, client, runner, addresses, n_ctx,
            placeholder_check=placeholder_check, prefix=prefix, max_attempts=max_attempts,
        )  # fmt: skip
        if progress:
            progress(i, prediction)
        yield prediction


def summary(predictions: list[dict]) -> dict:
    """Counts of the run for the meta file: decisions, errors, repairs, seconds."""
    asked = [p for p in predictions if p["attempts"]]
    final = [p["attempts"][-1]["error_kind"] or "ran" for p in asked]
    seconds = [sum(a["seconds"] for a in p["attempts"]) for p in asked]
    first = [p["attempts"][0]["seconds"] for p in asked]
    return {
        "questions": len(predictions),
        "decisions": dict(Counter(p["decision"] for p in predictions).most_common()),
        "skipped": sum("skipped" in p for p in predictions),
        "asked": len(asked),
        "repaired": sum(len(p["attempts"]) > 1 for p in asked),
        "repair_ran": sum(
            len(p["attempts"]) > 1 and p["attempts"][-1]["error_kind"] is None for p in asked
        ),
        "first_attempt": dict(
            Counter(p["attempts"][0]["error_kind"] or "ran" for p in asked).most_common()
        ),
        "final": dict(Counter(final).most_common()),
        "seconds_per_question": sum(seconds) / len(seconds) if seconds else None,
        "seconds_first_attempt": sum(first) / len(first) if first else None,
        "max_completion_tokens": max(
            (a["completion_tokens"] or 0 for p in asked for a in p["attempts"]), default=None
        ),
    }
