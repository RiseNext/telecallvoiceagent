"""Instruction composition: the four layers, and the untrusted-content boundary."""

from rn_agent.instructions.compose import (
    AGENT_LAYER_HEADING,
    MAX_LAYER_CHARS,
    ORG_LAYER_HEADING,
    compose_instruction_prefix,
    sanitize_tenant_text,
)
from rn_agent.instructions.platform import PLATFORM_INSTRUCTIONS
from rn_agent.instructions.render import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    CallInstructionContext,
    render_call_instructions,
    untrusted_block,
)

__all__ = [
    "AGENT_LAYER_HEADING",
    "MAX_LAYER_CHARS",
    "ORG_LAYER_HEADING",
    "PLATFORM_INSTRUCTIONS",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "CallInstructionContext",
    "compose_instruction_prefix",
    "render_call_instructions",
    "sanitize_tenant_text",
    "untrusted_block",
]
