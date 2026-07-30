"""Guardrails enforced in code, not only asked of the model.

Phase 2 delivers the two that are enforceable as pure functions over a transcript:
AI-disclosure detection and multilingual opt-out recognition. Both are honest about
what they are:

* **Disclosure detection is detective, not preventive.** Generated speech cannot be
  constrained token by token. Absence is a compliance finding and a hard failure in
  evaluation — the strongest control available, and not the same thing as
  prevention.
* **Opt-out recognition is not opt-out enforcement.** The matcher fires
  independently of what the model does; writing the durable `suppressions` row and
  blocking future dialling is Phase 9.

The guardrails that *are* structural live elsewhere, deliberately: tenant scoping in
the repository layer, tool authorization in `rn_agent.tools.dispatch`, and the
enabled tool list in the session configuration. Where a rule can be made structural
it is, and the prompt is a courtesy (AGENT_ARCHITECTURE §6).
"""

from rn_agent.guardrails.disclosure import (
    DisclosureFinding,
    DisclosureKind,
    detect_disclosure,
    first_disclosing_turn,
    has_ai_disclosure,
)
from rn_agent.guardrails.optout import (
    OptOutFinding,
    OptOutLanguage,
    detect_opt_out,
    is_opt_out,
)

__all__ = [
    "DisclosureFinding",
    "DisclosureKind",
    "OptOutFinding",
    "OptOutLanguage",
    "detect_disclosure",
    "detect_opt_out",
    "first_disclosing_turn",
    "has_ai_disclosure",
    "is_opt_out",
]
