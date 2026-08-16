"""Domain exception hierarchy for Aegis.

Application code raises these instead of ``HTTPException`` so that business
logic stays transport-agnostic and independently testable. A single set of
handlers registered in :mod:`aegis.main` translates them into RFC 9457
``application/problem+json`` responses.

Design rules:

* Every exception carries a stable machine-readable ``code`` that clients may
  branch on. Human-facing wording can change freely; codes cannot.
* ``detail`` is safe to show a user. Anything sensitive belongs in ``context``,
  which is logged but never serialised into the response body.
* ``status_code`` lives on the class, so the HTTP mapping is declared once and
  cannot drift between call sites.
"""

from __future__ import annotations

from typing import Any


class AegisError(Exception):
    """Base class for every deliberate failure mode in Aegis.

    Attributes:
        status_code: HTTP status the API layer should return.
        code: Stable, machine-readable error identifier.
        detail: Safe-to-expose explanation of what went wrong.
        context: Structured diagnostic data. Logged, never returned to clients.
    """

    status_code: int = 500
    code: str = "internal_error"
    default_detail: str = "An unexpected error occurred."

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Build the error.

        Args:
            detail: Overrides the class-level ``default_detail``.
            code: Overrides the class-level ``code`` for finer-grained cases.
            context: Diagnostic key/values attached to logs only.
        """
        self.detail = detail or self.default_detail
        self.code = code or self.code
        self.context = context or {}
        super().__init__(self.detail)

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        """Render as an RFC 9457 problem document.

        Args:
            instance: Request path that produced the error.

        Returns:
            A JSON-serialisable problem document. ``context`` is intentionally
            excluded so internal detail never leaks to the caller.
        """
        problem: dict[str, Any] = {
            "type": f"https://docs.aegis.dev/errors/{self.code}",
            "title": self.__class__.__name__,
            "status": self.status_code,
            "code": self.code,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        return problem


# ----------------------------------------------------------------------
# 4xx - caller errors
# ----------------------------------------------------------------------


class ValidationError(AegisError):
    """Input failed a domain rule that Pydantic could not express."""

    status_code = 422
    code = "validation_error"
    default_detail = "The request payload failed validation."


class AuthenticationError(AegisError):
    """Credentials are missing, malformed, or expired."""

    status_code = 401
    code = "authentication_failed"
    default_detail = "Invalid or missing authentication credentials."


class AuthorizationError(AegisError):
    """Caller is authenticated but not allowed to touch this resource."""

    status_code = 403
    code = "permission_denied"
    default_detail = "You do not have permission to perform this action."


class ResourceNotFoundError(AegisError):
    """A referenced entity does not exist."""

    status_code = 404
    code = "not_found"
    default_detail = "The requested resource was not found."

    def __init__(self, resource: str, identifier: Any = None, **kwargs: Any) -> None:
        """Build a not-found error naming the resource type and id."""
        detail = f"{resource} not found" + (f": {identifier}" if identifier is not None else "")
        context = {"resource": resource, "identifier": str(identifier) if identifier is not None else None}
        context.update(kwargs.pop("context", {}))
        super().__init__(detail, context=context, **kwargs)


class ConflictError(AegisError):
    """The request collides with existing state (duplicate email, etc.)."""

    status_code = 409
    code = "conflict"
    default_detail = "The request conflicts with the current state of the resource."


class PayloadTooLargeError(AegisError):
    """Upload exceeded ``MAX_UPLOAD_BYTES``."""

    status_code = 413
    code = "payload_too_large"
    default_detail = "The uploaded payload exceeds the configured size limit."


class UnsupportedContentError(AegisError):
    """The ingestion pipeline has no loader for this content type."""

    status_code = 415
    code = "unsupported_content"
    default_detail = "The supplied content type is not supported by the ingestion pipeline."


# ----------------------------------------------------------------------
# 5xx - our failures and upstream dependency failures
# ----------------------------------------------------------------------


class ConfigurationError(AegisError):
    """A required setting is missing or internally inconsistent."""

    status_code = 500
    code = "configuration_error"
    default_detail = "The service is misconfigured."


class DatabaseError(AegisError):
    """A database operation failed for reasons the caller cannot fix."""

    status_code = 503
    code = "database_unavailable"
    default_detail = "The database is currently unavailable."


class LLMError(AegisError):
    """Every model in the fallback chain failed."""

    status_code = 502
    code = "llm_unavailable"
    default_detail = "The language model provider is currently unavailable."


class EmbeddingError(LLMError):
    """Embedding generation failed, so content could not be indexed or searched."""

    code = "embedding_failed"
    default_detail = "Failed to generate embeddings for the supplied content."


class RetrievalError(AegisError):
    """The retrieval layer could not service a search."""

    status_code = 503
    code = "retrieval_failed"
    default_detail = "Knowledge base retrieval failed."


class IngestionError(AegisError):
    """A document could not be ingested."""

    status_code = 500
    code = "ingestion_failed"
    default_detail = "Failed to ingest the supplied document."


class AgentError(AegisError):
    """The LangGraph workflow failed to produce a usable result."""

    status_code = 500
    code = "agent_failed"
    default_detail = "The incident response agent failed to complete the investigation."


class ToolExecutionError(AgentError):
    """A tool invoked by the agent raised.

    Non-fatal by design: the graph feeds the message back to the model as a
    ``ToolMessage`` so it can recover or choose a different approach.
    """

    code = "tool_execution_failed"
    default_detail = "An agent tool failed to execute."

    def __init__(self, tool_name: str, reason: str, **kwargs: Any) -> None:
        """Record which tool failed and why."""
        context = {"tool": tool_name, "reason": reason}
        context.update(kwargs.pop("context", {}))
        super().__init__(f"Tool '{tool_name}' failed: {reason}", context=context, **kwargs)
        self.tool_name = tool_name


__all__ = [
    "AegisError",
    "AgentError",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ConflictError",
    "DatabaseError",
    "EmbeddingError",
    "IngestionError",
    "LLMError",
    "PayloadTooLargeError",
    "ResourceNotFoundError",
    "RetrievalError",
    "ToolExecutionError",
    "UnsupportedContentError",
    "ValidationError",
]
