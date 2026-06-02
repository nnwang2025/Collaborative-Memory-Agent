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
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
    backend: str = "langgraph"  # "langgraph" | "react" | "linear"
    stream: bool = False


class ChatResponse(BaseModel):
    answer: str
    backend: str


# ---------------------------------------------------------------------------
# Agent factory (lazy singleton)
# ---------------------------------------------------------------------------

_agents: dict = {}


def _get_agent(backend: str):
    """Get or create an agent instance for the given backend."""
    if backend not in _agents:
        if backend == "react":
            from src.agents.react_agent import ReActAgent
            _agents[backend] = ReActAgent(max_iterations=5)
        elif backend == "linear":
            from src.agents.coordinator_agent import CoordinatorAgent
            _agents[backend] = CoordinatorAgent(use_langgraph=False)
        else:  # langgraph (default)
            from src.agents.coordinator_agent import CoordinatorAgent
            _agents[backend] = CoordinatorAgent(use_langgraph=True)
    return _agents[backend]


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

    agent = _get_agent(req.backend)

    if req.backend == "react":
        answer = agent.run(req.question)
    else:
        answer = agent.ask(req.question)

    return ChatResponse(answer=answer, backend=req.backend)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """Answer a question with Server-Sent Events streaming.

    When ``req.backend == 'langgraph'``, streams per-node progress
    updates (classifying → routing → retrieving → composing).
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    agent = _get_agent(req.backend)

    async def event_generator() -> AsyncGenerator[str, None]:
        if req.backend == "react":
            # ReAct: stream a single result (tool-calling makes
            # incremental streaming complex; full result is fine)
            answer = agent.run(req.question)
            yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        elif req.backend == "langgraph":
            # LangGraph: stream per-node progress
            try:
                for chunk in agent.astream(req.question):
                    node_name = list(chunk.keys())[0] if chunk else "unknown"
                    yield f"data: {json.dumps({'type': 'node', 'node': node_name}, ensure_ascii=False)}\n\n"

                # Get the final answer
                answer = agent.ask(req.question)
                yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        else:
            answer = agent.ask(req.question)
            yield f"data: {json.dumps({'type': 'answer', 'content': answer}, ensure_ascii=False)}\n\n"
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
