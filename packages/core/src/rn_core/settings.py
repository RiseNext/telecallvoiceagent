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
    "Environment",
    "LogFormat",
    "ObservabilitySettings",
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


class Settings(_Base):
    """The composed application configuration."""

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    compliance: ComplianceSettings = Field(default_factory=ComplianceSettings)

    @property
    def environment(self) -> Environment:
        return self.app.environment

    @model_validator(mode="after")
    def _validate_for_environment(self) -> Self:
        """Refuse configuration that is unsafe for the declared environment.

        This is the single place environment-specific rules live. Everything else
        in the codebase reads settings without asking where it is running.
        """
        env = self.app.environment
        problems: list[str] = []

        if self.compliance.calling_window_start == self.compliance.calling_window_end:
            problems.append(
                "CALLING_WINDOW_START and CALLING_WINDOW_END are identical, "
                "which permits no calling time at all."
            )

        if env.is_deployed:
            for label, secret in (
                ("DATABASE_URL", self.database.url),
                ("DATABASE_URL_DIRECT", self.database.url_direct),
                ("PHONE_HASH_PEPPER", self.compliance.phone_hash_pepper),
            ):
                value = secret.get_secret_value()
                lowered = value.lower()
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
