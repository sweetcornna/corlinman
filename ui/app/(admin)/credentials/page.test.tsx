/**
 * /credentials redirect-stub tests (PR4 model-hub consolidation).
 *
 * The credentials manager moved to `/models` (OAuth panel on the providers
 * tab, raw credential fields on the advanced tab); this page only replaces
 * the URL on mount and renders a fallback link. The old behavior suite
 * moved with the code:
 *   - components/model-hub/__tests__/credentials-advanced.test.tsx
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { replaceMock } = vi.hoisted(() => ({ replaceMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: replaceMock,
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/credentials",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
}));

import CredentialsPage from "./page";

describe("CredentialsPage (redirect stub)", () => {
  beforeEach(() => {
    replaceMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("replaces the URL with /models?tab=providers on mount", () => {
    render(<CredentialsPage />);
    expect(replaceMock).toHaveBeenCalledWith("/models?tab=providers");
  });

  it("renders a fallback link to the canonical page", () => {
    render(<CredentialsPage />);
    const link = screen.getByTestId("credentials-moved-link");
    expect(link).toHaveAttribute("href", "/models?tab=providers");
  });
});
