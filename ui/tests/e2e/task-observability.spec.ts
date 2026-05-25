/**
 * Task observability E2E — Phase 4 W4.2.
 *
 * Drives the complete observability loop end-to-end in the browser:
 *
 *   1. Login as admin (rotated to a non-default password so the
 *      /account/security gate doesn't intercept).
 *   2. Open `/admin/sessions/{key}` — the EventTimeline mounts the SSE
 *      stream + the CostFooter polls the cost endpoint.
 *   3. The spec replays a recorded 14-event turn through a stubbed SSE
 *      response so the run is deterministic. It exercises:
 *        - reasoning block (shimmer while streaming, settled when done)
 *        - 2 tool widgets with state transitions pending → running →
 *          completed (covered by replaying the matching events)
 *        - tool widget expand → args + result visible
 *        - cost footer pills populate from the /cost endpoint
 *   4. Navigate to `/admin/sessions/{key}/turns/{turn_id}` (replay
 *      mode) and assert the same timeline shape renders from the JSON
 *      replay endpoint.
 *
 * Gating: the spec splits into two layers.
 *
 *   - **stub layer (always-on)** — uses `page.route` to fake the auth +
 *     SSE + replay + cost endpoints. Runs without `CORLINMAN_E2E=1` so
 *     contributors who only have the UI dev server up can exercise the
 *     timeline plumbing.
 *   - **full-stack layer (CORLINMAN_E2E=1)** — TODO. Today no
 *     "trigger fixture turn" endpoint exists on the gateway and the
 *     bundled mock provider (`corlinman_providers.MockProvider`) only
 *     echoes the prompt — it never emits tool calls, so it can't drive
 *     the tool-state machine. Future work: add a debug endpoint
 *     (e.g. `POST /admin/__test__/fixture-turn`) that the gateway only
 *     mounts under `CORLINMAN_E2E=1` which synthesises a deterministic
 *     14-event turn into the journal.
 *
 * The stub layer is enough to prove the W2.x frontend renders the
 * EventTimeline + ToolWidget + ReasoningBlock + CostFooter against the
 * documented wire shape from `ui/lib/sessions/event-stream.ts`. The
 * full-stack TODO is what proves W1 backend emission actually wires
 * into that shape — that's a separate harness problem.
 */

import { expect, test, type Page, type Route } from "@playwright/test";

import { pinLocaleEn } from "./helpers/auth";

const SESSION_KEY = "telegram:42:test-observability";
const TURN_ID = "0123456789abcdef0123456789abcdef";

/**
 * The 14 typed events from the plan §1.1 taxonomy, modelled for a turn
 * that emits: 1 reasoning block, 1 text block, 2 tool calls
 * (`read_file` + `bash`), final reply, TurnComplete. We use this twice
 * — once as the SSE seed (encoded as `data:` frames) and once as the
 * JSON replay payload for the drill-down page.
 */
const FIXTURE_EVENTS = [
  {
    turn_id: TURN_ID,
    sequence: 1,
    timestamp_ms: 1_700_000_000_000,
    event_type: "TurnStart",
    payload: {
      model: "anthropic/claude-3-5-sonnet",
      user_text: "Read README.md then list the project root.",
      system_message_preview: "You are a helpful assistant.",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 2,
    timestamp_ms: 1_700_000_000_050,
    event_type: "BlockStart",
    payload: { index: 0, block_type: "reasoning" },
  },
  {
    turn_id: TURN_ID,
    sequence: 3,
    timestamp_ms: 1_700_000_000_100,
    event_type: "ReasoningDelta",
    payload: {
      index: 0,
      text: "The user wants me to inspect the README, then list files.",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 4,
    timestamp_ms: 1_700_000_000_200,
    event_type: "BlockStop",
    payload: { index: 0, elapsed_ms: 150 },
  },
  {
    turn_id: TURN_ID,
    sequence: 5,
    timestamp_ms: 1_700_000_000_250,
    event_type: "BlockStart",
    payload: {
      index: 1,
      block_type: "tool_use",
      tool_name: "read_file",
      tool_call_id: "call_aa",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 6,
    timestamp_ms: 1_700_000_000_260,
    event_type: "ToolStateRunning",
    payload: {
      tool_call_id: "call_aa",
      tool_name: "read_file",
      args_json: '{"path":"README.md"}',
      started_at_ms: 1_700_000_000_260,
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 7,
    timestamp_ms: 1_700_000_000_400,
    event_type: "ToolStateCompleted",
    payload: {
      tool_call_id: "call_aa",
      result_summary: "# corlinman\n\nAgent gateway README contents…",
      elapsed_ms: 140,
      is_error: false,
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 8,
    timestamp_ms: 1_700_000_000_420,
    event_type: "BlockStop",
    payload: { index: 1, elapsed_ms: 170 },
  },
  {
    turn_id: TURN_ID,
    sequence: 9,
    timestamp_ms: 1_700_000_000_450,
    event_type: "BlockStart",
    payload: {
      index: 2,
      block_type: "tool_use",
      tool_name: "bash",
      tool_call_id: "call_bb",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 10,
    timestamp_ms: 1_700_000_000_460,
    event_type: "ToolStateRunning",
    payload: {
      tool_call_id: "call_bb",
      tool_name: "bash",
      args_json: '{"command":"ls -la"}',
      started_at_ms: 1_700_000_000_460,
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 11,
    timestamp_ms: 1_700_000_000_700,
    event_type: "ToolStateCompleted",
    payload: {
      tool_call_id: "call_bb",
      result_summary: "drwxr-xr-x  CHANGELOG.md  README.md  docs  ui",
      elapsed_ms: 240,
      is_error: false,
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 12,
    timestamp_ms: 1_700_000_000_720,
    event_type: "BlockStop",
    payload: { index: 2, elapsed_ms: 270 },
  },
  {
    turn_id: TURN_ID,
    sequence: 13,
    timestamp_ms: 1_700_000_000_900,
    event_type: "TextDelta",
    payload: {
      index: 3,
      text: "Read README and listed the root — agent gateway project.",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 14,
    timestamp_ms: 1_700_000_001_000,
    event_type: "TurnComplete",
    payload: {
      finish_reason: "stop",
      usage: { input_tokens: 120, output_tokens: 80 },
      elapsed_ms: 1_000,
      estimated_cost_usd: 0.0123,
      cost_status: "estimated",
    },
  },
] as const;

const COST_RESPONSE = {
  session_key: SESSION_KEY,
  turn_count: 1,
  total_elapsed_ms: 1_000,
  total_cost_usd: 0.0123,
  cost_status_breakdown: { estimated: 1, billed: 0, unknown: 0 },
  total_tool_calls: 2,
  last_turn_at_ms: 1_700_000_001_000,
  avg_turn_ms: 1_000,
  last_tool_name: "bash",
};

/** Encode the fixture events into an SSE-formatted string body. */
function buildSseBody(): string {
  return (
    FIXTURE_EVENTS.map((ev) => {
      return `id: ${ev.turn_id}:${ev.sequence}\ndata: ${JSON.stringify(ev)}\n`;
    }).join("\n") + "\n"
  );
}

/**
 * Stub the admin auth + sessions endpoints. The /admin/me response
 * pretends the operator has already rotated the default password so
 * the layout shell doesn't trip the must_change_password gate.
 */
async function installCommonStubs(page: Page): Promise<void> {
  await page.route("**/admin/me", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: "admin",
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
        must_change_password: false,
      }),
    });
  });
  await page.route(
    `**/admin/sessions/${encodeURIComponent(SESSION_KEY)}/cost`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(COST_RESPONSE),
      });
    },
  );
  // Sessions list — referenced by the breadcrumb. Empty list is fine.
  await page.route("**/admin/sessions*", async (route: Route) => {
    const url = route.request().url();
    // Only respond to the bare list endpoint here; the more specific
    // /cost and SSE routes above match first via Playwright's ordering.
    if (
      url.includes("/events/live") ||
      url.includes("/turns/") ||
      url.endsWith("/cost") ||
      url.includes("/cost?")
    ) {
      return route.fallback();
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ sessions: [], total: 0 }),
    });
  });
}

/**
 * Install the SSE stub. We respond with a complete event stream body
 * up-front; the EventTimeline's batched-rAF reducer folds the events
 * into Turn parts on mount. EventSource happily drains a synchronous
 * body the same way it would a streamed one.
 */
async function installSseStub(page: Page): Promise<void> {
  const body = buildSseBody();
  await page.route(
    `**/admin/sessions/${encodeURIComponent(SESSION_KEY)}/events/live*`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
        },
        body,
      });
    },
  );
}

/**
 * Install the JSON-replay stub for the drill-down page. Mirrors the
 * `loadTurnEvents` contract in `ui/lib/api.ts`: returns a single page
 * with `next_cursor: null` so the consumer breaks out of its cursor
 * loop after one request.
 */
async function installReplayStub(page: Page): Promise<void> {
  await page.route(
    `**/admin/sessions/${encodeURIComponent(SESSION_KEY)}/turns/${TURN_ID}/events*`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          events: FIXTURE_EVENTS,
          next_cursor: null,
        }),
      });
    },
  );
}

test.describe("task observability — stubbed timeline", () => {
  test.beforeEach(async ({ page }) => {
    await pinLocaleEn(page);
    await installCommonStubs(page);
  });

  test("live timeline renders reasoning, two tools, cost footer", async ({
    page,
  }) => {
    await installSseStub(page);

    await page.goto(
      `/admin/sessions/detail?key=${encodeURIComponent(SESSION_KEY)}`,
    );

    // The timeline container mounts immediately; wait for the SSE to
    // drain into a Turn card before asserting individual parts.
    const timeline = page.getByTestId("event-timeline");
    await expect(timeline).toBeVisible();

    const turnCard = page.getByTestId("timeline-turn-card").first();
    await expect(turnCard).toBeVisible({ timeout: 10_000 });
    await expect(turnCard).toHaveAttribute("data-turn-status", "complete");

    // Reasoning block — settled (not streaming) post-replay.
    const reasoning = page.getByTestId("reasoning-block");
    await expect(reasoning).toBeVisible();
    await expect(reasoning).toHaveAttribute("data-streaming", "false");

    // Two tool widgets, both completed.
    const tools = page.getByTestId("tool-widget");
    await expect(tools).toHaveCount(2);
    const readFile = page.locator(
      '[data-testid="tool-widget"][data-tool-name="read_file"]',
    );
    const bash = page.locator(
      '[data-testid="tool-widget"][data-tool-name="bash"]',
    );
    await expect(readFile).toHaveAttribute("data-tool-state", "completed");
    await expect(bash).toHaveAttribute("data-tool-state", "completed");

    // Expand the read_file widget → args + result visible.
    await readFile.getByTestId("tool-widget-toggle").click();
    const body = readFile.getByTestId("tool-widget-body");
    await expect(body).toBeVisible();
    // Args render through the per-tool renderer; the result_summary
    // string ("README contents") is the dependable substring.
    await expect(body).toContainText(/README/);

    // Cost footer — pills populated by the polled /cost endpoint.
    const footer = page.getByTestId("cost-footer");
    await expect(footer).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("cost-footer-total")).toContainText("$");
    await expect(page.getByTestId("cost-footer-turns")).toContainText("1");
    await expect(page.getByTestId("cost-footer-tools")).toContainText("2");
  });

  test("drill-down replay renders identical timeline shape", async ({
    page,
  }) => {
    await installReplayStub(page);

    await page.goto(
      `/admin/sessions/turn?key=${encodeURIComponent(SESSION_KEY)}&turn=${TURN_ID}`,
    );

    // Replay-mode body (not the live wrapper).
    const body = page.getByTestId("event-timeline-body");
    await expect(body).toBeVisible({ timeout: 10_000 });
    await expect(body).toHaveAttribute("data-mode", "replay");

    // Same two tool widgets render — identical to the live view.
    const tools = page.getByTestId("tool-widget");
    await expect(tools).toHaveCount(2);
    await expect(
      page.locator('[data-testid="tool-widget"][data-tool-name="read_file"]'),
    ).toHaveAttribute("data-tool-state", "completed");
    await expect(
      page.locator('[data-testid="tool-widget"][data-tool-name="bash"]'),
    ).toHaveAttribute("data-tool-state", "completed");

    // Reasoning block rendered the same way in replay.
    await expect(page.getByTestId("reasoning-block")).toBeVisible();
  });
});

/*
 * TODO(W4.2-followup, gated on CORLINMAN_E2E=1):
 *   - Add a `POST /admin/__test__/fixture-turn` debug endpoint to the
 *     gateway (mounted only when an env flag is set) that synthesises a
 *     deterministic 14-event turn into the journal + emits it through
 *     the live emitter. Then re-implement these specs against the real
 *     SSE stream + real /cost aggregation, asserting:
 *       - state transitions pending → running → completed are observable
 *         in real time (not pre-baked).
 *       - the 10s ToolStateHeartbeat fires for tools that exceed 10s.
 *       - Cancelling propagates within 1s of the cancel POST.
 *   - The current stub layer proves the renderer; the full-stack layer
 *     would prove the emit-and-persist tee.
 */
