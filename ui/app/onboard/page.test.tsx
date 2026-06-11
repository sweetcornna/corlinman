/**
 * Onboarding page tests — 6-step first-run wizard.
 *
 * Contract: docs/PLAN_FIRST_RUN_WIZARD.md (2026-05-28 reshape). The wizard
 * chains six sequential steps; the indicator gates forward motion and (once
 * the password rotation lands) locks steps 1 + 2.
 *
 *   1. API config        (skippable, hands off to /admin/credentials + providers)
 *   2. Change username   (POST /admin/onboard/finalize-account)
 *   3. Change password   (POST /admin/onboard/finalize-password, gated)
 *   4. Persona           (POST /admin/onboard/finalize-persona — default/custom/skip)
 *   5. Image provider    (POST /admin/onboard/finalize-image-provider)
 *   6. Done              (router.push("/") — or a deferred /persona redirect)
 *
 * Covered here:
 *   1. After the /admin/me probe settles the wizard renders Step 1 (API
 *      config) with the two handoff cards + skip / next buttons.
 *   2. A mismatched password on Step 3 surfaces an inline error WITHOUT
 *      calling finalize-password.
 *   3. The Step-2 username form POSTs finalize-account with the new username
 *      and advances to Step 3 (password).
 *   4. The full happy path walks all six steps and pushes the operator at
 *      /admin, calling each finalize endpoint exactly once.
 *   5. A persona "custom" choice records a deferred /persona redirect that
 *      fires at the end of the wizard (after the image step), not immediately.
 *
 * Locale stays zh-CN (matches login + account/security suites): "下一步" is
 * the shared "next" CTA, "两次密码不一致" the mismatch error.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const replaceMock = vi.fn();
const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: pushMock }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/onboard",
}));

import OnboardPage from "./page";

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

  it("renders Step 1 (API config handoff) once /admin/me settles", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      return jsonResponse({ status: "ok" });
    });

    render(<OnboardPage />);
    // Wait for the /admin/me probe to settle so the wizard renders the
    // resolved step rather than the optimistic pre-probe mount.
    await waitFor(() => {
      expect(screen.getByTestId("onboard-me-checked")).toHaveAttribute(
        "data-checked",
        "true",
      );
    });

    // Step 1 is the API-config handoff: two provider-setup cards + a
    // skip / continue button pair. No account form here anymore.
    expect(screen.getByTestId("onboard-handoff-cards")).toBeInTheDocument();
    expect(
      screen.getByTestId("onboard-handoff-credentials"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("onboard-handoff-providers")).toBeInTheDocument();
    expect(screen.getByTestId("onboard-api-skip")).toBeInTheDocument();
    expect(screen.getByTestId("onboard-api-continue")).toBeInTheDocument();
    // The stepper exposes all six steps with step 1 current.
    expect(screen.getByTestId("onboard-step-1")).toHaveAttribute(
      "data-state",
      "current",
    );
    expect(screen.getByTestId("onboard-step-6")).toBeInTheDocument();
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

    render(<OnboardPage />);
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

    render(<OnboardPage />);
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

    render(<OnboardPage />);
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

    render(<OnboardPage />);
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
