import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { AssetRecord } from "@/lib/api/personas";

// Return the raw key so assertions don't depend on the translated copy.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

// Hoisted so the vi.mock factory can reference them safely.
const { listAssetsMock, uploadAssetMock, deleteAssetMock } = vi.hoisted(() => ({
  listAssetsMock: vi.fn(),
  uploadAssetMock: vi.fn(),
  deleteAssetMock: vi.fn(),
}));

vi.mock("@/lib/api/personas", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/personas")>();
  return {
    ...actual,
    listAssets: (...a: unknown[]) => listAssetsMock(...a),
    uploadAsset: (...a: unknown[]) => uploadAssetMock(...a),
    deleteAsset: (...a: unknown[]) => deleteAssetMock(...a),
  };
});

import { QzoneRefImagePicker } from "@/components/scheduler/qzone-ref-image-picker";

function refAsset(label: string, over: Partial<AssetRecord> = {}): AssetRecord {
  return {
    id: `id-${label}`,
    persona_id: "grantley",
    kind: "reference",
    label,
    file_name: `${label}.png`,
    mime: "image/png",
    size_bytes: 1234,
    sha256: "sha-abc",
    created_at_ms: 0,
    url: `/admin/personas/grantley/assets/id-${label}`,
    ...over,
  };
}

function renderPicker(opts: {
  personaId?: string;
  selected?: string[];
  onChange?: (labels: string[]) => void;
} = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const onChange = opts.onChange ?? vi.fn();
  const utils = render(
    <QueryClientProvider client={qc}>
      <QzoneRefImagePicker
        personaId={opts.personaId ?? "grantley"}
        selected={opts.selected ?? []}
        onChange={onChange}
      />
    </QueryClientProvider>,
  );
  return { ...utils, onChange };
}

beforeEach(() => {
  listAssetsMock.mockReset().mockResolvedValue([]);
  uploadAssetMock.mockReset();
  deleteAssetMock.mockReset();
});

afterEach(cleanup);

describe("QzoneRefImagePicker", () => {
  it("renders reference thumbnails (filtering emoji) and toggles selection", async () => {
    listAssetsMock.mockResolvedValue([
      refAsset("cat"),
      refAsset("dog"),
      refAsset("smile", { kind: "emoji", id: "id-smile" }),
    ]);
    const onChange = vi.fn();
    renderPicker({ selected: [], onChange });

    await screen.findByTestId("qzone-ref-toggle-cat");
    expect(screen.getByTestId("qzone-ref-toggle-dog")).toBeInTheDocument();
    // emoji assets never surface in the reference picker
    expect(screen.queryByTestId("qzone-ref-toggle-smile")).toBeNull();

    fireEvent.click(screen.getByTestId("qzone-ref-toggle-cat"));
    expect(onChange).toHaveBeenCalledWith(["cat"]);
  });

  it("marks an already-selected asset and toggles it off", async () => {
    listAssetsMock.mockResolvedValue([refAsset("cat")]);
    const onChange = vi.fn();
    renderPicker({ selected: ["cat"], onChange });

    const cell = await screen.findByTestId("qzone-ref-cell-cat");
    expect(cell).toHaveAttribute("data-selected", "true");
    fireEvent.click(screen.getByTestId("qzone-ref-toggle-cat"));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("renders dashed 'missing' chips for deleted-but-referenced labels and drops them on click", async () => {
    listAssetsMock.mockResolvedValue([refAsset("cat")]);
    const onChange = vi.fn();
    renderPicker({ selected: ["cat", "ghost"], onChange });

    await screen.findByTestId("qzone-ref-toggle-cat");
    const missing = await screen.findByTestId("qzone-ref-missing-ghost");
    expect(missing).toBeInTheDocument();
    // a label with a live asset is NOT a missing chip
    expect(screen.queryByTestId("qzone-ref-missing-cat")).toBeNull();

    fireEvent.click(missing);
    expect(onChange).toHaveBeenCalledWith(["cat"]);
  });

  it("shows the >8 cap hint aligned with the backend _MAX_REFS", async () => {
    listAssetsMock.mockResolvedValue([]);
    const selected = Array.from({ length: 9 }, (_, i) => `r${i}`);
    renderPicker({ selected });
    expect(await screen.findByTestId("qzone-ref-cap-hint")).toBeInTheDocument();
  });

  it("uploads a picked file and auto-selects the stored label", async () => {
    listAssetsMock.mockResolvedValue([]);
    uploadAssetMock.mockResolvedValue(refAsset("happy-cat"));
    const onChange = vi.fn();
    renderPicker({ selected: [], onChange });

    await screen.findByTestId("qzone-ref-file");
    const file = new File(["x"], "Happy Cat.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("qzone-ref-file"), {
      target: { files: [file] },
    });

    await waitFor(() =>
      expect(uploadAssetMock).toHaveBeenCalledWith(
        "grantley",
        "reference",
        "happy-cat",
        file,
      ),
    );
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(["happy-cat"]));
  });

  it("suffixes a colliding upload label with -2", async () => {
    listAssetsMock.mockResolvedValue([refAsset("happy-cat")]);
    uploadAssetMock.mockResolvedValue(refAsset("happy-cat-2"));
    renderPicker({ selected: [] });

    await screen.findByTestId("qzone-ref-toggle-happy-cat");
    const file = new File(["x"], "Happy Cat.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("qzone-ref-file"), {
      target: { files: [file] },
    });

    await waitFor(() =>
      expect(uploadAssetMock).toHaveBeenCalledWith(
        "grantley",
        "reference",
        "happy-cat-2",
        file,
      ),
    );
  });

  it("deletes a reference through the confirm dialog", async () => {
    listAssetsMock.mockResolvedValue([refAsset("cat")]);
    deleteAssetMock.mockResolvedValue(undefined);
    renderPicker({ selected: [] });

    await screen.findByTestId("qzone-ref-toggle-cat");
    fireEvent.click(screen.getByTestId("qzone-ref-delete-cat"));

    const confirm = await screen.findByTestId("qzone-ref-delete-confirm-confirm");
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(deleteAssetMock).toHaveBeenCalledWith("grantley", "id-cat"),
    );
  });

  it("prompts to pick a persona when none is chosen", () => {
    renderPicker({ personaId: "" });
    expect(screen.getByTestId("qzone-ref-pick-persona")).toBeInTheDocument();
    expect(listAssetsMock).not.toHaveBeenCalled();
  });
});
