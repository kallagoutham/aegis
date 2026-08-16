"""Prompt loading and rendering.

Prompts live in markdown files rather than Python string literals so they can be
diffed, reviewed, and edited without touching code - prompt changes are the most
frequent change in an LLM system and should not require a code review of
escaping.

**Placeholders use ``{{name}}``, not ``{name}``.** The previous implementation
called ``str.format()`` on the prompt body, which breaks the moment a prompt
contains a JSON example - and every prompt that specifies structured output
contains one. ``str.format`` reads ``{"summary": ...}`` as a format field named
``"summary"`` and raises ``KeyError``. Double-brace substitution has no such
collision, so prompts can show the exact JSON they expect back.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from aegis.core.config import settings

PROMPT_DIR = Path(__file__).parent

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


@lru_cache(maxsize=32)
def _read_prompt(name: str) -> str:
    """Read and cache a prompt file.

    Args:
        name: Filename stem, e.g. ``"system"``.

    Returns:
        The raw prompt text.

    Raises:
        FileNotFoundError: If the prompt does not exist.
    """
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        available = sorted(entry.stem for entry in PROMPT_DIR.glob("*.md"))
        raise FileNotFoundError(f"Prompt '{name}' not found in {PROMPT_DIR}. Available: {available}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **values: Any) -> str:
    """Load a prompt and substitute its placeholders.

    Unsupplied placeholders render as an empty string rather than raising. That
    is deliberate: an optional context block (say, log analysis when no logs were
    provided) should simply be absent, not crash the request.

    Args:
        name: Prompt filename stem.
        **values: Placeholder values.

    Returns:
        The rendered prompt.
    """
    template = _read_prompt(name)
    resolved = {**_default_values(), **{key: value for key, value in values.items() if value is not None}}

    def substitute(match: re.Match[str]) -> str:
        return str(resolved.get(match.group(1), ""))

    return _PLACEHOLDER_RE.sub(substitute, template)


def _default_values() -> dict[str, Any]:
    """Placeholder values available to every prompt."""
    from datetime import (
        UTC,
        datetime,
    )

    return {
        "agent_name": f"{settings.PROJECT_NAME} Agent",
        "project_name": settings.PROJECT_NAME,
        # Models have no clock. Without an explicit timestamp they compute
        # relative times ("30 minutes ago") against their training cutoff, which
        # produces confidently wrong incident timelines.
        "current_datetime": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def load_system_prompt(**values: Any) -> str:
    """Render the main incident response system prompt."""
    return render_prompt("system", **values)


def load_triage_prompt(**values: Any) -> str:
    """Render the triage classification prompt."""
    return render_prompt("triage", **values)


def load_synthesis_prompt(**values: Any) -> str:
    """Render the report synthesis prompt."""
    return render_prompt("synthesis", **values)


def clear_prompt_cache() -> None:
    """Drop cached prompt bodies so edits are picked up without a restart."""
    _read_prompt.cache_clear()


__all__ = [
    "PROMPT_DIR",
    "clear_prompt_cache",
    "load_synthesis_prompt",
    "load_system_prompt",
    "load_triage_prompt",
    "render_prompt",
]
