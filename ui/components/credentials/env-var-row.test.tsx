/**
 * EnvVarRow unit tests (Wave 2.3).
 *
 * Coverage:
 *   1. Unset row renders the "Add" button + env-var hint, no preview.
 *   2. Clicking Add → input is focused → typing a value → Save fires
 *      the onSave callback with the typed value (paste path tested
 *      separately via a clipboard paste event).
 *   3. Set rows render the masked dots; clicking the eye reveals the
 *      "…last4" preview; clicking again re-masks. Plaintext never
 *      appears on screen.
 *   4. Delete button fires the onDelete callback (the actual
 *      confirmation dialog lives in the page; the row's responsibility
 *      is just to surface the intent).
 *
 * Tests run under the zh-CN bundle so assertions read Chinese strings
 * (matches the rest of the suite, see `vitest.setup.ts`).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const { toastMessage } = vi.hoisted(() => ({
  toastMessage: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    message: toastMessage,
  },
}));

import { EnvVarRow } from "./env-var-row";

afterEach(() => {
  cleanup();
  toastMessage.mockClear();
});

describe("EnvVarRow", () => {
  it("renders an unset row with an Add button and env hint", () => {
    const onSave = vi.fn();
    const onDelete = vi.fn();
    render(
      <EnvVarRow
        provider="openai"
        field={{
          key: "api_key",
          set: false,
          preview: null,
          env_ref: "OPENAI_API_KEY",
        }}
        onSave={onSave}
        onDelete={onDelete}
      />,
    );

    expect(
      screen.getByTestId("cred-openai-api_key-add"),
    ).toBeInTheDocument();
    // env_ref hint surfaces in the unset row body.
    expect(screen.getByText(/OPENAI_API_KEY/)).toBeInTheDocument();
    // The set-row reveal control should NOT exist here.
    expect(
      screen.queryByTestId("cred-openai-api_key-reveal"),
    ).not.toBeInTheDocument();
  });

  it("Add → type → Save calls onSave with the typed value", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onDelete = vi.fn();
    render(
      <EnvVarRow
        provider="openai"
        field={{
          key: "api_key",
          set: false,
          preview: null,
          env_ref: "OPENAI_API_KEY",
        }}
        onSave={onSave}
        onDelete={onDelete}
      />,
    );

    fireEvent.click(screen.getByTestId("cred-openai-api_key-add"));
    const input = screen.getByTestId(
      "cred-openai-api_key-input",
    ) as HTMLInputElement;
    // First keystroke surfaces the paste-hint toast without blocking.
    fireEvent.change(input, { target: { value: "s" } });
    expect(toastMessage).toHaveBeenCalledTimes(1);

    fireEvent.change(input, { target: { value: "sk-typed-value-1234" } });
    fireEvent.click(screen.getByTestId("cred-openai-api_key-save"));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith("sk-typed-value-1234"),
    );
    await waitFor(() =>
      expect(
        screen.queryByTestId("cred-openai-api_key-input"),
      ).not.toBeInTheDocument(),
    );
  });

  it("paste event trims and uses the pasted value over keystrokes", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onDelete = vi.fn();
    render(
      <EnvVarRow
        provider="openai"
        field={{
          key: "api_key",
          set: false,
          preview: null,
          env_ref: null,
        }}
        onSave={onSave}
        onDelete={onDelete}
      />,
    );
    fireEvent.click(screen.getByTestId("cred-openai-api_key-add"));
    const input = screen.getByTestId(
      "cred-openai-api_key-input",
    ) as HTMLInputElement;

    fireEvent.paste(input, {
      clipboardData: { getData: () => "  sk-pasted-value  " },
    });
    fireEvent.click(screen.getByTestId("cred-openai-api_key-save"));
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith("sk-pasted-value"),
    );
    await waitFor(() =>
      expect(
        screen.queryByTestId("cred-openai-api_key-input"),
      ).not.toBeInTheDocument(),
    );
    // The paste path shouldn't have triggered the type-only nudge.
    expect(toastMessage).not.toHaveBeenCalled();
  });

  it("set row toggles cleartext visibility through the eye-icon", async () => {
    const onSave = vi.fn();
    const onDelete = vi.fn();
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ value: "sk-cleartext-secret" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    try {
      render(
        <EnvVarRow
          provider="openai"
          field={{
            key: "api_key",
            set: true,
            preview: "…xyz9",
            env_ref: "OPENAI_API_KEY",
          }}
          onSave={onSave}
          onDelete={onDelete}
        />,
      );

      // Initial render: dots, no cleartext yet.
      expect(
        screen.queryByTestId("cred-openai-api_key-preview-cleartext"),
      ).not.toBeInTheDocument();

      fireEvent.click(screen.getByTestId("cred-openai-api_key-reveal"));
      await waitFor(() =>
        expect(
          screen.getByTestId("cred-openai-api_key-preview-cleartext"),
        ).toHaveTextContent("sk-cleartext-secret"),
      );

      // Clicking again re-masks; no extra fetch since the value is cached.
      fireEvent.click(screen.getByTestId("cred-openai-api_key-reveal"));
      expect(
        screen.queryByTestId("cred-openai-api_key-preview-cleartext"),
      ).not.toBeInTheDocument();
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it("delete button fires onDelete callback", () => {
    const onSave = vi.fn();
    const onDelete = vi.fn();
    render(
      <EnvVarRow
        provider="openai"
        field={{
          key: "api_key",
          set: true,
          preview: "…abcd",
          env_ref: "OPENAI_API_KEY",
        }}
        onSave={onSave}
        onDelete={onDelete}
      />,
    );

    fireEvent.click(screen.getByTestId("cred-openai-api_key-delete"));
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it("env-shaped credentials show env: <name> in the value slot, no eye icon", () => {
    const onSave = vi.fn();
    const onDelete = vi.fn();
    render(
      <EnvVarRow
        provider="openai"
        field={{
          key: "api_key",
          set: true,
          preview: null,
          env_ref: "MY_ENV_KEY",
        }}
        onSave={onSave}
        onDelete={onDelete}
      />,
    );

    expect(screen.getByText(/MY_ENV_KEY/)).toBeInTheDocument();
    // No reveal control when preview is null — there's nothing to unmask.
    expect(
      screen.queryByTestId("cred-openai-api_key-reveal"),
    ).not.toBeInTheDocument();
  });
});
