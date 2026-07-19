"use client";

/**
 * `<QzoneRefImagePicker>` — reference-image (立绘) picker for a QZone
 * daily-publish job.
 *
 * Loads the persona's `reference` assets, renders them as a selectable
 * thumbnail grid, and hosts the upload / delete plumbing so an operator
 * can curate the pack the `qzone.daily_publish` builtin attaches when it
 * posts. Selection is controlled by the parent (a list of asset *labels*,
 * because that's what a scheduler job persists) — `selected` in,
 * `onChange` out.
 *
 * Cache consistency: the asset query MUST use the exact same queryKey as
 * the persona studio page (`["admin", "personas", personaId, "assets"]`)
 * so an upload here invalidates the studio's grid and vice-versa.
 *
 * A job can reference a label whose asset was later deleted out-of-band —
 * we render those as dashed "missing" chips (never silently pruned) so the
 * operator can consciously drop them. The backend only feeds the model the
 * first {@link REFS_VISIBLE_CAP} refs, so selecting more shows a hint.
 */

import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Check, ImagePlus, Trash2, X } from "@/components/icons";
import {
  ASSET_ALLOWED_MIMES,
  ASSET_LABEL_RE,
  ASSET_MAX_BYTES,
  AssetUploadError,
  deleteAsset,
  listAssets,
  slugifyAssetLabel,
  uploadAsset,
  type AssetRecord,
} from "@/lib/api/personas";

/** The gateway `qzone.daily_publish` builtin only forwards the first N
 * reference images to the image model (`_MAX_REFS = 8`). Kept in sync so
 * the picker can warn when a job selects more than the model will see. */
export const REFS_VISIBLE_CAP = 8;

export interface QzoneRefImagePickerProps {
  /** Persona whose reference assets are shown. Empty string = no persona
   * chosen yet (the form's persona dropdown is still on its placeholder). */
  personaId: string;
  /** Currently-selected asset labels (what the job persists). */
  selected: string[];
  /** Emits the next label list on every selection / upload change. */
  onChange: (labels: string[]) => void;
}

export function QzoneRefImagePicker({
  personaId,
  selected,
  onChange,
}: QzoneRefImagePickerProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const [pendingDelete, setPendingDelete] = React.useState<AssetRecord | null>(
    null,
  );

  // MUST match the persona studio page's key verbatim for cache coherence.
  const assetsKey = React.useMemo(
    () => ["admin", "personas", personaId, "assets"] as const,
    [personaId],
  );

  const assetsQuery = useQuery<AssetRecord[]>({
    queryKey: assetsKey,
    queryFn: () => listAssets(personaId),
    enabled: Boolean(personaId),
  });

  const refs = React.useMemo(
    () => (assetsQuery.data ?? []).filter((a) => a.kind === "reference"),
    [assetsQuery.data],
  );

  const selectedSet = React.useMemo(() => new Set(selected), [selected]);

  // Labels the job still references but which no longer have an asset row.
  const missing = React.useMemo(
    () => selected.filter((label) => !refs.some((r) => r.label === label)),
    [selected, refs],
  );

  const refresh = React.useCallback(
    () => queryClient.invalidateQueries({ queryKey: assetsKey }),
    [assetsKey, queryClient],
  );

  function toggle(label: string) {
    if (selectedSet.has(label)) {
      onChange(selected.filter((l) => l !== label));
    } else {
      onChange([...selected, label]);
    }
  }

  /** Suffix a base label with `-2`, `-3`, … until it doesn't collide with
   * an existing reference label (the backend also 409s duplicates, but a
   * client-side rename keeps a bulk upload from failing mid-way). */
  function uniqueLabel(base: string): string {
    const taken = new Set(refs.map((r) => r.label));
    if (!taken.has(base)) return base;
    for (let n = 2; n < 1000; n += 1) {
      const candidate = `${base}-${n}`.slice(0, 64);
      if (!taken.has(candidate)) return candidate;
    }
    return base;
  }

  function onPick() {
    fileInputRef.current?.click();
  }

  async function onFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const input = e.target;
    const files = input.files ? Array.from(input.files) : [];
    // Reset so re-picking the same filename fires `change` again.
    input.value = "";
    // Accumulate locally: `selected` is a controlled prop and won't update
    // between awaits within this loop, so batch the auto-selects.
    let nextSelected = selected;
    for (const file of files) {
      const label = await uploadOne(file);
      if (label && !nextSelected.includes(label)) {
        nextSelected = [...nextSelected, label];
      }
    }
    if (nextSelected !== selected) onChange(nextSelected);
  }

  /** Validate + upload one file. Returns the stored label on success (for
   * auto-selection) or `null` on any client/​server rejection. */
  async function uploadOne(file: File): Promise<string | null> {
    if (!ASSET_ALLOWED_MIMES.includes(file.type)) {
      toast.error(t("schedulerQzone.refs.uploadFail", { msg: file.type }));
      return null;
    }
    if (file.size > ASSET_MAX_BYTES) {
      toast.error(t("schedulerQzone.refs.uploadFail", { msg: file.name }));
      return null;
    }
    const label = uniqueLabel(slugifyAssetLabel(file.name) || "reference");
    if (!ASSET_LABEL_RE.test(label)) {
      toast.error(t("schedulerQzone.refs.uploadFail", { msg: file.name }));
      return null;
    }
    try {
      await uploadAsset(personaId, "reference", label, file);
      await refresh();
      toast.success(t("schedulerQzone.refs.uploadOk", { label }));
      return label;
    } catch (err) {
      const msg =
        err instanceof AssetUploadError
          ? err.code
          : err instanceof Error
            ? err.message
            : String(err);
      toast.error(t("schedulerQzone.refs.uploadFail", { msg }));
      return null;
    }
  }

  async function confirmDelete() {
    const target = pendingDelete;
    setPendingDelete(null);
    if (!target) return;
    try {
      await deleteAsset(personaId, target.id);
      await refresh();
      toast.success(t("schedulerQzone.refs.deleted", { label: target.label }));
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(t("schedulerQzone.refs.deleteFail", { msg }));
    }
  }

  const header = (
    <div className="flex items-start justify-between gap-3">
      <div className="flex flex-col gap-0.5">
        <span className="text-[13px] font-medium text-sg-ink-2">
          {t("schedulerQzone.refs.title")}
        </span>
        <span className="text-xs text-sg-ink-4">
          {t("schedulerQzone.refs.help")}
        </span>
      </div>
      {personaId ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onPick}
          data-testid="qzone-ref-upload"
        >
          <ImagePlus className="h-3.5 w-3.5" aria-hidden />
          {t("schedulerQzone.refs.upload")}
        </Button>
      ) : null}
      <input
        ref={fileInputRef}
        type="file"
        accept={ASSET_ALLOWED_MIMES.join(",")}
        multiple
        className="hidden"
        onChange={onFileInputChange}
        data-testid="qzone-ref-file"
      />
    </div>
  );

  if (!personaId) {
    return (
      <div className="flex flex-col gap-3" data-testid="qzone-ref-picker">
        {header}
        <p className="text-xs text-sg-ink-4" data-testid="qzone-ref-pick-persona">
          {t("schedulerQzone.refs.pickPersonaFirst")}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="qzone-ref-picker">
      {header}

      {refs.length === 0 && !assetsQuery.isLoading ? (
        <p className="text-xs text-sg-ink-4" data-testid="qzone-ref-empty">
          {t("schedulerQzone.refs.empty")}
        </p>
      ) : (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
          {refs.map((asset) => {
            const isSel = selectedSet.has(asset.label);
            return (
              <div
                key={asset.id}
                className={cn(
                  "group relative flex flex-col gap-1 rounded-md border p-1.5 transition-colors",
                  isSel
                    ? "border-sg-tint bg-sg-tint-soft"
                    : "border-sg-border bg-sg-card hover:bg-sg-inset-hover",
                )}
                data-testid={`qzone-ref-cell-${asset.label}`}
                data-selected={isSel || undefined}
              >
                <button
                  type="button"
                  onClick={() => toggle(asset.label)}
                  aria-pressed={isSel}
                  aria-label={asset.label}
                  // Description authored on the persona page rides along
                  // here as a tooltip — same asset row, zero duplication.
                  title={asset.description || undefined}
                  data-testid={`qzone-ref-toggle-${asset.label}`}
                  className="flex flex-col items-center gap-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40 focus-visible:rounded"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={asset.url}
                    alt={asset.label}
                    className="mx-auto h-20 w-20 rounded-sm object-contain"
                    loading="lazy"
                  />
                  <span className="max-w-full truncate font-mono text-[10px] text-sg-ink-3">
                    {asset.label}
                  </span>
                  {asset.description ? (
                    <span
                      className="line-clamp-2 max-w-full px-0.5 text-left text-[9.5px] leading-snug text-sg-ink-4"
                      data-testid={`qzone-ref-desc-${asset.label}`}
                    >
                      {asset.description}
                    </span>
                  ) : null}
                </button>

                {isSel ? (
                  <span
                    aria-hidden
                    className="absolute left-1 top-1 flex h-4 w-4 items-center justify-center rounded-full bg-sg-tint text-sg-tint-ink"
                  >
                    <Check className="h-2.5 w-2.5" aria-hidden />
                  </span>
                ) : null}

                <button
                  type="button"
                  onClick={() => setPendingDelete(asset)}
                  aria-label={t("schedulerQzone.refs.deleteTitle")}
                  title={t("schedulerQzone.refs.deleteTitle")}
                  data-testid={`qzone-ref-delete-${asset.label}`}
                  className="absolute right-1 top-1 inline-flex h-5 w-5 items-center justify-center rounded-md text-sg-ink-4 opacity-0 transition-opacity hover:bg-sg-inset-hover hover:text-sg-err focus-visible:opacity-100 group-hover:opacity-100"
                >
                  <Trash2 className="h-3 w-3" aria-hidden />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {missing.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5" data-testid="qzone-ref-missing">
          {missing.map((label) => (
            <button
              key={label}
              type="button"
              onClick={() => toggle(label)}
              title={t("schedulerQzone.refs.missing")}
              data-testid={`qzone-ref-missing-${label}`}
              className="inline-flex items-center gap-1 rounded-full border border-dashed border-sg-border px-2 py-1 font-mono text-[10.5px] text-sg-ink-3 hover:text-sg-err"
            >
              <X className="h-3 w-3" aria-hidden />
              {label}
            </button>
          ))}
        </div>
      ) : null}

      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-sg-ink-4" data-testid="qzone-ref-selected-count">
          {t("schedulerQzone.refs.selectedCount", { count: selected.length })}
        </span>
        {selected.length > REFS_VISIBLE_CAP ? (
          <span className="text-xs text-sg-warn" data-testid="qzone-ref-cap-hint">
            {t("schedulerQzone.refs.capHint", { cap: REFS_VISIBLE_CAP })}
          </span>
        ) : null}
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
        title={t("schedulerQzone.refs.deleteTitle")}
        description={t("schedulerQzone.refs.deleteBody", {
          label: pendingDelete?.label ?? "",
        })}
        cancelLabel={t("common.cancel")}
        confirmLabel={t("common.delete")}
        testId="qzone-ref-delete-confirm"
        onConfirm={confirmDelete}
      />
    </div>
  );
}
