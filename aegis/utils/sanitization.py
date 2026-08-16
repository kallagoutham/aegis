"""Input validation and safe rendering helpers.

**Why the previous approach was wrong.** The old ``sanitize_string`` ran
``html.escape()`` over every incoming value, including auth tokens and log text.
That is escaping at the wrong boundary, and it breaks this product specifically:
logs are full of ``<``, ``>``, and ``&``. A stack trace containing
``List<String>`` was silently rewritten to ``List&lt;String&gt;`` before it ever
reached the analyser, corrupting template extraction and every subsequent
citation.

Escaping is a *rendering* concern. HTML-escape when writing into HTML; do not
mutate data on the way in. What the input boundary should do is:

* **reject** what can never be legitimate (null bytes, control characters,
  oversized payloads),
* **normalise** what has a canonical form (emails, identifiers),
* **leave everything else exactly as the user sent it**, so the analyser sees
  what actually happened on the wire.

The two genuine injection surfaces are handled where they occur: SQL uses bound
parameters throughout (see :mod:`aegis.retrieval.vector_store`), and HTML is
escaped by :func:`escape_html` at render time.
"""

from __future__ import annotations

import html
import re
import unicodedata

# C0/C1 control characters, excluding tab, newline, and carriage return, which
# are legitimate and load-bearing in log text.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Characters that terminate or redirect a shell command. Used only to decide
# whether a string is safe to display as a runnable command, never to rewrite it.
_SHELL_METACHARS = frozenset(";|&$`\n\r><")

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

# Conservative identifier shape for values interpolated into log context or used
# as filter keys: letters, digits, and a few separators.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,199}$")


def reject_control_characters(value: str, *, field: str = "value") -> str:
    """Reject strings containing control characters.

    Null bytes in particular cannot be stored in a Postgres text column, so
    catching them here turns a confusing 500 at write time into a clear 422 at
    the boundary.

    Args:
        value: The string to check.
        field: Field name used in the error message.

    Returns:
        The unmodified string.

    Raises:
        ValueError: If a control character is present.
    """
    match = _CONTROL_CHARS_RE.search(value)
    if match:
        raise ValueError(f"{field} contains a disallowed control character (0x{ord(match.group()):02x})")
    return value


def strip_control_characters(value: str) -> str:
    """Remove control characters, preserving tabs and newlines.

    Used for text destined for storage where rejecting the whole payload would
    be disproportionate - a single stray byte in a 40 MB log capture should not
    discard the capture.
    """
    return _CONTROL_CHARS_RE.sub("", value)


def normalise_email(email: str) -> str:
    """Validate and canonicalise an email address.

    Lowercases and NFKC-normalises so ``User@Example.com`` and a Unicode
    homoglyph variant cannot register as distinct accounts.

    Args:
        email: The raw address.

    Returns:
        The canonical form.

    Raises:
        ValueError: If the address is malformed or too long.
    """
    candidate = unicodedata.normalize("NFKC", email.strip()).lower()

    if len(candidate) > 320:
        raise ValueError("Email address exceeds the maximum length of 320 characters")
    if not _EMAIL_RE.match(candidate):
        raise ValueError("Email address is not correctly formatted")

    local, _, _domain = candidate.partition("@")
    if len(local) > 64:
        raise ValueError("Email local part exceeds the maximum length of 64 characters")

    return candidate


def validate_identifier(value: str, *, field: str = "identifier") -> str:
    """Validate a service name, tag, or similar short identifier.

    Args:
        value: The candidate identifier.
        field: Field name used in the error message.

    Returns:
        The trimmed, lowercased identifier.

    Raises:
        ValueError: If the value does not match the permitted shape.
    """
    candidate = value.strip().lower()
    if not _IDENTIFIER_RE.match(candidate):
        raise ValueError(
            f"{field} must start with a letter or digit and contain only letters, "
            "digits, dots, underscores, and hyphens (max 200 characters)"
        )
    return candidate


def escape_html(value: str) -> str:
    """Escape a string for safe inclusion in HTML.

    Call this at the point of rendering, never on the way in.
    """
    return html.escape(value, quote=True)


def truncate(value: str, limit: int, *, suffix: str = "...") -> str:
    """Truncate a string to ``limit`` characters, appending a marker.

    The marker matters: silently cut text reads as complete, which during an
    incident means a responder can believe they have seen a whole log excerpt
    when they have seen a third of it.
    """
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))] + suffix


def looks_like_shell_injection(command: str) -> bool:
    """Whether a command string contains shell metacharacters.

    Used to decide whether a model-proposed command can be displayed as
    directly runnable, or must be flagged for review first. Aegis never executes
    commands; this only governs how one is *presented*.

    Args:
        command: The candidate command.

    Returns:
        True if the command should be flagged before a human runs it.
    """
    return any(character in _SHELL_METACHARS for character in command)


def redact_secrets(text: str) -> str:
    """Mask credential-shaped substrings before logging.

    Pasted configuration and log excerpts routinely contain connection strings
    and bearer tokens. Logging them verbatim copies the secret into a second
    system with different access controls and a different retention policy.

    Args:
        text: Text about to be logged.

    Returns:
        The text with recognised secrets replaced by ``***``.
    """
    # Order matters. The scheme-qualified rules must run before the generic
    # key=value rule, which would otherwise consume only the first token after
    # the separator - masking the word "Bearer" and leaving the token beside it
    # in plain sight.
    patterns = (
        # Password inside a connection URI: scheme://user:secret@host
        (re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)"), r"\1***\3"),
        # Bearer tokens, with or without a preceding header name.
        (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE), r"\1***"),
        # An Authorization header: mask everything after the separator, since
        # the scheme and the credential are both on the same line.
        (re.compile(r"\b(authorization)(\s*[=:]\s*).+", re.IGNORECASE), r"\1\2***"),
        # key=value / key: value for sensitive-looking key names
        (
            re.compile(
                r"\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)" r"(\s*[=:]\s*)(\"?)([^\s\"',}]+)",
                re.IGNORECASE,
            ),
            r"\1\2\3***",
        ),
        # Common provider key prefixes
        (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9]{16,}\b"), "***"),
        # AWS access key ids
        (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    )

    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


__all__ = [
    "escape_html",
    "looks_like_shell_injection",
    "normalise_email",
    "redact_secrets",
    "reject_control_characters",
    "strip_control_characters",
    "truncate",
    "validate_identifier",
]
