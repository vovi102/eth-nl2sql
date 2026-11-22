"""llama-server for the app: one process per configuration, and a streaming client.

Each configuration runs as its frozen runs ran: the base GGUF alone for phase 1
(scripts/phase1/test.sh), the base GGUF with `--lora <adapter>` for phase 2
(scripts/phase2/dev.sh), always with -c 8192 -ngl 99 -fa on -np 1 --cache-ram 0. A GTX 1650
(4 GB) holds one model, so another configuration means another process. Changing the LoRA of
a running server per request (`lora` field) was tried first: llama.cpp then reallocates its
compute buffers, and on this WSL GPU that crashed llama-server twice in about 30 changes
(CUDA "unknown error"), a path the frozen runs never took.

A new process would recompute the fixed prefix of generate-sql (4,195 tokens, about 30 s on the
GTX 1650 for 3B). So after the first time, the slot's KV cache of the prefix is saved
(`--slot-save-path`, a flag that changes no computation) together with the prefix tokens and
a key of what it depends on (model files, llama-server binary, flags, prompt file). A later
start with the same key restores it (POST /slots/0?action=restore) instead: the state the
first priming of `generate` leaves, computed in one batch from an empty cache. The priming
before each question then recomputes the last token, as in the frozen runs. `eth-nl2sql
check-serve` checks that live answers equal the frozen runs.

`StreamingClient` is `OpenAICompatibleClient` with the reply as a stream (the page shows it
token by token; the text is the same) and a flag that stops it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from eth_nl2sql.api.models import ModelConfig
from eth_nl2sql.linking.evaluation import read_records
from eth_nl2sql.sql.generation import (
    ClientError,
    OpenAICompatibleClient,
    served_adapter,
    shared_prefix,
)

DEFAULT_BINARY = Path(
    os.environ.get("LLAMA", "~/opt/llama.cpp/src-b12032/build/bin/llama-server")
).expanduser()
SERVER_FLAGS = ("-c", "8192", "-ngl", "99", "-fa", "on", "-np", "1", "--cache-ram", "0")
LOG_TAIL_CHARS = 1500


class LlamaError(RuntimeError):
    pass


class Cancelled(Exception):
    """The person stopped the question."""


class StreamingClient(OpenAICompatibleClient):
    def __init__(
        self,
        base_url: str,
        *,
        on_token: Callable[[str], None] | None = None,
        on_chat: Callable[[list[dict]], None] | None = None,
        cancel: threading.Event | None = None,
        **kwargs,
    ) -> None:
        super().__init__(base_url, **kwargs)
        self.on_token = on_token
        self.on_chat = on_chat
        self.cancel = cancel

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def chat(self, messages: list[dict]) -> dict:
        self._check_cancel()
        if self.on_chat:
            self.on_chat(messages)
        payload = {
            "messages": messages,
            **self.generation_parameters(),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.model:
            payload["model"] = self.model
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions", data=json.dumps(payload).encode(),
            headers=headers,
        )  # fmt: skip
        parts: list[str] = []
        usage: dict = {}
        try:
            # Leaving the with block closes the connection: llama-server stops generating.
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    self._check_cancel()
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            parts.append(text)
                            if self.on_token:
                                self.on_token(text)
        except urllib.error.HTTPError as e:
            body = e.read()[:500].decode(errors="replace")
            raise ClientError(f"/v1/chat/completions: HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ClientError(f"/v1/chat/completions: {e}") from e
        return {
            "reply": "".join(parts),
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _file_id(path: Path) -> list:
    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns]


class LlamaManager:
    """The one llama-server of the app. Not thread-safe: the caller holds the app's GPU lock.

    state: stopped, loading, ready or error. `prefix` is the fixed prefix of generate-sql: the
    tokens every prompt of `prefix_prompts` shares.
    """

    def __init__(
        self,
        binary: Path = DEFAULT_BINARY,
        *,
        port: int = 8080,
        log_dir: Path,
        prefix_prompts: Path,
        startup_timeout: float = 300.0,
    ) -> None:
        self.binary = Path(binary)
        self.port = port
        self.log_dir = Path(log_dir)
        self.prefix_prompts = Path(prefix_prompts)
        self.startup_timeout = startup_timeout
        self.proc: subprocess.Popen | None = None
        self.model: ModelConfig | None = None
        self.state = "stopped"
        self.error: str | None = None
        self.prefix: list[int] = []
        self.prefix_source: str | None = None  # computed | restored
        self.log_path: Path | None = None
        self.load_seconds: float | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def slot_dir(self) -> Path:
        return self.log_dir / "slots"

    def command(self, model: ModelConfig) -> list[str]:
        lora = ["--lora", str(model.adapter.resolve())] if model.phase == 2 else []
        return [
            str(self.binary),
            "-m", str(model.base.resolve()),
            *lora,
            *SERVER_FLAGS,
            "--slot-save-path", str(self.slot_dir.resolve()),
            "--host", "127.0.0.1",
            "--port", str(self.port),
        ]  # fmt: skip

    # ---- the saved prefix ------------------------------------------------------------

    def cache_key(self, model: ModelConfig) -> dict:
        """What the saved KV of the prefix depends on."""
        return {
            "model": model.id,
            "base": _file_id(model.base),
            "adapter": _file_id(model.adapter) if model.phase == 2 else None,
            "binary": _file_id(self.binary),
            "flags": list(SERVER_FLAGS),
            "prompts_sha256": hashlib.sha256(self.prefix_prompts.read_bytes()).hexdigest(),
        }

    def _cache_paths(self, model: ModelConfig) -> tuple[Path, str]:
        return self.slot_dir / f"prefix-{model.id}.json", f"prefix-{model.id}.bin"

    def cached_prefix(self, model: ModelConfig) -> list[int] | None:
        """The prefix tokens when a valid saved KV of them exists, else None."""
        meta, name = self._cache_paths(model)
        if not (meta.is_file() and (self.slot_dir / name).is_file()):
            return None
        try:
            saved = json.loads(meta.read_text())
            return saved["prefix"] if saved["key"] == self.cache_key(model) else None
        except (OSError, ValueError, KeyError):
            return None

    # ---- the process -----------------------------------------------------------------

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def serves(self, model: ModelConfig) -> bool:
        return (
            self.state == "ready"
            and self.model is not None
            and self.model.id == model.id
            and self.alive()
        )

    def ensure(self, model: ModelConfig) -> bool:
        """Serve `model`; True when a process had to be started for it."""
        if self.serves(model):
            return False
        self.start(model)
        return True

    def start(self, model: ModelConfig) -> None:
        self.stop()
        self.model, self.state, self.error = model, "loading", None
        self.prefix, self.prefix_source = [], None
        started = time.monotonic()
        try:
            missing = model.missing_files()
            if missing:
                raise LlamaError(f"missing files: {', '.join(missing)}")
            if not self.binary.is_file():
                raise LlamaError(f"no llama-server at {self.binary} (set LLAMA or --llama-bin)")
            if _port_open(self.port):
                raise LlamaError(f"port {self.port} is in use (another llama-server?)")
            self.slot_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = self.log_dir / f"llama-server-{model.id}.log"
            with self.log_path.open("w") as log:
                self.proc = subprocess.Popen(
                    self.command(model), stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )  # fmt: skip
            self._wait_healthy()
            client = OpenAICompatibleClient(self.url)
            adapter = model.adapter.resolve() if model.phase == 2 else None
            served_adapter(client.lora_adapters(), adapter)
            self._load_prefix(model, client)
            self.load_seconds = round(time.monotonic() - started, 1)
            self.state = "ready"
        except Exception as e:
            error = str(e)
            self.stop()
            self.state, self.error = "error", error
            raise LlamaError(error) from e

    def _load_prefix(self, model: ModelConfig, client: OpenAICompatibleClient) -> None:
        meta, name = self._cache_paths(model)
        cached = self.cached_prefix(model)
        if cached:
            client._request("/slots/0?action=restore", {"filename": name})
            self.prefix, self.prefix_source = cached, "restored"
            return
        self.prefix = shared_prefix(client, read_records(self.prefix_prompts))
        if self.prefix:
            client.prime(self.prefix)  # one batch from the empty cache, as generate() starts
            client._request("/slots/0?action=save", {"filename": name})
            meta.write_text(json.dumps({"key": self.cache_key(model), "prefix": self.prefix}))
        self.prefix_source = "computed"

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if not self.alive():
                raise LlamaError(f"llama-server exited:\n{self.log_tail()}")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        raise LlamaError(f"llama-server not ready after {self.startup_timeout:g} s")

    def died(self, wait: float = 2.0) -> bool:
        """Did the process exit (a crash)? Waits a little: a connection may break first."""
        deadline = time.monotonic() + wait
        while self.proc is not None and time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return True
            time.sleep(0.1)
        return self.proc is not None and self.proc.poll() is not None

    def log_tail(self) -> str:
        if self.log_path is None or not self.log_path.is_file():
            return ""
        return self.log_path.read_text(errors="replace")[-LOG_TAIL_CHARS:]

    def stop(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc = None
        if self.state != "error":
            self.state = "stopped"

    def status(self) -> dict:
        if self.state == "ready" and not self.alive():
            self.state, self.error = "error", f"llama-server exited:\n{self.log_tail()}"
        return {
            "state": self.state,
            "model": self.model.id if self.model else None,
            "error": self.error,
            "log": str(self.log_path) if self.log_path else None,
            "prefix_tokens": len(self.prefix),
            "prefix_source": self.prefix_source,
            "load_seconds": self.load_seconds,
        }
