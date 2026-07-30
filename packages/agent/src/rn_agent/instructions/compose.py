"""Instruction composition. Deterministic, ordered, byte-stable.

Four layers, strictly decreasing authority and strictly increasing volatility
(AGENT_ARCHITECTURE §5):

    1. PLATFORM   code, this repository, composed first          -> prefix
    2. ORG        company identity, tone, escalation policy       -> prefix
    3. AGENT      role, objective, script hints, tool guidance    -> prefix
    4. PER CALL   caller, campaign, language, time                -> rendered live

Layers 1-3 are composed **once, at publish**, and stored on the snapshot. That is
not a stylistic split. The composed prefix is byte-identical across every call on
an agent version, which is the precondition for provider prompt caching — and the
cached/fresh spread on realtime audio input is roughly 80x (AGENT_ARCHITECTURE §5),
so a prefix that varies per call is a first-order cost defect, not an untidiness.

**There is no override parameter, and there never will be.** `compose_instruction_prefix`
takes the tenant layers and nothing else; layer 1 is not addressable from its
signature. A test asserts that — the signature itself is the guarantee, so the
signature is what is tested.

Tenant text is **untrusted configuration**: bounded, stripped of control
characters, and never interpolated into layer 1.
"""

from __future__ import annotations

import re

from rn_agent.errors import AgentConfigurationError
from rn_agent.instructions.platform import PLATFORM_INSTRUCTIONS

__all__ = [
    "AGENT_LAYER_HEADING",
    "MAX_LAYER_CHARS",
    "ORG_LAYER_HEADING",
    "compose_instruction_prefix",
    "sanitize_tenant_text",
]

#: Per-layer character ceiling.
#:
#: A bound is required: instructions are the longest thing in every request, they
#: are charged per token, and an unbounded field is a way for one tenant to make
#: every one of their calls expensive and slow. 20,000 characters is far more than
#: any sensible agent brief and far less than a pasted document.
MAX_LAYER_CHARS = 20_000

#: Headings, not delimiters.
#:
#: They orient the model ("this part is the company, this part is my role") and
#: they make a composed prefix readable in a diff when someone is debugging why an
#: agent behaves oddly. They are deliberately NOT relied on as a boundary: tenant
#: instructions are semi-trusted configuration authored by the account owner, not
#: hostile input from a caller. Genuinely hostile content — retrieved documents,
#: caller speech — is fenced by `instructions.render.untrusted_block`, which does
#: neutralise its own fence.
ORG_LAYER_HEADING = "## About this business"
AGENT_LAYER_HEADING = "## Your role on this call"

#: Control characters have no place in prompt text and several have effects:
#: a NUL can truncate a string at a provider boundary, and bidirectional overrides
#: can make a reviewed instruction display differently from how it is read.
#: Newline, carriage return and tab survive; everything else in the C0/C1 ranges
#: and the Unicode bidi controls (U+202A..U+202E, U+2066..U+2069) does not.
#:
#: Written as escapes rather than literals on purpose. A literal bidi override in
#: source is invisible to a reviewer — which is exactly the property that makes it
#: worth removing from prompt text, and exactly why it must not be in this file.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

#: Three or more blank lines collapse to one blank line, so that the prefix from a
#: dashboard textarea is byte-stable regardless of how the author spaced it.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def sanitize_tenant_text(text: str, *, field: str) -> str:
    """Normalise one tenant-authored layer.

    Raises:
        AgentConfigurationError: if the text exceeds `MAX_LAYER_CHARS`. Rejected
            rather than truncated: silently cutting an agent's instructions in half
            produces behaviour nobody authored and nobody can explain.
    """
    if len(text) > MAX_LAYER_CHARS:
        raise AgentConfigurationError(
            "Agent instruction layer is longer than the permitted maximum.",
            detail={"field": field, "length": len(text), "maximum": MAX_LAYER_CHARS},
        )
    cleaned = _CONTROL_CHARS.sub("", text)
    # Normalise line endings before collapsing blank lines, so that CRLF from a
    # Windows paste and LF from an API call compose to identical bytes.
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _EXCESS_BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()


def compose_instruction_prefix(
    *,
    organization_layer: str | None,
    agent_layer: str,
) -> str:
    """Compose instruction layers 1-3 into the stored prefix.

    Pure and deterministic: the same inputs produce byte-identical output, with no
    clock, no identifiers and no per-call content anywhere in it.

    Note what is *absent* from this signature. There is no `platform_layer`
    parameter, no `override_platform`, no `skip_safety`. Layer 1 is a module
    constant referenced directly below, so no caller — including a future one that
    would find it convenient — can supply, suppress or reorder it.

    Args:
        organization_layer: Layer 2, tenant-authored. **Nothing stores this yet**;
            organization-level instructions arrive with organization settings in a
            later phase. The parameter exists so the ordering is implemented and
            tested now rather than retrofitted into an already-composed prefix.
        agent_layer: Layer 3, from `AgentVersion.instructions`.
    """
    sections = [PLATFORM_INSTRUCTIONS.strip()]

    if organization_layer is not None:
        cleaned = sanitize_tenant_text(organization_layer, field="organization_instructions")
        if cleaned:
            sections.append(f"{ORG_LAYER_HEADING}\n\n{cleaned}")

    cleaned_agent = sanitize_tenant_text(agent_layer, field="agent_instructions")
    if not cleaned_agent:
        raise AgentConfigurationError(
            "Agent instructions are empty after normalisation.",
            detail={"field": "agent_instructions"},
        )
    sections.append(f"{AGENT_LAYER_HEADING}\n\n{cleaned_agent}")

    return "\n\n".join(sections)
