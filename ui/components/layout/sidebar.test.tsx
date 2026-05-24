/**
 * Sidebar tests — covers:
 *   1. Collapsible "Channels" group (default-collapsed, click+keyboard
 *      expansion, auto-expand on matching route).
 *   2. Operator-vs-Developer mode filtering: by default only 9 operator
 *      entries + a "Developer Settings" link appear; flipping
 *      `useDevMode()` reveals the 11 hidden power-user pages.
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import React from "react";

import {
  DEV_MODE_KEY,
  __resetDevModeForTests,
} from "@/lib/dev-mode";

let mockPathname = "/";

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import {
  SIDEBAR_DEV_ITEMS,
  SIDEBAR_DEV_SETTINGS_ENTRY,
  SIDEBAR_OPERATOR_ITEMS,
  Sidebar,
  resolveSidebarEntries,
} from "./sidebar";

function installMatchMedia(matches = false) {
  const mm = vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: mm,
  });
}

describe("Sidebar", () => {
  beforeEach(() => {
    installMatchMedia();
    mockPathname = "/";
    try {
      window.localStorage?.clear?.();
    } catch {
      /* localStorage may be stubbed out in some envs; safe to ignore. */
    }
    __resetDevModeForTests();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    __resetDevModeForTests();
  });

  it("renders the operator flat entries (no regression)", () => {
    render(<Sidebar user="admin" />);
    // Operator entries — Scheduler is one we kept visible by default.
    expect(screen.getByRole("link", { name: /定时任务|Scheduler/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /审批|Approvals/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /开发者设置|Developer Settings/i })).toBeInTheDocument();
    // User chip is rendered.
    expect(screen.getByTestId("nav-user")).toHaveTextContent("admin");
  });

  it("hides developer-only pages in operator mode (default)", () => {
    render(<Sidebar user="admin" />);
    // Hooks, Tenants, Plugins, Agents are developer-only.
    expect(screen.queryByRole("link", { name: /^Hooks$/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /租户|^Tenants$/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /插件|^Plugins$/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /^Agents?$|^Agent$/ })).toBeNull();
  });

  it("shows all developer pages when devMode is on", async () => {
    window.localStorage.setItem(DEV_MODE_KEY, "1");
    render(<Sidebar user="admin" />);
    // Effect hydrates dev-mode from localStorage — wait for the dev links.
    await waitFor(() => {
      expect(screen.getByRole("link", { name: /^Hooks$/ })).toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: /租户|^Tenants$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /插件|^Plugins$/ })).toBeInTheDocument();
    // Developer Settings link is still present at the bottom.
    expect(screen.getByRole("link", { name: /开发者设置|Developer Settings/i })).toBeInTheDocument();
  });

  it("resolveSidebarEntries returns operator+dev-settings in operator mode", () => {
    const entries = resolveSidebarEntries(false);
    // OPERATOR_ITEMS (9 entries — 1 is the Channels group) + dev-settings = 10 NavEntries.
    expect(entries).toHaveLength(SIDEBAR_OPERATOR_ITEMS.length + 1);
    expect(entries[entries.length - 1]).toBe(SIDEBAR_DEV_SETTINGS_ENTRY);
  });

  it("resolveSidebarEntries appends all 11 dev pages when devMode is on", () => {
    const entries = resolveSidebarEntries(true);
    expect(entries).toHaveLength(
      SIDEBAR_OPERATOR_ITEMS.length + SIDEBAR_DEV_ITEMS.length + 1,
    );
    expect(SIDEBAR_DEV_ITEMS).toHaveLength(11);
    // All dev items carry the isDeveloper flag.
    for (const entry of SIDEBAR_DEV_ITEMS) {
      expect("isDeveloper" in entry && entry.isDeveloper === true).toBe(true);
    }
  });

  it("renders the Channels group collapsed by default (no child links visible)", () => {
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // QQ / Telegram child links are not rendered until expanded.
    expect(screen.queryByRole("link", { name: /^QQ$/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Telegram$/ })).toBeNull();
  });

  it("click on the group toggle expands it and reveals the children", () => {
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: /^QQ$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Telegram$/ })).toBeInTheDocument();
  });

  it("Enter / Space on the toggle flips expanded", () => {
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    fireEvent.keyDown(toggle, { key: "Enter" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(toggle, { key: " " });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("auto-expands when the current route matches a child (QQ)", () => {
    mockPathname = "/channels/qq";
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Active child's anchor carries aria-current=page.
    const qq = screen.getByRole("link", { name: /^QQ$/ });
    expect(qq).toHaveAttribute("aria-current", "page");
    // The group label gets medium weight when a child is active
    // (Tidepool uses `font-medium` — lighter than the legacy semibold).
    expect(toggle.className).toMatch(/font-medium/);
  });

  it("auto-expands for the Telegram child route", () => {
    mockPathname = "/channels/telegram";
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const tg = screen.getByRole("link", { name: /Telegram$/ });
    expect(tg).toHaveAttribute("aria-current", "page");
  });

  it("sets role=group and aria-label on the group wrapper", () => {
    render(<Sidebar />);
    const wrapper = screen.getByTestId("sidebar-group-channels");
    expect(wrapper).toHaveAttribute("role", "group");
    // Either the English or Chinese label is acceptable depending on the
    // test runner's locale — the wrapper has one of them.
    const aria = wrapper.getAttribute("aria-label");
    expect(aria === "Channels" || aria === "通道").toBe(true);
  });

  it("removes the closed mobile drawer from keyboard navigation", async () => {
    installMatchMedia(true);
    const { container } = render(<Sidebar user="admin" />);
    const aside = container.querySelector("#admin-sidebar");
    expect(aside).not.toBeNull();

    await waitFor(() => {
      expect(aside).toHaveAttribute("aria-hidden", "true");
    });
    expect(aside).toHaveAttribute("inert");
  });
});
