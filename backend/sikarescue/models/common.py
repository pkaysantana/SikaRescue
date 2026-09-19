"""Shared Pydantic primitives: base models, money, identifiers, clock."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from sikarescue.models.enums import Currency


class DomainModel(BaseModel):
    """Immutable, closed record. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


TransactionId = Annotated[str, StringConstraints(pattern=r"^SK-\d{5}$")]
# Generated identifiers: `<prefix>_<12 hex>`, e.g. `plan_1a2b3c4d5e6f`.
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,5}_[0-9a-f]{12}$")]
RouteId = Annotated[str, StringConstraints(pattern=r"^rt_[A-Z0-9_]+$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
OpaqueToken = Annotated[str, StringConstraints(pattern=r"^[a-z]{3}_[0-9a-z]{6,32}$")]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
FxRate = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=6)]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Money(DomainModel):
    """A monetary amount. Decimal only: floats are rejected to avoid binary rounding."""

    amount: Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
    currency: Currency

    @field_validator("amount", mode="before")
    @classmethod
    def _reject_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("money amounts must be Decimal/str/int, never float")
        return value

    @classmethod
    def of(cls, amount: str | int | Decimal, currency: Currency) -> Money:
        return cls(amount=amount, currency=currency)

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency.value}"
