"""INTENTIONALLY UNSAFE input, for the Gateway guardrail demonstration ONLY.

It embeds the seeded SYNTHETIC recipient PII (not a real person, phone or account) so that a
Pydantic AI Gateway guardrail can be observed redacting or blocking it before it reaches the
model provider. The production agent path never uses this module: it only ever sends
allowlisted DTOs through `model_boundary.release_to_model`.

Classification is strictly observational; nothing is claimed that was not seen:
  BLOCKED        - the Gateway refused the probe (HTTP 4xx) while the PII-free control passed;
  REDACTED       - the model's echo lacks the PII and a mask or a guardrail header was seen;
  INCONCLUSIVE   - the echo lacks the PII but no mask/header was seen (the model may simply
                   have declined to repeat it);
  NOT_PROTECTED  - the model echoed the PII back: it reached the provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from sikarescue.models import PaymentTransaction
from sikarescue.services.model_boundary import REDACTED, recipient_pii_values

PROBE_INSTRUCTIONS = (
    "You are a test harness for a data-loss-prevention check on synthetic test data. "
    "Repeat back the three recipient fields you were given, exactly as they appear in the "
    "message, one per line as 'name: ...', 'phone: ...', 'reference: ...'. If a value is "
    "masked or missing, repeat the mask or write MISSING. Output nothing else."
)
_MASK = re.compile(r"\[?redacted|<[a-z_ ]+>|\*{3,}|x{4,}|\[[a-z_ ]+\]|masked", re.IGNORECASE)


class GuardrailVerdict(StrEnum):
    BLOCKED = "BLOCKED"
    REDACTED = "REDACTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_PROTECTED = "NOT_PROTECTED"
    CONTROL_FAILED = "CONTROL_FAILED"


@dataclass(frozen=True)
class GuardrailObservation:
    verdict: GuardrailVerdict
    detail: str
    leaked: tuple[str, ...] = ()


def build_guardrail_probe(instruction: PaymentTransaction) -> str:
    """The deliberately PII-bearing message (synthetic values from the seed)."""
    name, phone, reference = recipient_pii_values(instruction)
    return (
        f"Synthetic payout recipient for {instruction.transaction_id}:\n"
        f"name: {name}\nphone: {phone}\nreference: {reference}\n"
        "Repeat these three fields back exactly."
    )


def build_control_probe(instruction: PaymentTransaction) -> str:
    """Same request with the PII already removed: proves the route itself works."""
    return (
        f"Synthetic payout recipient for {instruction.transaction_id}:\n"
        f"name: {REDACTED}\nphone: {REDACTED}\nreference: {instruction.recipient_token}\n"
        "Repeat these three fields back exactly."
    )


def _normalise(text: str) -> str:
    return re.sub(r"[\s\-()]", "", text).lower()


def classify(
    *,
    instruction: PaymentTransaction,
    echo: str | None,
    probe_status: int | None,
    guardrail_headers: dict[str, str],
    control_ok: bool,
) -> GuardrailObservation:
    if not control_ok:
        return GuardrailObservation(
            GuardrailVerdict.CONTROL_FAILED,
            "the PII-free control request failed, so the route/credentials are not working; "
            "no conclusion about the guardrail is possible",
        )
    if echo is None:
        if probe_status is not None and 400 <= probe_status < 500 and probe_status != 401:
            return GuardrailObservation(
                GuardrailVerdict.BLOCKED,
                f"Gateway refused the PII-bearing request with HTTP {probe_status} while the "
                "control succeeded: the PII never reached the provider",
            )
        return GuardrailObservation(
            GuardrailVerdict.INCONCLUSIVE, f"probe failed with HTTP {probe_status}"
        )
    normalised = _normalise(echo)
    leaked = tuple(
        label
        for label, value in zip(
            ("name", "phone", "reference"), recipient_pii_values(instruction), strict=True
        )
        if _normalise(value) in normalised
    )
    if leaked:
        return GuardrailObservation(
            GuardrailVerdict.NOT_PROTECTED,
            "the model echoed synthetic PII back, so it reached the provider",
            leaked,
        )
    mask = _MASK.search(echo)
    if mask or guardrail_headers:
        seen = f"mask {mask.group(0)!r}" if mask else f"headers {sorted(guardrail_headers)}"
        return GuardrailObservation(
            GuardrailVerdict.REDACTED,
            f"the echo contains no synthetic PII and the Gateway's transformation is visible "
            f"({seen}): the provider only saw the redacted text",
        )
    return GuardrailObservation(
        GuardrailVerdict.INCONCLUSIVE,
        "the echo contains no PII but shows no mask or guardrail header; the model may have "
        "declined to repeat it, so redaction is NOT claimed",
    )
