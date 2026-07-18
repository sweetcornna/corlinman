/**
 * Sidebar tests — covers:
 *   1. Collapsible "Channels" group (default-collapsed, click+keyboard
 *      expansion, auto-expand on matching route).
 *   2. Operator-vs-Developer mode filtering: by default only the operator
 *      sections render; flipping `useDevMode()` appends the Developer
 *      section.
 *   3. Registry-driven sections (PR6): every assertion about counts and
 *      membership derives from `sidebarSections()` in `@/lib/nav-registry`
 *      so the test tracks the single source of truth instead of magic
 *      numbers. PR4 removed the Credentials row (/credentials is a
 *      redirect stub into /models now).
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
  within,
} from "@testing-library/react";
import React from "react";

import {
  DEV_MODE_KEY,
  __resetDevModeForTests,
} from "@/lib/dev-mode";
import { sidebarSections } from "@/lib/nav-registry";

let mockPathname = "/";

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { Sidebar } from "./sidebar";

/** Flat (non-group) sidebar link count for the given dev-mode state. */
function flatItemCount(devMode: boolean): number {
  return sidebarSections(devMode)
    .flatMap((s) => s.entries)
    .filter((e) => e.kind === "item").length;
}

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

  it("overlays the avatar initial on the presence orb without displacing it", () => {
    // Regression (PR-F5): the initial must be an absolute overlay centered
    // on the orb, and the orb itself must NOT carry `absolute` — otherwise
    // `.presence-orb`'s own position:relative drops it back into flow and
    // shoves the "A"/"C" out of the pearl.
    const { container } = render(<Sidebar user="admin" />);
    const initial = screen.getByTestId("nav-user-initial");
    // The initial shows the uppercased first character.
    expect(initial).toHaveTextContent("A");
    // It is positioned as a centered absolute overlay.
    expect(initial.className).toMatch(/\babsolute\b/);
    expect(initial.className).toMatch(/\binset-0\b/);
    // The orb (the .presence-orb sibling) stays in-flow — no `absolute`.
    const orb = container.querySelector(".presence-orb") as HTMLElement;
    expect(orb).not.toBeNull();
    expect(orb.className).not.toMatch(/\babsolute\b/);
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

  it("renders a header + wrapper for every operator section", () => {
    const { container } = render(<Sidebar user="admin" />);
    const nav = container.querySelector("nav");
    expect(nav).not.toBeNull();
    const sections = sidebarSections(false);
    expect(sections.map((s) => s.id)).toEqual([
      "chat",
      "ops",
      "config",
      "system",
    ]);
    for (const section of sections) {
      expect(
        screen.getByTestId(`sidebar-section-${section.id}`),
      ).toBeInTheDocument();
    }
    // No developer section wrapper in operator mode.
    expect(screen.queryByTestId("sidebar-section-developer")).toBeNull();
  });

  it("renders one link per flat registry entry (groups collapsed)", () => {
    const { container } = render(<Sidebar user="admin" />);
    const nav = container.querySelector("nav") as HTMLElement;
    // Channels stays collapsed by default, so only kind:"item" entries
    // render as links.
    expect(within(nav).getAllByRole("link")).toHaveLength(flatItemCount(false));
    // The Credentials row was removed in the PR4 model-hub consolidation.
    expect(
      within(nav)
        .getAllByRole("link")
        .some((a) => a.getAttribute("href") === "/credentials"),
    ).toBe(false);
  });

  it("dev mode appends the developer section with all developer pages", async () => {
    window.localStorage.setItem(DEV_MODE_KEY, "1");
    const { container } = render(<Sidebar user="admin" />);
    await waitFor(() => {
      expect(
        screen.getByTestId("sidebar-section-developer"),
      ).toBeInTheDocument();
    });
    const nav = container.querySelector("nav") as HTMLElement;
    expect(within(nav).getAllByRole("link")).toHaveLength(flatItemCount(true));
    // Every developer page renders inside the developer section wrapper.
    const devSection = screen.getByTestId("sidebar-section-developer");
    const devEntries = sidebarSections(true).find(
      (s) => s.id === "developer",
    )?.entries;
    expect(devEntries?.length).toBeGreaterThan(0);
    expect(within(devSection).getAllByRole("link")).toHaveLength(
      devEntries?.filter((e) => e.kind === "item").length ?? -1,
    );
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

  it("exposes all 7 channel leaves when the group is expanded", () => {
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    fireEvent.click(toggle);
    // QQ + Telegram + Discord + Slack + Feishu + WeChat Official + QQ Official.
    expect(screen.getByRole("link", { name: /^QQ$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Telegram$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Discord/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Slack/i })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Feishu|飞书/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /WeChat Official|微信公众号/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /QQ Official|QQ 官方/i }),
    ).toBeInTheDocument();
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
