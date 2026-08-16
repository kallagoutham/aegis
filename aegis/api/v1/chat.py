"""Conversational endpoints: chat, streaming chat, and history."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json

from fastapi import (
    APIRouter,
    Request,
    status,
)
from fastapi.responses import StreamingResponse

from aegis.api.deps import (
    CurrentSession,
    DbSession,
)
from aegis.core.config import settings
from aegis.core.langgraph.graph import get_agent
from aegis.core.limiter import limiter
from aegis.core.logging import logger
from aegis.core.metrics import (
    llm_stream_duration_seconds,
    llm_time_to_first_token_seconds,
)
from aegis.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Message,
    StreamChunk,
)
from aegis.services.database import UserRepository

router = APIRouter()


@router.post("", response_model=ChatResponse, summary="Send a message and get a reply")
@limiter.limit(settings.rate_limit_for("chat"))
async def chat(
    request: Request,
    payload: ChatRequest,
    chat_session: CurrentSession,
    session: DbSession,
) -> ChatResponse:
    """Run one conversational turn against the incident response agent.

    Retrieval still runs for chat: even a casual follow-up benefits from being
    grounded in the runbook corpus.
    """
    logger.info("chat_request", message_count=len(payload.messages))

    agent = get_agent()
    result = await agent.chat(
        [{"role": message.role, "content": message.content} for message in payload.messages],
        session_id=chat_session.id,
        user_id=chat_session.user_id,
        service=payload.service,
    )

    repository = UserRepository(session)
    await repository.touch_session(chat_session.id, message_delta=2)
    await session.commit()

    return ChatResponse(
        messages=[Message(role="assistant", content=result["reply"])],
        session_id=chat_session.id,
        citations=result.get("citations", []),
    )


@router.post("/stream", summary="Stream a reply as server-sent events")
@limiter.limit(settings.rate_limit_for("chat_stream"))
async def chat_stream(
    request: Request,
    payload: ChatRequest,
    chat_session: CurrentSession,
) -> StreamingResponse:
    """Stream the agent's reply token by token.

    Errors that occur mid-stream cannot be returned as an HTTP status - headers
    are long gone by then - so they are delivered as a final SSE frame with
    ``error`` populated. Clients must check that field rather than assuming a
    200 means success.
    """
    logger.info("chat_stream_request", message_count=len(payload.messages))
    agent = get_agent()
    messages = [{"role": message.role, "content": message.content} for message in payload.messages]

    async def event_stream() -> AsyncGenerator[str, None]:
        """Yield SSE frames for each token, then a terminal frame."""
        import time

        started = time.perf_counter()
        first_token_at: float | None = None
        model = settings.DEFAULT_LLM_MODEL

        try:
            async for token in agent.stream(
                messages,
                session_id=chat_session.id,
                user_id=chat_session.user_id,
                service=payload.service,
            ):
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                    llm_time_to_first_token_seconds.labels(model=model).observe(first_token_at - started)

                yield f"data: {json.dumps(StreamChunk(content=token).model_dump())}\n\n"

            from aegis.core.langgraph.tools import drain_citations

            final = StreamChunk(done=True, citations=drain_citations())
            yield f"data: {json.dumps(final.model_dump())}\n\n"

        except Exception as exc:
            logger.error("chat_stream_failed", error=str(exc), exc_info=True)
            failure = StreamChunk(done=True, error=str(exc))
            yield f"data: {json.dumps(failure.model_dump())}\n\n"

        finally:
            llm_stream_duration_seconds.labels(model=model).observe(time.perf_counter() - started)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # Without this, nginx buffers the whole response and streaming
            # silently degrades to a single delayed payload.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/messages", response_model=ChatResponse, summary="Read this session's history")
@limiter.limit(settings.rate_limit_for("messages"))
async def read_messages(request: Request, chat_session: CurrentSession) -> ChatResponse:
    """Return the stored conversation for the authenticated session."""
    agent = get_agent()
    history = await agent.history(chat_session.id)
    return ChatResponse(
        messages=[Message(role=entry["role"], content=entry["content"]) for entry in history],
        session_id=chat_session.id,
    )


@router.delete(
    "/messages",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear this session's history",
)
@limiter.limit(settings.rate_limit_for("messages"))
async def clear_messages(request: Request, chat_session: CurrentSession, session: DbSession) -> None:
    """Delete the conversation history while keeping the session open."""
    agent = get_agent()
    await agent.clear_history(chat_session.id)

    chat_session.message_count = 0
    session.add(chat_session)
    await session.commit()

    logger.info("chat_history_cleared", session_id=str(chat_session.id))


__all__ = ["router"]
