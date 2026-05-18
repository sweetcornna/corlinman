/**
 * Onboarding page tests — 2-step wizard (2026-05 reshape).
 *
 * Covered:
 *   1. When `/admin/me` returns 401 (no admin yet) the wizard starts at
 *      Step 1 (account) with three fields.
 *   2. Mismatched passwords surface an inline error without hitting the
 *      onboard endpoint.
 *   3. Submitting matching credentials calls POST /admin/onboard and
 *      advances to Step 2 (handoff) with three provider-setup cards.
 *   4. When `/admin/me` returns 200 with `must_change_password=true` the
 *      wizard skips Step 1, lands on Step 2, and renders the
 *      "Using default admin/root" hint.
 *   5. The Step-2 skip button POSTs `/admin/onboard/finalize-skip` and
 *      pushes the operator at `/admin`.
 *
 * Locale stays zh-CN (matches login + account/security suites).
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

/** Most tests want a default unauth `/admin/me` reply. */
function unauthMeHandler(): Response {
  return new Response(JSON.stringify({ detail: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json" },
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

  it("starts at Step 1 when /admin/me returns 401 (no admin yet)", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    render(<OnboardPage />);
    // Wait for the /admin/me probe to settle so the wizard renders the
    // resolved step (not the optimistic "account" mount).
    await waitFor(() => {
      expect(screen.getByTestId("onboard-me-checked")).toHaveAttribute(
        "data-checked",
        "true",
      );
    });

    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByLabelText("确认密码")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建管理员" }),
    ).toBeInTheDocument();
  });

  it("surfaces an inline mismatch error without calling onboard", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    render(<OnboardPage />);
    await waitFor(() => {
      expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    });

    // Clear the call log so we can assert no /admin/onboard POST happens.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();

    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "abcdefgh" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "different" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建管理员" }));

    await waitFor(() => {
      expect(screen.getByTestId("onboard-error")).toHaveTextContent(
        "两次密码不一致",
      );
    });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    // The mount-time /admin/me is the only fetch — onboard POST never fires.
    for (const call of fetchMock.mock.calls) {
      expect(String(call[0])).not.toContain("/admin/onboard");
    }
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("calls /admin/onboard and advances to the handoff step on success", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    render(<OnboardPage />);
    await waitFor(() => {
      expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockClear();

    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建管理员" }));

    // No redirect after step 1 — wizard advances to step 2 (handoff)
    // with the three provider-setup cards.
    await waitFor(() => {
      expect(
        screen.getByTestId("onboard-handoff-cards"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("onboard-handoff-credentials"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("onboard-handoff-providers"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("onboard-handoff-oauth"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("onboard-skip-mock")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
    const onboardCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).endsWith("/admin/onboard"),
    );
    expect(onboardCall).toBeTruthy();
    expect(onboardCall![1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ username: "alice", password: "goodpassphrase" }),
    });
  });

  it("skips Step 1 + shows the default-admin hint when must_change_password=true", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) {
        return new Response(
          JSON.stringify({
            user: "admin",
            created_at: "2026-05-17T00:00:00Z",
            expires_at: "2026-05-24T00:00:00Z",
            must_change_password: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("", { status: 404 });
    });

    render(<OnboardPage />);

    // The hint only appears once the /admin/me probe resolves.
    await waitFor(() => {
      expect(
        screen.getByTestId("onboard-default-admin-hint"),
      ).toBeInTheDocument();
    });
    // We should be on Step 2 (handoff), not Step 1 (account).
    expect(
      screen.getByTestId("onboard-handoff-cards"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("确认密码")).toBeNull();
    // And the "Customize admin account" escape hatch is reachable.
    expect(
      screen.getByTestId("onboard-customize-admin"),
    ).toBeInTheDocument();
  });

  it("clicking Skip → mock provider hits finalize-skip + pushes /admin", async () => {
    let skipCalls = 0;
    stubFetch((url) => {
      if (url.includes("/admin/me")) {
        return new Response(
          JSON.stringify({
            user: "admin",
            created_at: "2026-05-17T00:00:00Z",
            expires_at: "2026-05-24T00:00:00Z",
            must_change_password: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/admin/onboard/finalize-skip")) {
        skipCalls++;
        return new Response(
          JSON.stringify({ status: "ok", mode: "mock" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("", { status: 404 });
    });

    render(<OnboardPage />);
    await waitFor(() => {
      expect(screen.getByTestId("onboard-skip-mock")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("onboard-skip-mock"));

    await waitFor(() => {
      expect(skipCalls).toBe(1);
    });
    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/admin");
    });
  });
});
