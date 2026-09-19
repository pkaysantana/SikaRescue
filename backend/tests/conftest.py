from __future__ import annotations

import pydantic_ai
import pytest
from pydantic_ai import models

from sikarescue import telemetry
from sikarescue.demo_data.sk10421 import TRANSACTION_ID, DemoWorld, build_demo_world

# The unit suite never talks to a real model: only FunctionModel / TestModel may run.
models.ALLOW_MODEL_REQUESTS = False
pydantic_ai.BANNER_ENABLED = False


@pytest.fixture
def world() -> DemoWorld:
    return build_demo_world()


@pytest.fixture
def txn_id() -> str:
    return TRANSACTION_ID


@pytest.fixture(autouse=True)
def _telemetry_off():
    """Each test starts and ends with telemetry disabled (tests opt in explicitly)."""
    telemetry.reset_telemetry()
    yield
    telemetry.reset_telemetry()
