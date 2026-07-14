/**
 * /providers redirect-stub tests (PR4 model-hub consolidation).
 *
 * The providers admin surface moved to `/models?tab=providers`; this page
 * only replaces the URL on mount and renders a fallback link. The old
 * behavior suites moved with the code:
 *   - components/model-hub/__tests__/providers-admin-content.test.tsx
 *   - components/model-hub/__tests__/alias-helpers.test.tsx
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
  usePathname: () => "/providers",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
}));

import ProvidersPage from "./page";

describe("ProvidersPage (redirect stub)", () => {
  beforeEach(() => {
    replaceMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("replaces the URL with /models?tab=providers on mount", () => {
    render(<ProvidersPage />);
    expect(replaceMock).toHaveBeenCalledWith("/models?tab=providers");
  });

  it("renders a fallback link to the canonical page", () => {
    render(<ProvidersPage />);
    const link = screen.getByTestId("providers-moved-link");
    expect(link).toHaveAttribute("href", "/models?tab=providers");
  });
});
