"""Typed application configuration.

One object, validated once, at startup. A process that cannot build valid
settings **must not start** — a service running with half its configuration is
worse than one that refused to boot, because it fails later, partially, and
usually while handling a real call.

Design notes:

* **Flat environment variable names**, matching `.env.example` exactly. The
  example file is the documented config surface and the two must not drift; a
  test asserts every setting here appears there.
* **Secrets are `SecretStr`.** They do not render in `repr()`, in tracebacks, or
  in a structlog event. DSNs count as secrets — they carry a password.
* **Environment-aware validation instead of environment branching.** There is no
  `if ENVIRONMENT == "production"` scattered through the codebase; there is one
  validator here that refuses unsafe values when deployed. Application code just
  reads settings.
* **Only what Phase 1 uses is modelled.** `.env.example` documents the eventual
  surface including provider keys; those become settings in the phase that calls
  the provider. Modelling unused configuration invites code that reads it.
"""

from __future__ import annotations

import functools
from datetime import time
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, SecretStr, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from rn_core.errors import ConfigurationError

__all__ = [
    "AppSettings",
    "ComplianceSettings",
    "DatabaseSettings",
    "EmbeddingSettings",
    "Environment",
    "IterativeScanMode",
    "LogFormat",
    "ObservabilitySettings",
    "RetrievalSettings",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]

# Values that are fine locally and must never reach a deployed environment.
_PLACEHOLDER_MARKERS = ("replace_me", "replace-me", "changeme", "your_", "xxx")


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    CI = "ci"
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_deployed(self) -> bool:
        """Runs on shared infrastructure with real data and real credentials."""
        return self in {Environment.DEV, Environment.STAGING, Environment.PRODUCTION}

    @property
    def is_ephemeral(self) -> bool:
        """A throwaway database is acceptable and placeholder secrets are fine."""
        return self in {Environment.TEST, Environment.CI}


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )


class AppSettings(_Base):
    environment: Environment = Field(default=Environment.LOCAL, alias="ENVIRONMENT")
    service_name: str = Field(default="rn-api", alias="SERVICE_NAME")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: LogFormat = Field(default=LogFormat.CONSOLE, alias="LOG_FORMAT")
    debug: bool = Field(default=False, alias="DEBUG")


class DatabaseSettings(_Base):
    """Two DSNs, deliberately.

    `url` is the pooled endpoint used by all ordinary application traffic. Under
    transaction-mode pooling it cannot hold session state: no session-level
    `SET`, no `LISTEN/NOTIFY`, no session advisory locks, no server-side
    `PREPARE`. Use `SET LOCAL` inside an explicit transaction instead.

    `url_direct` bypasses the pooler. It is for migrations, index builds, and
    (later) the scheduler's advisory-lock leader lease — anything that genuinely
    needs a session. Locally both point at the same Postgres; the split exists so
    that the *call sites* are already correct when a pooler appears in front of
    a managed database.

    See PROVIDER_CONSTRAINTS HC-26 and ADR-006.
    """

    url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://risenext:risenext@localhost:5432/risenext"),
        alias="DATABASE_URL",
    )
    url_direct: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://risenext:risenext@localhost:5432/risenext"),
        alias="DATABASE_URL_DIRECT",
    )
    pool_size: int = Field(default=10, ge=1, le=100, alias="DATABASE_POOL_SIZE")
    max_overflow: int = Field(default=5, ge=0, le=100, alias="DATABASE_MAX_OVERFLOW")
    pool_timeout_seconds: float = Field(
        default=10.0, gt=0, le=120, alias="DATABASE_POOL_TIMEOUT_SECONDS"
    )
    # Recycle below typical pooler/proxy idle timeouts so the application never
    # hands out a connection the far end has already closed.
    pool_recycle_seconds: int = Field(
        default=1800, ge=60, le=7200, alias="DATABASE_POOL_RECYCLE_SECONDS"
    )
    statement_timeout_ms: int = Field(
        default=15_000, ge=100, le=600_000, alias="DATABASE_STATEMENT_TIMEOUT_MS"
    )
    echo_sql: bool = Field(default=False, alias="DATABASE_ECHO_SQL")

    @property
    def max_connections(self) -> int:
        """Ceiling this process can open. Sizing input, not a promise."""
        return self.pool_size + self.max_overflow


class ObservabilitySettings(_Base):
    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_service_name: str | None = Field(default=None, alias="OTEL_SERVICE_NAME")
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0, alias="OTEL_TRACES_SAMPLER_ARG")


class ComplianceSettings(_Base):
    """Regulatory parameters — configuration, never constants.

    The permitted calling window could not be confirmed from any official source
    (PRD **D-4**; PROVIDER_CONSTRAINTS anti-fact 11 records two different windows
    in secondary sources). The values here are placeholders that operations can
    change without a deploy. Hardcoding a regulatory constant is how a platform
    ends up non-compliant in a jurisdiction it did not anticipate.
    """

    calling_window_start: time = Field(default=time(9, 0), alias="CALLING_WINDOW_START")
    calling_window_end: time = Field(default=time(21, 0), alias="CALLING_WINDOW_END")
    calling_window_timezone: str = Field(default="Asia/Kolkata", alias="CALLING_WINDOW_TIMEZONE")
    enforce_consent_gate: bool = Field(default=True, alias="ENFORCE_CONSENT_GATE")
    enforce_dnd_check: bool = Field(default=True, alias="ENFORCE_DND_CHECK")
    #: Peppers the deterministic phone hash used by `suppressions` and
    #: `consent_records`. Without it a hashed blocklist is trivially reversible
    #: by enumerating the ~10^9 valid Indian mobile numbers.
    phone_hash_pepper: SecretStr = Field(
        default=SecretStr("local-development-pepper-not-for-deployment"),
        alias="PHONE_HASH_PEPPER",
    )


class IterativeScanMode(StrEnum):
    """pgvector's `hnsw.iterative_scan` values.

    Modelled as an enum so a typo is a configuration failure at startup rather than
    an unrecognised GUC value discovered on the first filtered query — which,
    because this GUC exists to *stop* silent under-returning (HC-25), would fail in
    exactly the way it is meant to prevent.
    """

    OFF = "off"
    STRICT_ORDER = "strict_order"
    RELAXED_ORDER = "relaxed_order"


class EmbeddingSettings(_Base):
    """The embedding provider's configuration.

    **There is deliberately no default model and no default width.** Open decision
    **D-8** has not chosen either (ADR-010), and a default here is precisely how a
    vendor default becomes a de facto decision: the first fixture written against it
    makes it real, and the width then becomes part of a Postgres column type where
    changing it costs a full re-embed plus a table rewrite of every tenant.

    So both are `None` until someone states them, and `require_model()` /
    `require_dimensions()` refuse rather than guess. `.env.example` carries an
    explicit development placeholder with that warning attached; this module does
    not repeat it as a code default.
    """

    api_key: SecretStr = Field(default=SecretStr(""), alias="OPENAI_API_KEY")
    model: str | None = Field(default=None, alias="OPENAI_EMBEDDING_MODEL")
    dimensions: int | None = Field(default=None, ge=1, le=8192, alias="OPENAI_EMBEDDING_DIMENSIONS")
    base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    request_timeout_seconds: float = Field(
        default=30.0, gt=0, le=300, alias="OPENAI_REQUEST_TIMEOUT_SECONDS"
    )

    @property
    def is_configured(self) -> bool:
        """Whether an embedding call could be attempted at all."""
        return bool(self.model) and bool(self.api_key.get_secret_value().strip())

    def require_model(self) -> str:
        if not self.model:
            raise ConfigurationError(
                "No embedding model is configured. This is open decision D-8 and it "
                "has no default — set OPENAI_EMBEDDING_MODEL explicitly.",
            )
        return self.model

    def require_dimensions(self) -> int:
        """The configured width, or a refusal.

        Refuses rather than falling back to a model's native width: the native width
        belongs to the adapter, which has it verified against primary documentation,
        and a second place that knows widths is a second place that can be wrong.
        """
        if self.dimensions is None:
            raise ConfigurationError(
                "No embedding width is configured. This is open decision D-8 and it "
                "has no default — set OPENAI_EMBEDDING_DIMENSIONS explicitly.",
                detail={"model": self.model},
            )
        return self.dimensions


class RetrievalSettings(_Base):
    """Vector-retrieval tuning. Consumed by the single retrieval helper.

    Every value here is a **starting default, not a measurement** — D-8 has measured
    nothing yet. They are configuration so that operations can tune retrieval
    without a deploy, which matters because the symptom of getting them wrong is
    "the agent forgot our knowledge base" rather than an error.

    `hnsw_ef_search` deliberately defaults far above pgvector's own 40: HC-25 says a
    filtered approximate scan post-filters and silently under-returns, and 40 is the
    value that makes that worst. `hnsw_iterative_scan` defaults to `relaxed_order`
    for the same reason. Both are issued as `SET LOCAL` inside the helper's
    transaction, because transaction-mode pooling forbids session-level `SET`
    (HC-26) — and note that whether a pooler honours `SET LOCAL` for
    `hnsw.iterative_scan` specifically is **still unverified**
    (PROVIDER_CONSTRAINTS §6a-35); we have no pooler to test against until D-1.
    """

    default_k: int = Field(default=4, ge=1, le=50, alias="RETRIEVAL_DEFAULT_K")
    max_k: int = Field(default=16, ge=1, le=50, alias="RETRIEVAL_MAX_K")
    #: Over-fetch multiplier. The helper asks for `k * factor` and trims after
    #: post-filtering, so a row removed by a status or model filter does not silently
    #: reduce what the agent gets to answer from.
    overfetch_factor: int = Field(default=2, ge=1, le=10, alias="RETRIEVAL_OVERFETCH_FACTOR")
    hnsw_ef_search: int = Field(default=200, ge=1, le=1000, alias="RETRIEVAL_HNSW_EF_SEARCH")
    hnsw_iterative_scan: IterativeScanMode = Field(
        default=IterativeScanMode.RELAXED_ORDER, alias="RETRIEVAL_HNSW_ITERATIVE_SCAN"
    )


class Settings(_Base):
    """The composed application configuration."""

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    compliance: ComplianceSettings = Field(default_factory=ComplianceSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)

    @property
    def environment(self) -> Environment:
        return self.app.environment

    def _coherence_problems(self) -> list[str]:
        """Configuration that is self-contradictory in *any* environment.

        Split out from the deployed-only rules so each set reads as one rule list
        rather than as one long function with a mode switch in the middle.
        """
        problems: list[str] = []

        if self.compliance.calling_window_start == self.compliance.calling_window_end:
            problems.append(
                "CALLING_WINDOW_START and CALLING_WINDOW_END are identical, "
                "which permits no calling time at all."
            )

        # A default above the ceiling would make every unqualified retrieval silently
        # clamp, which presents as a recall problem rather than as a misconfiguration
        # — the same class of quiet failure as HC-25 itself.
        if self.retrieval.default_k > self.retrieval.max_k:
            problems.append(
                "RETRIEVAL_DEFAULT_K exceeds RETRIEVAL_MAX_K, so the default request "
                "could never be served in full."
            )

        # A model configured with no usable key is the failure mode where ingestion
        # *appears* to work — documents upload, jobs enqueue — and every embed call
        # fails. Better to refuse at boot.
        if self.embedding.model and not self.embedding.api_key.get_secret_value().strip():
            problems.append(
                "OPENAI_EMBEDDING_MODEL is set but OPENAI_API_KEY is empty, so no "
                "embedding call could succeed."
            )

        return problems

    def _deployed_problems(self) -> list[str]:
        """Configuration that is unsafe specifically on shared infrastructure."""
        problems: list[str] = []

        if self.embedding.model and self.embedding.dimensions is None:
            # A width that is implicit in a deployed environment is a width nobody
            # wrote down, and it is the single thing D-8 exists to pin.
            problems.append(
                "OPENAI_EMBEDDING_DIMENSIONS must be set explicitly in a deployed "
                "environment; an implicit embedding width is open decision D-8 being "
                "answered by accident."
            )

        for label, secret in (
            ("DATABASE_URL", self.database.url),
            ("DATABASE_URL_DIRECT", self.database.url_direct),
            ("PHONE_HASH_PEPPER", self.compliance.phone_hash_pepper),
            ("OPENAI_API_KEY", self.embedding.api_key),
        ):
            lowered = secret.get_secret_value().lower()
            if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
                problems.append(f"{label} still contains a placeholder value.")
            if "localhost" in lowered or "127.0.0.1" in lowered:
                problems.append(f"{label} points at localhost.")
            if label == "PHONE_HASH_PEPPER" and "local-development" in lowered:
                problems.append(
                    "PHONE_HASH_PEPPER is the development default; a deployed "
                    "environment needs a unique high-entropy value. Changing it "
                    "later invalidates every stored phone hash."
                )

        if self.app.debug:
            problems.append("DEBUG must be false in a deployed environment.")
        if self.app.log_format is not LogFormat.JSON:
            problems.append(
                "LOG_FORMAT must be 'json' in a deployed environment so logs are "
                "machine-ingestible and redaction is uniform."
            )
        if not self.compliance.enforce_consent_gate:
            problems.append(
                "ENFORCE_CONSENT_GATE must not be disabled in a deployed "
                "environment — it is the pre-dial compliance gate."
            )
        if not self.compliance.enforce_dnd_check:
            problems.append("ENFORCE_DND_CHECK must not be disabled in a deployed environment.")

        return problems

    @model_validator(mode="after")
    def _validate_for_environment(self) -> Self:
        """Refuse configuration that is unsafe for the declared environment.

        This is the single place environment-specific rules live. Everything else
        in the codebase reads settings without asking where it is running.
        """
        problems = self._coherence_problems()
        if self.app.environment.is_deployed:
            problems.extend(self._deployed_problems())

        if problems:
            raise ValueError("; ".join(problems))
        return self

    @classmethod
    def for_testing(cls, **overrides: Any) -> Settings:
        """Build settings from explicit values, ignoring `.env` and the process env.

        Tests must not depend on a developer's local `.env` — that is exactly the
        hidden machine state that makes a suite pass here and fail in CI.
        """
        defaults: dict[str, Any] = {
            "app": AppSettings(_env_file=None, ENVIRONMENT=Environment.TEST),
            "database": DatabaseSettings(_env_file=None),
            "observability": ObservabilitySettings(_env_file=None),
            "compliance": ComplianceSettings(_env_file=None),
            # No model and no width by default, so a test that needs to embed has to
            # say what it is embedding with. A test suite quietly agreeing on one
            # width is how D-8 would get decided in a fixture.
            "embedding": EmbeddingSettings(_env_file=None),
            "retrieval": RetrievalSettings(_env_file=None),
        }
        defaults.update(overrides)
        return cls(_env_file=None, **defaults)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process.

    Raises `ConfigurationError` — not pydantic's error — so that callers depend
    on our taxonomy and startup failures are reported uniformly. The underlying
    validation detail is attached for logs; note that pydantic redacts
    `SecretStr` values in its own error rendering, so the message is safe.
    """
    try:
        return Settings()
    except PydanticValidationError as exc:
        raise ConfigurationError(
            "Invalid application configuration. The process cannot start.",
            detail={"errors": exc.errors(include_url=False, include_input=False)},
        ) from exc
    except ValueError as exc:
        raise ConfigurationError(
            "Invalid application configuration. The process cannot start.",
            detail={"error": str(exc)},
        ) from exc


def reset_settings_cache() -> None:
    """Clear the cached settings. Test-support only."""
    get_settings.cache_clear()
