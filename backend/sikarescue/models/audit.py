"""Human-readable audit timeline (narrative), separate from the financial journal (evidence)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from sikarescue.models.common import DomainModel, EntityId, TransactionId
from sikarescue.models.enums import Actor, AuditEventType, RecoveryState

AuditValue = str | int | float | bool | None


class AuditEvent(DomainModel):
    event_id: EntityId
    transaction_id: TransactionId
    sequence: int = Field(ge=1)
    timestamp: datetime
    event_type: AuditEventType
    actor: Actor
    summary: str = Field(max_length=280)
    revision: int = Field(ge=0)
    recovery_state: RecoveryState
    # Sanitised scalar facts only. Never recipient PII or raw provider payloads.
    data: dict[str, AuditValue] = Field(default_factory=dict)
