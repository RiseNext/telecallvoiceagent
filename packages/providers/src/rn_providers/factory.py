"""The provider factory, and the one safety interlock that guards it.

[TESTING.md §3.1](../../../../docs/TESTING.md) states the policy this module implements,
and states it as a Phase-4 deliverable:

> **The policy: fakes are environment-agnostic and pure. The production interlock lives
> at the single provider factory in the composition root**, which is the one place that
> reads `PROVIDER_MODE` and decides between a fake and a real adapter.

Three reasons the check is here and not in each fake's constructor, all from that
section and all worth keeping:

1. **A per-fake check would make constructing a fake load and validate the entire
   application configuration** — which is precisely the ambient machine state that
   `tests/conftest.py` exists to eliminate. It would trade determinism, the whole reason
   fakes exist, for a check that fires in an environment a unit test is not in.
2. **It is N places to get right instead of one**, and the one somebody forgets is the
   one that ships.
3. **The realistic failure is the *wiring* choosing a fake, not a fake being
   constructed.** A fake object is inert. A factory that hands one to the dial path is
   not, and that is where a loud refusal belongs.

**What this deliberately does not do.** It constructs no real adapter, because none
exists: the OpenAI Realtime client is Phase 5 and the Exotel client is Phase 8. Asking
for `PROVIDER_MODE=real` therefore raises a typed `ConfigurationError` naming the phase
that will supply it — a refusal that tells you what is missing, rather than an
`AttributeError` three frames deep.

**Where the composition root actually is.** Nowhere yet: `apps/` has no entrypoints, so
today nothing calls this. TESTING says the same thing — *"Today the interlock protects
nothing either way"*. It exists now so that the first entrypoint, in Phase 5, has one
obvious place to wire providers and cannot invent a second.
"""

from __future__ import annotations

from rn_core.errors import ConfigurationError
from rn_core.settings import ProviderMode, Settings
from rn_providers.audio.formats import AudioFormat
from rn_providers.fakes.realtime import FakeRealtimeProvider
from rn_providers.fakes.telephony import FakeTelephonyProvider
from rn_providers.realtime.session import VoiceSession
from rn_providers.telephony.base import TelephonyTransport

__all__ = [
    "ProviderFactory",
    "require_fakes_are_permitted",
]


def require_fakes_are_permitted(settings: Settings) -> None:
    """Refuse `PROVIDER_MODE=fake` in a deployed environment. **The interlock.**

    Loud, early, and in one place. A deployed process wired to fakes would answer real
    calls with scripted audio and record them as though they happened — a failure that
    looks like success from every dashboard, which is why it is a refusal to start
    rather than a warning.

    Raises:
        ConfigurationError: when the mode is `fake` and the environment is deployed.
    """
    if settings.providers.mode is ProviderMode.FAKE and settings.environment.is_deployed:
        raise ConfigurationError(
            "PROVIDER_MODE=fake is refused in a deployed environment. Fakes answer with "
            "scripted audio and would record calls that never happened.",
            detail={"environment": settings.environment.value},
        )


class ProviderFactory:
    """Builds provider instances for one process, honouring `PROVIDER_MODE`.

    Constructed once, at startup, by whichever app owns the process. The interlock runs
    in `__init__` rather than per call: a process configured to do the wrong thing
    should fail to start, not fail on its first phone call.
    """

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        require_fakes_are_permitted(settings)
        self._settings = settings

    @property
    def mode(self) -> ProviderMode:
        return self._settings.providers.mode

    @property
    def uses_fakes(self) -> bool:
        return self.mode is ProviderMode.FAKE

    def telephony(self, *, media_format: AudioFormat | None = None) -> TelephonyTransport:
        """A telephony transport.

        Args:
            media_format: The negotiated rate. Defaults to the configured per-agent
                fallback — note ADR-003 pins the real rate to the **agent version**, so
                this default only applies where no agent has spoken yet.
        """
        fmt = media_format or AudioFormat(rate_hz=self._settings.telephony.default_sample_rate)
        if self.uses_fakes:
            return FakeTelephonyProvider(media_format=fmt)
        raise self._not_yet(seam="TelephonyProvider", phase="Phase 8 (telephony inbound)")

    def realtime(self) -> VoiceSession:
        """A realtime voice session."""
        if self.uses_fakes:
            return FakeRealtimeProvider()
        raise self._not_yet(seam="VoiceSession", phase="Phase 5 (realtime voice prototype)")

    def _not_yet(self, *, seam: str, phase: str) -> ConfigurationError:
        return ConfigurationError(
            f"PROVIDER_MODE=real was requested but no {seam} adapter exists yet; "
            f"it arrives in {phase}.",
            detail={"seam": seam, "mode": self.mode.value},
        )
