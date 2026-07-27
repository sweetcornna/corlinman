/**
 * Login page smoke test. Exercises:
 *   1. The form renders with username + password fields.
 *   2. Submitting with mocked `/admin/login` → router.replace('/').
 *
 * The `fetch` stub returns 200 so `login()` resolves cleanly; that's
 * enough to cover the happy path without pulling in MSW or vi.mock
 * gymnastics.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const replaceMock = vi.fn();
const pushMock = vi.fn();
let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: pushMock }),
  useSearchParams: () => searchParams,
  usePathname: () => "/",
}));

import LoginPage from "./page";

describe("LoginPage", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    pushMock.mockClear();
    searchParams = new URLSearchParams();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ expires_in: 86400 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders username + password fields", () => {
    render(<LoginPage />);
    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
  });

  it("uses no backdrop blur anywhere on the page (Eclipse: matte only)", () => {
    const { container } = render(<LoginPage />);
    // The canvas (pure black + moonrise halo) is painted on <html> by
    // globals.css — the page composes no blur layers of its own.
    expect(container.innerHTML).not.toContain("backdrop-");
  });

  it("calls /admin/login and redirects on success", async () => {
    // The Wave 1.4 flow does a follow-up /admin/me probe — return a
    // session that has already rotated so we hit the `/` redirect branch.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/admin/me")) {
          return new Response(
            JSON.stringify({
              user: "admin",
              created_at: "2026-05-17T00:00:00Z",
              expires_at: "2026-05-24T00:00:00Z",
              must_change_password: false,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ expires_in: 86400 }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );

    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/"));
    const fetchCalls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls;
    // The OIDC status probe may fire first on mount — find the login
    // call by URL rather than assuming it is call #0.
    const loginCall = fetchCalls.find(([url]) =>
      String(url).includes("/admin/login"),
    );
    expect(loginCall).toBeDefined();
    expect(loginCall![1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ username: "admin", password: "secret" }),
    });
  });

  it(
    "ignores ?redirect= and forces /account/security when " +
      "must_change_password is true",
    async () => {
      // Land with a redirect target. The Wave 1.4 contract says we
      // *ignore* this whenever the gateway tells us the seed hasn't
      // been rotated yet.
      searchParams = new URLSearchParams({ redirect: "/agents" });

      vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string) => {
          if (url.includes("/admin/me")) {
            return new Response(
              JSON.stringify({
                user: "admin",
                created_at: "2026-05-17T00:00:00Z",
                expires_at: "2026-05-24T00:00:00Z",
                must_change_password: true,
              }),
              {
                status: 200,
                headers: { "content-type": "application/json" },
              },
            );
          }
          return new Response(
            JSON.stringify({ expires_in: 86400 }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }),
      );

      render(<LoginPage />);
      fireEvent.change(screen.getByLabelText("用户名"), {
        target: { value: "admin" },
      });
      fireEvent.change(screen.getByLabelText("密码"), {
        target: { value: "root" },
      });
      fireEvent.click(screen.getByRole("button", { name: "登录" }));

      await waitFor(() =>
        expect(replaceMock).toHaveBeenCalledWith("/account/security"),
      );
      // Importantly, /agents was NEVER navigated to.
      expect(replaceMock).not.toHaveBeenCalledWith("/agents");
    },
  );

  it("shows the SSO button when /auth/oidc/status reports available", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/auth/oidc/status")) {
          return new Response(
            JSON.stringify({ enabled: true, available: true }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    render(<LoginPage />);
    const link = await screen.findByTestId("oidc-login");
    expect(link).toHaveTextContent("使用 SSO 登录");
    expect(link.getAttribute("href")).toContain("/auth/oidc/login");
  });

  it("hides the SSO button when OIDC is disabled or discovery failed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/auth/oidc/status")) {
          return new Response(
            JSON.stringify({ enabled: true, available: false }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    render(<LoginPage />);
    // Let the status probe settle, then assert absence.
    await waitFor(() =>
      expect(
        (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some(
          ([url]) => String(url).includes("/auth/oidc/status"),
        ),
      ).toBe(true),
    );
    expect(screen.queryByTestId("oidc-login")).toBeNull();
  });

  it("surfaces ?oidc_error= from a failed SSO callback", async () => {
    searchParams = new URLSearchParams({ oidc_error: "email_not_allowed" });
    render(<LoginPage />);
    const alert = await screen.findByTestId("oidc-error");
    expect(alert).toHaveTextContent("该账号不在 SSO 白名单内。");
  });
});
