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
from dataclasses import dataclass

import phonenumbers

from rn_core.errors import InvariantViolation, ValidationError

__all__ = ["LanguageTag", "PhoneNumber"]

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
