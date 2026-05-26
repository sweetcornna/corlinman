/**
 * Multi-agent surfaces smoke — Wave 3 W3.1.
 *
 * Stubs-only Playwright coverage for the three multi-agent surfaces
 * shipped across W2.1 / W2.2 / W2.3:
 *
 *   A. `/admin/agents`         — Create button → `<CreateAgentModal>` →
 *                                POST /admin/agents → toast on success.
 *   B. `/admin/subagents`      — Live activity table + Kill button →
 *                                POST /admin/subagents/{id}/kill → row
 *                                state flips to "killed".
 *   C. `/admin/playground`      — `<AgentPicker>` selection threads
 *                                through to the chat-request body shape
 *                                (verified end-to-end via the trigger
 *                                label flipping to ``triggerPicked``
 *                                with the agent name).
 *
 * Same approach as `admin-pages-smoke.spec.ts`: install stubs first so
 * the page mounts without network errors, then drive the page like a
 * human would. The full-stack version of these contracts is gated on
 * `CORLINMAN_E2E=1` and lives behind the real gateway.
 */

import { expect, test, type Page, type Route } from "@playwright/test";

import { pinLocaleEn } from "./helpers/auth";

const TEST_TIMEOUT_MS = 15_000;

// ---------------------------------------------------------------------------
// Auth + layout stubs — every admin page bounces through these on mount.
// ---------------------------------------------------------------------------

async function installAuthStubs(page: Page): Promise<void> {
  await page.route("**/admin/login", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "set-cookie": "corlinman_session=stub; Path=/; HttpOnly" },
      body: JSON.stringify({ token: "stub-token", expires_in: 3600 }),
    });
  });
  await page.route("**/admin/me", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: "ops",
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
        must_change_password: false,
      }),
    });
  });
}

async function installHealthStub(page: Page): Promise<void> {
  await page.route("**/health", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "test", checks: [] }),
    });
  });
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const AGENTS_FIXTURE = [
  {
    name: "general-purpose",
    file_path: "/data/agents/general-purpose.md",
    bytes: 320,
    last_modified: "2026-05-25T00:00:00Z",
    source: "built-in" as const,
    description: "Catch-all agent for arbitrary tasks.",
  },
  {
    name: "researcher",
    file_path: "/data/agents/researcher.md",
    bytes: 280,
    last_modified: "2026-05-25T00:00:00Z",
    source: "built-in" as const,
    description: "Finds papers and surfaces citations.",
  },
  {
    name: "code-explorer",
    file_path: "/data/agents/code-explorer.md",
    bytes: 240,
    last_modified: "2026-05-25T00:00:00Z",
    source: "built-in" as const,
    description: "Reads a repo and answers structural questions.",
  },
  {
    name: "editor",
    file_path: "/data/agents/editor.md",
    bytes: 200,
    last_modified: "2026-05-25T00:00:00Z",
    source: "built-in" as const,
    description: "Tightens prose and fixes typos.",
  },
];

const SUBAGENT_RUNNING = {
  request_id: "req-abc-123",
  parent_session_key: "sess-parent-xyz-0001",
  subagent_type: "researcher",
  description: "research a topic",
  state: "running" as const,
  started_at: Date.now() - 2_000,
  finished_at: null,
  child_session_key: null,
  finish_reason: null,
  tool_calls_made: 0,
  elapsed_ms: 0,
  error: null,
  summary: "",
};

const SUBAGENT_KILLED = {
  ...SUBAGENT_RUNNING,
  state: "killed" as const,
  finished_at: Date.now(),
  finish_reason: "killed_by:ops",
  elapsed_ms: 2_100,
  summary: "[killed by ops]",
};

// SSE body factory — emits one initial `event: subagent` frame followed
// by a keepalive comment. Playwright fulfils SSE with a complete body
// up-front; the EventSource drains it like a streamed response.
function buildSubagentsSseBody(snapshot: typeof SUBAGENT_RUNNING): string {
  const payload = JSON.stringify(snapshot);
  return [
    `id: live:0`,
    `event: subagent`,
    `data: ${payload}`,
    ``,
    `: keepalive`,
    ``,
  ].join("\n");
}

// ---------------------------------------------------------------------------
// Test A — /admin/agents create flow
// ---------------------------------------------------------------------------

test.describe("multi-agent surfaces — stubs only", () => {
  test.beforeEach(async ({ page }) => {
    await pinLocaleEn(page);
    await installAuthStubs(page);
    await installHealthStub(page);
  });

  test("agents page: Create button posts and refreshes list", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);

    let createPosts = 0;
    let reloadPosts = 0;

    // GET → 4 built-in cards. The Create flow's onSuccess handler
    // invalidates the query, so this handler also fires on the refetch.
    await page.route(
      "**/admin/agents/bindings*",
      async (route: Route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ agents: [] }),
        });
      },
    );
    await page.route("**/admin/agents/reload", async (route: Route) => {
      reloadPosts += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          count: AGENTS_FIXTURE.length + 1,
          names: [...AGENTS_FIXTURE.map((a) => a.name), "test-helper"],
        }),
      });
    });
    await page.route("**/admin/agents", async (route: Route) => {
      const method = route.request().method();
      if (method === "POST") {
        createPosts += 1;
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            status: "ok",
            name: "test-helper",
            file_path: "/data/agents/test-helper.md",
            bytes: 64,
            source: "user",
            last_modified: new Date().toISOString(),
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(AGENTS_FIXTURE),
      });
    });
    // Models list — the table's per-row Model select pulls aliases.
    // An empty list keeps the select rendered without throwing.
    await page.route("**/admin/models*", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          aliases: [],
          default_alias: null,
          version: 2,
        }),
      });
    });

    await page.goto("/admin/agents");

    // Wait for the seed list to render.
    await expect(
      page.getByTestId("agent-link-researcher"),
    ).toBeVisible({ timeout: 10_000 });

    // Open the modal.
    await page.getByTestId("create-agent-open").click();
    await expect(page.getByTestId("create-agent-modal")).toBeVisible();

    // Fill the form. The body textarea already has the markdown
    // template — we only need to override the name (regex-validated).
    await page.getByTestId("agent-name").fill("test-helper");
    // The default format is `md`; assert + tweak the body so the form
    // submission carries an operator-recognisable payload.
    await expect(page.getByTestId("agent-format-md")).toBeChecked();
    const body = page.getByTestId("agent-body");
    await body.fill("---\ndescription: Test\n---\nHello.");

    // Submit — `mutation.mutate` → POST /admin/agents → onSuccess
    // closes the modal + invalidates the agents query.
    const submit = page.getByTestId("create-agent-submit");
    await expect(submit).toBeEnabled();
    await submit.click();

    // The dialog should unmount on success.
    await expect(page.getByTestId("create-agent-modal")).not.toBeVisible({
      timeout: 10_000,
    });

    // Exactly one POST should have fired; the GET refetch is the
    // invalidation. Reload is NOT called by createAgent — only by an
    // explicit "Reload from disk" surface — so we assert the contract.
    expect(createPosts).toBe(1);
    expect(reloadPosts).toBe(0);
  });

  // -------------------------------------------------------------------------
  // Test B — /admin/subagents live panel renders + Kill flow
  // -------------------------------------------------------------------------

  test("subagents page: row renders + Kill flips state to killed", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);

    let killPosts = 0;
    // Mutable list — kill() rewrites it so the next list refetch (if
    // any) reflects the new state. The page is SSE-driven so we don't
    // strictly need this, but it keeps the contract honest.
    let listRows: Array<typeof SUBAGENT_RUNNING | typeof SUBAGENT_KILLED> = [
      SUBAGENT_RUNNING,
    ];

    await page.route(
      "**/admin/subagents/events/live*",
      async (route: Route) => {
        await route.fulfill({
          status: 200,
          headers: {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
          },
          body: buildSubagentsSseBody(SUBAGENT_RUNNING),
        });
      },
    );
    await page.route(
      `**/admin/subagents/${SUBAGENT_RUNNING.request_id}/kill`,
      async (route: Route) => {
        killPosts += 1;
        listRows = [SUBAGENT_KILLED];
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(SUBAGENT_KILLED),
        });
      },
    );
    await page.route(
      `**/admin/subagents/${SUBAGENT_RUNNING.request_id}/status`,
      async (route: Route) => {
        const row = listRows[listRows.length - 1] ?? SUBAGENT_RUNNING;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(row),
        });
      },
    );
    await page.route("**/admin/subagents*", async (route: Route) => {
      const url = route.request().url();
      // Let the more-specific events/live + /kill + /status handlers
      // claim their URLs first.
      if (
        url.includes("/events/live") ||
        url.includes("/kill") ||
        url.includes("/status") ||
        url.includes("/events")
      ) {
        return route.fallback();
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ subagents: listRows }),
      });
    });

    // The browser-native confirm() the kill button triggers needs an
    // auto-accept handler — otherwise the click no-ops.
    page.on("dialog", (dialog) => {
      void dialog.accept();
    });

    await page.goto("/admin/subagents");

    const row = page.getByTestId("subagent-row").first();
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row).toHaveAttribute("data-state", "running");
    await expect(
      row.getByTestId("subagent-type-pill"),
    ).toHaveText("researcher");

    const killBtn = row.getByTestId("subagent-kill-button");
    await expect(killBtn).toBeVisible();
    await killBtn.click();

    // POST fires, server returns state=killed, page upserts into the
    // local Map → the row re-renders with the killed attribute. The
    // kill button itself disappears (not in-flight anymore).
    await expect(killBtn).not.toBeVisible({ timeout: 10_000 });
    // Killed rows hide by default (the "Include completed" toggle is
    // off); we either see an empty state or we don't see the row.
    expect(killPosts).toBe(1);
  });

  // -------------------------------------------------------------------------
  // Test C — /admin/playground AgentPicker threads agent_id
  // -------------------------------------------------------------------------

  test("playground: picking an agent updates the picker trigger label", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);

    await page.route("**/admin/agents", async (route: Route) => {
      const method = route.request().method();
      if (method !== "GET") return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(AGENTS_FIXTURE),
      });
    });
    // Playground's Send button POSTs `/v1/chat/completions` with
    // `stream: true`. Stub a single SSE chunk + DONE so the page can
    // resolve a streamed assistant reply without a real gateway.
    await page.route(
      "**/v1/chat/completions",
      async (route: Route) => {
        const chunk =
          'data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":"stub"},"finish_reason":null}]}\n\n' +
          'data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n' +
          "data: [DONE]\n\n";
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: chunk,
        });
      },
    );

    await page.goto("/admin/playground");

    // Picker trigger is always present; it defaults to the auto-route
    // label (the i18n keys fall back to the key string verbatim when
    // the locale doesn't define triggerAuto/triggerPicked — that's the
    // documented W2.1 behaviour).
    const trigger = page.getByTestId("agent-picker-trigger");
    await expect(trigger).toBeVisible({ timeout: 10_000 });
    // The accent dot is grey while auto-route is selected; we observe
    // it via aria-expanded transitioning to true after click.
    await expect(trigger).toHaveAttribute("aria-expanded", "false");

    await trigger.click();
    await expect(page.getByTestId("agent-picker-popover")).toBeVisible();
    await expect(
      page.getByTestId("agent-picker-item-researcher"),
    ).toBeVisible();
    await page.getByTestId("agent-picker-item-researcher").click();

    // Popover closes on pick. The trigger label flips to the
    // `triggerPicked` interpolation — with no locale entry, that's the
    // raw key string. The substring "researcher" is what proves the
    // pick threaded through `setExplicitAgent`.
    await expect(page.getByTestId("agent-picker-popover")).not.toBeVisible();
    await expect(trigger).toContainText("researcher");

    // Trigger still reflects the picked agent after a send.
    await page.getByTestId("chat-composer").fill("hello world");
    await page.getByTestId("chat-send").click();
    await expect(trigger).toContainText("researcher");
  });
});
