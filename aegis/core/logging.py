"""Structured logging for Aegis.

Every log line is a structured event: a short snake_case event name plus typed
key/value fields. That makes logs queryable (``event="retrieval_completed" AND
latency_ms > 500``) instead of forcing regex over prose.

Two renderers are wired up:

* ``console`` - colourised, human-readable. Development default.
* ``json``    - one JSON object per line. Staging/production default, ready for
  Loki, CloudWatch, or any JSON-aware collector.

Request-scoped fields (``request_id``, ``user_id``, ``session_id``) are carried
in a :class:`~contextvars.ContextVar` and merged into every event automatically,
so call sites never thread correlation IDs through function signatures. Because
``contextvars`` propagate into ``asyncio`` tasks, a background task spawned
inside a request inherits its context for free.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from contextvars import ContextVar
from datetime import (
    UTC,
    datetime,
)
import logging
import logging.handlers
import sys
from typing import (
    Any,
)

import structlog

from aegis.core.config import (
    Environment,
    settings,
)

# Request-scoped structured context.
#
# The default is None rather than an empty dict: a ContextVar's default object
# is shared by every context that never called ``set()``, so a mutable default
# could accumulate fields visible to unrelated requests. Every write below
# rebinds a whole new dict, so concurrent tasks sharing a parent context never
# see each other's writes.
_request_context: ContextVar[dict[str, Any] | None] = ContextVar("aegis_request_context", default=None)

# Third-party loggers that are noisy at DEBUG and rarely actionable.
_NOISY_LOGGERS = (
    "httpcore",
    "httpx",
    "urllib3",
    "asyncio",
    "openai._base_client",
    "langfuse",
    "psycopg.pool",
    "multipart",
)


def bind_context(**kwargs: Any) -> None:
    """Merge key/value pairs into the current request's logging context.

    Args:
        **kwargs: Fields to attach to every subsequent log event in this context.
    """
    _request_context.set({**(_request_context.get() or {}), **kwargs})


def clear_context() -> None:
    """Reset the logging context. Called at request boundaries."""
    _request_context.set({})


def get_context() -> dict[str, Any]:
    """Return a copy of the current logging context."""
    return dict(_request_context.get() or {})


def _merge_request_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor that folds request context into each event.

    Explicit keyword arguments at the call site win over context values, so a
    log call can always override an inherited field.
    """
    context = _request_context.get() or {}
    for key, value in context.items():
        event_dict.setdefault(key, value)
    return event_dict


def _add_service_metadata(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Stamp every event with the fields log aggregators index on."""
    event_dict.setdefault("service", settings.PROJECT_NAME.lower().replace(" ", "-"))
    event_dict.setdefault("environment", settings.ENVIRONMENT.value)
    event_dict.setdefault("version", settings.VERSION)
    return event_dict


def _drop_color_message(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Remove uvicorn's duplicate pre-coloured message field."""
    event_dict.pop("color_message", None)
    return event_dict


def _build_processor_chain() -> list[Any]:
    """Assemble the shared processor chain applied before rendering.

    Order matters: level and timestamp first so downstream processors can read
    them, context merging next, then callsite enrichment (expensive, so it is
    limited to non-deployed environments).
    """
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _merge_request_context,
        _add_service_metadata,
        _drop_color_message,
    ]

    # Callsite resolution walks the stack on every event. Worth it locally for
    # click-through file:line, too expensive under production throughput.
    if settings.ENVIRONMENT in (Environment.DEVELOPMENT, Environment.TEST):
        processors.append(
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                }
            )
        )

    return processors


def _build_handlers(level: int) -> list[logging.Handler]:
    """Create stdout and (optionally) rotating-file handlers.

    Args:
        level: Numeric logging level applied to each handler.

    Returns:
        Handlers to attach to the root logger.
    """
    handlers: list[logging.Handler] = []

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(level)
    handlers.append(stdout_handler)

    if settings.LOG_TO_FILE:
        try:
            settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_file = settings.LOG_DIR / f"{settings.ENVIRONMENT.value}.jsonl"
            # Rotate at 50 MB, keep 5 generations. Unbounded log files are one
            # of the more common ways a container fills its disk.
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setLevel(level)
            handlers.append(file_handler)
        except OSError:
            # A read-only filesystem is a legitimate deployment choice; stdout
            # logging still works, so degrade rather than crash at import time.
            pass

    return handlers


def setup_logging() -> None:
    """Configure structlog and the stdlib logging bridge.

    Safe to call more than once; later calls replace the previous configuration.
    Routing stdlib logs through ``ProcessorFormatter`` means records emitted by
    third-party libraries (uvicorn, SQLAlchemy) are rendered identically to our
    own structured events.
    """
    level = logging.getLevelNamesMapping()[settings.LOG_LEVEL]
    shared_processors = _build_processor_chain()

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=True, exception_formatter=structlog.dev.plain_traceback)
        if settings.LOG_FORMAT == "console"
        else structlog.processors.JSONRenderer(sort_keys=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain runs for records that did NOT originate in structlog,
        # giving third-party logs the same enrichment as ours.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    for handler in _build_handlers(level):
        handler.setFormatter(formatter)
        root.addHandler(handler)

    root.setLevel(level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    # uvicorn installs its own handlers; clear them so records propagate to root
    # and get our formatting instead of uvicorn's default text output.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    Args:
        name: Logger name, conventionally ``__name__``.

    Returns:
        A logger whose events flow through the configured processor chain.
    """
    return structlog.get_logger(name)


setup_logging()

logger: structlog.stdlib.BoundLogger = get_logger("aegis")

logger.info(
    "logging_initialized",
    log_level=settings.LOG_LEVEL,
    log_format=settings.LOG_FORMAT,
    log_to_file=settings.LOG_TO_FILE,
    started_at=datetime.now(UTC).isoformat(),
)

__all__ = ["bind_context", "clear_context", "get_context", "get_logger", "logger", "setup_logging"]
