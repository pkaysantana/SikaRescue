"""Run the SikaRescue demo API, plus the built frontend if present, in ONE process / ONE worker.

The repository, journal and locks are in-memory, so this must never run with >1 worker.

  npm --prefix frontend install && npm --prefix frontend run build     # once
  uv run python scripts/serve_demo.py --compute modal --agent pydantic  # http://127.0.0.1:8000

Frontend development instead: run this, then `npm --prefix frontend run dev` (port 5173).
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="SikaRescue demo server (single worker)")
    parser.add_argument("--compute", choices=("local", "modal"), help="override compute backend")
    parser.add_argument(
        "--agent", choices=("deterministic", "pydantic"), help="override advice orchestrator"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.compute:
        os.environ["SIKARESCUE_COMPUTE_BACKEND"] = args.compute
    if args.agent:
        os.environ["SIKARESCUE_AGENT_MODE"] = args.agent
    try:
        import pydantic_ai
        import uvicorn

        from sikarescue.api.app import DEFAULT_FRONTEND_DIST, create_app
        from sikarescue.config import Settings
    except ImportError:
        sys.exit("Run inside the project environment:  uv run python scripts/serve_demo.py")

    pydantic_ai.BANNER_ENABLED = False
    settings = Settings()
    app = create_app(settings)
    frontend = (DEFAULT_FRONTEND_DIST / "index.html").is_file()
    print(f"compute backend : {settings.compute_backend} ({settings.effective_workload} workload)")
    print(f"agent mode      : {settings.agent_mode}")
    print(
        f"frontend        : {'served from frontend/dist' if frontend else 'not built (API only)'}"
    )
    print(f"open            : http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
