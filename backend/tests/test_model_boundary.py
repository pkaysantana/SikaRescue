"""Allowlisted model-facing views never carry recipient PII."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sikarescue.demo_data.sk10421 import (
    RECIPIENT_NAME,
    RECIPIENT_PHONE,
    RECIPIENT_REFERENCE,
    RECIPIENT_TOKEN,
)
from sikarescue.errors import PIILeakError
from sikarescue.models import FundsLocation, ModelTransactionView, RejectionReason
from sikarescue.services.model_boundary import (
    REDACTED,
    assert_no_recipient_pii,
    build_model_route_views,
    build_model_transaction_view,
    redacted_recipient_preview,
)

PII = (RECIPIENT_NAME, RECIPIENT_PHONE, RECIPIENT_REFERENCE)


def test_model_transaction_view_has_no_pii(world, txn_id):
    aggregate = world.repository.get(txn_id)
    view = build_model_transaction_view(aggregate)
    payload = view.model_dump_json()
    for value in PII:
        assert value not in payload
    assert_no_recipient_pii(payload, aggregate.instruction)
    assert view.recipient_token == RECIPIENT_TOKEN
    assert view.funds_location is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert view.failure_summary == "HTTP 503 PROVIDER_UNAVAILABLE (pre-acceptance)"
    assert view.safe_to_restart_from_origin is False


def test_model_view_rejects_added_pii_fields(world, txn_id):
    view = build_model_transaction_view(world.repository.get(txn_id))
    with pytest.raises(ValidationError):
        ModelTransactionView(**view.model_dump(), recipient_name=RECIPIENT_NAME)


async def test_route_views_are_serialisable_and_pii_free(world, txn_id):
    views = build_model_route_views(await world.service.evaluate_recovery_routes(txn_id))
    token = next(v for v in views if v.rail_id == "TOKEN_BRIDGE")
    assert token.rejection_reasons == (RejectionReason.POLICY_DENIED,)
    payload = "".join(v.model_dump_json() for v in views)
    assert_no_recipient_pii(payload, world.repository.get(txn_id).instruction)


@pytest.mark.parametrize(
    "payload",
    [
        '{"note": "pay Ama Mensah"}',
        '{"msisdn": "+233205550142"}',  # whitespace-stripped phone still caught
        '{"ref": "gh-9821-synth-0042"}',  # case-insensitive
    ],
)
def test_defence_in_depth_guard_catches_leaks(world, txn_id, payload):
    with pytest.raises(PIILeakError):
        assert_no_recipient_pii(payload, world.repository.get(txn_id).instruction)


def test_secretstr_masks_pii_in_dumps_and_repr(world, txn_id):
    instruction = world.repository.get(txn_id).instruction
    for rendered in (instruction.model_dump_json(), repr(instruction), str(instruction)):
        for value in PII:
            assert value not in rendered


def test_redacted_preview_for_ui(world, txn_id):
    preview = redacted_recipient_preview(world.repository.get(txn_id).instruction)
    assert preview["raw"]["recipient_name"] == RECIPIENT_NAME
    boundary = preview["model_boundary"]
    assert {boundary[k] for k in ("recipient_name", "recipient_phone", "recipient_reference")} == {
        REDACTED
    }
