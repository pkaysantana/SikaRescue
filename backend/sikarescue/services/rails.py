"""Synthetic rail registry: rail metadata, availability and quotes."""

from __future__ import annotations

from collections.abc import Iterable

from sikarescue.errors import NotFoundError
from sikarescue.models import FundsLocation, Rail, RailId, RailQuote, RailStatus
from sikarescue.models.enums import PAYOUT_RAIL_TYPES


class RailRegistry:
    def __init__(self, rails: Iterable[Rail], quotes: Iterable[RailQuote]):
        self._rails = {r.rail_id: r for r in rails}
        self._quotes = {q.rail_id: q for q in quotes}

    def get(self, rail_id: RailId) -> Rail:
        try:
            return self._rails[rail_id]
        except KeyError:
            raise NotFoundError(f"unknown rail {rail_id}") from None

    def all(self) -> tuple[Rail, ...]:
        return tuple(self._rails.values())

    def payout_rails_from(self, source: FundsLocation) -> tuple[Rail, ...]:
        return tuple(
            r
            for r in self._rails.values()
            if r.rail_type in PAYOUT_RAIL_TYPES and r.payout_source is source
        )

    def quote(self, rail_id: RailId) -> RailQuote:
        try:
            return self._quotes[rail_id]
        except KeyError:
            raise NotFoundError(f"no quote for rail {rail_id}") from None

    # --- demo / test controls: simulate the outside world changing -----------------------

    def set_status(self, rail_id: RailId, status: RailStatus) -> None:
        self._rails[rail_id] = Rail.model_validate(
            self.get(rail_id).model_dump() | {"status": status}
        )

    def replace_rail(self, rail: Rail) -> None:
        self._rails[rail.rail_id] = Rail.model_validate(rail.model_dump())

    def replace_quote(self, quote: RailQuote) -> None:
        self._quotes[quote.rail_id] = RailQuote.model_validate(quote.model_dump())
