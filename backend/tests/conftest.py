from __future__ import annotations

import pytest

from sikarescue.demo_data.sk10421 import TRANSACTION_ID, DemoWorld, build_demo_world


@pytest.fixture
def world() -> DemoWorld:
    return build_demo_world()


@pytest.fixture
def txn_id() -> str:
    return TRANSACTION_ID
