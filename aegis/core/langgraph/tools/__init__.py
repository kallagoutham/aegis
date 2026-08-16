"""Agent tool registry.

Tool *order* is a real prompt-engineering lever, not cosmetic. Models are biased
toward tools listed earlier when several look applicable, so the registry is
ordered by how much we want each one reached for: grounded internal knowledge
first, log analysis next, public web search last.

Tool *count* matters too. Beyond roughly a dozen tools, selection accuracy
degrades noticeably - the model starts picking plausible-but-wrong tools. The
set here is deliberately small and non-overlapping.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from aegis.core.langgraph.tools.diagnostics import (
    analyze_log_excerpt,
    compute_incident_timeline,
    extract_error_signatures,
    web_search,
)
from aegis.core.langgraph.tools.knowledge import (
    drain_citations,
    find_similar_incidents,
    list_documented_services,
    reset_citations,
    search_knowledge_base,
    search_postmortems,
    search_runbooks,
)

# Ordered by preferred reach. Grounded sources first, public web last.
tools: list[BaseTool] = [
    search_runbooks,
    search_postmortems,
    find_similar_incidents,
    search_knowledge_base,
    analyze_log_excerpt,
    extract_error_signatures,
    compute_incident_timeline,
    list_documented_services,
    web_search,
]

tools_by_name: dict[str, BaseTool] = {tool.name: tool for tool in tools}

# Tools whose results carry citations that belong in the final report.
RETRIEVAL_TOOLS = frozenset(
    {
        search_runbooks.name,
        search_postmortems.name,
        search_knowledge_base.name,
    }
)

__all__ = [
    "RETRIEVAL_TOOLS",
    "analyze_log_excerpt",
    "compute_incident_timeline",
    "drain_citations",
    "extract_error_signatures",
    "find_similar_incidents",
    "list_documented_services",
    "reset_citations",
    "search_knowledge_base",
    "search_postmortems",
    "search_runbooks",
    "tools",
    "tools_by_name",
    "web_search",
]
