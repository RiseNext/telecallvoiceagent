"""Regenerate the audio golden files. **Run deliberately, never casually.**

    uv run python -m tests.fixtures.regenerate_audio_goldens

A golden diff is a signal that the resampler's output changed — a soxr upgrade, a
quality-preset edit, a refactor that reordered the filter. Regenerating without first
understanding *why* it changed converts that signal into silence, which is the one thing
a golden file exists to prevent. Read the diff, decide the change is intended, then run
this and say so in the commit message.

The generator deliberately imports the test module's own helpers rather than
reimplementing the tone and the framing. Two copies of that arithmetic would eventually
disagree, and the golden would then encode the generator's version of the signal rather
than the one the test feeds in.
"""

from __future__ import annotations

import sys

from tests.unit.test_audio_transcoder import (
    GOLDEN_DIR,
    GOLDEN_MS,
    GOLDEN_PAIRS,
    GOLDEN_TONE_HZ,
    golden_name,
    tone,
    transcode_whole,
)


def main() -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for source, target in GOLDEN_PAIRS:
        produced = transcode_whole(
            source, target, tone(source, hz=GOLDEN_TONE_HZ, milliseconds=GOLDEN_MS)
        )
        path = GOLDEN_DIR / golden_name(source, target)
        path.write_bytes(produced)
        sys.stdout.write(f"wrote {path.name} ({len(produced)} bytes)\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - developer entry point
    raise SystemExit(main())
