"""
FastAPI application factory for the Collaborative Memory Agent.

Endpoints:
  POST /chat        – Ask a question (compatibility JSON response)
  POST /chat/stream – Ask a question (compatibility SSE streaming)
  GET  /health      – Health check
  Session/Run REST + SSE endpoints under /sessions and /runs

Importing this module must not read a developer's ``.env`` or create a
database.  Runtime dependencies are injected through ``create_app``; the
default runtime is created lazily on the first Session/Run API call.

Usage:
    uvicorn api:app --reload --port 8000
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import AsyncGenerator, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from src.api.runtime_routes import build_runtime_router
from src.evidence import EvidenceScope
from src.runtime.config import RuntimeConfig


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    question: str
    backend: Literal["langgraph", "react", "linear"] = "langgraph"
    stream: bool = False
    scope: EvidenceScope = EvidenceScope()


class ChatResponse(BaseModel):
    answer: str
    backend: str
    run_id: str | None = None
    termination_reason: str | None = None
    evidence_refs: list[dict] = []
    investigation: dict | None = None


# ---------------------------------------------------------------------------
# Agent factory (request-local, scope bound)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
)


def _stream_error_message(exc: Exception) -> str:
    """Describe a stream failure without echoing credentials."""
    text = str(exc).strip() or exc.__class__.__name__
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:500]


def _get_agent(backend: str, scope=None):
    """Create request-local agents until authenticated sessions are available."""
    if backend == "react":
        from src.agents.react_agent import ReActAgent
        return ReActAgent(max_iterations=5, scope=scope)
    if backend not in {"linear", "langgraph"}:
        raise ValueError("Unsupported backend")
    from src.agents.coordinator_agent import CoordinatorAgent
    return CoordinatorAgent(use_langgraph=backend == "langgraph", scope=scope)


# ---------------------------------------------------------------------------
# Compatibility routes
# ---------------------------------------------------------------------------

async def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "backends": ["langgraph", "react", "linear"],
    }


async def chat(req: ChatRequest):
    """Answer a question (non-streaming, in-request compatibility path)."""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    agent = await run_in_threadpool(_get_agent, req.backend, req.scope)

    if req.backend == "react":
        if hasattr(agent, "run_result"):
            result = await run_in_threadpool(agent.run_result, req.question)
            return ChatResponse(answer=result.answer, backend=req.backend, run_id=result.run_id,
                termination_reason=result.termination_reason,
                evidence_refs=[r.model_dump() for r in result.evidence_refs],
                investigation=result.model_dump(exclude={"answer"}, mode="json"))
        answer = await run_in_threadpool(agent.run, req.question)
    else:
        answer = await run_in_threadpool(agent.ask, req.question)

    return ChatResponse(answer=answer, backend=req.backend)


async def chat_stream(req: ChatRequest):
    """Compatibility SSE path; the persistent event stream lives at /runs/{id}/stream."""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    agent = await run_in_threadpool(_get_agent, req.backend, req.scope)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            if req.backend == "react":
                if hasattr(agent, "run_result"):
                    result = await run_in_threadpool(agent.run_result, req.question)
                    yield f"data: {json.dumps({'type': 'investigation', 'run_id': result.run_id, 'termination_reason': result.termination_reason, 'evidence_refs': [r.model_dump() for r in result.evidence_refs]}, ensure_ascii=False)}\n\n"
                    answer = result.answer
                else:
                    answer = await run_in_threadpool(agent.run, req.question)
                yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
            elif req.backend == "langgraph":
                answer = None
                async for chunk in agent.astream(req.question):
                    node_name = list(chunk.keys())[0] if chunk else "unknown"
                    yield f"data: {json.dumps({'type': 'node', 'node': node_name}, ensure_ascii=False)}\n\n"
                    if "generate_answer" in chunk:
                        answer = chunk["generate_answer"].get("answer")

                if answer is None:
                    raise RuntimeError("Graph completed without an answer")
                yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
            else:
                answer = await run_in_threadpool(agent.ask, req.question)
                yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': _stream_error_message(exc)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(runtime_service=None, runtime_config=None) -> FastAPI:
    """Build an isolated app; tests inject a RunService bound to temp SQLite."""
    app = FastAPI(
        title="Collaborative Memory Agent",
        description="基于 Agent + RAG 的研发协作长期记忆系统 API",
        version="2.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.runtime_service = runtime_service
    app.state.runtime_config = runtime_config or RuntimeConfig.from_env()
    app.state.runtime_lock = threading.Lock()
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/chat", chat, methods=["POST"], response_model=ChatResponse)
    app.add_api_route("/chat/stream", chat_stream, methods=["POST"])
    app.include_router(build_runtime_router())
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    from src.runtime.config import load_env_file

    load_env_file()
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
