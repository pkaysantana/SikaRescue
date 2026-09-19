"""Synthetic corridor policy. Illustrative rules only, NOT real compliance policy."""

from __future__ import annotations

from sikarescue.models import (
    FundsLocation,
    Money,
    PaymentTransaction,
    PolicyDecision,
    PolicyRuleResult,
    Rail,
    RailType,
)

POLICY_VERSION = "synthetic-2026.09"


class PolicyEngine:
    def __init__(
        self,
        corridor_rail_types: dict[str, frozenset[RailType]],
        max_payout: Money,
        version: str = POLICY_VERSION,
    ):
        self._corridor_rail_types = corridor_rail_types
        self._max_payout = max_payout
        self.version = version

    def set_allowed_rail_types(self, corridor: str, rail_types: frozenset[RailType]) -> None:
        """Demo / test control: simulate a policy change."""
        self._corridor_rail_types = {**self._corridor_rail_types, corridor: rail_types}

    def check(
        self, transaction: PaymentTransaction, rail: Rail, source: FundsLocation, amount: Money
    ) -> PolicyDecision:
        corridor = transaction.corridor
        allowed = self._corridor_rail_types.get(corridor, frozenset())
        rules = (
            PolicyRuleResult(
                rule_id="POL-001",
                passed=rail.rail_type in allowed,
                detail=(
                    f"{rail.rail_type} approved for {corridor}"
                    if rail.rail_type in allowed
                    else f"{rail.rail_type} is not an approved payout rail type for {corridor}"
                ),
            ),
            PolicyRuleResult(
                rule_id="POL-002",
                passed=source is not FundsLocation.SENDER_ACCOUNT,
                detail="recovery never re-debits the sender (no restart from origin)",
            ),
            PolicyRuleResult(
                rule_id="POL-003",
                passed=(
                    amount.currency == self._max_payout.currency
                    and amount.amount <= self._max_payout.amount
                ),
                detail=f"payout within corridor limit of {self._max_payout}",
            ),
        )
        return PolicyDecision(
            rail_id=rail.rail_id,
            permitted=all(r.passed for r in rules),
            policy_version=self.version,
            rule_results=rules,
        )
