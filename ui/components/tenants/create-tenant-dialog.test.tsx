/**
 * Unit tests for the create-tenant dialog (Phase 4 W1 4-1B).
 *
 * Covers:
 *   - Empty / uppercase slug client-side validation
 *   - Successful POST: dialog closes + onCreated fires with the slug
 *   - 400 invalid_tenant_slug → renders the slug error inline
 *   - 409 tenant_exists      → renders the form-level error inline
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

// Mock the tenant API before the component imports it.
vi.mock("@/lib/api/tenants", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/tenants")>(
    "@/lib/api/tenants",
  );
  return {
    ...actual,
    createTenant: vi.fn(),
  };
});

// Sonner toasts are not the focus of these tests; stub it.
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

import { createTenant } from "@/lib/api/tenants";
import { CorlinmanApiError } from "@/lib/api";
import { CreateTenantDialog } from "./create-tenant-dialog";

const mockedCreate = vi.mocked(createTenant);

function renderDialog(onCreated?: (slug: string) => void) {
  const onOpenChange = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <CreateTenantDialog
        open
        onOpenChange={onOpenChange}
        onCreated={onCreated}
      />
    </QueryClientProvider>,
  );
  return { ...utils, onOpenChange };
}

beforeEach(() => {
  mockedCreate.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("CreateTenantDialog — client-side validation", () => {
  it("rejects an empty slug without calling the API", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("create-tenant-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("tenant-slug-error")).toBeInTheDocument(),
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("rejects an uppercase slug with the lowercase hint", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("tenant-slug"), {
      target: { value: "ACME" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-username"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-password"), {
      target: { value: "hunter2hunter2" },
    });
    fireEvent.click(screen.getByTestId("create-tenant-submit"));
    const err = await screen.findByTestId("tenant-slug-error");
    expect(err.textContent ?? "").toMatch(/小写|lowercase/i);
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("rejects an empty admin password", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("tenant-slug"), {
      target: { value: "acme" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-username"), {
      target: { value: "admin" },
    });
    fireEvent.click(screen.getByTestId("create-tenant-submit"));
    expect(
      await screen.findByTestId("tenant-admin-password-error"),
    ).toBeInTheDocument();
    expect(mockedCreate).not.toHaveBeenCalled();
  });
});

describe("CreateTenantDialog — submission flow", () => {
  it("posts and closes on a 201 response", async () => {
    mockedCreate.mockResolvedValueOnce({ tenant_id: "acme" });
    const onCreated = vi.fn();
    const { onOpenChange } = renderDialog(onCreated);

    fireEvent.change(screen.getByTestId("tenant-slug"), {
      target: { value: "acme" },
    });
    // display_name sits behind the collapsed "advanced" disclosure.
    expect(screen.queryByTestId("tenant-display-name")).toBeNull();
    fireEvent.click(screen.getByTestId("tenant-toggle-advanced"));
    fireEvent.change(screen.getByTestId("tenant-display-name"), {
      target: { value: "ACME Industries" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-username"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-password"), {
      target: { value: "hunter2hunter2" },
    });
    fireEvent.click(screen.getByTestId("create-tenant-submit"));

    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
    expect(mockedCreate).toHaveBeenCalledWith({
      slug: "acme",
      display_name: "ACME Industries",
      admin_username: "admin",
      admin_password: "hunter2hunter2",
    });
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("acme"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders the slug error inline on a 400 invalid_tenant_slug", async () => {
    mockedCreate.mockRejectedValueOnce(
      new CorlinmanApiError(
        JSON.stringify({
          error: "invalid_tenant_slug",
          reason: "slug too long",
        }),
        400,
      ),
    );
    renderDialog();

    fireEvent.change(screen.getByTestId("tenant-slug"), {
      target: { value: "acme" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-username"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-password"), {
      target: { value: "hunter2hunter2" },
    });
    fireEvent.click(screen.getByTestId("create-tenant-submit"));

    const err = await screen.findByTestId("tenant-slug-error");
    expect(err.textContent ?? "").toMatch(/slug too long/);
  });

  it("renders the form error inline on a 409 tenant_exists", async () => {
    mockedCreate.mockRejectedValueOnce(
      new CorlinmanApiError(
        JSON.stringify({ error: "tenant_exists" }),
        409,
      ),
    );
    renderDialog();

    fireEvent.change(screen.getByTestId("tenant-slug"), {
      target: { value: "acme" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-username"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByTestId("tenant-admin-password"), {
      target: { value: "hunter2hunter2" },
    });
    fireEvent.click(screen.getByTestId("create-tenant-submit"));

    expect(
      await screen.findByTestId("create-tenant-form-error"),
    ).toBeInTheDocument();
  });
});
