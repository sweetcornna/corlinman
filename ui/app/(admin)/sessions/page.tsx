"use client";

import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { PowerOff, Trash2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  deleteAllSessions,
  deleteSession,
  fetchSessions,
  type SessionSummary,
  type SessionsListResult,
} from "@/lib/api/sessions";
import { ReplayDialog } from "@/components/sessions/replay-dialog";
import { SessionRow } from "@/components/sessions/session-row";

/**
 * `/admin/sessions` — Phase 4 Wave 2 task 4-2D + operator simplification.
 *
 * Lists session keys with last-message + last-seen timestamps and message
 * count. Per-row actions: Replay (opens `<ReplayDialog>`) and Delete
 * (`DELETE /admin/sessions/{key}`). A top-right "Clear all" button calls
 * `DELETE /admin/sessions` to wipe the journal.
 *
 * Both delete paths are optimistic — the row(s) disappear from the table
 * immediately on success; on backend error we restore the snapshot we
 * captured before the call. Confirmations go through `<ConfirmDialog>`,
 * our AlertDialog-equivalent built on top of the existing Radix Dialog
 * primitive (no new dep needed).
 *
 * 503 `sessions_disabled` continues to render the W2 4-2D banner.
 */

const SESSIONS_QUERY_KEY = ["admin", "sessions"] as const;

export default function SessionsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [active, setActive] = React.useState<SessionSummary | null>(null);
  const [pendingDelete, setPendingDelete] =
    React.useState<SessionSummary | null>(null);
  const [confirmClearAllOpen, setConfirmClearAllOpen] =
    React.useState<boolean>(false);

  const query = useQuery<SessionsListResult>({
    queryKey: SESSIONS_QUERY_KEY,
    queryFn: () => fetchSessions(),
  });

  // Convenience: a flat array regardless of disabled/empty status. Used by
  // the Clear-all confirmation copy and the top-right button enablement.
  const sessions: SessionSummary[] =
    query.data?.kind === "ok" ? query.data.sessions : [];

  /**
   * Replace the cached list with a synchronous mutator. We use this for
   * optimistic add/remove so the table updates instantly. On error the
   * caller restores the snapshot it captured before mutating.
   */
  function mutateCache(
    mutator: (prev: SessionsListResult | undefined) => SessionsListResult,
  ): SessionsListResult | undefined {
    const prev = queryClient.getQueryData<SessionsListResult>(SESSIONS_QUERY_KEY);
    queryClient.setQueryData<SessionsListResult>(SESSIONS_QUERY_KEY, mutator(prev));
    return prev;
  }

  async function performDelete(session: SessionSummary) {
    const prev = mutateCache((cur) => {
      if (!cur || cur.kind !== "ok") {
        return cur ?? { kind: "ok", sessions: [] };
      }
      return {
        kind: "ok",
        sessions: cur.sessions.filter(
          (s) => s.session_key !== session.session_key,
        ),
      };
    });

    try {
      const result = await deleteSession(session.session_key);
      // 404 is treated as success — the row's already gone.
      if (result.kind === "ok" || result.kind === "not_found") {
        toast.success(
          t("sessions.deleteSucceeded", { key: session.session_key }),
        );
        return;
      }
      // `disabled` is a backend reconfiguration — restore the row so the
      // operator knows it wasn't actually wiped.
      throw new Error(result.kind);
    } catch (err) {
      // Restore the snapshot.
      if (prev !== undefined) {
        queryClient.setQueryData<SessionsListResult>(SESSIONS_QUERY_KEY, prev);
      }
      toast.error(
        t("sessions.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    }
  }

  async function performClearAll() {
    const snapshotCount = sessions.length;
    const prev = mutateCache(() => ({ kind: "ok", sessions: [] }));
    try {
      const result = await deleteAllSessions();
      if (result.kind === "ok") {
        toast.success(
          t("sessions.clearAllSucceeded", {
            n: result.deleted || snapshotCount,
          }),
        );
        // Refetch so a fresh truth replaces our optimistic empty state.
        await queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY });
        return;
      }
      throw new Error(result.kind);
    } catch (err) {
      if (prev !== undefined) {
        queryClient.setQueryData<SessionsListResult>(SESSIONS_QUERY_KEY, prev);
      }
      toast.error(
        t("sessions.clearAllFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    }
  }

  const isDisabled = query.data?.kind === "disabled";

  return (
    <>
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("sessions.title")}
          </h1>
          <p className="text-sm text-tp-ink-3">{t("sessions.subtitle")}</p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setConfirmClearAllOpen(true)}
          disabled={isDisabled || sessions.length === 0}
          data-testid="sessions-clear-all"
          aria-label={t("sessions.clearAll")}
          className="self-start text-destructive hover:bg-destructive/10 hover:text-destructive sm:self-auto"
        >
          <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
          {t("sessions.clearAll")}
        </Button>
      </header>

      {isDisabled ? <SessionsDisabledBanner /> : null}

      <section className="overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
              <TableHead className="pl-4">
                {t("sessions.colSessionKey")}
              </TableHead>
              <TableHead className="w-24">
                {t("sessions.colMessageCount")}
              </TableHead>
              <TableHead className="w-48">
                {t("sessions.colLastMessageAt")}
              </TableHead>
              <TableHead className="w-48">
                {t("sessions.colLastSeenAt")}
              </TableHead>
              <TableHead className="w-48 pr-4 text-right">
                {t("sessions.colActions")}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              <SessionsTableSkeleton />
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={5}
                  className="py-10 text-center text-sm text-destructive"
                  data-testid="sessions-load-failed"
                >
                  {t("sessions.loadFailed")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : isDisabled ? (
              <TableRow>
                <TableCell
                  colSpan={5}
                  className="py-10 text-center text-sm text-tp-ink-3"
                  data-testid="sessions-disabled-row"
                >
                  {t("sessions.sessionsDisabledHint")}
                </TableCell>
              </TableRow>
            ) : sessions.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={5}
                  className="py-10 text-center text-sm text-tp-ink-3"
                  data-testid="sessions-empty"
                >
                  {t("sessions.empty")}
                </TableCell>
              </TableRow>
            ) : (
              sessions.map((s) => (
                <SessionRow
                  key={s.session_key}
                  session={s}
                  onReplay={setActive}
                  onDelete={setPendingDelete}
                />
              ))
            )}
          </TableBody>
        </Table>
      </section>

      <ReplayDialog session={active} onClose={() => setActive(null)} />

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={t("sessions.deleteConfirmTitle")}
        description={t("sessions.deleteConfirmBody")}
        cancelLabel={t("sessions.cancel")}
        confirmLabel={t("sessions.deleteConfirmAction")}
        testId="sessions-delete-confirm"
        onConfirm={async () => {
          const target = pendingDelete;
          setPendingDelete(null);
          if (target) await performDelete(target);
        }}
      />

      <ConfirmDialog
        open={confirmClearAllOpen}
        onOpenChange={setConfirmClearAllOpen}
        title={t("sessions.clearAllConfirmTitle")}
        description={t("sessions.clearAllConfirmBody", { n: sessions.length })}
        cancelLabel={t("sessions.cancel")}
        confirmLabel={t("sessions.clearAllConfirmAction")}
        testId="sessions-clear-all-confirm"
        onConfirm={async () => {
          setConfirmClearAllOpen(false);
          await performClearAll();
        }}
      />
    </>
  );
}

function SessionsDisabledBanner() {
  const { t } = useTranslation();
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-3 rounded-lg border px-4 py-3",
        "border-amber-500/40 bg-amber-500/10 text-amber-200",
      )}
      data-testid="sessions-disabled-banner"
    >
      <PowerOff
        aria-hidden="true"
        className="mt-0.5 h-4 w-4 shrink-0 text-amber-400"
      />
      <div className="space-y-1">
        <div className="text-sm font-medium">
          {t("sessions.sessionsDisabledTitle")}
        </div>
        <div className="text-xs text-amber-200/80">
          {t("sessions.sessionsDisabledHint")}
        </div>
      </div>
    </div>
  );
}

function SessionsTableSkeleton() {
  return (
    <>
      {Array.from({ length: 3 }).map((_, i) => (
        <TableRow
          key={`session-sk-${i}`}
          className="border-b border-tp-glass-edge"
        >
          <TableCell className="pl-4">
            <Skeleton className="h-4 w-32" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-10" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-40" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-40" />
          </TableCell>
          <TableCell className="pr-4 text-right">
            <Skeleton className="ml-auto h-7 w-32" />
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}
