import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import ProtocolPlaygroundPage from "./page";

// W2.3: the page now renders <AgentPicker> which fetches via
// `useQuery(listAgents)`. Wrap the page in a QueryClientProvider so the
// hook resolves; stub `fetch` so the agent list resolves to ``[]`` (the
// picker just renders an "empty" footnote).
function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ProtocolPlaygroundPage />
    </QueryClientProvider>,
  );
}

function mockMatchMedia(opts: { reduce?: boolean } = {}) {
  const { reduce = false } = opts;
  window.matchMedia = vi.fn().mockImplementation((query: string) => {
    const matches =
      query === "(prefers-reduced-motion: reduce)" ? reduce : false;
    return {
      matches,
      media: query,
      onchange: null,
      addEventListener: () => void 0,
      removeEventListener: () => void 0,
      addListener: () => void 0,
      removeListener: () => void 0,
      dispatchEvent: () => false,
    };
  }) as typeof window.matchMedia;
}

// Point the mock generators at 0-delay so tokens flush synchronously on each
// `await vi.advanceTimersByTime` tick. This keeps the page test fast and
// deterministic without depending on wall-clock timing.
vi.mock("@/lib/mocks/protocol-streams", async () => {
  async function* mkGen(label: string) {
    const toks = [label, " ", "one", " ", "two", " ", "three"];
    for (const t of toks) {
      yield t;
      await Promise.resolve();
    }
  }
  return {
    streamBlockProtocol: () => mkGen("BLOCK"),
    streamFunctionCall: () => mkGen("FUNCTION"),
  };
});

beforeEach(() => {
  mockMatchMedia({ reduce: false });
  // W2.3: <AgentPicker> calls `listAgents()` via apiFetch on mount.
  // Resolve to an empty list so the picker renders its "empty" state
  // without throwing — these tests are about the protocol panes, not
  // the picker behaviour.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      ({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => [],
        text: async () => "[]",
      }) as unknown as Response,
    ),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ProtocolPlaygroundPage", () => {
  it("renders both panes with headers and run button", () => {
    renderPage();
    expect(screen.getByTestId("pane-block")).toBeInTheDocument();
    expect(screen.getByTestId("pane-function-call")).toBeInTheDocument();
    expect(screen.getByTestId("run-button")).toBeInTheDocument();
    expect(screen.getByTestId("prompt-input")).toBeInTheDocument();
    expect(screen.getByTestId("split-pane-divider")).toBeInTheDocument();
    // Divider is a proper ARIA separator with value range.
    const divider = screen.getByTestId("split-pane-divider");
    expect(divider.getAttribute("role")).toBe("separator");
    expect(divider.getAttribute("aria-orientation")).toBe("vertical");
    expect(divider.getAttribute("aria-valuemin")).toBe("0");
    expect(divider.getAttribute("aria-valuemax")).toBe("100");
  });

  it("clicking Run both streams tokens into both panes", async () => {
    renderPage();
    await act(async () => {
      fireEvent.click(screen.getByTestId("run-button"));
    });
    await waitFor(
      () => {
        expect(
          screen.getByTestId("stream-block").textContent ?? "",
        ).toContain("BLOCK");
        expect(
          screen.getByTestId("stream-function-call").textContent ?? "",
        ).toContain("FUNCTION");
      },
      { timeout: 2000 },
    );
    // Both panes have grown past the empty state.
    expect(
      (screen.getByTestId("stream-block").textContent ?? "").length,
    ).toBeGreaterThan(3);
    expect(
      (screen.getByTestId("stream-function-call").textContent ?? "").length,
    ).toBeGreaterThan(3);
  });

  it("under reduced-motion renders streams without per-token fadeUp wrapper", async () => {
    mockMatchMedia({ reduce: true });
    renderPage();
    await act(async () => {
      fireEvent.click(screen.getByTestId("run-button"));
    });
    await waitFor(
      () => {
        expect(
          screen.getByTestId("stream-block").textContent ?? "",
        ).toContain("BLOCK");
      },
      { timeout: 2000 },
    );
    // Reduced-motion path renders <pre> rather than the animated <div>.
    const el = screen.getByTestId("stream-block");
    expect(el.tagName.toLowerCase()).toBe("pre");
  });
});
