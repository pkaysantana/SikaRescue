"""The terminal demo runs the complete seeded SK-10421 story without an LLM."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from sikarescue.cli.demo import run_demo
from sikarescue.demo_data.sk10421 import build_demo_world
from sikarescue.models import AttemptOutcome, OperationType, RailId, RecoveryState

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _run(world=None, *, auto_approve=True, answer=None):
    world = world or build_demo_world()
    out = io.StringIO()

    def fake_input(prompt: str) -> str:
        if answer is None:
            raise EOFError
        return answer

    outcome = await run_demo(world, auto_approve=auto_approve, input_fn=fake_input, out=out)
    return outcome, out.getvalue()


async def test_cli_flow_completes_and_reconciles(txn_id):
    outcome, text = await _run()
    assert outcome.exit_code == 0
    assert outcome.reconciliation is not None and outcome.reconciliation.reconciled
    assert outcome.world.repository.get(txn_id).state is RecoveryState.RECONCILED
    for expected in (
        "DEFINITIVE_FAILED",
        "Funds currently located: GH_SETTLEMENT_ACCOUNT",
        "do NOT restart from origin",
        "TOKEN_BRIDGE      REJECTED: POLICY_DENIED",
        "MOMO_A            REJECTED: RAIL_UNAVAILABLE / FAILED_EARLIER_FOR_TRANSACTION",
        "recipient_name      : [REDACTED]",
        "PII scan of model payload: clean",
        "recipient credited       : yes",
        "duplicate sender debits  : 0",
        "state                    : RECONCILED",
        "compute backend          : local",
    ):
        assert expected in text, expected


async def test_cli_journal_has_exactly_one_debit_and_one_credit(txn_id):
    outcome, _ = await _run()
    journal = outcome.world.repository.get(txn_id).journal
    assert journal.count_effects(OperationType.SENDER_DEBIT) == 1
    assert journal.count_effects(OperationType.RECIPIENT_CREDIT) == 1
    assert [r.rail_id for r in outcome.world.gateway.requests] == [RailId.MOMO_B]


async def test_cli_interactive_approval(txn_id):
    outcome, text = await _run(auto_approve=False, answer="y")
    assert outcome.exit_code == 0
    assert "Operator answered: approve" in text


async def test_cli_interactive_decline_moves_no_money(txn_id):
    outcome, text = await _run(auto_approve=False, answer="n")
    assert outcome.exit_code == 1
    assert outcome.world.repository.get(txn_id).state is RecoveryState.MANUAL_REVIEW
    assert outcome.world.gateway.requests == []
    assert "recipient credited       : no" in text


async def test_cli_without_input_leaves_plan_awaiting_approval(txn_id):
    outcome, text = await _run(auto_approve=False, answer=None)
    assert outcome.exit_code == 2
    assert outcome.world.repository.get(txn_id).state is RecoveryState.AWAITING_APPROVAL
    assert "--approve" in text and outcome.world.gateway.requests == []


async def test_cli_unknown_payout_cannot_auto_recover(txn_id):
    world = build_demo_world()
    world.gateway.script(RailId.MOMO_B, AttemptOutcome.UNKNOWN)
    outcome, text = await _run(world)
    assert outcome.exit_code == 1
    state = world.service.get_transaction_state(txn_id)
    assert state.recovery_state is RecoveryState.MANUAL_REVIEW
    assert state.recipient_credit_count == 0
    assert len(world.gateway.requests) == 1  # no automatic retry or reroute
    assert "No automatic retry or reroute" in text


def _run_script(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ | {"SIKARESCUE_PAYOUT_LATENCY_SECONDS": "0", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "demo_recovery.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )


def test_script_end_to_end_with_approve_flag():
    result = _run_script("--approve", "--no-timeline")
    assert result.returncode == 0, result.stderr
    assert "state                    : RECONCILED" in result.stdout
    assert "sender debit count       : 1" in result.stdout
    assert "recipient credit count   : 1" in result.stdout


def test_script_interactive_prompt_accepts_typed_answer():
    result = _run_script(stdin="y\n")
    assert result.returncode == 0, result.stderr
    assert "Operator answered: approve" in result.stdout
    assert "Audit timeline" in result.stdout
