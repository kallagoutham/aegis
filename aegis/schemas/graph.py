"""LangGraph state definition.

``GraphState`` is the single object threaded through every node. LangGraph
merges each node's returned partial state into it, so field types determine
merge semantics: ``messages`` uses the ``add_messages`` reducer (append and
deduplicate by id), while plain fields are overwritten by whichever node wrote
last.

Everything the agent learns during an investigation lives here rather than in
node-local variables, because the state is what gets checkpointed. A field kept
outside it disappears when the process restarts mid-investigation, and
resuming from the checkpoint would silently lose the log analysis or the
retrieved context.
"""

from __future__ import annotations

from typing import (
    Annotated,
    Any,
)
import uuid

from langgraph.graph.message import add_messages
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from aegis.models.incident import Severity


class GraphState(BaseModel):
    """State carried through the incident response workflow."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- Conversation -------------------------------------------------
    messages: Annotated[list, add_messages] = Field(
        default_factory=list,
        description="Conversation history. The add_messages reducer appends rather than replaces.",
    )

    # --- Request context ----------------------------------------------
    query: str = Field(default="", description="The originating question or symptom description.")
    service: str | None = Field(default=None, description="Service filter applied to retrieval.")
    severity: Severity | None = Field(default=None, description="Severity hint, if supplied.")
    incident_id: uuid.UUID | None = Field(default=None)
    user_id: uuid.UUID | None = Field(default=None)

    # --- Triage output -------------------------------------------------
    triage: dict[str, Any] = Field(
        default_factory=dict,
        description="Classification from the triage node: intent, inferred service, search terms.",
    )
    search_queries: list[str] = Field(
        default_factory=list,
        description="Query variants generated during triage. Searching several phrasings "
        "materially improves recall over using the raw user text alone.",
    )

    # --- Evidence gathered ---------------------------------------------
    retrieved_context: str = Field(default="", description="Formatted knowledge base passages.")
    citations: list[dict[str, Any]] = Field(default_factory=list, description="Structured provenance for retrieval.")
    log_analysis: dict[str, Any] = Field(
        default_factory=dict, description="Structured log analysis, if logs supplied."
    )
    log_summary: str = Field(default="", description="Prompt-ready rendering of the log analysis.")
    past_incidents: list[dict[str, Any]] = Field(default_factory=list, description="Similar resolved incidents.")
    long_term_memory: str = Field(default="", description="Recalled user-specific facts from mem0.")

    # --- Control flow ---------------------------------------------------
    iterations: int = Field(default=0, description="Investigate->tool loops completed. Bounds the loop.")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, description="Trace of tool invocations.")
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal failures. Recorded rather than raised so a degraded "
        "investigation still returns something, with its gaps stated.",
    )

    # --- Output ----------------------------------------------------------
    report: dict[str, Any] = Field(default_factory=dict, description="The structured report from synthesis.")

    def add_error(self, message: str) -> None:
        """Record a non-fatal failure."""
        self.errors.append(message)

    def has_evidence(self) -> bool:
        """Whether anything was gathered worth synthesising a report from."""
        return bool(self.retrieved_context or self.log_summary or self.past_incidents)


__all__ = ["GraphState"]
