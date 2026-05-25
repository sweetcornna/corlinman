/**
 * Unit tests for the create-agent modal (W2.1).
 *
 * Covers the three submission-flow guards the spec calls out:
 *   - Submit stays disabled until the name matches `^[a-z][a-z0-9_-]*$`
 *   - 400 from the server renders the name conflict error inline
 *   - 409 from the server reveals the Force checkbox
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

// Mock the api before the component imports it. We keep the real
// CorlinmanApiError class around so the 400/409 paths still match the
// `instanceof` checks inside the component.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    createAgent: vi.fn(),
    fetchAgent: vi.fn(),
    listAgents: vi.fn(),
  };
});

// Toasts are not the focus here; stub the sonner surface so the
// component's `toast.success` / `toast.error` calls don't blow up.
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

import { CorlinmanApiError, createAgent } from "@/lib/api";
import { CreateAgentModal } from "../create-agent-modal";

const mockedCreate = vi.mocked(createAgent);

function renderModal(initialAgents = [] as Parameters<
  typeof CreateAgentModal
>[0]["initialAgents"]) {
  const onOpenChange = vi.fn();
  const onCreated = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <CreateAgentModal
        open
        onOpenChange={onOpenChange}
        onCreated={onCreated}
        initialAgents={initialAgents}
      />
    </QueryClientProvider>,
  );
  return { ...utils, onOpenChange, onCreated };
}

beforeEach(() => {
  mockedCreate.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("CreateAgentModal — submit gating", () => {
  it("keeps the submit button disabled until the name regex matches", () => {
    renderModal();
    const submit = screen.getByTestId(
      "create-agent-submit",
    ) as HTMLButtonElement;
    // Empty name → disabled.
    expect(submit.disabled).toBe(true);

    // Uppercase → disabled (fails the regex).
    fireEvent.change(screen.getByTestId("agent-name"), {
      target: { value: "BAD" },
    });
    expect(submit.disabled).toBe(true);

    // Starts with a digit → disabled.
    fireEvent.change(screen.getByTestId("agent-name"), {
      target: { value: "1bad" },
    });
    expect(submit.disabled).toBe(true);

    // Valid → enabled.
    fireEvent.change(screen.getByTestId("agent-name"), {
      target: { value: "my-agent" },
    });
    expect(submit.disabled).toBe(false);
    expect(mockedCreate).not.toHaveBeenCalled();
  });
});

describe("CreateAgentModal — submission flow", () => {
  it("renders the name conflict error on a 400 response", async () => {
    mockedCreate.mockRejectedValueOnce(
      new CorlinmanApiError(
        JSON.stringify({
          error: "agent_exists",
          message: "agent 'dupe' already exists",
        }),
        400,
      ),
    );
    renderModal();

    fireEvent.change(screen.getByTestId("agent-name"), {
      target: { value: "dupe" },
    });
    fireEvent.click(screen.getByTestId("create-agent-submit"));

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledTimes(1),
    );
    const err = await screen.findByTestId("agent-name-server-error");
    expect(err).toBeInTheDocument();
  });

  it("reveals the Force checkbox on a 409 shadows_builtin response", async () => {
    mockedCreate.mockRejectedValueOnce(
      new CorlinmanApiError(
        JSON.stringify({ error: "shadows_builtin" }),
        409,
      ),
    );
    renderModal();

    fireEvent.change(screen.getByTestId("agent-name"), {
      target: { value: "builtin-name" },
    });
    fireEvent.click(screen.getByTestId("create-agent-submit"));

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledTimes(1),
    );
    // Force checkbox is hidden by default and appears after the 409.
    const force = await screen.findByTestId("agent-force");
    expect(force).toBeInTheDocument();
  });
});
