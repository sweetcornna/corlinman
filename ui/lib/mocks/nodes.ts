/**
 * Empty data stubs for `/nodes` page (B4 prototype).
 *
 * HONEST-ALIGN (R5): `fetchRunnersMock` is NOT wired into the page anymore —
 * it always returned `[]` and the gateway exposes no runner-registry endpoint,
 * so polling it just rendered a misleading "empty registry" state. The page now
 * shows an explicit "not yet available" panel (see `app/(admin)/nodes/page.tsx`).
 * The `Runner` type + `summariseRunners` are retained because the topology /
 * side-rail / detail-drawer components in `@/components/nodes/*` still type
 * against them and will be re-wired once the backend lands.
 *
 * TODO(R5): swap to `apiFetch<Runner[]>("/v1/wstool/runners")` once the gateway
 * exposes the runner registry (registry.py `runner_count` / `runners`). See
 * `audit/ARCH_DEBT.md` → "R5 — /nodes runner registry".
 */

export type RunnerHealth = "healthy" | "degraded" | "offline";

export interface Runner {
  id: string;
  hostname: string;
  ring: 0 | 1;
  slot: number;
  health: RunnerHealth;
  latencyMs: number;
  toolCount: number;
  connectedForSec: number;
  lastPingMs: number;
  errorRate: number;
  tools: string[];
}

export async function fetchRunnersMock(): Promise<Runner[]> {
  return [];
}

export function summariseRunners(runners: Runner[]): {
  connected: number;
  disconnected: number;
  avgLatencyMs: number;
  tasksPerMin: number;
} {
  let connected = 0;
  let disconnected = 0;
  let latencySum = 0;
  let latencyCount = 0;
  for (const r of runners) {
    if (r.health === "offline") {
      disconnected += 1;
    } else {
      connected += 1;
      latencySum += r.latencyMs;
      latencyCount += 1;
    }
  }
  const avgLatencyMs =
    latencyCount === 0 ? 0 : Math.round(latencySum / latencyCount);
  return { connected, disconnected, avgLatencyMs, tasksPerMin: 0 };
}
