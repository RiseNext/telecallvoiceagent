"""`AudioSettings` and `TelephonySettings`, and the duplication they deliberately carry.

`rn_core` sits **below** `rn_providers` in the layer graph, so the settings module
cannot import the audio format constants it needs to validate against — a settings
package that imported a provider package would invert the architecture to save a
literal. The values are therefore written twice, and these tests are what stop the two
copies drifting.

That is the whole trade: duplication plus a test, rather than an upward import.
"""

from __future__ import annotations

import pytest

from rn_core.settings import (
    AudioSettings,
    ResamplerQualityName,
    Settings,
    TelephonySettings,
)
from rn_providers.audio.formats import SUPPORTED_RATES
from rn_providers.audio.transcoder import ResamplerQuality

pytestmark = pytest.mark.unit


def test_the_quality_names_match_the_provider_enum() -> None:
    """A preset the settings accept but the transcoder does not know would fail at
    session open, on a live call, rather than at startup."""
    assert {member.value for member in ResamplerQualityName} == {
        member.value for member in ResamplerQuality
    }


def test_every_configurable_quality_is_constructible() -> None:
    for member in ResamplerQualityName:
        assert ResamplerQuality(member.value)


def test_the_permitted_rates_match_the_audio_package() -> None:
    """ADR-003 permits exactly three configurations. Both copies must say so."""
    for rate in SUPPORTED_RATES:
        assert TelephonySettings(_env_file=None, DEFAULT_TELEPHONY_SAMPLE_RATE=rate)


def test_an_unsupported_default_rate_is_refused_at_startup() -> None:
    """A rate no component was written for produces audio that is wrong in a way nobody
    traces back to a missing branch. Refused before the process starts."""
    with pytest.raises(Exception, match="8000, 16000 or 24000"):
        TelephonySettings(_env_file=None, DEFAULT_TELEPHONY_SAMPLE_RATE=44100)


def test_the_defaults_are_the_documented_ones() -> None:
    """24 kHz removes resampling on the OpenAI path and cuts chunk accumulation from
    200 ms to 80 ms; quality starts high per ADR-003."""
    settings = Settings.for_testing()
    assert settings.telephony.default_sample_rate == 24000
    assert settings.audio.resampler_quality is ResamplerQualityName.HIGH


def test_audio_settings_read_their_documented_environment_names() -> None:
    assert AudioSettings(_env_file=None, AUDIO_RESAMPLER_QUALITY="VHQ").resampler_quality is (
        ResamplerQualityName.VERY_HIGH
    )
