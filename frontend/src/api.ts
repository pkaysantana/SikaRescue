import type { DemoView } from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly view: DemoView | null;

  constructor(message: string, code: string, view: DemoView | null) {
    super(message);
    this.code = code;
    this.view = view;
  }
}

async function request(path: string, init?: RequestInit): Promise<DemoView> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(
      "Can't reach the SikaRescue API. Start it with: uv run python scripts/serve_demo.py",
      "NetworkError",
      null,
    );
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof body?.detail === "string"
        ? body.detail
        : `The API answered ${response.status} ${response.statusText}.`;
    throw new ApiError(detail, body?.error ?? `HTTP ${response.status}`, body?.view ?? null);
  }
  return body as DemoView;
}

const post = (path: string, payload?: unknown) =>
  request(path, { method: "POST", body: payload === undefined ? undefined : JSON.stringify(payload) });

export const api = {
  status: () => request("/api/demo/status"),
  reset: () => post("/api/demo/reset"),
  analyse: () => post("/api/demo/analyse"),
  approve: (planId: string, planHash: string) =>
    post("/api/demo/approve", { plan_id: planId, plan_hash: planHash }),
  execute: (planId: string) => post("/api/demo/execute", { plan_id: planId }),
};
