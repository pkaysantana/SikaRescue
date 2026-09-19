"""Logfire telemetry for the recovery workflow: spans, events and trace correlation.

Rules:
  * Telemetry NEVER breaks recovery. Every call is guarded; a telemetry failure is swallowed
    (logged at DEBUG) and the financial flow continues unchanged.
  * Only sanitised scalar attributes are emitted: opaque ids, enums, counts and timings. Never
    recipient PII, raw provider payloads or credentials. `PII_SCRUB_PATTERNS` make Logfire's
    scrubber a second line of defence.
  * Nothing is emitted until `configure_telemetry()` succeeds, so the core stays silent in
    library use and unit tests.
Traces are exported only when a Logfire token (or local Logfire credentials) is present.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import metadata
from typing import Any

from sikarescue.models import AuditEvent, AuditEventType, RecoveryState

logger = logging.getLogger(__name__)

SERVICE_NAME = "sikarescue"

# Defence in depth: Logfire scrubs attribute keys/values matching these (case-insensitive).
PII_SCRUB_PATTERNS = (
    r"recipient[._ -]?(name|phone|reference)",
    r"ama\s*mensah",
    r"\+?233[\s-]?\d{2}[\s-]?\d{3}[\s-]?\d{4}",
    r"GH-\d{4}-SYNTH-\d{4}",
)

# Audit timeline -> telemetry event names used in the Logfire dashboard.
_AUDIT_EVENT_NAMES = {
    AuditEventType.DIAGNOSIS_COMPLETED: "transaction_reconstructed",
    AuditEventType.ROUTES_EVALUATED: "route_evaluations_verified",
    AuditEventType.COMPUTE_FALLBACK: "compute_fallback_used",
    AuditEventType.RECOVERY_PLAN_CREATED: "recovery_plan_created",
    AuditEventType.RECOVERY_ADVICE_GENERATED: "recovery_advice_generated",
    AuditEventType.APPROVAL_REQUESTED: "approval_requested",
    AuditEventType.APPROVAL_GRANTED: "approval_received",
    AuditEventType.APPROVAL_DENIED: "approval_received",
    AuditEventType.EXECUTION_STARTED: "execution_admitted",
    AuditEventType.EXECUTION_SUCCEEDED: "recipient_credit_recorded",
    AuditEventType.RECONCILED: "reconciliation_completed",
}
_WARN_EVENTS = frozenset(
    {
        AuditEventType.COMPUTE_FALLBACK,
        AuditEventType.PLANNING_RESULT_DISCARDED,
        AuditEventType.PLAN_MARKED_STALE,
        AuditEventType.EXECUTION_FAILED,
        AuditEventType.EXECUTION_OUTCOME_UNKNOWN,
        AuditEventType.MANUAL_REVIEW_REQUIRED,
        AuditEventType.RECONCILIATION_FAILED,
    }
)


@dataclass(frozen=True)
class TelemetryStatus:
    enabled: bool  # spans/events are being created
    exporting: bool  # ... and sent to a Logfire project
    detail: str
    project_url: str | None = None


_status = TelemetryStatus(enabled=False, exporting=False, detail="not configured")


def status() -> TelemetryStatus:
    return _status


def _version() -> str:
    try:
        return metadata.version("sikarescue")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def configure_telemetry(
    *,
    token: str | None = None,
    environment: str = "dev",
    capture_model_content: bool = True,
    configure_logfire: bool = True,
) -> TelemetryStatus:
    """Configure Logfire + Pydantic AI instrumentation. Never raises.

    `configure_logfire=False` reuses an already-configured Logfire (e.g. the `capfire` fixture).
    """
    global _status
    try:
        import logfire

        if configure_logfire:
            logfire.configure(
                send_to_logfire="if-token-present",
                token=token,
                service_name=SERVICE_NAME,
                service_version=_version(),
                environment=environment,
                console=False,
                inspect_arguments=False,
                scrubbing=logfire.ScrubbingOptions(extra_patterns=list(PII_SCRUB_PATTERNS)),
            )
        logfire.instrument_pydantic_ai(include_content=capture_model_content)
        config = logfire.DEFAULT_LOGFIRE_INSTANCE.config
        exporting = configure_logfire and bool(getattr(config, "token", None))
        project_url = getattr(config, "_project_url", None) if exporting else None
    except Exception as exc:
        _status = TelemetryStatus(
            enabled=False,
            exporting=False,
            detail=f"Logfire unavailable ({type(exc).__name__}); recovery continues untraced",
        )
        return _status
    detail = (
        "exporting to Logfire"
        if exporting
        else "local spans only (no LOGFIRE_TOKEN: nothing is exported)"
    )
    _status = TelemetryStatus(
        enabled=True, exporting=exporting, detail=detail, project_url=project_url
    )
    return _status


def reset_telemetry() -> None:
    """Stop emitting (tests). Leaves Logfire's own global configuration alone."""
    global _status
    _status = TelemetryStatus(enabled=False, exporting=False, detail="not configured")
    with suppress(Exception):
        from pydantic_ai import Agent

        Agent.instrument_all(False)


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return {k: _scalar(v) for k, v in attributes.items() if v is not None}


class SpanHandle:
    """Lets the caller add attributes to an open span. A no-op when telemetry is off."""

    def __init__(self) -> None:
        self._span: Any = None

    def set(self, **attributes: Any) -> None:
        if self._span is None:
            return
        try:
            for key, value in _attributes(attributes).items():
                self._span.set_attribute(key, value)
        except Exception:
            logger.debug("telemetry: set_attribute failed", exc_info=True)

    @property
    def trace_id(self) -> str | None:
        try:
            context = self._span.get_span_context() if self._span is not None else None
        except Exception:
            return None
        return format(context.trace_id, "032x") if context and context.is_valid else None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[SpanHandle]:
    """A Logfire span that can never raise into, or swallow errors from, the wrapped code."""
    handle = SpanHandle()
    manager: Any = None
    if _status.enabled:
        try:
            import logfire

            manager = logfire.span(name, _span_name=name, **_attributes(attributes))
            handle._span = manager.__enter__()
        except Exception:
            manager = None
            logger.debug("telemetry: span %s failed to open", name, exc_info=True)
    try:
        yield handle
    except BaseException as exc:
        if manager is not None:
            with suppress(Exception):
                manager.__exit__(type(exc), exc, exc.__traceback__)
        raise
    if manager is not None:
        with suppress(Exception):
            manager.__exit__(None, None, None)


def event(name: str, *, level: str = "info", **attributes: Any) -> None:
    if not _status.enabled:
        return
    try:
        import logfire

        logfire.log(level, name, attributes=_attributes(attributes))  # type: ignore[arg-type]
    except Exception:
        logger.debug("telemetry: event %s failed", name, exc_info=True)


def audit_event_name(audit: AuditEvent) -> str:
    if (
        audit.event_type is AuditEventType.STATE_TRANSITION
        and audit.data.get("to_state") == RecoveryState.DIAGNOSING.value
    ):
        return "incident_received"
    if audit.event_type is AuditEventType.COMPUTE_FALLBACK and (
        audit.data.get("fallback_from") == "modal"
    ):
        return "modal_fallback_used"
    return _AUDIT_EVENT_NAMES.get(audit.event_type, f"audit_{audit.event_type.value.lower()}")


def record_audit_event(audit: AuditEvent) -> None:
    """Mirror an (already sanitised) audit timeline entry as a Logfire event."""
    if not _status.enabled:
        return
    try:
        attributes: dict[str, Any] = {
            "transaction_id": audit.transaction_id,
            "transaction_revision": audit.revision,
            "state": audit.recovery_state.value,
            "audit_sequence": audit.sequence,
            "audit_event_type": audit.event_type.value,
            "actor": audit.actor.value,
            "summary": audit.summary,
        }
        attributes.update({f"audit_{k}": v for k, v in audit.data.items()})
        level = "warn" if audit.event_type in _WARN_EVENTS else "info"
        event(audit_event_name(audit), level=level, **attributes)
    except Exception:
        logger.debug("telemetry: audit mirror failed", exc_info=True)


def current_trace_id() -> str | None:
    """Hex trace id of the active span (for 'View trace' links), or None."""
    if not _status.enabled:
        return None
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
    except Exception:
        return None
    return format(context.trace_id, "032x") if context.is_valid else None


def flush(timeout_millis: int = 5000) -> None:
    if not _status.enabled:
        return
    with suppress(Exception):
        import logfire

        logfire.force_flush(timeout_millis=timeout_millis)
