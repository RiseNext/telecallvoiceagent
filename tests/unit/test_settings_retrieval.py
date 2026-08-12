"""Embedding and retrieval configuration.

The load-bearing assertion is the absence of a default. D-8 has chosen no embedding
model and no width, and a default in code is how a vendor default becomes a decision:
the first fixture written against it makes it real, and by the time anyone notices, the
width is a Postgres column type whose change costs a full re-embed plus a table rewrite
of every tenant. So both are `None` and both accessors refuse.

The alias-parity test at the bottom makes good on a claim `settings.py`'s own docstring
has been making since Phase 1 — "a test asserts every setting here appears there" —
which was true of the intent and not of the repository.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from rn_core.errors import ConfigurationError
from rn_core.settings import (
    EmbeddingSettings,
    Environment,
    IterativeScanMode,
    LogFormat,
    RetrievalSettings,
    Settings,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# EmbeddingSettings
# ---------------------------------------------------------------------------


def test_there_is_no_default_embedding_model_or_width() -> None:
    """Open decision D-8 has no default, and this is what keeps it that way."""
    embedding = EmbeddingSettings(_env_file=None)
    assert embedding.model is None
    assert embedding.dimensions is None
    assert not embedding.is_configured


def test_require_model_refuses_rather_than_guessing() -> None:
    with pytest.raises(ConfigurationError, match="D-8"):
        EmbeddingSettings(_env_file=None).require_model()


def test_require_dimensions_refuses_rather_than_guessing() -> None:
    """Deliberately does not fall back to a model's native width.

    The native width belongs to the adapter, which has it verified against primary
    documentation. A second place that knows widths is a second place that can be wrong.
    """
    embedding = EmbeddingSettings(_env_file=None, OPENAI_EMBEDDING_MODEL="text-embedding-3-small")
    with pytest.raises(ConfigurationError, match="D-8"):
        embedding.require_dimensions()


def test_a_configured_model_and_width_are_returned() -> None:
    embedding = EmbeddingSettings(
        _env_file=None,
        OPENAI_EMBEDDING_MODEL="text-embedding-3-small",
        OPENAI_EMBEDDING_DIMENSIONS=768,
        OPENAI_API_KEY="sk-x",
    )
    assert embedding.require_model() == "text-embedding-3-small"
    assert embedding.require_dimensions() == 768
    assert embedding.is_configured


@pytest.mark.parametrize("width", [0, -1, 9000])
def test_an_out_of_range_width_is_refused_by_the_field(width: int) -> None:
    with pytest.raises(PydanticValidationError):
        EmbeddingSettings(_env_file=None, OPENAI_EMBEDDING_DIMENSIONS=width)


# ---------------------------------------------------------------------------
# RetrievalSettings
# ---------------------------------------------------------------------------


def test_ef_search_defaults_far_above_the_pgvector_default() -> None:
    """HC-25: a filtered approximate scan post-filters and silently under-returns, and
    pgvector's own default of 40 is the value that makes that worst."""
    retrieval = RetrievalSettings(_env_file=None)
    assert retrieval.hnsw_ef_search > 40
    assert retrieval.hnsw_iterative_scan is IterativeScanMode.RELAXED_ORDER


def test_an_unknown_iterative_scan_mode_is_refused() -> None:
    """A typo must fail at startup, not on the first filtered query — which, because
    this GUC exists to stop silent under-returning, would fail in exactly the way it is
    meant to prevent."""
    with pytest.raises(PydanticValidationError):
        RetrievalSettings(_env_file=None, RETRIEVAL_HNSW_ITERATIVE_SCAN="fast")


def test_default_k_above_max_k_is_refused() -> None:
    """Otherwise every unqualified retrieval silently clamps, which reads as a recall
    problem rather than as a misconfiguration."""
    with pytest.raises(ConfigurationError, match="RETRIEVAL_DEFAULT_K"):
        _build(
            retrieval=RetrievalSettings(_env_file=None, RETRIEVAL_DEFAULT_K=20, RETRIEVAL_MAX_K=8)
        )


# ---------------------------------------------------------------------------
# Cross-cutting validation
# ---------------------------------------------------------------------------


def _build(**overrides: object) -> Settings:
    """Build settings the way `for_testing` does, then surface pydantic's error as ours.

    `Settings(...)` raises `PydanticValidationError`; `get_settings()` is what translates
    that into `ConfigurationError`. These tests assert on the message rather than the
    wrapper, so the translation is done here.
    """
    try:
        return Settings.for_testing(**overrides)
    except PydanticValidationError as exc:
        raise ConfigurationError(str(exc)) from exc


def test_a_model_with_no_api_key_is_refused_in_any_environment() -> None:
    """The failure mode where ingestion *appears* to work — documents upload, jobs
    enqueue — and every embed call fails."""
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY is empty"):
        _build(
            embedding=EmbeddingSettings(
                _env_file=None, OPENAI_EMBEDDING_MODEL="text-embedding-3-small"
            )
        )


def test_a_deployed_environment_requires_an_explicit_width() -> None:
    """An implicit width in a deployed environment is D-8 being answered by accident."""
    from rn_core.settings import AppSettings, ComplianceSettings, DatabaseSettings

    with pytest.raises(ConfigurationError, match="OPENAI_EMBEDDING_DIMENSIONS"):
        _build(
            app=AppSettings(
                _env_file=None,
                ENVIRONMENT=Environment.PRODUCTION,
                LOG_FORMAT=LogFormat.JSON,
            ),
            database=DatabaseSettings(
                _env_file=None,
                DATABASE_URL="postgresql+asyncpg://u:p@db.internal:5432/x",
                DATABASE_URL_DIRECT="postgresql+asyncpg://u:p@db.internal:5432/x",
            ),
            compliance=ComplianceSettings(
                _env_file=None, PHONE_HASH_PEPPER="a-real-high-entropy-value"
            ),
            embedding=EmbeddingSettings(
                _env_file=None,
                OPENAI_EMBEDDING_MODEL="text-embedding-3-small",
                OPENAI_API_KEY="sk-real-looking-value",
            ),
        )


def test_for_testing_leaves_embedding_unconfigured() -> None:
    """A test that needs to embed has to say what it is embedding with.

    A suite quietly agreeing on one width is how D-8 would get decided in a fixture.
    """
    settings = Settings.for_testing()
    assert settings.embedding.model is None
    assert settings.embedding.dimensions is None


# ---------------------------------------------------------------------------
# The claim `settings.py` has been making since Phase 1
# ---------------------------------------------------------------------------


def test_every_modelled_setting_appears_in_env_example() -> None:
    """`settings.py`: "a test asserts every setting here appears there".

    It did not, until now. The direction is deliberately one-way: `.env.example`
    documents the eventual surface including provider keys that are not modelled yet, so
    extra entries there are expected. What must never happen is a *modelled* setting
    with no documented entry — that is a setting a deployer cannot discover.
    """
    import pathlib

    from pydantic_settings import BaseSettings

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    missing: list[str] = []
    for section in Settings.model_fields.values():
        annotation = section.annotation
        if not (isinstance(annotation, type) and issubclass(annotation, BaseSettings)):
            continue
        for field in annotation.model_fields.values():
            alias = field.alias
            if alias and alias not in documented:
                missing.append(alias)

    assert not missing, (
        f"settings modelled in code but absent from .env.example: {sorted(missing)}. "
        "Adding a setting means documenting it in the same change."
    )
