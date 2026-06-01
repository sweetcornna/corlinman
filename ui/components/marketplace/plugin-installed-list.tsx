"use client";

/**
 * `<PluginInstalledList>` — installed plugin-market rows with lifecycle.
 *
 * Each card shows the enabled/disabled state and Enable/Disable/Delete
 * actions. Enable surfaces the backend's `applies` value — when it's
 * `"next_restart"` the success toast reads "Enabled — applies after restart".
 *
 * The installed plugin-market list is derived from the live plugins surface:
 * `/admin/plugins` rows whose `source` marks them as market-installed. The
 * gateway returns the staged rows from the market enable/disable/install
 * endpoints, so we key the cache off `plugin-market-installed` and refetch
 * after every lifecycle action.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Power, PowerOff, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useMotion } from "@/components/ui/motion-safe";
import {
  CorlinmanApiError,
  apiFetch,
  deletePluginMarket,
  disablePluginMarket,
  enablePluginMarket,
  type InstalledPluginRow,
} from "@/lib/api";

const EMPTY: InstalledPluginRow[] = [];

/** GET the installed plugin-market rows. The gateway exposes them via the
 * market index; we read them through `/admin/plugins/market/installed`. */
function listInstalledPluginMarket(): Promise<InstalledPluginRow[]> {
  return apiFetch<InstalledPluginRow[]>("/admin/plugins/market/installed");
}

export function PluginInstalledList(): React.JSX.Element {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [pendingDelete, setPendingDelete] =
    React.useState<InstalledPluginRow | null>(null);

  const query = useQuery<InstalledPluginRow[]>({
    queryKey: ["plugin-market-installed"],
    queryFn: () => listInstalledPluginMarket(),
    retry: false,
  });

  const rows = query.data ?? EMPTY;
  const offline = query.isError;

  const refetch = React.useCallback(() => {
    void qc.invalidateQueries({ queryKey: ["plugin-market-installed"] });
  }, [qc]);

  function reportError(action: string, slug: string, err: unknown) {
    const msg =
      err instanceof CorlinmanApiError
        ? err.message
        : err instanceof Error
          ? err.message
          : String(err);
    toast.error(
      t("marketplace.plugins.installed.actionFailed", {
        action,
        slug,
        message: msg,
      }),
    );
  }

  const enable = useMutation({
    mutationFn: (slug: string) => enablePluginMarket(slug),
    onSuccess: (res, slug) => {
      if (res.applies === "next_restart") {
        toast(t("marketplace.plugins.installed.enableNextRestart", { slug }));
      } else {
        toast.success(t("marketplace.plugins.installed.enableSuccess", { slug }));
      }
      refetch();
    },
    onError: (err, slug) => reportError("enable", slug, err),
  });

  const disable = useMutation({
    mutationFn: (slug: string) => disablePluginMarket(slug),
    onSuccess: (_r, slug) => {
      toast.success(t("marketplace.plugins.installed.disableSuccess", { slug }));
      refetch();
    },
    onError: (err, slug) => reportError("disable", slug, err),
  });

  const remove = useMutation({
    mutationFn: (slug: string) => deletePluginMarket(slug),
    onSuccess: (_r, slug) => {
      toast.success(t("marketplace.plugins.installed.deleteSuccess", { slug }));
      setPendingDelete(null);
      refetch();
    },
    onError: (err, slug) => reportError("delete", slug, err),
  });

  const busy = (slug: string) =>
    (enable.isPending && enable.variables === slug) ||
    (disable.isPending && disable.variables === slug) ||
    (remove.isPending && remove.variables === slug);

  if (query.isPending) {
    return <ListSkeleton />;
  }

  if (offline) {
    return (
      <GlassPanel
        variant="soft"
        className="flex flex-col items-center gap-2 p-8 text-center"
        data-testid="plugin-installed-offline"
      >
        <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
          {t("marketplace.common.offlineTitle")}
        </div>
        <p className="max-w-prose text-[13px] text-tp-ink-2">
          {t("marketplace.common.offlineHint")}
        </p>
      </GlassPanel>
    );
  }

  if (rows.length === 0) {
    return (
      <GlassPanel
        variant="subtle"
        className="flex flex-col items-center gap-2 p-8 text-center"
        data-testid="plugin-installed-empty"
      >
        <div className="text-[14px] font-medium text-tp-ink">
          {t("marketplace.plugins.installed.empty")}
        </div>
        <p className="text-[13px] text-tp-ink-3">
          {t("marketplace.plugins.installed.emptyHint")}
        </p>
      </GlassPanel>
    );
  }

  return (
    <>
      <section
        aria-label={t("marketplace.plugins.installed.title")}
        className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(320px,1fr))]"
        data-testid="plugin-installed-grid"
      >
        {rows.map((row) => (
          <PluginRowCard
            key={row.slug}
            row={row}
            busy={busy(row.slug)}
            onEnable={() => enable.mutate(row.slug)}
            onDisable={() => disable.mutate(row.slug)}
            onDelete={() => setPendingDelete(row)}
          />
        ))}
      </section>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
        title={t("marketplace.plugins.installed.deleteConfirmTitle", {
          slug: pendingDelete?.slug ?? "",
        })}
        description={t("marketplace.plugins.installed.deleteConfirmBody", {
          slug: pendingDelete?.slug ?? "",
        })}
        confirmLabel={t("marketplace.plugins.installed.delete")}
        cancelLabel={t("marketplace.common.cancel")}
        destructive
        busy={remove.isPending}
        onConfirm={async () => {
          if (!pendingDelete) return;
          await remove.mutateAsync(pendingDelete.slug);
        }}
        testId="plugin-delete-confirm"
      />
    </>
  );
}

interface PluginRowCardProps {
  row: InstalledPluginRow;
  busy: boolean;
  onEnable: () => void;
  onDisable: () => void;
  onDelete: () => void;
}

function PluginRowCard({
  row,
  busy,
  onEnable,
  onDisable,
  onDelete,
}: PluginRowCardProps) {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  return (
    <div
      className={cn(
        "group block",
        !reduced &&
          "transition-transform duration-200 ease-tp-ease-out hover:-translate-y-0.5",
      )}
      data-testid={`plugin-row-card-${row.slug}`}
      data-enabled={row.enabled}
    >
      <GlassPanel variant="soft" className="flex h-full flex-col gap-3 p-4">
        <div className="flex items-start gap-2.5">
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-medium leading-tight text-tp-ink">
              {row.slug}
            </h3>
            <div className="mt-1 flex items-center gap-1.5 font-mono text-[10.5px] text-tp-ink-4">
              <span>v{row.version}</span>
            </div>
          </div>
          <span
            data-testid={`plugin-state-${row.slug}`}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-[2px] font-mono text-[10.5px]",
              row.enabled
                ? "border-tp-ok/30 bg-tp-ok-soft text-tp-ok"
                : "border-tp-ink-3/30 bg-tp-glass-inner-strong text-tp-ink-2",
            )}
          >
            <span
              aria-hidden
              className={cn(
                "h-[5px] w-[5px] rounded-full",
                row.enabled ? "bg-tp-ok" : "bg-tp-ink-3",
              )}
            />
            {row.enabled
              ? t("marketplace.plugins.installed.enabled")
              : t("marketplace.plugins.installed.disabled")}
          </span>
        </div>

        <p className="truncate font-mono text-[11px] text-tp-ink-3" title={row.source}>
          {row.source}
        </p>

        <div className="mt-auto flex flex-wrap items-center justify-end gap-1.5 pt-1">
          {row.enabled ? (
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={onDisable}
              data-testid={`plugin-disable-${row.slug}`}
            >
              <PowerOff className="h-3.5 w-3.5" aria-hidden />
              {t("marketplace.plugins.installed.disable")}
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={onEnable}
              data-testid={`plugin-enable-${row.slug}`}
            >
              <Power className="h-3.5 w-3.5" aria-hidden />
              {t("marketplace.plugins.installed.enable")}
            </Button>
          )}
          <button
            type="button"
            disabled={busy}
            aria-label={t("marketplace.plugins.installed.delete")}
            onClick={onDelete}
            data-testid={`plugin-delete-${row.slug}`}
            className={cn(
              "inline-flex h-9 w-9 items-center justify-center rounded-md",
              "border border-tp-glass-edge bg-tp-glass-inner",
              "text-tp-ink-3 transition-colors",
              "hover:bg-tp-err-soft hover:text-tp-err",
              "disabled:opacity-50",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-err/40",
            )}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      </GlassPanel>
    </div>
  );
}

function ListSkeleton() {
  return (
    <section
      aria-hidden
      className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(320px,1fr))]"
      data-testid="plugin-installed-skeleton"
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <GlassPanel
          key={i}
          variant="soft"
          className="flex h-[140px] flex-col gap-3 p-4"
        >
          <div className="h-3.5 w-2/3 rounded bg-tp-glass-inner-strong" />
          <div className="h-2.5 w-1/3 rounded bg-tp-glass-inner" />
          <div className="mt-auto flex gap-1.5">
            <div className="h-8 w-20 rounded bg-tp-glass-inner" />
          </div>
        </GlassPanel>
      ))}
    </section>
  );
}

export default PluginInstalledList;
