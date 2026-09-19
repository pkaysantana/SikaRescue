"""Modal app: parallel synthetic route-simulation shards and systemic-outage allocation.

CPU only, no GPU.

Deploy:   uv run modal deploy -m sikarescue.compute.modal_app
Invoked by `ModalRouteComputeBackend` via `modal.Function.from_name(...).map.aio(...)`.

The worker only runs the pure simulation kernel on the inputs it is given. It never sees
policy, liquidity, money or transaction state, and it cannot change them: the calling
application re-verifies everything it returns.

`allocate_outage_scenario` runs the systemic-outage allocation kernel for ONE scenario. It
receives eligibility masks already decided by local policy, never the policy itself, and the
caller re-verifies every assignment and recomputes every metric.
"""

from __future__ import annotations

import modal
import pydantic

from sikarescue.compute.outage import OUTAGE_FUNCTION_NAME
from sikarescue.compute.shards import MODAL_APP_NAME, MODAL_FUNCTION_NAME

# Cost guard-rails: a demo run is ~5 CPU-seconds in total; cap burst width and idle time.
MAX_CONTAINERS = 24
SCALEDOWN_WINDOW_SECONDS = 120

image = (
    modal.Image.debian_slim(python_version="3.12")
    # Pin the exact pydantic the client uses so both sides validate identically.
    .uv_pip_install(f"pydantic=={pydantic.VERSION}")
    .add_local_python_source("sikarescue")
)

app = modal.App(MODAL_APP_NAME, image=image)


@app.function(
    name=MODAL_FUNCTION_NAME,
    cpu=1.0,
    memory=256,
    timeout=120,
    max_containers=MAX_CONTAINERS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
)
def simulate_shard(spec: dict) -> dict:
    """Run one shard (route x scenario x trial range) of the synthetic simulation."""
    from sikarescue.compute.shards import ShardSpec, run_shard

    return run_shard(ShardSpec.model_validate(spec)).model_dump()


@app.function(
    name=OUTAGE_FUNCTION_NAME,
    cpu=1.0,
    memory=512,
    timeout=120,
    max_containers=8,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
)
def allocate_outage_scenario(spec: dict) -> dict:
    """Allocate one systemic-outage scenario across its (pre-filtered) fallback rails."""
    from sikarescue.compute.outage import AllocationSpec, allocate

    return allocate(AllocationSpec.model_validate(spec)).model_dump(mode="json")
