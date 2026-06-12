import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";

vi.mock("./cmdk-palette", () => ({
  CommandPaletteProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

import { Providers } from "./providers";
import { i18next, LANG_STORAGE_KEY } from "@/lib/i18n";

describe("Providers", () => {
  beforeEach(() => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
  });

  afterEach(async () => {
    window.localStorage.clear();
    await i18next.changeLanguage("zh-CN");
  });

  it("applies persisted language after hydration", async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, "en");

    await act(async () => {
      render(
        <Providers>
          <span>mounted</span>
        </Providers>,
      );
    });

    await waitFor(() => expect(i18next.language).toBe("en"));
  });
});
