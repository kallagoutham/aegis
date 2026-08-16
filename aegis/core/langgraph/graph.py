"""The Aegis incident response workflow.

The original template ran a two-node loop: ``chat`` calls the model, ``tool_call``
runs whatever it asked for, repeat. That is a generic assistant. Incident
response has structure worth encoding in the graph itself:

    triage -> gather -> investigate <-> tools -> synthesize

**triage**
    Classify the request and, critically, *generate several search phrasings*.
    The user's wording ("checkout is broken") frequently shares no vocabulary
    with the runbook that answers them ("payment gateway upstream timeout"), so
    searching only the raw query systematically under-retrieves.

**gather**
    Retrieve knowledge base context, analyse any supplied logs, and pull prior
    incidents - concurrently. This is unconditional rather than left to the
    model's discretion: an agent that has to *decide* to look things up
    frequently decides not to, and then answers from parametric memory. Doing
    it up front means the model never sees an empty context.

**investigate**
    The reasoning loop, now starting from evidence rather than from nothing. The
    model may call tools to dig further, bounded by ``AGENT_MAX_TOOL_ITERATIONS``.

**synthesize**
    Force the accumulated reasoning into the structured report schema. Separated
    from investigation because the two need different things: investigation
    wants freedom to explore, synthesis wants rigid, validated output.

Chat mode short-circuits after ``investigate``, since a conversational follow-up
does not need a full report.

Two failure principles run throughout: every node degrades rather than raises
(a missing log analysis produces a report that says so, not a 500), and the loop
is hard-bounded (a model that keeps calling tools eventually gets forced into
synthesis rather than looping until the request times out).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import time
from typing import (
    Any,
    Literal,
)
import uuid

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    convert_to_openai_messages,
    trim_messages,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import (
    END,
    START,
    StateGraph,
)
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from aegis.analysis import analyse_logs
from aegis.core.config import settings
from aegis.core.exceptions import AgentError
from aegis.core.langgraph.tools import (
    RETRIEVAL_TOOLS,
    drain_citations,
    reset_citations,
    tools,
    tools_by_name,
)
from aegis.core.logging import logger
from aegis.core.metrics import (
    agent_confidence,
    agent_investigations_total,
    agent_iterations,
    agent_node_duration_seconds,
    agent_tool_calls_total,
    agent_tool_duration_seconds,
)
from aegis.core.prompts import (
    load_synthesis_prompt,
    load_system_prompt,
    load_triage_prompt,
)
from aegis.models.incident import Severity
from aegis.retrieval.hybrid import (
    HybridRetriever,
    RetrievalRequest,
)
from aegis.schemas.graph import GraphState
from aegis.schemas.incident import InvestigationReportSchema
from aegis.services.database import (
    IncidentRepository,
    session_scope,
)
from aegis.services.llm import (
    extract_text,
    get_llm_service,
)


class IncidentResponseAgent:
    """Owns the compiled LangGraph workflow and its shared resources.

    A single instance is shared across requests. It holds no per-request state -
    everything an investigation needs travels in :class:`GraphState`, which is
    also what makes checkpoint-based resumption work.
    """

    def __init__(self) -> None:
        """Initialise the agent. The graph itself is built lazily on first use."""
        self._graph: CompiledStateGraph | None = None
        self._pool: AsyncConnectionPool | None = None
        self._memory: Any = None
        self._build_lock = asyncio.Lock()
        logger.info(
            "agent_initialized",
            model=settings.DEFAULT_LLM_MODEL,
            tools=len(tools),
            max_iterations=settings.AGENT_MAX_TOOL_ITERATIONS,
        )

    # ==================================================================
    # Infrastructure
    # ==================================================================

    async def _get_pool(self) -> AsyncConnectionPool | None:
        """Open the psycopg pool backing LangGraph checkpointing.

        Separate from the SQLAlchemy pool because ``AsyncPostgresSaver``
        requires psycopg specifically, and needs ``autocommit`` with prepared
        statements disabled - settings that would be wrong for ORM traffic.

        Returns:
            The pool, or ``None`` if it could not be opened. A ``None`` pool
            degrades the agent to stateless operation rather than failing it.
        """
        if self._pool is not None:
            return self._pool

        try:
            self._pool = AsyncConnectionPool(
                settings.postgres_dsn,
                open=False,
                min_size=1,
                max_size=settings.POSTGRES_POOL_SIZE,
                kwargs={
                    "autocommit": True,
                    "connect_timeout": 10,
                    # Prepared statements break through connection poolers such
                    # as PgBouncer in transaction mode, where a statement
                    # prepared on one backend is invisible to the next.
                    "prepare_threshold": None,
                },
            )
            await self._pool.open(wait=True, timeout=15)
            logger.info("checkpoint_pool_opened", max_size=settings.POSTGRES_POOL_SIZE)
            return self._pool
        except Exception as exc:
            logger.error("checkpoint_pool_failed", error=str(exc))
            self._pool = None
            return None

    async def _get_memory(self) -> Any:
        """Initialise mem0 long-term memory, if enabled.

        Returns ``None`` when disabled or unavailable. Long-term memory is a
        personalisation nicety; losing it must never block an investigation.
        """
        if not settings.LONG_TERM_MEMORY_ENABLED:
            return None
        if self._memory is not None:
            return self._memory

        try:
            from mem0 import AsyncMemory

            self._memory = await AsyncMemory.from_config(
                config_dict={
                    "vector_store": {
                        "provider": "pgvector",
                        "config": {
                            "collection_name": settings.LONG_TERM_MEMORY_COLLECTION_NAME,
                            "dbname": settings.POSTGRES_DB,
                            "user": settings.POSTGRES_USER,
                            "password": settings.POSTGRES_PASSWORD.get_secret_value(),
                            "host": settings.POSTGRES_HOST,
                            "port": settings.POSTGRES_PORT,
                        },
                    },
                    "llm": {"provider": "openai", "config": {"model": settings.LONG_TERM_MEMORY_MODEL}},
                    "embedder": {
                        "provider": "openai",
                        "config": {"model": settings.LONG_TERM_MEMORY_EMBEDDER_MODEL},
                    },
                }
            )
            return self._memory
        except Exception as exc:
            logger.warning("long_term_memory_unavailable", error=str(exc))
            return None

    async def _recall_memory(self, user_id: uuid.UUID | None, query: str) -> str:
        """Retrieve user-specific facts relevant to the query."""
        if user_id is None:
            return ""
        memory = await self._get_memory()
        if memory is None:
            return ""
        try:
            results = await memory.search(user_id=str(user_id), query=query)
            entries = results.get("results", []) if isinstance(results, dict) else []
            return "\n".join(f"- {entry['memory']}" for entry in entries if entry.get("memory"))
        except Exception as exc:
            logger.warning("memory_recall_failed", error=str(exc))
            return ""

    async def _persist_memory(self, user_id: uuid.UUID | None, messages: list[dict[str, Any]]) -> None:
        """Write conversation facts to long-term memory. Best effort."""
        if user_id is None:
            return
        memory = await self._get_memory()
        if memory is None:
            return
        try:
            await memory.add(messages, user_id=str(user_id))
        except Exception as exc:
            logger.warning("memory_persist_failed", error=str(exc))

    # ==================================================================
    # Nodes
    # ==================================================================

    async def _triage(self, state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        """Classify the request and generate search query variants.

        Falls back to using the raw query as the only search term if
        classification fails. Degraded retrieval beats no investigation.
        """
        started = time.perf_counter()
        try:
            signals: list[str] = []
            if state.service:
                signals.append(f"User specified service: {state.service}")
            if state.severity:
                signals.append(f"User specified severity: {state.severity.value}")
            if state.log_summary:
                signals.append(f"Logs were supplied. Summary:\n{state.log_summary[:2000]}")

            llm_service = await get_llm_service()
            classification = await llm_service.complete_json(
                load_triage_prompt(
                    query=state.query,
                    signals="\n".join(signals) if signals else "None supplied.",
                ),
                max_tokens=1200,
            )

            queries = [
                str(item)
                for item in classification.get("search_queries", [])
                if isinstance(item, str) and item.strip()
            ][:5]
            if not queries:
                queries = [state.query]

            # A service named by the user is authoritative; the model's guess is
            # only a fallback, because a wrong service filter silently hides the
            # correct runbook.
            inferred_service = classification.get("service")
            service = state.service or (
                str(inferred_service) if isinstance(inferred_service, str) and inferred_service else None
            )

            severity = state.severity
            if severity is None:
                try:
                    severity = Severity(str(classification.get("severity", "")).lower())
                except ValueError:
                    severity = None

            logger.info(
                "triage_completed",
                intent=classification.get("intent"),
                service=service,
                severity=severity.value if severity else None,
                query_variants=len(queries),
            )

            return {
                "triage": classification,
                "search_queries": queries,
                "service": service,
                "severity": severity,
            }

        except Exception as exc:
            logger.warning("triage_failed_using_fallback", error=str(exc))
            return {
                "triage": {"intent": "investigate", "error": str(exc)},
                "search_queries": [state.query],
                "errors": [*state.errors, f"Triage failed: {exc}"],
            }
        finally:
            agent_node_duration_seconds.labels(node="triage").observe(time.perf_counter() - started)

    async def _gather(self, state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        """Collect evidence: knowledge base, logs, and prior incidents.

        All three run concurrently. They are independent, and during an incident
        the difference between 1.2s and 3.5s of setup latency is worth the small
        added complexity.
        """
        started = time.perf_counter()

        async def retrieve() -> tuple[str, list[dict[str, Any]]]:
            """Run every triage query variant and merge the results."""
            queries = state.search_queries or [state.query]
            try:
                async with session_scope() as session:
                    retriever = HybridRetriever(session)
                    responses = await asyncio.gather(
                        *(
                            retriever.search(
                                RetrievalRequest(
                                    query=query,
                                    service=state.service,
                                    top_k=settings.RETRIEVAL_TOP_K,
                                )
                            )
                            for query in queries
                        ),
                        return_exceptions=True,
                    )

                # Merge across query variants, keeping each chunk's best score.
                merged: dict[str, Any] = {}
                for response in responses:
                    if isinstance(response, BaseException):
                        logger.warning("retrieval_variant_failed", error=str(response))
                        continue
                    for result in response.results:
                        key = str(result.chunk_id)
                        if key not in merged or result.score > merged[key].score:
                            merged[key] = result

                if not merged:
                    return "", []

                ranked = sorted(merged.values(), key=lambda item: item.score, reverse=True)
                ranked = ranked[: settings.RETRIEVAL_TOP_K * 2]

                blocks = [result.to_context_block(index) for index, result in enumerate(ranked, start=1)]
                return "\n\n---\n\n".join(blocks), [result.to_dict() for result in ranked]

            except Exception as exc:
                logger.error("gather_retrieval_failed", error=str(exc), exc_info=True)
                return "", []

        async def prior_incidents() -> list[dict[str, Any]]:
            """Fetch resolved incidents with confirmed root causes."""
            try:
                async with session_scope() as session:
                    repository = IncidentRepository(session)
                    incidents = await repository.resolved_incidents_for_context(state.service, limit=5)
                    return [
                        {
                            "title": incident.title,
                            "severity": incident.severity.value,
                            "service": incident.service,
                            "root_cause": incident.root_cause,
                            "resolution": incident.resolution,
                            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
                        }
                        for incident in incidents
                    ]
            except Exception as exc:
                logger.warning("gather_prior_incidents_failed", error=str(exc))
                return []

        async def memory() -> str:
            return await self._recall_memory(state.user_id, state.query)

        (context, citations), incidents, recalled = await asyncio.gather(retrieve(), prior_incidents(), memory())

        logger.info(
            "gather_completed",
            context_chars=len(context),
            citations=len(citations),
            prior_incidents=len(incidents),
            has_logs=bool(state.log_summary),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        agent_node_duration_seconds.labels(node="gather").observe(time.perf_counter() - started)

        return {
            "retrieved_context": context,
            "citations": citations,
            "past_incidents": incidents,
            "long_term_memory": recalled,
        }

    def _build_context_block(self, state: GraphState) -> str:
        """Assemble the evidence sections injected into the system prompt."""
        sections: list[str] = []

        if state.retrieved_context:
            sections.append(f"### Knowledge base\n\n{state.retrieved_context}")
        else:
            sections.append(
                "### Knowledge base\n\nNo relevant documentation was found. Do not invent runbooks; "
                "state plainly that this failure mode is undocumented."
            )

        if state.log_summary:
            sections.append(f"### Supplied logs\n\n{state.log_summary}")

        if state.past_incidents:
            lines = ["### Previously resolved incidents", ""]
            for incident in state.past_incidents:
                lines.append(
                    f"- **{incident['title']}** ({incident['severity']}, {incident.get('service') or 'unspecified'}): "
                    f"{incident.get('root_cause')}"
                )
            sections.append("\n".join(lines))

        return "\n\n".join(sections)

    def _prepare_messages(self, state: GraphState) -> list[BaseMessage]:
        """Build the message list for a model call, trimmed to the token budget.

        The system prompt is rebuilt each turn rather than persisted in history
        so freshly gathered evidence is always present, and is excluded from
        trimming so it can never be dropped - a trimmed-away system prompt
        turns a grounded incident responder into a generic chatbot mid-
        conversation.
        """
        system = SystemMessage(
            content=load_system_prompt(
                context_block=self._build_context_block(state),
                long_term_memory=state.long_term_memory or "Nothing recorded yet.",
            )
        )

        history = [message for message in state.messages if not isinstance(message, SystemMessage)]

        try:
            history = trim_messages(
                history,
                strategy="last",
                token_counter=len,  # message count; cheap and monotonic
                max_tokens=60,
                start_on="human",
                include_system=False,
                allow_partial=False,
            )
        except Exception as exc:
            # Token counting can fail on unusual content block shapes. Trimming
            # is an optimisation, so proceed untrimmed rather than failing.
            logger.debug("message_trim_skipped", error=str(exc))

        return [system, *history]

    async def _investigate(self, state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        """Run the reasoning turn, optionally requesting tools."""
        started = time.perf_counter()
        try:
            llm_service = await get_llm_service()
            response = await llm_service.invoke(
                self._prepare_messages(state),
                tools=tools,
            )
            return {"messages": [response], "iterations": state.iterations + 1}

        except Exception as exc:
            logger.error("investigate_node_failed", error=str(exc), exc_info=True)
            # Return a message rather than raising, so synthesis still runs and
            # the user receives a report describing the failure.
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "I was unable to complete the reasoning step because the language model "
                            f"was unavailable ({exc}). The evidence gathered so far is still below."
                        )
                    )
                ],
                "iterations": state.iterations + 1,
                "errors": [*state.errors, f"Investigation step failed: {exc}"],
            }
        finally:
            agent_node_duration_seconds.labels(node="investigate").observe(time.perf_counter() - started)

    async def _run_tools(self, state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        """Execute every tool the last message requested, concurrently.

        Tool failures become ``ToolMessage`` content rather than exceptions, so
        the model can read what went wrong and adapt.
        """
        started = time.perf_counter()
        last = state.messages[-1]
        requested = getattr(last, "tool_calls", None) or []

        async def execute(call: dict[str, Any]) -> ToolMessage:
            name = call.get("name", "")
            call_id = call.get("id", "")
            arguments = call.get("args", {}) or {}
            tool_started = time.perf_counter()

            implementation = tools_by_name.get(name)
            if implementation is None:
                agent_tool_calls_total.labels(tool=name or "unknown", outcome="unknown_tool").inc()
                return ToolMessage(
                    content=f"Unknown tool '{name}'. Available tools: {', '.join(sorted(tools_by_name))}.",
                    name=name or "unknown",
                    tool_call_id=call_id,
                )

            try:
                result = await implementation.ainvoke(arguments)
                agent_tool_calls_total.labels(tool=name, outcome="success").inc()
                return ToolMessage(content=str(result), name=name, tool_call_id=call_id)
            except Exception as exc:
                agent_tool_calls_total.labels(tool=name, outcome="error").inc()
                logger.error("tool_execution_failed", tool=name, error=str(exc), exc_info=True)
                return ToolMessage(
                    content=f"Tool '{name}' failed: {exc}. Continue with the evidence you have.",
                    name=name,
                    tool_call_id=call_id,
                )
            finally:
                agent_tool_duration_seconds.labels(tool=name or "unknown").observe(time.perf_counter() - tool_started)

        results = await asyncio.gather(*(execute(call) for call in requested))

        trace = [
            {
                "tool": call.get("name", ""),
                "args": call.get("args", {}),
                "result_preview": str(message.content)[:500],
            }
            for call, message in zip(requested, results, strict=True)
        ]

        # Retrieval tools accumulate citations; fold them into state so they
        # survive into the final report.
        new_citations = drain_citations() if any(call.get("name") in RETRIEVAL_TOOLS for call in requested) else []

        agent_node_duration_seconds.labels(node="tools").observe(time.perf_counter() - started)

        return {
            "messages": list(results),
            "tool_calls": [*state.tool_calls, *trace],
            "citations": [*state.citations, *new_citations],
        }

    async def _synthesize(self, state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        """Force the investigation into the structured report schema."""
        started = time.perf_counter()
        try:
            # Only the assistant's reasoning and tool findings matter here; the
            # raw evidence is passed separately in its own prompt sections.
            notes: list[str] = []
            for message in state.messages:
                if isinstance(message, AIMessage) and message.content:
                    notes.append(f"Assistant: {extract_text(message)}")
                elif isinstance(message, ToolMessage):
                    notes.append(f"Tool [{message.name}]: {str(message.content)[:1500]}")

            past = (
                "\n".join(
                    f"- {incident['title']} ({incident['severity']}): {incident.get('root_cause')}"
                    for incident in state.past_incidents
                )
                or "None available."
            )

            llm_service = await get_llm_service()
            raw = await llm_service.complete_json(
                load_synthesis_prompt(
                    query=state.query,
                    severity=state.severity.value if state.severity else "not specified",
                    retrieved_context=state.retrieved_context or "No documentation was retrieved.",
                    log_summary=state.log_summary or "No logs were supplied.",
                    past_incidents=past,
                    investigation_notes="\n\n".join(notes[-30:]) or "No investigation notes.",
                ),
                max_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            )

            report = InvestigationReportSchema.model_validate(raw)

            # Attach retrieval provenance from state rather than trusting the
            # model to reproduce citation metadata accurately.
            report.citations = [
                {
                    "chunk_id": citation.get("chunk_id", ""),
                    "title": citation.get("title", ""),
                    "heading_path": citation.get("heading_path", ""),
                    "source_uri": citation.get("source_uri", ""),
                    "source_type": citation.get("source_type", ""),
                    "relevance": citation.get("score", 0.0),
                }
                for citation in state.citations[:30]
            ]  # type: ignore[assignment]

            agent_confidence.observe(report.confidence)
            agent_investigations_total.labels(outcome="completed").inc()

            logger.info(
                "synthesis_completed",
                hypotheses=len(report.hypotheses),
                evidence=len(report.evidence),
                remediation_steps=len(report.remediation_steps),
                confidence=report.confidence,
                needs_review=report.needs_human_review,
            )

            return {"report": report.model_dump(mode="json")}

        except Exception as exc:
            logger.error("synthesis_failed", error=str(exc), exc_info=True)
            agent_investigations_total.labels(outcome="failed").inc()

            # Emit a valid minimal report rather than nothing. A responder mid-
            # incident needs the gathered evidence even when structuring failed.
            fallback = InvestigationReportSchema(
                summary=(
                    "The investigation could not be structured into a report "
                    f"({exc}). The evidence gathered is available in the conversation history."
                ),
                confidence=0.0,
                open_questions=["Report synthesis failed; review the raw investigation transcript."],
            )
            return {
                "report": fallback.model_dump(mode="json"),
                "errors": [*state.errors, f"Synthesis failed: {exc}"],
            }
        finally:
            agent_node_duration_seconds.labels(node="synthesize").observe(time.perf_counter() - started)

    # ==================================================================
    # Routing
    # ==================================================================

    def _route_after_investigate(self, state: GraphState) -> Literal["tools", "synthesize", "__end__"]:
        """Decide what follows a reasoning turn.

        Three-way: run requested tools, stop (chat mode), or synthesize.

        The iteration cap is checked *before* honouring a tool request. Without
        it, a model that keeps calling tools would loop until the HTTP request
        times out - producing no answer at all after burning the full budget.
        """
        last = state.messages[-1] if state.messages else None
        wants_tools = bool(getattr(last, "tool_calls", None))

        if wants_tools and state.iterations < settings.AGENT_MAX_TOOL_ITERATIONS:
            return "tools"

        if wants_tools:
            logger.warning(
                "agent_iteration_cap_reached",
                iterations=state.iterations,
                cap=settings.AGENT_MAX_TOOL_ITERATIONS,
            )
            agent_investigations_total.labels(outcome="truncated").inc()

        agent_iterations.observe(state.iterations)

        # Chat mode wants a conversational reply, not a formal report.
        if state.triage.get("mode") == "chat":
            return "__end__"

        return "synthesize"

    # ==================================================================
    # Graph construction
    # ==================================================================

    async def build(self) -> CompiledStateGraph:
        """Compile the workflow, reusing the compiled instance thereafter."""
        if self._graph is not None:
            return self._graph

        async with self._build_lock:
            if self._graph is not None:
                return self._graph

            builder = StateGraph(GraphState)
            builder.add_node("triage", self._triage)
            builder.add_node("gather", self._gather)
            builder.add_node("investigate", self._investigate)
            builder.add_node("tools", self._run_tools)
            builder.add_node("synthesize", self._synthesize)

            builder.add_edge(START, "triage")
            builder.add_edge("triage", "gather")
            builder.add_edge("gather", "investigate")
            builder.add_conditional_edges(
                "investigate",
                self._route_after_investigate,
                {"tools": "tools", "synthesize": "synthesize", "__end__": END},
            )
            builder.add_edge("tools", "investigate")
            builder.add_edge("synthesize", END)

            checkpointer = None
            pool = await self._get_pool()
            if pool is not None:
                try:
                    checkpointer = AsyncPostgresSaver(pool)
                    await checkpointer.setup()
                except Exception as exc:
                    logger.error("checkpointer_setup_failed", error=str(exc))
                    checkpointer = None

            if checkpointer is None:
                logger.warning(
                    "running_without_checkpointer",
                    impact="conversation history will not persist across requests",
                )

            self._graph = builder.compile(
                checkpointer=checkpointer,
                name=f"{settings.PROJECT_NAME} incident response",
            )
            logger.info("graph_compiled", has_checkpointer=checkpointer is not None)
            return self._graph

    def _config(self, session_id: uuid.UUID, user_id: uuid.UUID | None) -> RunnableConfig:
        """Build the runnable config, including tracing callbacks when enabled."""
        callbacks: list[Any] = []
        if settings.LANGFUSE_ENABLED:
            try:
                from langfuse.langchain import CallbackHandler

                callbacks.append(CallbackHandler())
            except Exception as exc:
                logger.warning("langfuse_callback_unavailable", error=str(exc))

        return {
            "configurable": {"thread_id": str(session_id)},
            "callbacks": callbacks,
            "recursion_limit": settings.AGENT_MAX_TOOL_ITERATIONS * 3 + 10,
            "metadata": {
                "user_id": str(user_id) if user_id else None,
                "session_id": str(session_id),
                "environment": settings.ENVIRONMENT.value,
            },
        }

    def _initial_state(
        self,
        query: str,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        logs: str | None,
        service: str | None,
        severity: Severity | None,
        incident_id: uuid.UUID | None,
        mode: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Build the graph input, analysing logs up front if supplied."""
        log_analysis: dict[str, Any] = {}
        log_summary = ""

        if logs:
            try:
                analysis = analyse_logs(logs)
                log_analysis = analysis.to_dict()
                log_summary = analysis.to_prompt_summary()
            except Exception as exc:
                logger.warning("initial_log_analysis_failed", error=str(exc))

        messages: list[BaseMessage] = []
        for entry in history or []:
            role, content = entry.get("role"), entry.get("content", "")
            if not content:
                continue
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
        messages.append(HumanMessage(content=query))

        return {
            "messages": messages,
            "query": query,
            "service": service,
            "severity": severity,
            "incident_id": incident_id,
            "user_id": user_id,
            "log_analysis": log_analysis,
            "log_summary": log_summary,
            "triage": {"mode": mode},
            "iterations": 0,
        }

    # ==================================================================
    # Public API
    # ==================================================================

    async def investigate(
        self,
        query: str,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        logs: str | None = None,
        service: str | None = None,
        severity: Severity | None = None,
        incident_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Run a full investigation and return a structured report.

        Args:
            query: The question or symptom description.
            session_id: Conversation thread, also the checkpoint key.
            user_id: For long-term memory and audit.
            logs: Optional raw logs to analyse.
            service: Optional service filter for retrieval.
            severity: Optional severity hint.
            incident_id: Optional incident to attach the report to.

        Returns:
            A dict with ``report``, ``citations``, ``tool_calls``, ``errors``,
            and ``duration_ms``.

        Raises:
            AgentError: If the graph itself failed to execute.
        """
        started = time.perf_counter()
        reset_citations()
        graph = await self.build()

        try:
            final = await graph.ainvoke(
                self._initial_state(
                    query,
                    session_id=session_id,
                    user_id=user_id,
                    logs=logs,
                    service=service,
                    severity=severity,
                    incident_id=incident_id,
                    mode="investigate",
                ),
                config=self._config(session_id, user_id),
            )
        except Exception as exc:
            agent_investigations_total.labels(outcome="failed").inc()
            logger.error("investigation_failed", error=str(exc), exc_info=True)
            raise AgentError(
                "The investigation workflow failed to complete.",
                context={"error": str(exc), "session_id": str(session_id)},
            ) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)

        # Persist conversation facts without making the caller wait. Fire-and-
        # forget is safe because failures are logged and nothing depends on it.
        if user_id is not None and settings.LONG_TERM_MEMORY_ENABLED:
            task = asyncio.create_task(
                self._persist_memory(user_id, convert_to_openai_messages(final.get("messages", [])))
            )
            # Hold a reference so the task is not garbage collected mid-flight.
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        logger.info(
            "investigation_completed",
            session_id=str(session_id),
            duration_ms=duration_ms,
            iterations=final.get("iterations", 0),
            tool_calls=len(final.get("tool_calls", [])),
            errors=len(final.get("errors", [])),
        )

        return {
            "report": final.get("report", {}),
            "citations": final.get("citations", []),
            "tool_calls": final.get("tool_calls", []),
            "errors": final.get("errors", []),
            "duration_ms": duration_ms,
            "model": settings.DEFAULT_LLM_MODEL,
        }

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        service: str | None = None,
    ) -> dict[str, Any]:
        """Run a conversational turn without producing a formal report."""
        reset_citations()
        graph = await self.build()

        query = messages[-1]["content"]
        history = messages[:-1]

        final = await graph.ainvoke(
            self._initial_state(
                query,
                session_id=session_id,
                user_id=user_id,
                logs=None,
                service=service,
                severity=None,
                incident_id=None,
                mode="chat",
                history=history,
            ),
            config=self._config(session_id, user_id),
        )

        reply = ""
        for message in reversed(final.get("messages", [])):
            if isinstance(message, AIMessage) and message.content:
                reply = extract_text(message)
                break

        return {
            "reply": reply,
            "citations": final.get("citations", []),
            "messages": self._public_messages(final.get("messages", [])),
        }

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        service: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream a conversational reply token by token.

        Only content from the ``investigate`` node is yielded. Streaming triage
        and synthesis would emit raw JSON into the user's chat window.
        """
        reset_citations()
        graph = await self.build()

        query = messages[-1]["content"]
        history = messages[:-1]

        async for chunk, metadata in graph.astream(
            self._initial_state(
                query,
                session_id=session_id,
                user_id=user_id,
                logs=None,
                service=service,
                severity=None,
                incident_id=None,
                mode="chat",
                history=history,
            ),
            config=self._config(session_id, user_id),
            stream_mode="messages",
        ):
            if metadata.get("langgraph_node") != "investigate":
                continue
            text = extract_text(chunk) if isinstance(chunk, BaseMessage) else str(chunk)
            if text:
                yield text

    async def history(self, session_id: uuid.UUID) -> list[dict[str, str]]:
        """Return the stored conversation for a session."""
        graph = await self.build()
        try:
            snapshot = await graph.aget_state(config={"configurable": {"thread_id": str(session_id)}})
        except Exception as exc:
            logger.warning("history_read_failed", session_id=str(session_id), error=str(exc))
            return []

        if not snapshot or not snapshot.values:
            return []
        return self._public_messages(snapshot.values.get("messages", []))

    @staticmethod
    def _public_messages(messages: list[BaseMessage]) -> list[dict[str, str]]:
        """Filter internal traffic out of a message list.

        Tool calls and tool results are implementation detail. Surfacing them in
        a chat transcript is noise; they remain available in ``tool_calls`` for
        debugging.
        """
        public: list[dict[str, str]] = []
        for message in convert_to_openai_messages(messages):
            role, content = message.get("role"), message.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                public.append({"role": role, "content": content})
        return public

    async def clear_history(self, session_id: uuid.UUID) -> None:
        """Delete every checkpoint row for a session.

        Raises:
            AgentError: If the checkpoint store is unavailable.
        """
        pool = await self._get_pool()
        if pool is None:
            raise AgentError("Checkpoint store is unavailable; cannot clear history.")

        async with pool.connection() as connection:
            for table in settings.CHECKPOINT_TABLES:
                # Table names come from settings, never from user input, so
                # interpolating them here cannot be an injection vector.
                await connection.execute(f"DELETE FROM {table} WHERE thread_id = %s", (str(session_id),))

        logger.info("session_history_cleared", session_id=str(session_id))

    async def aclose(self) -> None:
        """Release the checkpoint connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("checkpoint_pool_closed")


# Strong references to fire-and-forget tasks. Without this, asyncio may garbage
# collect a running task mid-execution, cancelling it silently.
_background_tasks: set[asyncio.Task[Any]] = set()

_agent: IncidentResponseAgent | None = None


def get_agent() -> IncidentResponseAgent:
    """Return the shared agent instance."""
    global _agent
    if _agent is None:
        _agent = IncidentResponseAgent()
    return _agent


__all__ = ["IncidentResponseAgent", "get_agent"]
