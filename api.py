"""
FastAPI server for the Collaborative Memory Agent.

Endpoints:
  POST /chat       – Ask a question (JSON response)
  POST /chat/stream – Ask a question (SSE streaming)
  GET  /health      – Health check

Usage:
    uvicorn api:app --reload --port 8000
"""

from __future__ import annotations

import json
import os
import re
from typing import AsyncGenerator, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from src.evidence import EvidenceScope

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

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
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "backends": ["langgraph", "react", "linear"],
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Answer a question (non-streaming)."""
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


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """Answer a question with Server-Sent Events streaming.

    When ``req.backend == 'langgraph'``, streams per-node progress
    updates (classifying → routing → retrieving → composing).
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    agent = await run_in_threadpool(_get_agent, req.backend, req.scope)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            if req.backend == "react":
                # ReAct: stream a single result (tool-calling makes
                # incremental streaming complex; full result is fine)
                if hasattr(agent, "run_result"):
                    result = await run_in_threadpool(agent.run_result, req.question)
                    yield f"data: {json.dumps({'type': 'investigation', 'run_id': result.run_id, 'termination_reason': result.termination_reason, 'evidence_refs': [r.model_dump() for r in result.evidence_refs]}, ensure_ascii=False)}\n\n"
                    answer = result.answer
                else:
                    answer = await run_in_threadpool(agent.run, req.question)
                yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
            elif req.backend == "langgraph":
                # LangGraph: stream per-node progress
                answer = None
                async for chunk in agent.astream(req.question):
                    node_name = list(chunk.keys())[0] if chunk else "unknown"
                    yield f"data: {json.dumps({'type': 'node', 'node': node_name}, ensure_ascii=False)}\n\n"
                    if "generate_answer" in chunk:
                        answer = chunk["generate_answer"].get("answer")

                # Get the final answer
                if answer is None:
                    raise RuntimeError("Graph completed without an answer")
                yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
            else:
                answer = await run_in_threadpool(agent.ask, req.question)
                yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            # Structured terminal error for every backend; the DONE marker
            # below still runs so clients never hang on an aborted stream.
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
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
