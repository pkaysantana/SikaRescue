import type { DemoView, OutageView } from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly view: DemoView | null;

  constructor(message: string, code: string, view: DemoView | null) {
    super(message);
    this.code = code;
    this.view = view;
  }
}

async function request<T = DemoView>(path: string, init?: RequestInit): Promise<T> {
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
  return body as T;
}

const post = <T = DemoView>(path: string, payload?: unknown) =>
  request<T>(path, {
    method: "POST",
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });

export const api = {
  status: () => request("/api/demo/status"),
  reset: (scenario?: string) =>
    post("/api/demo/reset", scenario === undefined ? undefined : { scenario }),
  classify: () => post("/api/demo/classify"),
  analyse: () => post("/api/demo/analyse"),
  approve: (planId: string, planHash: string) =>
    post("/api/demo/approve", { plan_id: planId, plan_hash: planHash }),
  execute: (planId: string) => post("/api/demo/execute", { plan_id: planId }),
  outage: () => request<OutageView | null>("/api/outage"),
  runOutage: () => post<OutageView>("/api/outage/run"),
};
