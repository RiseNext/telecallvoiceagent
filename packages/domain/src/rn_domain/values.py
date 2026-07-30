"""Value objects.

Immutable, validated at construction, comparable by value. If one of these
exists, it is valid — that is the entire point, and it removes the "is this
string a phone number yet?" question from every layer above.

`phonenumbers` is used here despite the domain-purity rule. It is pure
computation with no I/O, and E.164 correctness for an India-first dialler is a
domain invariant rather than an infrastructure detail: a malformed number that
reaches the telephony adapter has already cost a failed call.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import phonenumbers

from rn_core.errors import InvariantViolation, ValidationError

__all__ = ["LanguagePolicy", "LanguageTag", "PhoneNumber"]

#: India. Numbers without a country code are interpreted in this region.
_DEFAULT_REGION = "IN"

#: BCP-47-ish: `en`, `hi-IN`, `te-IN`. Deliberately permissive on the subtag —
#: we validate shape, not membership of a registry we would have to vendor.
_LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-[A-Z]{2})?$")


@dataclass(frozen=True, slots=True, order=True)
class PhoneNumber:
    """A validated E.164 phone number.

    Construct via `parse`; the constructor trusts its input so that rows loaded
    from the database (already validated on the way in) do not pay for
    re-parsing on every read.

    **Never log `e164`.** Use `masked`. `rn_core.redaction` masks phone-shaped
    strings anyway, but relying on that as the only defence means a change to the
    regex becomes a data leak.
    """

    e164: str

    def __post_init__(self) -> None:
        if not self.e164.startswith("+") or not self.e164[1:].isdigit():
            raise InvariantViolation(
                "PhoneNumber must hold an E.164 value.",
                detail={"length": len(self.e164)},
            )

    @classmethod
    def parse(cls, raw: str, *, region: str = _DEFAULT_REGION) -> PhoneNumber:
        """Parse and validate user- or file-supplied input.

        Raises `ValidationError` — this is a boundary, and a bad number in a CSV
        row is a row to reject, not a crash. The error deliberately carries no
        copy of the input: a rejected value is still a phone number.
        """
        try:
            parsed = phonenumbers.parse(raw, region)
        except phonenumbers.NumberParseException as exc:
            raise ValidationError(
                "Not a valid phone number.", detail={"reason": exc.error_type}
            ) from exc
        if not phonenumbers.is_valid_number(parsed):
            raise ValidationError("Not a valid phone number.", detail={"reason": "invalid"})
        return cls(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))

    @classmethod
    def try_parse(cls, raw: str, *, region: str = _DEFAULT_REGION) -> PhoneNumber | None:
        """Parse, returning `None` instead of raising.

        For bulk import, where the useful outcome is "these 12 rows were
        rejected", not an exception on row 3.
        """
        try:
            return cls.parse(raw, region=region)
        except ValidationError:
            return None

    @property
    def country_code(self) -> int:
        """The E.164 country calling code.

        Re-parses rather than caching: the value object is frozen with slots, and
        this is used at import/reporting time, not per audio frame.
        """
        code = phonenumbers.parse(self.e164, None).country_code
        if code is None:  # pragma: no cover - unreachable for a validated E.164
            raise InvariantViolation("A validated E.164 number must carry a country code.")
        return code

    @property
    def masked(self) -> str:
        """`+919876543210` -> `+91XXXXXXXX10`. The only form safe for a log."""
        digits = self.e164[1:]
        head, tail = digits[:2], digits[-2:]
        return f"+{head}{'X' * max(len(digits) - 4, 1)}{tail}"

    def hashed(self, pepper: str) -> str:
        """Deterministic peppered hash, for suppression and consent lookup.

        HMAC-SHA256 rather than a bare digest: the phone-number space is ~10^9
        for Indian mobiles, so an unpeppered hash is reversible by enumeration in
        seconds. The pepper is passed in rather than read from configuration
        because the domain must not depend on settings — and because it makes the
        dependency visible at every call site.

        Rotating the pepper invalidates every stored hash. It is effectively part
        of the schema.
        """
        if not pepper:
            raise InvariantViolation("A phone hash requires a non-empty pepper.")
        return hmac.new(pepper.encode(), self.e164.encode(), hashlib.sha256).hexdigest()

    def __str__(self) -> str:
        """Masked, so that an accidental f-string cannot leak a number."""
        return self.masked

    def __repr__(self) -> str:
        return f"PhoneNumber({self.masked})"


@dataclass(frozen=True, slots=True)
class LanguageTag:
    """A BCP-47-style language tag: `en`, `hi-IN`, `te-IN`.

    A call has no single language — code-mixed speech is the norm (PRD §5.2), so
    these are collected as a set per call rather than one field.
    """

    value: str

    def __post_init__(self) -> None:
        if not _LANGUAGE_TAG_RE.match(self.value):
            raise ValidationError("Malformed language tag.", detail={"value": self.value})

    @property
    def primary(self) -> str:
        """The base language, ignoring region: `hi-IN` -> `hi`."""
        return self.value.split("-")[0]

    def __str__(self) -> str:
        return self.value


#: Storage keys for `LanguagePolicy`. Frozen: these are the JSONB keys on
#: `agent_versions.language_policy` and a rename is a migration, not a refactor.
_POLICY_PRIMARY = "primary"
_POLICY_ALLOWED = "allowed"
_POLICY_FOLLOW_CALLER = "follow_caller"
_POLICY_CODE_SWITCH = "code_switch"


@dataclass(frozen=True, slots=True)
class LanguagePolicy:
    """How one agent version handles language. Per-agent, never a global constant.

    **This is the single source of truth for an agent version's languages.**
    `agent_versions.languages` is a denormalised *projection* of `allowed`, kept
    so that "which agents speak Telugu?" is an indexable array query rather than a
    JSONB scan. The projection is not independently authored:

    * In the domain there is only one field — `AgentVersion.languages` is a
      read-only property over `language_policy.allowed`, so the two cannot be set
      to different values.
    * In the database a CHECK asserts
      `to_jsonb(languages) IS NOT DISTINCT FROM language_policy -> 'allowed'`,
      so a row where they disagree cannot exist, whichever writer produced it.

    A call has no single language — code-mixed Indian speech is the normal case
    (PRD §5.2) — so `follow_caller` and `code_switch` default to permissive. The
    honest caveat is PRD **D-2**: whether Telugu speech-to-speech is good enough
    to promise is settled by our own evaluation, not by this type existing.
    """

    primary: LanguageTag
    allowed: tuple[LanguageTag, ...]
    #: Switch to the caller's language mid-call.
    follow_caller: bool = True
    #: Allow mixing languages inside one utterance.
    code_switch: bool = True

    def __post_init__(self) -> None:
        if not self.allowed:
            raise InvariantViolation("A language policy must allow at least one language.")
        if len(set(self.allowed)) != len(self.allowed):
            raise InvariantViolation(
                "A language policy must not list a language twice.",
                detail={"allowed": [tag.value for tag in self.allowed]},
            )
        if self.primary not in self.allowed:
            raise InvariantViolation(
                "The primary language must be one of the allowed languages.",
                detail={
                    "primary": self.primary.value,
                    "allowed": [tag.value for tag in self.allowed],
                },
            )

    @classmethod
    def single(cls, tag: LanguageTag | str) -> LanguagePolicy:
        """A monolingual policy. Mostly for tests and for the simplest agents."""
        resolved = tag if isinstance(tag, LanguageTag) else LanguageTag(tag)
        return cls(primary=resolved, allowed=(resolved,))

    @classmethod
    def from_storage(cls, raw: Mapping[str, Any]) -> LanguagePolicy:
        """Parse the JSONB representation. **This is the translation boundary.**

        Nothing above this method ever sees `dict[str, Any]`: the runtime works
        with a typed, immutable policy, and a malformed stored value fails here
        with a typed error rather than surfacing as an `AttributeError` three
        layers up.

        Raises:
            ValidationError: if the stored shape is wrong. Distinct from
                `InvariantViolation` on purpose — a bad row read at a boundary is
                a validation problem, not a broken domain rule.
        """
        allowed_raw = raw.get(_POLICY_ALLOWED)
        primary_raw = raw.get(_POLICY_PRIMARY)
        if not isinstance(allowed_raw, list) or not all(isinstance(t, str) for t in allowed_raw):
            raise ValidationError(
                "Stored language policy has a malformed 'allowed' list.",
                detail={"type": type(allowed_raw).__name__},
            )
        if not isinstance(primary_raw, str):
            raise ValidationError(
                "Stored language policy has a malformed 'primary' tag.",
                detail={"type": type(primary_raw).__name__},
            )
        return cls(
            primary=LanguageTag(primary_raw),
            allowed=tuple(LanguageTag(tag) for tag in allowed_raw),
            follow_caller=_require_bool(raw, _POLICY_FOLLOW_CALLER),
            code_switch=_require_bool(raw, _POLICY_CODE_SWITCH),
        )

    def to_storage(self) -> dict[str, Any]:
        """The JSONB representation.

        Key order is fixed and `allowed` preserves declaration order, because this
        value is compared byte-for-byte against the `languages` projection by a
        database CHECK, and it feeds a snapshot content hash.
        """
        return {
            _POLICY_PRIMARY: self.primary.value,
            _POLICY_ALLOWED: [tag.value for tag in self.allowed],
            _POLICY_FOLLOW_CALLER: self.follow_caller,
            _POLICY_CODE_SWITCH: self.code_switch,
        }

    @property
    def projection(self) -> list[str]:
        """`allowed` in the exact form the `languages` column stores."""
        return [tag.value for tag in self.allowed]


def _require_bool(raw: Mapping[str, Any], key: str) -> bool:
    """Read a required boolean flag.

    Deliberately not `bool(raw.get(key))`: a stored `"false"` string would then
    read as `True`, which is the wrong answer for a policy flag that governs
    whether the agent may switch languages on a caller.
    """
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValidationError(
            "Stored language policy flag is missing or not a boolean.",
            detail={"key": key, "type": type(value).__name__},
        )
    return value
