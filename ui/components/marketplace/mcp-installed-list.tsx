"use client";

/**
 * `<McpInstalledList>` — installed MCP servers with live status + lifecycle.
 *
 * Each card shows a status badge (ready/error/pending/stopped), the tool
 * count, and Enable/Disable/Restart/Delete actions. Enable/Disable/Restart
 * are served by the existing plugins seam; Delete uninstalls the server.
 * The parent page owns the data query; this component owns the per-row
 * mutations + the delete confirm dialog.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Power, PowerOff, RotateCw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useMotion } from "@/components/ui/motion-safe";
import {
  CorlinmanApiError,
  deleteMcpServer,
  disableMcpServer,
  enableMcpServer,
  listMcpServers,
  restartMcpServer,
  type InstalledMcpServer,
} from "@/lib/api";

const STATUS_TONE: Record<InstalledMcpServer["status"], string> = {
  ready: "border-tp-ok/30 bg-tp-ok-soft text-tp-ok",
  error: "border-red-500/40 bg-red-500/10 text-red-600",
  pending: "border-tp-amber/30 bg-tp-amber-soft text-tp-amber",
  stopped: "border-tp-ink-3/30 bg-tp-glass-inner-strong text-tp-ink-2",
};

const STATUS_DOT: Record<InstalledMcpServer["status"], string> = {
  ready: "bg-tp-ok",
  error: "bg-tp-err",
  pending: "bg-tp-amber",
  stopped: "bg-tp-ink-3",
};

const STATUS_LABEL_KEY: Record<InstalledMcpServer["status"], string> = {
  ready: "marketplace.mcp.installed.statusReady",
  error: "marketplace.mcp.installed.statusError",
  pending: "marketplace.mcp.installed.statusPending",
  stopped: "marketplace.mcp.installed.statusStopped",
};

const EMPTY: InstalledMcpServer[] = [];

export function McpInstalledList(): React.JSX.Element {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [pendingDelete, setPendingDelete] =
    React.useState<InstalledMcpServer | null>(null);

  const query = useQuery<InstalledMcpServer[]>({
    queryKey: ["mcp-servers"],
    queryFn: () => listMcpServers(),
    retry: false,
  });

  const rows = query.data ?? EMPTY;
  const offline = query.isError;

  const refetch = React.useCallback(() => {
    void qc.invalidateQueries({ queryKey: ["mcp-servers"] });
  }, [qc]);

  function reportError(action: string, name: string, err: unknown) {
    const msg =
      err instanceof CorlinmanApiError
        ? err.message
        : err instanceof Error
          ? err.message
          : String(err);
    toast.error(
      t("marketplace.mcp.installed.actionFailed", { action, name, message: msg }),
    );
  }

  const enable = useMutation({
    mutationFn: (name: string) => enableMcpServer(name),
    onSuccess: (_r, name) => {
      toast.success(t("marketplace.mcp.installed.enableSuccess", { name }));
      refetch();
    },
    onError: (err, name) => reportError("enable", name, err),
  });

  const disable = useMutation({
    mutationFn: (name: string) => disableMcpServer(name),
    onSuccess: (_r, name) => {
      toast.success(t("marketplace.mcp.installed.disableSuccess", { name }));
      refetch();
    },
    onError: (err, name) => reportError("disable", name, err),
  });

  const restart = useMutation({
    mutationFn: (name: string) => restartMcpServer(name),
    onSuccess: (_r, name) => {
      toast.success(t("marketplace.mcp.installed.restartSuccess", { name }));
      refetch();
    },
    onError: (err, name) => reportError("restart", name, err),
  });

  const remove = useMutation({
    mutationFn: (name: string) => deleteMcpServer(name),
    onSuccess: (_r, name) => {
      toast.success(t("marketplace.mcp.installed.deleteSuccess", { name }));
      setPendingDelete(null);
      refetch();
    },
    onError: (err, name) => reportError("delete", name, err),
  });

  const busy = (name: string) =>
    (enable.isPending && enable.variables === name) ||
    (disable.isPending && disable.variables === name) ||
    (restart.isPending && restart.variables === name) ||
    (remove.isPending && remove.variables === name);

  if (query.isPending) {
    return <ListSkeleton />;
  }

  if (offline) {
    return (
      <GlassPanel
        variant="soft"
        className="flex flex-col items-center gap-2 p-8 text-center"
        data-testid="mcp-installed-offline"
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
        data-testid="mcp-installed-empty"
      >
        <div className="text-[14px] font-medium text-tp-ink">
          {t("marketplace.mcp.installed.empty")}
        </div>
        <p className="text-[13px] text-tp-ink-3">
          {t("marketplace.mcp.installed.emptyHint")}
        </p>
      </GlassPanel>
    );
  }

  return (
    <>
      <section
        aria-label={t("marketplace.mcp.installed.title")}
        className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(320px,1fr))]"
        data-testid="mcp-installed-grid"
      >
        {rows.map((row) => (
          <McpServerCard
            key={row.name}
            row={row}
            busy={busy(row.name)}
            onEnable={() => enable.mutate(row.name)}
            onDisable={() => disable.mutate(row.name)}
            onRestart={() => restart.mutate(row.name)}
            onDelete={() => setPendingDelete(row)}
          />
        ))}
      </section>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
        title={t("marketplace.mcp.installed.deleteConfirmTitle", {
          name: pendingDelete?.name ?? "",
        })}
        description={t("marketplace.mcp.installed.deleteConfirmBody", {
          name: pendingDelete?.name ?? "",
        })}
        confirmLabel={t("marketplace.mcp.installed.delete")}
        cancelLabel={t("marketplace.common.cancel")}
        destructive
        busy={remove.isPending}
        onConfirm={async () => {
          if (!pendingDelete) return;
          await remove.mutateAsync(pendingDelete.name);
        }}
        testId="mcp-delete-confirm"
      />
    </>
  );
}

interface McpServerCardProps {
  row: InstalledMcpServer;
  busy: boolean;
  onEnable: () => void;
  onDisable: () => void;
  onRestart: () => void;
  onDelete: () => void;
}

function McpServerCard({
  row,
  busy,
  onEnable,
  onDisable,
  onRestart,
  onDelete,
}: McpServerCardProps) {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  return (
    <div
      className={cn(
        "group block",
        !reduced &&
          "transition-transform duration-200 ease-tp-ease-out hover:-translate-y-0.5",
      )}
      data-testid={`mcp-server-card-${row.name}`}
      data-status={row.status}
    >
      <GlassPanel variant="soft" className="flex h-full flex-col gap-3 p-4">
        {/* Row 1 — name + status badge */}
        <div className="flex items-start gap-2.5">
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-medium leading-tight text-tp-ink">
              {row.name}
            </h3>
            <div className="mt-1 flex items-center gap-1.5 font-mono text-[10.5px] text-tp-ink-4">
              <span>v{row.version}</span>
              {row.transport ? (
                <>
                  <span aria-hidden>·</span>
                  <span className="normal-case">{row.transport}</span>
                </>
              ) : null}
              <span aria-hidden>·</span>
              <span data-testid={`mcp-server-tools-${row.name}`}>
                {t("marketplace.mcp.installed.tools", { count: row.tools })}
              </span>
            </div>
          </div>
          <span
            data-testid={`mcp-status-${row.name}`}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-[2px] font-mono text-[10.5px]",
              STATUS_TONE[row.status],
            )}
          >
            <span
              aria-hidden
              className={cn("h-[5px] w-[5px] rounded-full", STATUS_DOT[row.status])}
            />
            {t(STATUS_LABEL_KEY[row.status])}
          </span>
        </div>

        {/* Error rail */}
        {row.status === "error" && row.error ? (
          <p
            className="break-words text-[12px] text-red-600"
            data-testid={`mcp-server-error-${row.name}`}
          >
            {row.error}
          </p>
        ) : null}

        {/* Source */}
        <p className="truncate font-mono text-[11px] text-tp-ink-3" title={row.source}>
          {row.source}
        </p>

        {/* Actions */}
        <div className="mt-auto flex flex-wrap items-center justify-end gap-1.5 pt-1">
          {row.enabled ? (
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={onDisable}
              data-testid={`mcp-disable-${row.name}`}
            >
              <PowerOff className="h-3.5 w-3.5" aria-hidden />
              {t("marketplace.mcp.installed.disable")}
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={onEnable}
              data-testid={`mcp-enable-${row.name}`}
            >
              <Power className="h-3.5 w-3.5" aria-hidden />
              {t("marketplace.mcp.installed.enable")}
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={onRestart}
            data-testid={`mcp-restart-${row.name}`}
          >
            <RotateCw className="h-3.5 w-3.5" aria-hidden />
            {t("marketplace.mcp.installed.restart")}
          </Button>
          <button
            type="button"
            disabled={busy}
            aria-label={t("marketplace.mcp.installed.delete")}
            onClick={onDelete}
            data-testid={`mcp-delete-${row.name}`}
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
      data-testid="mcp-installed-skeleton"
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <GlassPanel
          key={i}
          variant="soft"
          className="flex h-[160px] flex-col gap-3 p-4"
        >
          <div className="h-3.5 w-2/3 rounded bg-tp-glass-inner-strong" />
          <div className="h-2.5 w-1/3 rounded bg-tp-glass-inner" />
          <div className="mt-auto flex gap-1.5">
            <div className="h-8 w-20 rounded bg-tp-glass-inner" />
            <div className="h-8 w-20 rounded bg-tp-glass-inner" />
          </div>
        </GlassPanel>
      ))}
    </section>
  );
}

export default McpInstalledList;
