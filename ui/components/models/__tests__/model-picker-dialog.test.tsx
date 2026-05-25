/**
 * Smoke test for <ModelPickerDialog>.
 *
 * Validates the W2.2 happy path:
 *   1. open with 2 providers
 *   2. first provider auto-selected → models list resolves
 *   3. double-click a model → onConfirm fires with {provider, model}
 *
 * We stub `fetch` to drive `api.getProviderModels` (which goes through
 * the shared `apiFetch` wrapper, so the stub must surface `headers.get`
 * and `status` in addition to `ok` + `json`).
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";

import { ModelPickerDialog } from "../model-picker-dialog";
import { initI18n } from "@/lib/i18n";

const i18n = initI18n();

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
    </QueryClientProvider>
  );
}

const PROVIDERS = [
  { name: "anthropic", kind: "anthropic", enabled: true },
  { name: "openai", kind: "openai", enabled: true },
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("<ModelPickerDialog>", () => {
  beforeEach(() => {
    // `apiFetch` reads `res.headers.get("x-request-id")` + `res.status`
    // in addition to `res.ok` + `res.json()`, so the stub must surface
    // a `headers.get` callable and a `status` field.
    const respond = (body: unknown) =>
      ({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => body,
        text: async () => JSON.stringify(body),
      }) as unknown as Response;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/admin/providers/anthropic/models")) {
          return respond({
            models: [
              { id: "claude-opus-4-7" },
              { id: "claude-sonnet-4-5" },
            ],
          });
        }
        if (url.includes("/admin/providers/openai/models")) {
          return respond({ models: [{ id: "gpt-5" }] });
        }
        return {
          ok: false,
          status: 404,
          headers: { get: () => null },
          json: async () => ({}),
          text: async () => "",
        } as unknown as Response;
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders providers, loads models, and confirms on double-click", async () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      wrap(
        <ModelPickerDialog
          open
          providers={PROVIDERS}
          onConfirm={onConfirm}
          onClose={onClose}
        />,
      ),
    );

    // Provider list visible.
    expect(
      screen.getByTestId("model-picker-provider-anthropic"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("model-picker-provider-openai"),
    ).toBeInTheDocument();

    // First provider auto-selected → models load.
    await waitFor(() => {
      expect(
        screen.getByTestId("model-picker-model-claude-opus-4-7"),
      ).toBeInTheDocument();
    });

    // Double-click confirms with provider+model.
    fireEvent.doubleClick(
      screen.getByTestId("model-picker-model-claude-opus-4-7"),
    );

    await waitFor(() => {
      expect(onConfirm).toHaveBeenCalledWith({
        provider: "anthropic",
        model: "claude-opus-4-7",
      });
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("switches model list when a different provider is clicked", async () => {
    render(
      wrap(
        <ModelPickerDialog
          open
          providers={PROVIDERS}
          onConfirm={() => {}}
          onClose={() => {}}
        />,
      ),
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("model-picker-model-claude-opus-4-7"),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("model-picker-provider-openai"));

    await waitFor(() => {
      expect(screen.getByTestId("model-picker-model-gpt-5")).toBeInTheDocument();
    });
  });

  it("shows backend error when models endpoint returns {error}", async () => {
    vi.unstubAllGlobals();
    const respond = (body: unknown) =>
      ({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => body,
        text: async () => JSON.stringify(body),
      }) as unknown as Response;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/admin/providers/anthropic/models")) {
          return respond({ models: [], error: "provider_not_found" });
        }
        return respond({ models: [] });
      }),
    );

    render(
      wrap(
        <ModelPickerDialog
          open
          providers={PROVIDERS}
          onConfirm={() => {}}
          onClose={() => {}}
        />,
      ),
    );

    await waitFor(() => {
      expect(screen.getByText(/provider_not_found/i)).toBeInTheDocument();
    });
  });

  it("does not get stuck on loading after provider switch race", async () => {
    vi.unstubAllGlobals();
    const respond = (body: unknown) =>
      ({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => body,
        text: async () => JSON.stringify(body),
      }) as unknown as Response;

    const openaiFirst = deferred<Response>();
    let openaiCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/admin/providers/anthropic/models")) {
          return respond({ models: [{ id: "claude-opus-4-7" }] });
        }
        if (url.includes("/admin/providers/openai/models")) {
          openaiCalls += 1;
          if (openaiCalls === 1) return openaiFirst.promise;
          return respond({ models: [{ id: "gpt-5" }] });
        }
        return respond({ models: [] });
      }),
    );

    render(
      wrap(
        <ModelPickerDialog
          open
          providers={PROVIDERS}
          onConfirm={() => {}}
          onClose={() => {}}
        />,
      ),
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("model-picker-model-claude-opus-4-7"),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("model-picker-provider-openai"));
    fireEvent.click(screen.getByTestId("model-picker-provider-anthropic"));
    openaiFirst.resolve(respond({ models: [{ id: "gpt-5" }] }));

    fireEvent.click(screen.getByTestId("model-picker-provider-openai"));

    await waitFor(() => {
      expect(screen.getByTestId("model-picker-model-gpt-5")).toBeInTheDocument();
    });
  });
});
