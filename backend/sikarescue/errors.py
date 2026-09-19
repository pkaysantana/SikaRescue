"""Domain errors. Each maps cleanly onto an API status code later (404 / 409 / 422)."""

from __future__ import annotations


class SikaRescueError(Exception):
    """Base class for all expected domain failures."""


class NotFoundError(SikaRescueError):
    pass


class DomainInvariantError(SikaRescueError):
    """A structural financial invariant would be violated. Never recoverable by retry."""


class DuplicateEffectError(DomainInvariantError):
    """A value-moving effect with this transaction-wide key already succeeded."""


class JournalIntegrityError(DomainInvariantError):
    pass


class IllegalTransitionError(SikaRescueError):
    pass


class RecoveryPreconditionError(SikaRescueError):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class ManualReviewRequiredError(SikaRescueError):
    pass


class NoEligibleRouteError(SikaRescueError):
    pass


class ApprovalRequiredError(SikaRescueError):
    pass


class ApprovalMismatchError(SikaRescueError):
    """An approval does not authorise exactly this plan content / revision."""


class ExecutionConflictError(SikaRescueError):
    pass


class StalePlanError(SikaRescueError):
    """The plan no longer matches reality. It was marked STALE; fresh approval is required."""

    def __init__(self, plan_id: str, reasons: list[str], replacement_plan_id: str | None):
        self.plan_id = plan_id
        self.reasons = reasons
        self.replacement_plan_id = replacement_plan_id
        super().__init__(f"plan {plan_id} is stale: {'; '.join(reasons)}")


class StalePlanningResultError(SikaRescueError):
    """The transaction changed while routes were being evaluated; no plan was persisted."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__(f"planning result discarded as stale: {'; '.join(reasons)}")


class ConfigurationError(SikaRescueError):
    """Invalid or unavailable runtime configuration (e.g. an unknown compute backend)."""


class ComputeIntegrityError(SikaRescueError):
    """A compute backend's result failed independent local verification; nothing persisted."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(f"compute result rejected: {'; '.join(problems)}")


class PIILeakError(SikaRescueError):
    """Recipient PII was about to cross the model boundary."""
