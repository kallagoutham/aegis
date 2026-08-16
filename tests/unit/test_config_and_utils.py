"""Tests for configuration validation, sanitization, and LLM response parsing."""

from __future__ import annotations

import pytest

from aegis.core.config import (
    Environment,
    Settings,
)
from aegis.services.llm import (
    extract_text,
    parse_json_response,
)
from aegis.utils.sanitization import (
    normalise_email,
    redact_secrets,
    reject_control_characters,
    truncate,
    validate_identifier,
)


def _settings(**overrides) -> Settings:
    """Build a Settings instance directly, bypassing env files."""
    base = {
        "ENVIRONMENT": Environment.PRODUCTION,
        "JWT_SECRET_KEY": "x" * 48,
        "OPENAI_API_KEY": "sk-real-looking-key",
        "POSTGRES_PASSWORD": "a-real-database-password",
        "ALLOWED_ORIGINS": ["https://ops.example.com"],
        "DEBUG": False,
    }
    base.update(overrides)
    return Settings(**base)


class TestProductionHardening:
    """Configuration that must be refused outside development."""

    def test_valid_production_config_is_accepted(self):
        assert _settings().ENVIRONMENT is Environment.PRODUCTION

    def test_short_jwt_secret_is_rejected(self):
        with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
            _settings(JWT_SECRET_KEY="tooshort")

    def test_placeholder_jwt_secret_is_rejected(self):
        with pytest.raises(ValueError, match="placeholder"):
            _settings(JWT_SECRET_KEY="supersecretkeythatshouldbechangedforproduction")

    def test_wildcard_cors_origin_is_rejected(self):
        with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
            _settings(ALLOWED_ORIGINS=["*"])

    def test_missing_api_key_is_rejected(self):
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            _settings(OPENAI_API_KEY="")

    def test_debug_is_rejected(self):
        with pytest.raises(ValueError, match="DEBUG"):
            _settings(DEBUG=True)

    def test_sql_echo_is_rejected(self):
        with pytest.raises(ValueError, match="POSTGRES_ECHO"):
            _settings(POSTGRES_ECHO=True)

    def test_development_permits_everything(self):
        # Local development must stay frictionless with no secrets set.
        relaxed = Settings(
            ENVIRONMENT=Environment.DEVELOPMENT,
            JWT_SECRET_KEY="",
            OPENAI_API_KEY="",
            ALLOWED_ORIGINS=["*"],
            DEBUG=True,
        )
        assert relaxed.DEBUG is True


class TestInternalConsistency:
    """Settings that are individually valid but jointly broken."""

    def test_overlap_must_be_smaller_than_chunk_size(self):
        with pytest.raises(ValueError, match="CHUNK_OVERLAP_TOKENS"):
            _settings(CHUNK_TARGET_TOKENS=100, CHUNK_OVERLAP_TOKENS=100)

    def test_top_k_cannot_exceed_candidate_k(self):
        with pytest.raises(ValueError, match="RETRIEVAL_TOP_K"):
            _settings(RETRIEVAL_TOP_K=50, RETRIEVAL_CANDIDATE_K=10)

    def test_rerank_top_n_must_cover_top_k(self):
        with pytest.raises(ValueError, match="RERANK_TOP_N"):
            _settings(RERANK_ENABLED=True, RETRIEVAL_TOP_K=20, RERANK_TOP_N=5)


class TestDerivedValues:
    """Computed configuration."""

    def test_dsn_escapes_special_characters_in_password(self):
        settings = _settings(POSTGRES_PASSWORD="p@ss:word/with?specials")
        # Unescaped, these would be parsed as URL delimiters and the connection
        # would target the wrong host.
        assert "p%40ss%3Aword%2Fwith%3Fspecials" in settings.postgres_dsn

    def test_async_dsn_uses_asyncpg_driver(self):
        assert _settings().async_postgres_dsn.startswith("postgresql+asyncpg://")

    def test_fallback_chain_starts_with_primary(self):
        settings = _settings(DEFAULT_LLM_MODEL="gpt-4o", FALLBACK_LLM_MODELS=["gpt-4o-mini"])
        assert settings.model_fallback_chain[0] == "gpt-4o"

    def test_fallback_chain_deduplicates(self):
        settings = _settings(DEFAULT_LLM_MODEL="gpt-4o", FALLBACK_LLM_MODELS=["gpt-4o", "gpt-4o-mini"])
        assert settings.model_fallback_chain == ["gpt-4o", "gpt-4o-mini"]

    def test_api_prefix_is_normalised(self):
        assert _settings(API_V1_STR="api/v2/").API_V1_STR == "/api/v2"

    def test_rate_limit_lookup_falls_back_to_default(self):
        assert _settings().rate_limit_for("no-such-endpoint")

    def test_csv_lists_are_parsed(self):
        # Env files are far more readable with CSV than with JSON.
        assert _settings(ALLOWED_ORIGINS="https://a.com,https://b.com").ALLOWED_ORIGINS == [
            "https://a.com",
            "https://b.com",
        ]


class TestSanitization:
    """Input validation that does not mutate data."""

    def test_email_is_lowercased(self):
        assert normalise_email("  User@Example.COM ") == "user@example.com"

    def test_malformed_email_is_rejected(self):
        with pytest.raises(ValueError):
            normalise_email("not-an-email")

    def test_control_characters_are_rejected(self):
        with pytest.raises(ValueError):
            reject_control_characters("bad\x00value")

    def test_newlines_and_tabs_are_permitted(self):
        # Load-bearing in log text.
        text = "line one\nline two\tcolumn"
        assert reject_control_characters(text) == text

    def test_identifier_validation(self):
        assert validate_identifier("Payments-API") == "payments-api"
        with pytest.raises(ValueError):
            validate_identifier("bad identifier with spaces")

    def test_truncate_marks_that_it_truncated(self):
        result = truncate("a" * 100, 20)
        assert len(result) == 20
        # An unmarked cut reads as complete text.
        assert result.endswith("...")

    def test_truncate_leaves_short_text_alone(self):
        assert truncate("short", 100) == "short"


class TestSecretRedaction:
    """Credential masking before logging."""

    def test_masks_connection_string_password(self):
        redacted = redact_secrets("postgresql://user:hunter2@db.internal:5432/aegis")
        assert "hunter2" not in redacted
        assert "***" in redacted

    def test_masks_key_value_secrets(self):
        assert "s3cret" not in redact_secrets("password=s3cret other=fine")
        assert "abc123xyz" not in redact_secrets('api_key: "abc123xyz"')

    def test_masks_bearer_tokens(self):
        assert "eyJhbGciOiJIUzI1NiJ9" not in redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9")

    def test_masks_provider_key_prefixes(self):
        assert "sk-abcdefghijklmnopqrst" not in redact_secrets("key is sk-abcdefghijklmnopqrst")

    def test_leaves_ordinary_text_untouched(self):
        text = "connection to the database timed out after 30000ms"
        assert redact_secrets(text) == text


class TestJsonResponseParsing:
    """Recovering JSON from imperfect model output."""

    def test_plain_json(self):
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_markdown_fenced_json(self):
        assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_unlabelled_fence(self):
        assert parse_json_response('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_leading_prose(self):
        assert parse_json_response('Here is the result:\n{"a": 1}') == {"a": 1}

    def test_json_with_trailing_commentary(self):
        assert parse_json_response('{"a": 1}\n\nHope that helps!') == {"a": 1}

    def test_braces_inside_strings_do_not_terminate_the_scan(self):
        payload = 'Result: {"template": "connection to {host} failed", "count": 3}'
        parsed = parse_json_response(payload)
        assert parsed["count"] == 3
        assert parsed["template"] == "connection to {host} failed"

    def test_nested_objects(self):
        parsed = parse_json_response('{"outer": {"inner": {"deep": true}}}')
        assert parsed["outer"]["inner"]["deep"] is True

    def test_unrecoverable_output_raises(self):
        with pytest.raises(ValueError):
            parse_json_response("there is no json here at all")


class TestExtractText:
    """Flattening structured content blocks."""

    def test_plain_string_passes_through(self):
        from langchain_core.messages import AIMessage

        assert extract_text(AIMessage(content="hello")) == "hello"

    def test_reasoning_blocks_are_dropped(self):
        from langchain_core.messages import AIMessage

        message = AIMessage(
            content=[
                {"type": "reasoning", "id": "r1", "summary": []},
                {"type": "text", "text": "the actual answer"},
            ]
        )
        assert extract_text(message) == "the actual answer"

    def test_multiple_text_blocks_are_concatenated(self):
        from langchain_core.messages import AIMessage

        message = AIMessage(content=[{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}])
        assert extract_text(message) == "part one part two"
