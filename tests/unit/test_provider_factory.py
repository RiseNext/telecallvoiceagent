"""The provider factory and its production interlock.

[TESTING.md §3.1](../../docs/TESTING.md) assigns both to Phase 4 and states the policy
exactly: *"fakes are environment-agnostic and pure. The production interlock lives at
the single provider factory in the composition root."*

The interlock is the only thing standing between `PROVIDER_MODE=fake` and a deployed
process that answers real phone calls with scripted audio and records them as though
they happened — a failure that looks like success from every dashboard, which is why it
is a refusal to start rather than a warning.
"""

from __future__ import annotations

import pytest

from rn_core.errors import ConfigurationError
from rn_core.settings import (
    AppSettings,
    ComplianceSettings,
    DatabaseSettings,
    Environment,
    LogFormat,
    ProviderMode,
    ProviderSettings,
    Settings,
)
from rn_providers.factory import ProviderFactory, require_fakes_are_permitted
from rn_providers.fakes.realtime import FakeRealtimeProvider
from rn_providers.fakes.telephony import FakeTelephonyProvider

pytestmark = pytest.mark.unit


def _deployed(mode: ProviderMode) -> Settings:
    """A settings object that is valid for a deployed environment except for the mode.

    Every other deployed-environment rule is satisfied deliberately: otherwise the
    coherence validators fire first and the test would pass without the interlock
    existing at all.
    """
    return Settings.for_testing(
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
        providers=ProviderSettings(_env_file=None, PROVIDER_MODE=mode.value),
    )


# ---------------------------------------------------------------------------
# The interlock
# ---------------------------------------------------------------------------


def test_fakes_are_refused_in_a_deployed_environment() -> None:
    """**The interlock.** A deployed process on fakes is a silent, total product failure."""
    with pytest.raises(ConfigurationError, match="PROVIDER_MODE=fake is refused"):
        require_fakes_are_permitted(_deployed(ProviderMode.FAKE))


def test_the_refusal_happens_at_construction_not_at_first_call() -> None:
    """A process configured to do the wrong thing must fail to start, not fail on its
    first phone call — by which point a caller is already on the line."""
    with pytest.raises(ConfigurationError):
        ProviderFactory(_deployed(ProviderMode.FAKE))


def test_real_mode_is_permitted_in_a_deployed_environment() -> None:
    """The interlock is about fakes, not about deployment. Real mode may deploy."""
    require_fakes_are_permitted(_deployed(ProviderMode.REAL))


def test_fakes_are_permitted_everywhere_else() -> None:
    for environment in (Environment.LOCAL, Environment.TEST, Environment.CI):
        settings = Settings.for_testing(app=AppSettings(_env_file=None, ENVIRONMENT=environment))
        require_fakes_are_permitted(settings)


def test_fake_is_the_default_mode() -> None:
    """A fresh clone must work with no credentials at all — and the one environment
    where that default is dangerous refuses to start with it."""
    assert Settings.for_testing().providers.mode is ProviderMode.FAKE


# ---------------------------------------------------------------------------
# What it builds
# ---------------------------------------------------------------------------


def test_the_factory_builds_the_documented_fakes() -> None:
    factory = ProviderFactory(Settings.for_testing())

    assert factory.uses_fakes
    assert isinstance(factory.telephony(), FakeTelephonyProvider)
    assert isinstance(factory.realtime(), FakeRealtimeProvider)


def test_the_telephony_fake_defaults_to_the_configured_rate() -> None:
    """ADR-003 pins the real rate to the **agent version**; this default only applies
    where no agent has spoken yet."""
    factory = ProviderFactory(Settings.for_testing())
    transport = factory.telephony()
    assert transport.media_format.rate_hz == 24000


def test_real_mode_refuses_by_naming_the_phase_that_will_supply_the_adapter() -> None:
    """A refusal that says what is missing beats an `AttributeError` three frames deep.

    Neither adapter exists yet — realtime is Phase 5, telephony is Phase 8 — and the
    factory is where that fact is stated once rather than discovered per caller.
    """
    factory = ProviderFactory(
        Settings.for_testing(providers=ProviderSettings(_env_file=None, PROVIDER_MODE="real"))
    )

    with pytest.raises(ConfigurationError, match="Phase 8"):
        factory.telephony()
    with pytest.raises(ConfigurationError, match="Phase 5"):
        factory.realtime()


def test_the_factory_is_the_only_module_that_reads_the_mode() -> None:
    """One switch, one reader. A per-seam check is N places to get right instead of one,
    and the one somebody forgets is the one that ships."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "packages"
    readers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "PROVIDER_MODE" in path.read_text(encoding="utf-8")
        or "providers.mode" in path.read_text(encoding="utf-8")
    )
    assert readers == [
        "core/src/rn_core/settings.py",
        "providers/src/rn_providers/factory.py",
    ], f"PROVIDER_MODE is read in more than one place: {readers}"
