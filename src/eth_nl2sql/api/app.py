"""The HTTP API of `eth-nl2sql serve`, and the static page of web/ (Next.js export).

POST /api/ask answers with Server-Sent Events (`event: <name>`, `data: <json>`), one per step of
`Engine.ask`, and always ends with `done`. The page reads them with fetch, since EventSource
cannot POST. Closing the stream stops the question.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from eth_nl2sql.api import serialize
from eth_nl2sql.api.service import Busy, Service, Unavailable

DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]  # `npm run dev` in web/
BUSY_MESSAGES = {
    "loading": "Đang nạp model, thử lại sau ít giây.",
    "asking": "Đang trả lời một câu khác.",
    "running": "Đang chạy một truy vấn khác.",
}


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    model: str


class SelectBody(BaseModel):
    model: str


class RunSqlBody(BaseModel):
    split: str
    question_id: str
    source: str  # gold, or a model id


def sse(event: str, data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False, default=serialize.cell)
    return f"event: {event}\ndata: {text}\n\n"


def _busy(e: Busy) -> HTTPException:
    return HTTPException(409, BUSY_MESSAGES.get(str(e), str(e)))


def create_app(service: Service, *, static_dir: Path | None = None,
               load_default: bool = True) -> FastAPI:  # fmt: skip
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if load_default and service.llama is not None:
            service.warm()  # the prefix of every model once, then the default model
        yield
        service.close()

    app = FastAPI(title="eth-nl2sql", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_methods=["*"],
                       allow_headers=["*"])  # fmt: skip

    @app.get("/api/status")
    def status() -> dict:
        return service.status()

    @app.get("/api/models")
    def models() -> dict:
        return {"models": service.public_models(), "default": service.settings.default_model}

    @app.post("/api/models/select", status_code=202)
    def select(body: SelectBody) -> dict:
        try:
            started = service.select(body.model)
        except KeyError as e:
            raise HTTPException(404, f"unknown model {body.model}") from e
        except Unavailable as e:
            raise HTTPException(503, str(e)) from e
        except Busy as e:
            raise _busy(e) from e
        return {"loading": started, "status": service.status()}

    @app.post("/api/ask")
    async def ask(body: AskBody) -> StreamingResponse:
        try:
            cancel = service.begin(body.model)
        except KeyError as e:
            raise HTTPException(404, f"unknown model {body.model}") from e
        except Unavailable as e:
            raise HTTPException(503, str(e)) from e
        except Busy as e:
            raise _busy(e) from e

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

        def emit(event: str, data: dict) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (event, data))
            except RuntimeError:  # the loop is gone: the server stops
                pass

        threading.Thread(
            target=service.ask_stream, args=(body.question, body.model, emit, cancel), daemon=True
        ).start()

        async def events():
            finished = False
            try:
                while not finished:
                    event, data = await queue.get()
                    finished = event == "done"
                    yield sse(event, data)
            finally:
                if not finished:  # the page went away or pressed stop
                    cancel.set()
                    service.cancel()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/cancel")
    def cancel() -> dict:
        return {"cancelled": service.cancel()}

    @app.get("/api/samples")
    def samples() -> dict:
        return {"samples": service.engine.examples.samples()}

    def _split(name: str):
        split = service.engine.examples.splits.get(name)
        if split is None:
            hint = " (start the server with --allow-test)" if name == "test" else ""
            raise HTTPException(404, f"split {name} is not loaded{hint}")
        return split

    @app.get("/api/examples")
    def examples(split: str = "dev") -> dict:
        s = _split(split)
        return {"split": split, "models": list(s.runs),
                "questions": [s.summary(qid) for qid in s.gold]}  # fmt: skip

    @app.get("/api/examples/{split}/{qid}")
    def example(split: str, qid: str) -> dict:
        s = _split(split)
        if qid not in s.gold:
            raise HTTPException(404, f"no question {qid} in {split}")
        return s.detail(qid)

    @app.post("/api/run-sql")
    def run_sql(body: RunSqlBody) -> dict:
        _split(body.split)
        try:
            return service.run_stored(body.split, body.question_id, body.source)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Busy as e:
            raise _busy(e) from e

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app
