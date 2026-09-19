"""Synthetic prefunded payout balances per rail."""

from __future__ import annotations

from sikarescue.errors import DomainInvariantError, NotFoundError
from sikarescue.models import LiquidityStatus, Money, RailId


class LiquidityBook:
    def __init__(self, balances: dict[RailId, Money]):
        self._balances = dict(balances)

    def balance(self, rail_id: RailId) -> Money:
        try:
            return self._balances[rail_id]
        except KeyError:
            raise NotFoundError(f"no liquidity account for rail {rail_id}") from None

    def check(self, rail_id: RailId, required: Money) -> LiquidityStatus:
        return LiquidityStatus(rail_id=rail_id, available=self.balance(rail_id), required=required)

    def consume(self, rail_id: RailId, amount: Money) -> None:
        """Draw down prefunding after a successful payout."""
        available = self.balance(rail_id)
        if available.currency != amount.currency or available.amount < amount.amount:
            raise DomainInvariantError(f"insufficient prefunding on {rail_id}")
        self._balances[rail_id] = Money(
            amount=available.amount - amount.amount, currency=available.currency
        )

    def set_balance(self, rail_id: RailId, balance: Money) -> None:
        """Demo / test control."""
        self._balances[rail_id] = balance
