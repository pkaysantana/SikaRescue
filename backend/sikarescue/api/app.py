"""Thin demo API over the existing deterministic services. SINGLE PROCESS, ONE WORKER ONLY.

  uv run python scripts/serve_demo.py          # API + built frontend on http://127.0.0.1:8000

Every POST returns the full `DemoView`, so the UI renders backend state and never derives it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sikarescue import telemetry
from sikarescue.api.session import DemoSession
from sikarescue.api.views import DemoView, TransactionSummary
from sikarescue.config import Settings
from sikarescue.errors import (
    ComputeIntegrityError,
    NotFoundError,
    PIILeakError,
    SikaRescueError,
    StalePlanError,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


class ApproveRequest(BaseModel):
    plan_id: str = Field(pattern=r"^plan_[0-9a-f]{12}$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecuteRequest(BaseModel):
    plan_id: str = Field(pattern=r"^plan_[0-9a-f]{12}$")


class Health(BaseModel):
    status: str
    configured_compute_backend: str
    agent_mode: str
    telemetry: str
    single_worker: bool = True


def _status_code(exc: SikaRescueError) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ComputeIntegrityError | PIILeakError):
        return 500
    return 409  # the request conflicts with the transaction's current state


def create_app(
    settings: Settings | None = None,
    *,
    session: DemoSession | None = None,
    configure_telemetry: bool = True,
    frontend_dist: Path | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if configure_telemetry:
            token = settings.logfire_token
            telemetry.configure_telemetry(
                token=token.get_secret_value() if token else None,
                environment=settings.environment,
                capture_model_content=settings.logfire_capture_model_content,
            )
        yield
        telemetry.flush()

    app = FastAPI(title="SikaRescue demo API", version="0.1.0", lifespan=lifespan)
    app.state.session = session or DemoSession(settings)

    def demo(request: Request) -> DemoSession:
        return request.app.state.session

    @app.exception_handler(SikaRescueError)
    async def domain_error(request: Request, exc: SikaRescueError) -> JSONResponse:
        body: dict[str, object] = {"detail": str(exc), "error": type(exc).__name__}
        if isinstance(exc, StalePlanError):
            body["replacement_plan_id"] = exc.replacement_plan_id
        body["view"] = demo(request).view().model_dump(mode="json")
        return JSONResponse(status_code=_status_code(exc), content=body)

    @app.get("/api/health")
    async def health(request: Request) -> Health:
        session = demo(request)
        return Health(
            status="ok",
            configured_compute_backend=session.world.service.compute_backend,
            agent_mode=session.agent_mode,
            telemetry=telemetry.status().detail,
        )

    @app.get("/api/demo/status")
    async def status(request: Request) -> DemoView:
        return demo(request).view()

    @app.get("/api/demo/transaction")
    async def transaction(request: Request) -> TransactionSummary:
        return demo(request).view().transaction

    @app.post("/api/demo/reset")
    async def reset(request: Request) -> DemoView:
        with telemetry.span("demo_api_reset"):
            return await demo(request).reset()

    @app.post("/api/demo/analyse")
    async def analyse(request: Request) -> DemoView:
        with telemetry.span("demo_api_analyse"):
            return await demo(request).analyse()

    @app.post("/api/demo/approve")
    async def approve(request: Request, body: ApproveRequest) -> DemoView:
        with telemetry.span("demo_api_approve", plan_id=body.plan_id):
            return await demo(request).approve(body.plan_id, body.plan_hash)

    @app.post("/api/demo/execute")
    async def execute(request: Request, body: ExecuteRequest) -> DemoView:
        with telemetry.span("demo_api_execute", plan_id=body.plan_id):
            return await demo(request).execute(body.plan_id)

    dist = frontend_dist or Path(os.getenv("SIKARESCUE_FRONTEND_DIST", DEFAULT_FRONTEND_DIST))
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app
