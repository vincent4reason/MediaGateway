"""OpenAI chat-completions face for the local qwen LLM (server/llm.py lifecycle).

POST /v1/chat/completions — non-streaming only. While a video/shot job is
running on the GPU the LLM is not admitted: 503 with a retryable message
(front-end retries later; no long blocking wait).
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional

from . import core, llm

router = APIRouter()

UPSTREAM_TIMEOUT_S = 300.0


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


def _video_running() -> bool:
    rows = core.db().execute(
        "SELECT 1 FROM jobs WHERE status='running' AND type IN ('video','shot') LIMIT 1"
    ).fetchall()
    return bool(rows)


@router.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    if req.stream:
        return JSONResponse(status_code=400, content={
            "error": {"message": "stream=true 不支持，请用非流式请求", "type": "invalid_request_error"}})
    if _video_running():
        return JSONResponse(status_code=503, content={
            "error": {"message": "视频生成中，LLM 排队稍后重试", "type": "busy",
                      "retryable": True}})
    llm.ensure()
    body = req.model_dump(exclude_none=True)
    with llm.busy_guard():
        r = httpx.post(f"{llm.BASE_URL}/v1/chat/completions", json=body,
                       timeout=UPSTREAM_TIMEOUT_S)
    return JSONResponse(status_code=r.status_code, content=r.json())
