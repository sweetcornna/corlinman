/**
 * Onboarding page tests — 6-step first-run wizard.
 *
 * Contract: docs/PLAN_FIRST_RUN_WIZARD.md (2026-05-28 reshape). The wizard
 * chains six sequential steps; the indicator gates forward motion and (once
 * the password rotation lands) locks steps 1 + 2.
 *
 *   1. API config        (skippable; PR5 — the guided ProviderSetupFlow runs
 *                         INLINE, replacing the old new-tab hand-off cards)
 *   2. Change username   (POST /admin/onboard/finalize-account)
 *   3. Change password   (POST /admin/onboard/finalize-password, gated)
 *   4. Persona           (POST /admin/onboard/finalize-persona — default/custom/skip)
 *   5. Image provider    (POST /admin/onboard/finalize-image-provider)
 *   6. Done              (router.push("/") — or a deferred /persona redirect)
 *
 * Covered here:
 *   1. After the /admin/me probe settles the wizard renders Step 1 (API
 *      config) with the inline setup flow + skip / next buttons; "下一步"
 *      is disabled until the deployment is configured.
 *   2. A gateway that is already configured swaps the flow for a summary
 *      card and enables "下一步".
 *   3. A failing config surface (503-style) swaps the flow for the
 *      BackendPendingBanner while keeping the pure skip available.
 *   4. A mismatched password on Step 3 surfaces an inline error WITHOUT
 *      calling finalize-password.
 *   5. The Step-2 username form POSTs finalize-account with the new username
 *      and advances to Step 3 (password).
 *   6. The full happy path walks all six steps and pushes the operator at
 *      /admin, calling each finalize endpoint exactly once — and never
 *      calls finalize-skip (the "暂时跳过" button is a PURE skip).
 *   7. A persona "custom" choice records a deferred /persona redirect that
 *      fires at the end of the wizard (after the image step), not immediately.
 *
 * Locale stays zh-CN (matches login + account/security suites): "下一步" is
 * the shared "next" CTA, "两次密码不一致" the mismatch error.
 */

import * as React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const replaceMock = vi.fn();
const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: pushMock }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/onboard",
}));

import OnboardPage from "./page";

/** PR5: step 1 mounts react-query consumers (useSetupStatus + the setup
 * flow), so the page needs a QueryClient like the real Providers shell. */
function renderOnboard() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <OnboardPage />
    </QueryClientProvider>,
  );
}

/**
 * Build a fetch stub that lets each test wire up the route table.
 * Returns 404 by default so unexpected calls are obvious in failures.
 */
function stubFetch(
  handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => handler(url, init)),
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Most tests want a default unauth `/admin/me` reply. */
function unauthMeHandler(): Response {
  return jsonResponse({ detail: "unauthorized" }, 401);
}

const fetchMock = () => globalThis.fetch as ReturnType<typeof vi.fn>;

/** Find the init of the (first) fetch call whose URL ends with `suffix`. */
function callTo(suffix: string): RequestInit | undefined {
  const hit = fetchMock().mock.calls.find((c) => String(c[0]).endsWith(suffix));
  return hit?.[1] as RequestInit | undefined;
}

/**
 * Drive the wizard from the freshly-mounted Step 1 to the start of Step 3
 * (password). Step 1 is skipped, Step 2 (username) is submitted with a valid
 * value. The caller must have already stubbed `/admin/me` +
 * `/admin/onboard/finalize-account`.
 */
async function advanceToPasswordStep() {
  // Step 1 → skip the API config handoff.
  await waitFor(() => {
    expect(screen.getByTestId("onboard-api-skip")).toBeInTheDocument();
  });
  fireEvent.click(screen.getByTestId("onboard-api-skip"));

  // Step 2 → submit a new username.
  await waitFor(() => {
    expect(screen.getByTestId("onboard-username-input")).toBeInTheDocument();
  });
  fireEvent.change(screen.getByTestId("onboard-username-input"), {
    target: { value: "alice" },
  });
  fireEvent.click(screen.getByTestId("onboard-username-submit"));

  // Step 3 → password form is now mounted.
  await waitFor(() => {
    expect(screen.getByTestId("onboard-confirm-password")).toBeInTheDocument();
  });
}

describe("OnboardPage", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    pushMock.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders Step 1 with the inline setup flow once /admin/me settles", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      if (url.includes("/admin/providers")) {
        return jsonResponse({ providers: [] });
      }
      if (url.includes("/admin/models")) {
        return jsonResponse({ default: "", aliases: [], providers: [] });
      }
      return jsonResponse({ status: "ok" });
    });

    renderOnboard();
    // Wait for the /admin/me probe to settle so the wizard renders the
    // resolved step rather than the optimistic pre-probe mount.
    await waitFor(() => {
      expect(screen.getByTestId("onboard-me-checked")).toHaveAttribute(
        "data-checked",
        "true",
      );
    });

    // Step 1 hosts the guided setup flow INLINE (PR5) — no more
    // new-tab hand-off cards.
    await waitFor(() => {
      expect(screen.getByTestId("provider-setup-flow")).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("provider-setup-flow"),
    ).toHaveAttribute("data-variant", "onboarding");
    expect(screen.queryByTestId("onboard-handoff-cards")).toBeNull();
    // The preset grid is step 1 of the flow.
    expect(screen.getByTestId("setup-preset-anthropic")).toBeInTheDocument();
    expect(screen.getByTestId("onboard-api-skip")).toBeInTheDocument();
    // "下一步" stays gated until the deployment is configured.
    expect(screen.getByTestId("onboard-api-continue")).toBeDisabled();
    // The stepper exposes all six steps with step 1 current.
    expect(screen.getByTestId("onboard-step-1")).toHaveAttribute(
      "data-state",
      "current",
    );
    expect(screen.getByTestId("onboard-step-6")).toBeInTheDocument();
  });

  it("shows a configured summary and enables 下一步 when the gateway is already set up", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      if (url.includes("/admin/providers")) {
        return jsonResponse({
          providers: [
            {
              name: "anthropic",
              kind: "anthropic",
              enabled: true,
              base_url: null,
              api_key_source: "env",
              api_key_env_name: "ANTHROPIC_API_KEY",
              params: {},
              params_schema: { type: "object", properties: {} },
            },
          ],
        });
      }
      if (url.includes("/admin/models")) {
        return jsonResponse({
          default: "claude-opus-4-8",
          aliases: [
            {
              name: "claude-opus-4-8",
              provider: "anthropic",
              model: "claude-opus-4-8",
              params: {},
              effective_params_schema: {},
            },
          ],
          providers: [],
        });
      }
      return jsonResponse({ status: "ok" });
    });

    renderOnboard();

    const summary = await screen.findByTestId("onboard-setup-summary");
    expect(summary).toHaveTextContent("anthropic");
    expect(summary).toHaveTextContent("claude-opus-4-8");
    // The flow itself is NOT mounted — nothing left to configure.
    expect(screen.queryByTestId("provider-setup-flow")).toBeNull();
    await waitFor(() => {
      expect(screen.getByTestId("onboard-api-continue")).toBeEnabled();
    });
  });

  it("falls back to the backend-pending banner (skip stays available) on 503s", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      if (url.includes("/admin/providers") || url.includes("/admin/models")) {
        return jsonResponse({ error: "backend_pending" }, 503);
      }
      return jsonResponse({ status: "ok" });
    });

    renderOnboard();

    await waitFor(() => {
      expect(screen.getByTestId("backend-pending")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("provider-setup-flow")).toBeNull();
    // The pure skip is still there and still advances the wizard.
    fireEvent.click(screen.getByTestId("onboard-api-skip"));
    await waitFor(() => {
      expect(
        screen.getByTestId("onboard-username-input"),
      ).toBeInTheDocument();
    });
    // Pure skip — no finalize-skip / mock bootstrap call fired.
    for (const call of fetchMock().mock.calls) {
      expect(String(call[0])).not.toContain("finalize-skip");
    }
  });

  it("surfaces an inline mismatch error on Step 3 without calling finalize-password", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      if (url.includes("/admin/onboard/finalize-account")) {
        return jsonResponse({ status: "ok", username: "alice" });
      }
      if (url.includes("/admin/onboard/finalize-password")) {
        // Should never be reached for a client-side mismatch.
        return jsonResponse({ status: "ok", must_change_password: false });
      }
      return jsonResponse({ status: "ok" });
    });

    renderOnboard();
    await advanceToPasswordStep();

    // Clear the call log so we can assert finalize-password never fires.
    fetchMock().mockClear();

    fireEvent.change(screen.getByTestId("onboard-old-password"), {
      target: { value: "root" },
    });
    fireEvent.change(screen.getByTestId("onboard-new-password"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.change(screen.getByTestId("onboard-confirm-password"), {
      target: { value: "different-one" },
    });
    fireEvent.click(screen.getByTestId("onboard-password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("onboard-error")).toHaveTextContent(
        "两次密码不一致",
      );
    });
    // The mismatch is caught client-side — no finalize-password POST.
    for (const call of fetchMock().mock.calls) {
      expect(String(call[0])).not.toContain("/admin/onboard/finalize-password");
    }
    // Still on Step 3 — no advance to persona.
    expect(screen.queryByTestId("onboard-persona-default")).toBeNull();
  });

  it("Step 2 POSTs finalize-account and advances to the password step", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      if (url.includes("/admin/onboard/finalize-account")) {
        return jsonResponse({ status: "ok", username: "alice" });
      }
      return jsonResponse({ status: "ok" });
    });

    renderOnboard();
    await waitFor(() => {
      expect(screen.getByTestId("onboard-api-skip")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("onboard-api-skip"));

    await waitFor(() => {
      expect(screen.getByTestId("onboard-username-input")).toBeInTheDocument();
    });
    fetchMock().mockClear();

    fireEvent.change(screen.getByTestId("onboard-username-input"), {
      target: { value: "alice" },
    });
    fireEvent.click(screen.getByTestId("onboard-username-submit"));

    // Advances to Step 3 (password) — no redirect mid-wizard.
    await waitFor(() => {
      expect(
        screen.getByTestId("onboard-confirm-password"),
      ).toBeInTheDocument();
    });
    expect(replaceMock).not.toHaveBeenCalled();
    expect(pushMock).not.toHaveBeenCalled();

    const accountInit = callTo("/admin/onboard/finalize-account");
    expect(accountInit).toBeTruthy();
    expect(accountInit).toMatchObject({
      method: "POST",
      body: JSON.stringify({ new_username: "alice" }),
    });
  });

  it("walks all six steps and pushes /admin on finish", async () => {
    const seen: string[] = [];
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      if (url.includes("/admin/onboard/finalize-account")) {
        seen.push("account");
        return jsonResponse({ status: "ok", username: "alice" });
      }
      if (url.includes("/admin/onboard/finalize-password")) {
        seen.push("password");
        return jsonResponse({ status: "ok", must_change_password: false });
      }
      if (url.includes("/admin/onboard/finalize-persona")) {
        seen.push("persona");
        return jsonResponse({ status: "ok", choice: "default" });
      }
      if (url.includes("/admin/onboard/finalize-image-provider")) {
        seen.push("image");
        return jsonResponse({ status: "ok", image_provider: "mock" });
      }
      return jsonResponse({ status: "ok" });
    });

    renderOnboard();
    await advanceToPasswordStep();

    // Step 3 → password.
    fireEvent.change(screen.getByTestId("onboard-old-password"), {
      target: { value: "root" },
    });
    fireEvent.change(screen.getByTestId("onboard-new-password"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.change(screen.getByTestId("onboard-confirm-password"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.click(screen.getByTestId("onboard-password-submit"));

    // Step 4 → persona (pick default).
    await waitFor(() => {
      expect(screen.getByTestId("onboard-persona-default")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("onboard-persona-default"));

    // Step 5 → image provider (skip).
    await waitFor(() => {
      expect(screen.getByTestId("onboard-image-skip")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("onboard-image-skip"));

    // Step 6 → done.
    await waitFor(() => {
      expect(screen.getByTestId("onboard-finish")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("onboard-finish"));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/");
    });
    expect(seen).toEqual(["account", "password", "persona", "image"]);

    const passwordInit = callTo("/admin/onboard/finalize-password");
    expect(passwordInit).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        old_password: "root",
        new_password: "goodpassphrase",
      }),
    });
  });

  it("defers a persona 'custom' redirect until the wizard finishes", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      if (url.includes("/admin/onboard/finalize-account")) {
        return jsonResponse({ status: "ok", username: "alice" });
      }
      if (url.includes("/admin/onboard/finalize-password")) {
        return jsonResponse({ status: "ok", must_change_password: false });
      }
      if (url.includes("/admin/onboard/finalize-persona")) {
        return jsonResponse({
          status: "ok",
          choice: "custom",
          redirect: "/persona",
        });
      }
      if (url.includes("/admin/onboard/finalize-image-provider")) {
        return jsonResponse({ status: "ok", image_provider: "mock" });
      }
      return jsonResponse({ status: "ok" });
    });

    renderOnboard();
    await advanceToPasswordStep();

    fireEvent.change(screen.getByTestId("onboard-old-password"), {
      target: { value: "root" },
    });
    fireEvent.change(screen.getByTestId("onboard-new-password"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.change(screen.getByTestId("onboard-confirm-password"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.click(screen.getByTestId("onboard-password-submit"));

    // Step 4 → persona: pick "custom" (records a deferred /persona redirect).
    await waitFor(() => {
      expect(screen.getByTestId("onboard-persona-custom")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("onboard-persona-custom"));

    // The redirect is deferred — we land on Step 5 (image), not /persona.
    await waitFor(() => {
      expect(screen.getByTestId("onboard-image-skip")).toBeInTheDocument();
    });
    expect(pushMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("onboard-image-skip"));

    // Step 6 → finishing now fires the deferred /persona redirect, NOT /admin.
    await waitFor(() => {
      expect(screen.getByTestId("onboard-finish")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("onboard-finish"));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/persona");
    });
    expect(pushMock).not.toHaveBeenCalledWith("/");
  });
});
