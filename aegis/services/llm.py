"""LLM access with retries, model fallback, and structured output helpers.

**The bug this rewrite fixes.** The previous implementation kept the active
model in a mutable instance attribute on a module-level singleton, and rotated
it in place on failure. Under concurrency that is a data race with two distinct
symptoms:

* Request A fails on ``gpt-4o`` and rotates the shared attribute. Request B,
  midway through its own call, silently switches models - so B's second turn
  runs on a different model from its first, with different tool-calling
  behaviour and a different token budget.
* ``bind_tools`` mutated the same shared attribute, so whichever caller bound
  last determined the tools every other caller saw.

Both are invisible in single-request testing and reliably wrong under load.

The fix is to make model selection *per call*. Model instances are immutable and
cached by ``(model, tools)``; a call walks a local copy of the fallback chain
and never writes shared state. The service object holds no per-request data at
all, so it is safe to share across the process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
import re
import time
from typing import (
    Any,
)

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from aegis.core.config import settings
from aegis.core.exceptions import LLMError
from aegis.core.logging import logger
from aegis.core.metrics import (
    llm_fallback_total,
    llm_inference_duration_seconds,
    llm_requests_total,
    llm_tokens_total,
)

# Errors worth retrying: transient capacity, network, and timeout conditions.
# Deliberately excludes auth failures and malformed-request errors, where
# retrying only wastes time and money before failing identically.
_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)

# Models whose API rejects `temperature`, exposing a `reasoning_effort` control
# instead. Sending an unsupported parameter is a hard 400, so the distinction
# has to be encoded rather than discovered at runtime.
_REASONING_MODEL_RE = re.compile(r"^(o[1-9]|gpt-5)")

# Strips ```json fences that models add despite instructions to the contrary.
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _is_reasoning_model(model: str) -> bool:
    """Whether a model uses the reasoning-effort parameter family."""
    return bool(_REASONING_MODEL_RE.match(model))


def build_chat_model(model: str, **overrides: Any) -> ChatOpenAI:
    """Construct a chat model configured for this deployment.

    Args:
        model: Provider model identifier.
        **overrides: Parameters overriding the defaults.

    Returns:
        A configured, immutable :class:`ChatOpenAI`.
    """
    params: dict[str, Any] = {
        "model": model,
        "api_key": settings.OPENAI_API_KEY.get_secret_value(),
        "base_url": settings.OPENAI_BASE_URL,
        "timeout": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        # Retries are owned by tenacity below. Letting the SDK retry as well
        # would multiply the attempt count and the worst-case latency.
        "max_retries": 0,
    }

    if _is_reasoning_model(model):
        params["reasoning_effort"] = overrides.pop("reasoning_effort", "medium")
        params["max_completion_tokens"] = overrides.pop("max_tokens", settings.LLM_MAX_OUTPUT_TOKENS)
    else:
        params["temperature"] = overrides.pop("temperature", settings.DEFAULT_LLM_TEMPERATURE)
        params["max_tokens"] = overrides.pop("max_tokens", settings.LLM_MAX_OUTPUT_TOKENS)

    params.update(overrides)
    return ChatOpenAI(**params)


def extract_text(message: BaseMessage) -> str:
    """Flatten a message's content to plain text.

    Reasoning-capable models return content as a list of typed blocks::

        [{"type": "reasoning", ...}, {"type": "text", "text": "the answer"}]

    Callers that expect a string get a stringified list unless this normalises
    first. Reasoning blocks are intentionally dropped - they are the model's
    scratchpad, not part of the answer.

    Args:
        message: A message from the model.

    Returns:
        The concatenated text content.
    """
    content = message.content
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)

    return str(content)


def normalise_message(message: BaseMessage) -> BaseMessage:
    """Rewrite structured content blocks into plain text, in place.

    Applied to graph output so downstream code, serialisation, and checkpoint
    storage all see a consistent string shape.
    """
    if not isinstance(message.content, str):
        message.content = extract_text(message)
    return message


def parse_json_response(raw: str) -> dict[str, Any]:
    """Parse a JSON object from a model response.

    Handles the three ways models deviate from "JSON only": markdown fences,
    leading prose, and trailing commentary. Falls back to extracting the
    outermost balanced brace pair.

    Args:
        raw: The raw response text.

    Returns:
        The parsed object.

    Raises:
        ValueError: If no valid JSON object can be recovered.
    """
    text = raw.strip()

    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Scan for the outermost balanced {...}, ignoring braces inside strings so
    # a JSON value containing "}" does not terminate the scan early.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            char = text[position]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start : position + 1])
                        if isinstance(candidate, dict):
                            return candidate
                    except json.JSONDecodeError:
                        break

    raise ValueError(f"No valid JSON object found in model response: {raw[:200]!r}")


def _record_usage(model: str, message: BaseMessage) -> None:
    """Emit token usage metrics when the provider reports them."""
    usage = getattr(message, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        return
    if input_tokens := usage.get("input_tokens"):
        llm_tokens_total.labels(model=model, direction="input").inc(input_tokens)
    if output_tokens := usage.get("output_tokens"):
        llm_tokens_total.labels(model=model, direction="output").inc(output_tokens)


class LLMService:
    """Stateless facade over the chat model provider.

    Holds only an immutable cache of configured model instances. Every method
    takes the model as an argument, so concurrent callers cannot influence each
    other.
    """

    def __init__(self) -> None:
        """Initialise the model cache."""
        # Keyed by (model_name, tool_signature). Building a ChatOpenAI is cheap
        # but not free, and cached instances also reuse the underlying HTTP
        # connection pool.
        self._models: dict[tuple[str, str], BaseChatModel] = {}
        self._lock = asyncio.Lock()

    def _cache_key(self, model: str, tools: Sequence[BaseTool] | None) -> tuple[str, str]:
        """Build a cache key that distinguishes tool bindings."""
        signature = ",".join(sorted(tool.name for tool in tools)) if tools else ""
        return (model, signature)

    def get_model(
        self,
        model: str | None = None,
        tools: Sequence[BaseTool] | None = None,
        **overrides: Any,
    ) -> BaseChatModel:
        """Return a configured model instance.

        Args:
            model: Model name; defaults to ``DEFAULT_LLM_MODEL``.
            tools: Tools to bind. Binding returns a new runnable rather than
                mutating the base model, which is what makes caching safe.
            **overrides: Per-call parameter overrides. Supplying any override
                bypasses the cache, since the result is not reusable.

        Returns:
            A ready-to-invoke model.
        """
        model = model or settings.DEFAULT_LLM_MODEL

        if overrides:
            base = build_chat_model(model, **overrides)
            return base.bind_tools(list(tools)) if tools else base

        key = self._cache_key(model, tools)
        cached = self._models.get(key)
        if cached is None:
            base = build_chat_model(model)
            cached = base.bind_tools(list(tools)) if tools else base
            self._models[key] = cached
        return cached

    @retry(
        stop=stop_after_attempt(settings.MAX_LLM_CALL_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def _invoke_once(self, model_instance: BaseChatModel, messages: list[Any]) -> BaseMessage:
        """Invoke one model with retries on transient failures."""
        return await model_instance.ainvoke(messages)

    async def invoke(
        self,
        messages: list[Any],
        *,
        model: str | None = None,
        tools: Sequence[BaseTool] | None = None,
        **overrides: Any,
    ) -> BaseMessage:
        """Invoke the model, failing over through the fallback chain.

        The chain is walked over a *local* list. Nothing about the failover is
        recorded on the service, so a fallback triggered by one request cannot
        change which model another request uses.

        Args:
            messages: Conversation messages.
            model: Starting model; defaults to the configured primary.
            tools: Tools to bind for this call.
            **overrides: Per-call parameter overrides.

        Returns:
            The model's response.

        Raises:
            LLMError: If every model in the chain fails.
        """
        chain = list(settings.model_fallback_chain)
        if model and model in chain:
            # Start at the requested model but keep the remaining chain as
            # fallbacks, so an explicit choice still degrades gracefully.
            chain = chain[chain.index(model) :] + chain[: chain.index(model)]
        elif model:
            chain = [model, *chain]

        last_error: Exception | None = None

        for position, candidate in enumerate(chain):
            model_instance = self.get_model(candidate, tools, **overrides)
            started = time.perf_counter()
            try:
                response = await self._invoke_once(model_instance, messages)
                llm_inference_duration_seconds.labels(model=candidate).observe(time.perf_counter() - started)
                llm_requests_total.labels(model=candidate, outcome="success").inc()
                _record_usage(candidate, response)
                if position > 0:
                    logger.info("llm_fallback_succeeded", model=candidate, attempts=position + 1)
                return normalise_message(response)

            except Exception as exc:
                last_error = exc
                llm_requests_total.labels(model=candidate, outcome="error").inc()
                logger.warning(
                    "llm_call_failed",
                    model=candidate,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    remaining_models=len(chain) - position - 1,
                )
                if position + 1 < len(chain):
                    llm_fallback_total.labels(from_model=candidate, to_model=chain[position + 1]).inc()

        logger.error("llm_all_models_failed", chain=chain, error=str(last_error))
        raise LLMError(
            "All configured language models failed to respond.",
            context={"chain": chain, "last_error": str(last_error)},
        ) from last_error

    async def complete(self, prompt: str, *, model: str | None = None, **overrides: Any) -> str:
        """Run a single-turn prompt and return plain text.

        Args:
            prompt: The user prompt.
            model: Optional model override.
            **overrides: Per-call parameter overrides.

        Returns:
            The response text.
        """
        response = await self.invoke([{"role": "user", "content": prompt}], model=model, **overrides)
        return extract_text(response)

    async def complete_json(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_attempts: int = 2,
        **overrides: Any,
    ) -> dict[str, Any]:
        """Run a prompt expected to return JSON, retrying on malformed output.

        The retry feeds the parse error back to the model. That is markedly more
        effective than a blind retry: the second attempt is told exactly what was
        wrong with the first.

        Args:
            prompt: The prompt, which should request JSON.
            model: Optional model override.
            max_attempts: Parse attempts before giving up.
            **overrides: Per-call parameter overrides.

        Returns:
            The parsed object.

        Raises:
            LLMError: If no attempt produced valid JSON.
        """
        # response_format nudges compliant providers into guaranteed-valid JSON.
        # Gateways that ignore it still work; the parser handles the fallout.
        overrides.setdefault("response_format", {"type": "json_object"})

        conversation: list[Any] = [{"role": "user", "content": prompt}]
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            response = await self.invoke(conversation, model=model, **overrides)
            raw = extract_text(response)
            try:
                return parse_json_response(raw)
            except ValueError as exc:
                last_error = exc
                logger.warning("llm_json_parse_failed", attempt=attempt + 1, response_preview=raw[:200])
                conversation = [
                    *conversation,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            f"That response could not be parsed as JSON ({exc}). "
                            "Reply with a single valid JSON object and nothing else - "
                            "no prose, no markdown fences."
                        ),
                    },
                ]

        raise LLMError(
            "The model did not return valid JSON.",
            context={"attempts": max_attempts, "last_error": str(last_error)},
        ) from last_error


_llm_service: LLMService | None = None
_service_lock = asyncio.Lock()


async def get_llm_service() -> LLMService:
    """Return the shared LLM service, constructing it once."""
    global _llm_service
    if _llm_service is None:
        async with _service_lock:
            if _llm_service is None:
                _llm_service = LLMService()
                logger.info(
                    "llm_service_initialized",
                    primary_model=settings.DEFAULT_LLM_MODEL,
                    fallback_chain=settings.model_fallback_chain,
                )
    return _llm_service


__all__ = [
    "LLMService",
    "build_chat_model",
    "extract_text",
    "get_llm_service",
    "normalise_message",
    "parse_json_response",
]
