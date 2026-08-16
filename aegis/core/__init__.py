"""Core infrastructure: configuration, logging, metrics, errors, and the agent.

Import order matters here. :mod:`aegis.core.config` must be imported before
:mod:`aegis.core.logging`, because logging configures itself from settings at
import time.
"""

from aegis.core.config import (
    Environment,
    Settings,
    get_settings,
    settings,
)
from aegis.core.exceptions import AegisError
from aegis.core.logging import (
    bind_context,
    get_logger,
    logger,
)

__all__ = [
    "AegisError",
    "Environment",
    "Settings",
    "bind_context",
    "get_logger",
    "get_settings",
    "logger",
    "settings",
]
