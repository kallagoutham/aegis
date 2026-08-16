"""Chat request and response schemas."""

from __future__ import annotations

from datetime import datetime
import re
from typing import (
    Annotated,
    Any,
    Literal,
)
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

# Patterns rejected outright rather than escaped. These have no legitimate place
# in an incident description, and blocking them at the schema boundary means
# every downstream consumer - the model, the database, a web UI - receives text
# that has already been screened.
_SCRIPT_RE = re.compile(r"<\s*script[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_EVENT_HANDLER_RE = re.compile(r"\bon(?:error|load|click|mouseover)\s*=", re.IGNORECASE)


class Message(BaseModel):
    """One turn in a conversation."""

    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant", "system", "tool"] = Field(..., description="Who produced this message.")
    content: Annotated[str, Field(min_length=1, max_length=100_000)] = Field(
        ..., description="Message text. The generous ceiling accommodates pasted stack traces."
    )
    name: str | None = Field(default=None, max_length=100, description="Tool name, for tool messages.")

    @field_validator("content")
    @classmethod
    def _screen_content(cls, value: str) -> str:
        """Reject content carrying obvious injection payloads.

        Null bytes are rejected because Postgres cannot store them in a text
        column - allowing them here would turn into a 500 at write time rather
        than a clear 422 at the boundary.
        """
        if "\x00" in value:
            raise ValueError("content must not contain null bytes")
        if _SCRIPT_RE.search(value):
            raise ValueError("content must not contain script tags")
        if _EVENT_HANDLER_RE.search(value):
            raise ValueError("content must not contain inline event handlers")
        return value


class ChatRequest(BaseModel):
    """Request body for the chat endpoints."""

    messages: list[Message] = Field(..., min_length=1, max_length=200)
    service: str | None = Field(default=None, max_length=200, description="Scope retrieval to one service.")
    incident_id: uuid.UUID | None = Field(default=None, description="Ground the conversation in an incident.")

    @field_validator("messages")
    @classmethod
    def _require_trailing_user_message(cls, value: list[Message]) -> list[Message]:
        """Require the conversation to end with a user turn.

        Without this the agent would be asked to respond to its own last
        message, which produces confident, untethered rambling.
        """
        if value[-1].role != "user":
            raise ValueError("the final message must have role 'user'")
        return value


class ChatResponse(BaseModel):
    """Response body for the chat endpoints."""

    messages: list[Message]
    session_id: uuid.UUID | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)


class StreamChunk(BaseModel):
    """One server-sent event in a streaming response."""

    content: str = Field(default="", description="Token text for this chunk.")
    done: bool = Field(default=False, description="True on the final chunk.")
    error: str | None = Field(default=None, description="Set when the stream terminated abnormally.")
    citations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Sent only on the final chunk, once retrieval has settled.",
    )


class SessionCreate(BaseModel):
    """Request body for opening a session."""

    title: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    """A conversation session as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    message_count: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "Message",
    "SessionCreate",
    "SessionResponse",
    "StreamChunk",
]
