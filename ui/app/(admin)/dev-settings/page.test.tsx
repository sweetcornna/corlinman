/**
 * Dev Settings page tests.
 *
 * Asserts:
 *   - Renders one card per hidden page (11 total).
 *   - The toggle reflects + writes the `useDevMode()` flag.
 *   - Toggling the switch persists to localStorage and shows the new state.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import * as React from "react";

import { i18next, initI18n } from "@/lib/i18n";
import { I18nextProvider } from "react-i18next";

import {
  DEV_MODE_KEY,
  __resetDevModeForTests,
} from "@/lib/dev-mode";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/dev-settings",
  useSearchParams: () => new URLSearchParams(),
}));

import DevSettingsPage, { DEV_PAGE_KEYS } from "./page";

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
  __resetDevModeForTests();
});

afterEach(() => {
  cleanup();
});

function renderPage() {
  return render(
    <I18nextProvider i18n={i18next}>
      <DevSettingsPage />
    </I18nextProvider>,
  );
}

describe("DevSettingsPage", () => {
  it("renders the dashboard header", () => {
    renderPage();
    expect(screen.getByTestId("dev-settings-page")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Developer Settings/i }),
    ).toBeInTheDocument();
  });

  it("renders one card per hidden developer page", () => {
    renderPage();
    expect(DEV_PAGE_KEYS).toHaveLength(10);
    for (const key of DEV_PAGE_KEYS) {
      expect(
        screen.getByTestId(`dev-settings-card-${key}`),
      ).toBeInTheDocument();
    }
  });

  it("links each card to its admin route", () => {
    renderPage();
    const config = screen.getByTestId("dev-settings-card-config");
    expect(config).toHaveAttribute("href", "/config");
    const evolution = screen.getByTestId("dev-settings-card-evolution");
    expect(evolution).toHaveAttribute("href", "/evolution");
  });

  it("the toggle is off by default", () => {
    renderPage();
    const toggle = screen.getByTestId("dev-settings-toggle");
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("clicking the toggle persists the new state to localStorage", () => {
    renderPage();
    const toggle = screen.getByTestId("dev-settings-toggle");
    expect(toggle).toHaveAttribute("aria-checked", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(window.localStorage.getItem(DEV_MODE_KEY)).toBe("1");
    // Flipping it back off persists too.
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(window.localStorage.getItem(DEV_MODE_KEY)).toBe("0");
  });

  it("hydrates the persisted devMode value on mount", () => {
    window.localStorage.setItem(DEV_MODE_KEY, "1");
    renderPage();
    const toggle = screen.getByTestId("dev-settings-toggle");
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });
});
