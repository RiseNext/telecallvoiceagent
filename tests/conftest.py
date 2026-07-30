"""Shared test configuration.

Tests must not depend on a developer's machine. No `.env` is read, no local
database is assumed, and no test touches a container it did not create.
"""

from __future__ import annotations

import pytest

from rn_core.settings import Settings, reset_settings_cache


@pytest.fixture(autouse=True)
def _isolated_settings_cache() -> object:
    """Clear the cached settings around every test.

    `get_settings()` is `lru_cache`d for the life of a process, which is right in
    production and wrong in a suite where one test changes the environment. This
    makes that cache per-test rather than per-process.
    """
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings built from explicit values.

    Deliberately not `get_settings()`: that reads `.env` and the process
    environment, which is exactly the hidden machine state that makes a suite
    pass locally and fail in CI.
    """
    return Settings.for_testing()
