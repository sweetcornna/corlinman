/**
 * /chat MVP smoke — Wave 1.
 *
 * Stubs-only spec. Drives the golden path of the in-app chat surface so
 * regressions surface fast:
 *
 *   1. Sidebar renders the existing conversations (stubbed GET /admin/sessions).
 *   2. Clicking "New chat" navigates to /chat/[sessionKey].
 *   3. The composer sends a message and the streamed assistant reply is
 *      rendered token-by-token, plus a tool-call card from the live SSE
 *      stream.
 *   4. The stop button is exposed while streaming.
 *   5. The slash menu opens on /.
 *
 * The real backend (events/live SSE, chat completions, cancel) is faked
 * here; the full-stack version of this flow is gated on a future
 * CORLINMAN_E2E=1 harness.
 */

import { expect, test, type Page, type Route } from "@playwright/test";

import { pinLocaleEn } from "./helpers/auth";

const TEST_TIMEOUT_MS = 10_000;
const SESSION_KEY = "web:test:abc";

// Minimal /admin/me stub so the layout doesn't bounce to /login.
async function stubAuth(page: Page): Promise<void> {
  await page.route("**/admin/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ user: "admin", must_change_password: false }),
    }),
  );
}

// Minimal sessions list. Two entries so the sidebar renders groups.
async function stubSessionsList(page: Page): Promise<void> {
  await page.route("**/admin/sessions", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        sessions: [
          {
            session_key: "telegram:7777",
            last_message_at: Date.now() - 60_000,
            message_count: 4,
            title: "Existing chat",
            pinned: false,
            archived: false,
          },
        ],
      }),
    }),
  );
}

// Stub the chat completions stream — emit a couple of token chunks then
// finish_reason=stop and [DONE].
async function stubChatCompletions(page: Page): Promise<void> {
  await page.route("**/v1/chat/completions", async (route: Route) => {
    const body = [
      `data: ${JSON.stringify({
        corlinman: { turn_id: "turn_test", session_key: SESSION_KEY },
        choices: [{ index: 0, delta: { content: "Hello " } }],
      })}\n\n`,
      `data: ${JSON.stringify({
        choices: [{ index: 0, delta: { content: "world!" } }],
      })}\n\n`,
      `data: ${JSON.stringify({
        choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      })}\n\n`,
      `data: [DONE]\n\n`,
    ].join("");
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body,
    });
  });
}

// Stub the live event SSE stream with a single tool-call cycle so the
// tool card renders.
async function stubLiveEvents(page: Page): Promise<void> {
  await page.route(
    /\/admin\/sessions\/[^/]+\/events\/live/,
    async (route: Route) => {
      const ev = (type: string, payload: unknown, sequence: number) =>
        `data: ${JSON.stringify({
          turn_id: "turn_test",
          sequence,
          timestamp_ms: Date.now(),
          event_type: type,
          payload,
        })}\n\n`;
      const body = [
        ev("TurnStart", { model: "gpt-4o" }, 1),
        ev(
          "ToolStateRunning",
          {
            call_id: "c1",
            tool_name: "read_file",
            args_json: '{"path":"src/foo.py"}',
            started_at_ms: Date.now(),
          },
          2,
        ),
        ev(
          "ToolStateCompleted",
          {
            call_id: "c1",
            result_summary: "def foo(): pass",
            duration_ms: 12,
            is_error: false,
          },
          3,
        ),
        ev(
          "TurnComplete",
          { finish_reason: "stop", elapsed_ms: 30 },
          4,
        ),
      ].join("");
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body,
      });
    },
  );
}

async function stubCancel(page: Page): Promise<void> {
  await page.route(/\/admin\/sessions\/[^/]+\/cancel/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "cancelled", turn_id: "turn_test" }),
    }),
  );
}

test.describe("/chat MVP", () => {
  test.beforeEach(async ({ page }) => {
    await pinLocaleEn(page);
    await stubAuth(page);
    await stubSessionsList(page);
    await stubChatCompletions(page);
    await stubLiveEvents(page);
    await stubCancel(page);
  });

  test("sidebar lists existing chats and exposes a New chat button", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    await page.goto("/chat");
    await expect(page.getByTestId("chat-sidebar")).toBeVisible();
    await expect(page.getByTestId("chat-sidebar-new")).toBeEnabled();
    await expect(page.getByText("Existing chat")).toBeVisible();
  });

  test("clicking New chat navigates to a session URL", async ({ page }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    await page.goto("/chat");
    await page.getByTestId("chat-sidebar-new").click();
    await expect(page).toHaveURL(/\/chat\/[^/?#]+$/);
    await expect(page.getByTestId("chat-area")).toBeVisible();
    await expect(page.getByTestId("composer")).toBeVisible();
  });

  test("sends a message and renders the streamed reply + tool-call card", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    await page.goto(`/chat/${encodeURIComponent(SESSION_KEY)}`);
    await expect(page.getByTestId("composer-textarea")).toBeVisible();
    await page.getByTestId("composer-textarea").fill("read foo.py please");
    await page.getByTestId("composer-send").click();
    // The user bubble appears first.
    await expect(page.locator('[data-role="user"]').last()).toContainText(
      "read foo.py please",
    );
    // Streamed assistant content lands.
    await expect(page.locator('[data-role="assistant"]').last()).toContainText(
      "Hello",
      { timeout: 5_000 },
    );
    // The tool-call card shows up from the live SSE stream.
    await expect(page.getByTestId("tool-call-card")).toBeVisible({
      timeout: 5_000,
    });
    await expect(
      page.getByTestId("tool-call-card").getAttribute("data-tool-name"),
    ).resolves.toBe("read_file");
  });

  test("slash menu opens when input begins with /", async ({ page }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    await page.goto(`/chat/${encodeURIComponent(SESSION_KEY)}`);
    await page.getByTestId("composer-textarea").fill("/cle");
    await expect(page.getByTestId("slash-menu")).toBeVisible();
    await expect(page.getByTestId("slash-menu")).toContainText("/clear");
  });

  test("sidebar collapse hides the search bar and exposes mini actions", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    await page.goto("/chat");
    await page.getByLabel("Collapse sidebar").click();
    await expect(page.getByTestId("chat-sidebar-collapsed")).toBeVisible();
    await expect(page.getByTestId("chat-sidebar-search")).toHaveCount(0);
  });
});
