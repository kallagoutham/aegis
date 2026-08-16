"""Application configuration for Aegis.

Configuration is resolved once at import time into a single frozen ``Settings``
object.  Values are sourced, in descending order of precedence, from:

1. Real process environment variables (including anything exported by the shell
   or injected by the container runtime / secret manager).
2. The first environment file that exists, searched in this order::

       .env.<environment>.local
       .env.<environment>
       .env.local
       .env

3. The defaults declared on the model below.

The environment itself is selected by ``APP_ENV`` and drives a set of
*environment overlays* (:func:`_environment_overlay`) that tighten defaults as
you move toward production - stricter logging, lower rate limits, mandatory
secrets.  Overlays never clobber a value the operator set explicitly; see
:meth:`Settings.apply_environment_overlay`.

Validation is deliberately strict and happens at process start rather than at
first use, so a misconfigured deployment fails immediately and loudly instead of
throwing a confusing error on the first request that touches the bad value.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
import os
from pathlib import Path
from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Environment(str, Enum):
    """Deployment environments Aegis knows how to configure itself for."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"

    @property
    def is_deployed(self) -> bool:
        """Whether this environment faces real traffic (staging or production)."""
        return self in (Environment.STAGING, Environment.PRODUCTION)


def _detect_environment() -> Environment:
    """Resolve the active environment from ``APP_ENV``, tolerating aliases."""
    raw = os.getenv("APP_ENV", "development").strip().lower()
    aliases = {
        "prod": Environment.PRODUCTION,
        "production": Environment.PRODUCTION,
        "stage": Environment.STAGING,
        "staging": Environment.STAGING,
        "test": Environment.TEST,
        "testing": Environment.TEST,
        "dev": Environment.DEVELOPMENT,
        "development": Environment.DEVELOPMENT,
        "local": Environment.DEVELOPMENT,
    }
    return aliases.get(raw, Environment.DEVELOPMENT)


def _candidate_env_files() -> tuple[Path, ...]:
    """Return the env-file search path, most specific first."""
    env = _detect_environment().value
    return (
        PROJECT_ROOT / f".env.{env}.local",
        PROJECT_ROOT / f".env.{env}",
        PROJECT_ROOT / ".env.local",
        PROJECT_ROOT / ".env",
    )


def _active_env_file() -> Path | None:
    """Return the first existing env file, or ``None`` when running on pure env vars."""
    return next((path for path in _candidate_env_files() if path.is_file()), None)


# Environment overlays: defaults that get tightened per environment. These are
# applied only to fields the operator did not set explicitly, so an explicit
# LOG_LEVEL=DEBUG in production is still honoured (with a warning logged by the
# logging module).
_OVERLAYS: dict[Environment, dict[str, Any]] = {
    Environment.DEVELOPMENT: {
        "DEBUG": True,
        "LOG_LEVEL": "DEBUG",
        "LOG_FORMAT": "console",
        "RATE_LIMIT_DEFAULT": ["1000 per day", "200 per hour"],
    },
    Environment.TEST: {
        "DEBUG": True,
        "LOG_LEVEL": "DEBUG",
        "LOG_FORMAT": "console",
        "RATE_LIMIT_DEFAULT": ["100000 per day"],
        "RATE_LIMIT_ENABLED": False,
    },
    Environment.STAGING: {
        "DEBUG": False,
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "RATE_LIMIT_DEFAULT": ["500 per day", "100 per hour"],
    },
    Environment.PRODUCTION: {
        "DEBUG": False,
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "RATE_LIMIT_DEFAULT": ["200 per day", "50 per hour"],
    },
}


class _LenientComplexDecodeMixin:
    """Lets list-valued settings accept plain comma-separated strings.

    pydantic-settings decodes "complex" fields (lists, dicts) by running
    ``json.loads`` inside the *source* layer, which happens before any field
    validator. So ``ALLOWED_ORIGINS=http://a,http://b`` raises a
    ``SettingsError`` and the ``mode="before"`` validator on the field never
    gets a chance to split it.

    Overriding the decode step to fall back to the raw string defers the
    decision to that validator, which is where the CSV handling lives. Valid
    JSON still parses as JSON, so ``["http://a"]`` keeps working too.
    """

    def decode_complex_value(self, field_name: str, field: Any, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field, value)  # type: ignore[misc]
        except ValueError:
            return value


class _LenientEnvSource(_LenientComplexDecodeMixin, EnvSettingsSource):
    """Environment-variable source that tolerates comma-separated lists."""


class _LenientDotEnvSource(_LenientComplexDecodeMixin, DotEnvSettingsSource):
    """Env-file source that tolerates comma-separated lists."""


class Settings(BaseSettings):
    """Fully validated Aegis configuration.

    Every field maps to an upper-case environment variable of the same name.
    Grouping below mirrors the sections in ``.env.example``.
    """

    model_config = SettingsConfigDict(
        env_file=_active_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        validate_default=True,
    )

    # ------------------------------------------------------------------
    # Application identity
    # ------------------------------------------------------------------
    ENVIRONMENT: Environment = Field(
        default_factory=_detect_environment,
        description="Active deployment environment; normally set via APP_ENV.",
    )
    PROJECT_NAME: str = Field(default="Aegis", description="Human-readable service name.")
    VERSION: str = Field(default="1.0.0", description="Service version reported by /health and OpenAPI.")
    DESCRIPTION: str = Field(
        default="AI incident response platform - retrieval-grounded root cause analysis for on-call engineers.",
        description="OpenAPI description.",
    )
    API_V1_STR: str = Field(default="/api/v1", description="Mount prefix for the v1 API router.")
    DEBUG: bool = Field(default=False, description="Enable verbose errors and debug logging.")

    # ------------------------------------------------------------------
    # HTTP / CORS
    # ------------------------------------------------------------------
    ALLOWED_ORIGINS: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        description="Comma-separated CORS origins. '*' is rejected in production.",
    )
    REQUEST_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, description="Server-side cap on a single request.")

    # ------------------------------------------------------------------
    # LLM provider
    # ------------------------------------------------------------------
    OPENAI_API_KEY: SecretStr = Field(default=SecretStr(""), description="OpenAI API key.")
    OPENAI_BASE_URL: str = Field(
        default="https://api.openai.com/v1",
        description="Override to target an OpenAI-compatible gateway (vLLM, LiteLLM, Azure proxy).",
    )
    DEFAULT_LLM_MODEL: str = Field(default="gpt-4o-mini", description="Primary reasoning model.")
    FALLBACK_LLM_MODELS: list[str] = Field(
        default_factory=lambda: ["gpt-4o", "gpt-4o-mini"],
        description="Ordered fallback chain used when the primary model errors.",
    )
    DEFAULT_LLM_TEMPERATURE: float = Field(default=0.1, ge=0.0, le=2.0)
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=4000, gt=0, description="Cap on generated tokens per LLM call.")
    LLM_CONTEXT_TOKEN_BUDGET: int = Field(
        default=32000,
        gt=0,
        description="Token budget for conversation history; older turns are trimmed to fit.",
    )
    MAX_LLM_CALL_RETRIES: int = Field(default=3, ge=1, le=10, description="Retries per model before failing over.")
    LLM_REQUEST_TIMEOUT_SECONDS: float = Field(default=90.0, gt=0)

    # ------------------------------------------------------------------
    # Embeddings and retrieval
    # ------------------------------------------------------------------
    EMBEDDING_MODEL: str = Field(default="text-embedding-3-small", description="Model used for all indexed content.")
    EMBEDDING_DIMENSIONS: int = Field(
        default=1536,
        gt=0,
        description="Vector width. MUST match the pgvector column; changing it requires a reindex.",
    )
    EMBEDDING_BATCH_SIZE: int = Field(default=64, gt=0, le=2048, description="Chunks per embedding API call.")
    RETRIEVAL_TOP_K: int = Field(default=8, gt=0, le=100, description="Chunks returned to the agent per search.")
    RETRIEVAL_CANDIDATE_K: int = Field(
        default=40,
        gt=0,
        le=500,
        description="Candidates pulled from each retrieval arm before fusion and reranking.",
    )
    RETRIEVAL_MIN_SCORE: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Drop fused results scoring below this threshold. 0 disables filtering.",
    )
    HYBRID_VECTOR_WEIGHT: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Weight of dense vector results during reciprocal rank fusion; lexical gets 1 - this.",
    )
    RRF_K: int = Field(default=60, gt=0, description="Reciprocal rank fusion smoothing constant.")
    RERANK_ENABLED: bool = Field(default=True, description="Enable the LLM cross-encoder rerank stage.")
    RERANK_MODEL: str = Field(default="gpt-4o-mini", description="Model used to rerank fused candidates.")
    RERANK_TOP_N: int = Field(default=20, gt=0, le=100, description="Candidates handed to the reranker.")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    CHUNK_TARGET_TOKENS: int = Field(default=512, gt=0, description="Target chunk size in tokens.")
    CHUNK_OVERLAP_TOKENS: int = Field(default=64, ge=0, description="Overlap between adjacent chunks.")
    CHUNK_MIN_TOKENS: int = Field(default=32, ge=0, description="Chunks smaller than this are merged forward.")
    INGESTION_CONCURRENCY: int = Field(default=4, gt=0, le=64, description="Parallel documents during ingestion.")
    MAX_UPLOAD_BYTES: int = Field(default=25 * 1024 * 1024, gt=0, description="Largest accepted upload.")

    # ------------------------------------------------------------------
    # Agent behaviour
    # ------------------------------------------------------------------
    AGENT_MAX_TOOL_ITERATIONS: int = Field(
        default=6,
        gt=0,
        le=25,
        description="Hard cap on investigate -> tool loops before forced synthesis.",
    )
    AGENT_MAX_LOG_LINES: int = Field(default=5000, gt=0, description="Log lines parsed per analysis request.")
    LONG_TERM_MEMORY_ENABLED: bool = Field(default=True, description="Enable mem0 cross-session user memory.")
    LONG_TERM_MEMORY_MODEL: str = Field(default="gpt-4o-mini")
    LONG_TERM_MEMORY_EMBEDDER_MODEL: str = Field(default="text-embedding-3-small")
    LONG_TERM_MEMORY_COLLECTION_NAME: str = Field(default="aegis_longterm_memory")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    JWT_SECRET_KEY: SecretStr = Field(default=SecretStr(""), description="HMAC signing key for access tokens.")
    JWT_ALGORITHM: str = Field(default="HS256")
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 24, gt=0, description="User token lifetime.")
    JWT_SESSION_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 24 * 7, gt=0, description="Session token lifetime.")
    JWT_ISSUER: str = Field(default="aegis", description="'iss' claim, verified on decode.")
    JWT_AUDIENCE: str = Field(default="aegis-api", description="'aud' claim, verified on decode.")
    PASSWORD_MIN_LENGTH: int = Field(default=12, ge=8, le=128)
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=18, description="Lowered automatically under tests.")

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    POSTGRES_HOST: str = Field(default="localhost")
    POSTGRES_PORT: int = Field(default=5432, gt=0, lt=65536)
    POSTGRES_DB: str = Field(default="aegis")
    POSTGRES_USER: str = Field(default="aegis")
    POSTGRES_PASSWORD: SecretStr = Field(default=SecretStr("aegis"))
    POSTGRES_POOL_SIZE: int = Field(default=20, gt=0, le=200)
    POSTGRES_MAX_OVERFLOW: int = Field(default=10, ge=0, le=200)
    POSTGRES_POOL_TIMEOUT: int = Field(default=30, gt=0)
    POSTGRES_POOL_RECYCLE: int = Field(default=1800, gt=0, description="Recycle connections after N seconds.")
    POSTGRES_ECHO: bool = Field(default=False, description="Echo SQL. Never enable in production.")
    CHECKPOINT_TABLES: list[str] = Field(
        default_factory=lambda: ["checkpoint_blobs", "checkpoint_writes", "checkpoints"],
        description="LangGraph checkpoint tables purged when a session is cleared.",
    )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    LOG_DIR: Path = Field(default=Path("logs"))
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    LOG_FORMAT: Literal["json", "console"] = Field(default="json")
    LOG_TO_FILE: bool = Field(default=True, description="Disable when the platform captures stdout (k8s, ECS).")
    METRICS_ENABLED: bool = Field(default=True)
    LANGFUSE_ENABLED: bool = Field(default=False, description="Auto-enabled when both Langfuse keys are present.")
    LANGFUSE_PUBLIC_KEY: str = Field(default="")
    LANGFUSE_SECRET_KEY: SecretStr = Field(default=SecretStr(""))
    LANGFUSE_HOST: str = Field(default="https://cloud.langfuse.com")

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------
    RATE_LIMIT_ENABLED: bool = Field(default=True)
    RATE_LIMIT_DEFAULT: list[str] = Field(default_factory=lambda: ["200 per day", "50 per hour"])
    RATE_LIMIT_CHAT: list[str] = Field(default_factory=lambda: ["30 per minute"])
    RATE_LIMIT_CHAT_STREAM: list[str] = Field(default_factory=lambda: ["20 per minute"])
    RATE_LIMIT_MESSAGES: list[str] = Field(default_factory=lambda: ["50 per minute"])
    RATE_LIMIT_INVESTIGATE: list[str] = Field(default_factory=lambda: ["10 per minute"])
    RATE_LIMIT_INGEST: list[str] = Field(default_factory=lambda: ["20 per hour"])
    RATE_LIMIT_SEARCH: list[str] = Field(default_factory=lambda: ["60 per minute"])
    RATE_LIMIT_REGISTER: list[str] = Field(default_factory=lambda: ["10 per hour"])
    RATE_LIMIT_LOGIN: list[str] = Field(default_factory=lambda: ["20 per minute"])
    RATE_LIMIT_ROOT: list[str] = Field(default_factory=lambda: ["30 per minute"])
    RATE_LIMIT_HEALTH: list[str] = Field(default_factory=lambda: ["60 per minute"])

    # ------------------------------------------------------------------
    # Evaluation harness
    # ------------------------------------------------------------------
    EVALUATION_LLM: str = Field(default="gpt-4o", description="Judge model for the eval suite.")
    EVALUATION_BASE_URL: str = Field(default="https://api.openai.com/v1")
    EVALUATION_API_KEY: SecretStr = Field(default=SecretStr(""), description="Defaults to OPENAI_API_KEY when unset.")
    EVALUATION_SLEEP_TIME: int = Field(default=10, ge=0)

    # ==================================================================
    # Sources
    # ==================================================================

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Swap in sources that tolerate comma-separated list values.

        Precedence is unchanged and runs highest-first: explicit constructor
        arguments, then real environment variables, then the env file, then
        Docker/Kubernetes secret files.
        """
        return (
            init_settings,
            _LenientEnvSource(settings_cls),
            _LenientDotEnvSource(settings_cls),
            file_secret_settings,
        )

    # ==================================================================
    # Validators
    # ==================================================================

    @field_validator(
        "ALLOWED_ORIGINS",
        "FALLBACK_LLM_MODELS",
        "CHECKPOINT_TABLES",
        "RATE_LIMIT_DEFAULT",
        "RATE_LIMIT_CHAT",
        "RATE_LIMIT_CHAT_STREAM",
        "RATE_LIMIT_MESSAGES",
        "RATE_LIMIT_INVESTIGATE",
        "RATE_LIMIT_INGEST",
        "RATE_LIMIT_SEARCH",
        "RATE_LIMIT_REGISTER",
        "RATE_LIMIT_LOGIN",
        "RATE_LIMIT_ROOT",
        "RATE_LIMIT_HEALTH",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Accept ``a,b,c`` strings for list fields.

        pydantic-settings only auto-parses JSON for complex types, which makes
        ``ALLOWED_ORIGINS=["a","b"]`` the only valid form. Env files are far more
        readable with plain comma-separated values, so we normalise here.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip().strip("\"'")
        if stripped.startswith("["):  # already JSON, let pydantic handle it
            return stripped
        return [item.strip() for item in stripped.split(",") if item.strip()]

    @field_validator("API_V1_STR")
    @classmethod
    def _normalise_prefix(cls, value: str) -> str:
        """Guarantee the API prefix has a leading and no trailing slash."""
        value = "/" + value.strip().strip("/")
        return value

    @field_validator("LOG_DIR", mode="after")
    @classmethod
    def _absolutise_log_dir(cls, value: Path) -> Path:
        """Resolve a relative LOG_DIR against the project root, not the CWD."""
        return value if value.is_absolute() else PROJECT_ROOT / value

    @model_validator(mode="after")
    def apply_environment_overlay(self) -> Settings:
        """Apply per-environment defaults for fields the operator left unset.

        ``model_fields_set`` tells us which values arrived from the environment
        or an env file versus which fell back to a declared default. Only the
        latter are overlaid, so an explicit setting always wins.
        """
        overlay = _OVERLAYS.get(self.ENVIRONMENT, {})
        for key, value in overlay.items():
            if key not in self.model_fields_set:
                object.__setattr__(self, key, value)

        # Tests should not pay for 2^12 bcrypt rounds on every fixture user.
        if self.ENVIRONMENT is Environment.TEST and "BCRYPT_ROUNDS" not in self.model_fields_set:
            object.__setattr__(self, "BCRYPT_ROUNDS", 4)

        # Langfuse turns itself on only when it has usable credentials.
        if "LANGFUSE_ENABLED" not in self.model_fields_set:
            has_keys = bool(self.LANGFUSE_PUBLIC_KEY and self.LANGFUSE_SECRET_KEY.get_secret_value())
            object.__setattr__(self, "LANGFUSE_ENABLED", has_keys)

        # The eval harness shares the main API key unless given its own.
        if not self.EVALUATION_API_KEY.get_secret_value():
            object.__setattr__(self, "EVALUATION_API_KEY", self.OPENAI_API_KEY)

        return self

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> Settings:
        """Reject combinations that are individually valid but jointly broken."""
        if self.CHUNK_OVERLAP_TOKENS >= self.CHUNK_TARGET_TOKENS:
            raise ValueError(
                f"CHUNK_OVERLAP_TOKENS ({self.CHUNK_OVERLAP_TOKENS}) must be smaller than "
                f"CHUNK_TARGET_TOKENS ({self.CHUNK_TARGET_TOKENS}); otherwise chunking cannot advance."
            )
        if self.RETRIEVAL_TOP_K > self.RETRIEVAL_CANDIDATE_K:
            raise ValueError(
                f"RETRIEVAL_TOP_K ({self.RETRIEVAL_TOP_K}) cannot exceed "
                f"RETRIEVAL_CANDIDATE_K ({self.RETRIEVAL_CANDIDATE_K})."
            )
        if self.RERANK_ENABLED and self.RERANK_TOP_N < self.RETRIEVAL_TOP_K:
            raise ValueError(
                f"RERANK_TOP_N ({self.RERANK_TOP_N}) must be >= RETRIEVAL_TOP_K ({self.RETRIEVAL_TOP_K}), "
                "otherwise reranking would discard results the agent is meant to see."
            )
        return self

    @model_validator(mode="after")
    def _enforce_production_hardening(self) -> Settings:
        """Fail fast on configuration that is unsafe outside development.

        These checks only run for staging and production so local development
        stays frictionless with no secrets configured.
        """
        if not self.ENVIRONMENT.is_deployed:
            return self

        problems: list[str] = []

        secret = self.JWT_SECRET_KEY.get_secret_value()
        if len(secret) < 32:
            problems.append("JWT_SECRET_KEY must be at least 32 characters in staging/production.")
        if secret.lower() in _WEAK_SECRETS:
            problems.append("JWT_SECRET_KEY is a known placeholder value; generate a real secret.")
        if not self.OPENAI_API_KEY.get_secret_value():
            problems.append("OPENAI_API_KEY is required in staging/production.")
        if "*" in self.ALLOWED_ORIGINS:
            problems.append("ALLOWED_ORIGINS must not be '*' when credentials are allowed; list real origins.")
        if self.DEBUG:
            problems.append("DEBUG must be false in staging/production.")
        if self.POSTGRES_ECHO:
            problems.append("POSTGRES_ECHO leaks query contents into logs; disable it.")
        if self.POSTGRES_PASSWORD.get_secret_value() in _WEAK_SECRETS:
            problems.append("POSTGRES_PASSWORD is a default placeholder; set a real password.")

        if problems:
            bullets = "\n".join(f"  - {problem}" for problem in problems)
            raise ValueError(f"Refusing to start in {self.ENVIRONMENT.value} with unsafe configuration:\n{bullets}")
        return self

    # ==================================================================
    # Derived values
    # ==================================================================

    @computed_field  # type: ignore[prop-decorator]
    @property
    def postgres_dsn(self) -> str:
        """Synchronous libpq DSN (psycopg driver), used by LangGraph checkpointing."""
        from urllib.parse import quote_plus

        user = quote_plus(self.POSTGRES_USER)
        password = quote_plus(self.POSTGRES_PASSWORD.get_secret_value())
        return f"postgresql://{user}:{password}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_postgres_dsn(self) -> str:
        """SQLAlchemy async DSN (asyncpg driver), used by the ORM layer."""
        return self.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rate_limits(self) -> dict[str, list[str]]:
        """Endpoint name -> slowapi limit strings, as consumed by the API layer."""
        return {
            "chat": self.RATE_LIMIT_CHAT,
            "chat_stream": self.RATE_LIMIT_CHAT_STREAM,
            "messages": self.RATE_LIMIT_MESSAGES,
            "investigate": self.RATE_LIMIT_INVESTIGATE,
            "ingest": self.RATE_LIMIT_INGEST,
            "search": self.RATE_LIMIT_SEARCH,
            "register": self.RATE_LIMIT_REGISTER,
            "login": self.RATE_LIMIT_LOGIN,
            "root": self.RATE_LIMIT_ROOT,
            "health": self.RATE_LIMIT_HEALTH,
        }

    def rate_limit_for(self, endpoint: str) -> str:
        """Return the primary limit string for ``endpoint``.

        slowapi's decorator takes a single limit string, so we hand back the
        first configured entry and fall back to the global default.
        """
        limits = self.rate_limits.get(endpoint) or self.RATE_LIMIT_DEFAULT
        return limits[0]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def model_fallback_chain(self) -> list[str]:
        """Primary model followed by fallbacks, de-duplicated and order-preserving."""
        seen: set[str] = set()
        chain: list[str] = []
        for name in [self.DEFAULT_LLM_MODEL, *self.FALLBACK_LLM_MODELS]:
            if name and name not in seen:
                seen.add(name)
                chain.append(name)
        return chain


_WEAK_SECRETS = {
    "",
    "changeme",
    "secret",
    "password",
    "postgres",
    "aegis",
    "your-jwt-secret-key",
    "your-secret-key",
    "supersecretkeythatshouldbechangedforproduction",
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so validation runs exactly once. Tests can call
    ``get_settings.cache_clear()`` after patching the environment to force a
    reload.
    """
    return Settings()


settings = get_settings()

__all__ = ["PROJECT_ROOT", "Environment", "Settings", "get_settings", "settings"]
