"use client";

import * as React from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";

import { useMotion } from "@/components/ui/motion-safe";
import { FilterChipGroup } from "@/components/ui/filter-chip-group";
import { cn } from "@/lib/utils";
import {
  apiFetch,
  decideApproval,
  decideApprovalsBatch,
  fetchApprovals,
  openEventStream,
  type Approval,
} from "@/lib/api";

import { ApprovalCard } from "@/components/approvals/ApprovalCard";
import { ApprovalsEmptyState } from "@/components/approvals/EmptyState";
import { BatchToolbar } from "@/components/approvals/BatchToolbar";
import { DenyReasonDialog } from "@/components/approvals/DenyReasonDialog";
import { DetailDrawerContent } from "@/components/approvals/DetailDrawerContent";
import { PageHeader } from "@/components/approvals/PageHeader";
import { StatsRow } from "@/components/approvals/StatsRow";
import type { StreamEvent, Tab } from "@/components/approvals/types";

/**
 * Approvals — Tidepool (Phase 5a) cutover.
 *
 * Layout:
 *   [ page header (prose) ]
 *   [ StatChip × 4 ]
 *   [ FilterChipGroup: all · pending · decided ]
 *   [ list column (ApprovalCard stack)          │ DetailDrawer ]
 *   [ sticky BatchToolbar when selection > 0 ]
 *
 * Data flow is unchanged from the pre-cutover page:
 *   1. React Query polls `/admin/approvals` (15s safety net).
 *   2. SSE `/admin/approvals/stream` nudges the cache on pending/decided.
 *   3. Optimistic removal on approve/deny; rollback on mutation failure.
 *
 * Keyboard:
 *   - A  → approve the active row (or selection if > 0)
 *   - D  → open deny dialog for active row (or selection if > 0)
 *   - ⌫  → clear selection
 *   - Esc → deselect the active row (close the drawer)
 *
 * Shortcuts are suppressed while the user is typing in an input/textarea
 * or inside the deny-reason dialog.
 */

// Highlight window for a freshly-pushed Pending row.
const HIGHLIGHT_MS = 1_200;
// Fade-out window for a row that was just decided.
const FADE_MS = 400;

type Filter = "all" | "pending" | "decided";

// Keep `apiFetch` referenced so tree-shaking doesn't drop it — the rest of
// the admin surface still uses it and importing from `@/lib/api` here is
// load-bearing for the test suite.
void apiFetch;

export default function ApprovalsPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  // Filter state drives which tab the backend query uses. `all` and
  // `decided` need the history response (includes decided + pending);
  // `pending` uses the pending-only response.
  const [filter, setFilter] = useState<Filter>("pending");
  const tab: Tab = filter === "pending" ? "pending" : "history";

  // Coarse 1s tick purely for held-for pill / urgent flip.
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  // Row-level UI state
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [activeId, setActiveId] = useState<string | null>(null);
  const [denyDialog, setDenyDialog] = useState<
    | { kind: "single"; id: string }
    | { kind: "batch"; ids: string[] }
    | null
  >(null);
  const [highlightIds, setHighlightIds] = useState<Set<string>>(() => new Set());
  const [fadingIds, setFadingIds] = useState<Set<string>>(() => new Set());
  const [lagBanner, setLagBanner] = useState<string | null>(null);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);

  // ─── queries ──────────────────────────────────────────────────────────
  const qc = useQueryClient();
  const queryKey = useMemo(() => ["admin", "approvals", tab], [tab]);
  const query = useQuery<Approval[]>({
    queryKey,
    queryFn: () => fetchApprovals(tab === "history"),
    refetchInterval: 15_000,
    retry: false,
  });

  // ─── mutations (unchanged from pre-cutover) ───────────────────────────
  const pendingSnapshotRef = useRef<Approval[] | undefined>(undefined);

  const snapshotPending = () => {
    pendingSnapshotRef.current = qc.getQueryData<Approval[]>([
      "admin",
      "approvals",
      "pending",
    ]);
  };

  const removePendingLocally = (ids: Iterable<string>) => {
    const drop = new Set(ids);
    qc.setQueryData<Approval[]>(["admin", "approvals", "pending"], (prev) =>
      prev ? prev.filter((r) => !drop.has(r.id)) : prev,
    );
  };

  const restoreFailed = (failedIds: Iterable<string>) => {
    const failed = new Set(failedIds);
    const snap = pendingSnapshotRef.current;
    if (!snap) return;
    qc.setQueryData<Approval[]>(["admin", "approvals", "pending"], (prev) => {
      const current = prev ?? [];
      const seen = new Set(current.map((r) => r.id));
      const missing = snap.filter((r) => failed.has(r.id) && !seen.has(r.id));
      return [...current, ...missing];
    });
  };

  const singleMutation = useMutation({
    mutationFn: ({
      id,
      approve,
      reason,
    }: {
      id: string;
      approve: boolean;
      reason?: string;
    }) => decideApproval(id, approve, reason),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: ["admin", "approvals", "pending"] });
      snapshotPending();
      removePendingLocally([id]);
    },
    onError: (err, vars) => {
      restoreFailed([vars.id]);
      setErrorBanner(
        t("approvals.decideFailed", {
          id: vars.id,
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["admin", "approvals"] });
    },
  });

  const batchMutation = useMutation({
    mutationFn: ({
      ids,
      approve,
      reason,
    }: {
      ids: string[];
      approve: boolean;
      reason?: string;
    }) => decideApprovalsBatch(ids, approve, reason),
    onMutate: async ({ ids }) => {
      await qc.cancelQueries({ queryKey: ["admin", "approvals", "pending"] });
      snapshotPending();
      removePendingLocally(ids);
    },
    onSuccess: (outcomes) => {
      const failed = outcomes.filter((o) => !o.ok);
      if (failed.length > 0) {
        restoreFailed(failed.map((o) => o.id));
        setErrorBanner(
          t("approvals.batchSomeFailed", {
            n: failed.length,
            details: failed
              .map((f) => `${f.id}${f.error ? ` (${f.error})` : ""}`)
              .join("; "),
          }),
        );
      } else {
        setErrorBanner(null);
      }
      setSelected(new Set());
    },
    onError: (err, vars) => {
      restoreFailed(vars.ids);
      setErrorBanner(
        t("approvals.batchFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["admin", "approvals"] });
    },
  });

  const anyMutating = singleMutation.isPending || batchMutation.isPending;

  // ─── SSE wiring (unchanged) ───────────────────────────────────────────
  useEffect(() => {
    const close = openEventStream<StreamEvent | { message?: string }>(
      "/admin/approvals/stream",
      {
        events: ["message", "lag"],
        onMessage: ({ event, data }) => {
          if (event === "lag") {
            const message =
              typeof (data as { message?: string }).message === "string"
                ? (data as { message: string }).message
                : typeof data === "string"
                  ? (data as string)
                  : t("approvals.lagEventSkipped");
            setLagBanner(t("approvals.lagBanner", { msg: message }));
            qc.invalidateQueries({ queryKey: ["admin", "approvals"] });
            return;
          }
          if (!data || typeof data !== "object" || !("kind" in data)) return;
          const evt = data as StreamEvent;
          if (evt.kind === "pending") {
            qc.setQueryData<Approval[]>(
              ["admin", "approvals", "pending"],
              (prev) => {
                const next = prev ? [...prev] : [];
                if (!next.some((r) => r.id === evt.approval.id)) {
                  next.push(evt.approval);
                }
                return next;
              },
            );
            setHighlightIds((prev) => {
              const n = new Set(prev);
              n.add(evt.approval.id);
              return n;
            });
            const id = evt.approval.id;
            window.setTimeout(() => {
              setHighlightIds((prev) => {
                if (!prev.has(id)) return prev;
                const n = new Set(prev);
                n.delete(id);
                return n;
              });
            }, HIGHLIGHT_MS);
          } else if (evt.kind === "decided") {
            const id = evt.id;
            setFadingIds((prev) => {
              const n = new Set(prev);
              n.add(id);
              return n;
            });
            window.setTimeout(() => {
              qc.setQueryData<Approval[]>(
                ["admin", "approvals", "pending"],
                (prev) => (prev ? prev.filter((r) => r.id !== id) : prev),
              );
              setFadingIds((prev) => {
                if (!prev.has(id)) return prev;
                const n = new Set(prev);
                n.delete(id);
                return n;
              });
              qc.invalidateQueries({
                queryKey: ["admin", "approvals", "history"],
              });
            }, FADE_MS);
          }
        },
      },
    );
    return close;
  }, [qc, t]);

  // ─── derived rows ─────────────────────────────────────────────────────
  const rawRows = useMemo(() => query.data ?? [], [query.data]);

  const visibleRows = useMemo(() => {
    if (filter === "pending") return rawRows.filter((r) => r.decision === null);
    if (filter === "decided") return rawRows.filter((r) => r.decision !== null);
    return rawRows;
  }, [rawRows, filter]);

  // Count rows per filter for the chip labels.
  const counts = useMemo(() => {
    let pending = 0;
    let decided = 0;
    for (const r of rawRows) {
      if (r.decision === null) pending += 1;
      else decided += 1;
    }
    return { pending, decided, all: rawRows.length };
  }, [rawRows]);

  // `pendingCount` reflects the live pending total (ground truth from the
  // backend), not just the currently-visible filter — keeps the header
  // prose honest when the operator is looking at decided rows.
  const pendingLive = !query.isError;
  const pendingCount = useMemo(() => {
    if (filter === "pending") return visibleRows.length;
    return rawRows.filter((r) => r.decision === null).length;
  }, [filter, rawRows, visibleRows]);

  const oldestHeldMs = useMemo(() => {
    let oldest: number | null = null;
    for (const r of rawRows) {
      if (r.decision !== null) continue;
      const held = now - new Date(r.requested_at).getTime();
      if (oldest === null || held > oldest) oldest = held;
    }
    return oldest;
  }, [rawRows, now]);

  const activeApproval = useMemo(
    () => visibleRows.find((r) => r.id === activeId) ?? null,
    [visibleRows, activeId],
  );

  // When the active row disappears from the visible set (filter change,
  // decided-fade removal), drop it.
  useEffect(() => {
    if (activeId && !visibleRows.some((r) => r.id === activeId)) {
      setActiveId(null);
    }
  }, [activeId, visibleRows]);

  // ─── selection helpers ────────────────────────────────────────────────
  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  // ─── batch dispatch ───────────────────────────────────────────────────
  const confirmAndBatchApprove = () => {
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    if (
      !window.confirm(t("approvals.batchApproveConfirm", { n: ids.length }))
    )
      return;
    batchMutation.mutate({ ids, approve: true });
  };

  const openBatchDeny = () => {
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    setDenyDialog({ kind: "batch", ids });
  };

  const handleDenyConfirm = (reason: string) => {
    if (!denyDialog) return;
    if (denyDialog.kind === "single") {
      const id = denyDialog.id;
      setDenyDialog(null);
      singleMutation.mutate({ id, approve: false, reason });
    } else {
      const ids = denyDialog.ids;
      setDenyDialog(null);
      batchMutation.mutate({ ids, approve: false, reason });
    }
  };

  // ─── keyboard shortcuts ───────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Skip if the user is typing or a modifier is held.
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() ?? "";
      if (tag === "input" || tag === "textarea" || target?.isContentEditable) {
        return;
      }
      // Skip when the deny dialog is open.
      if (denyDialog !== null) return;

      const key = e.key.toLowerCase();
      if (key === "a") {
        if (selected.size > 0) {
          e.preventDefault();
          confirmAndBatchApprove();
          return;
        }
        if (activeId) {
          const row = visibleRows.find((r) => r.id === activeId);
          if (row && row.decision === null) {
            e.preventDefault();
            singleMutation.mutate({ id: activeId, approve: true });
          }
        }
      } else if (key === "d") {
        if (selected.size > 0) {
          e.preventDefault();
          openBatchDeny();
          return;
        }
        if (activeId) {
          const row = visibleRows.find((r) => r.id === activeId);
          if (row && row.decision === null) {
            e.preventDefault();
            setDenyDialog({ kind: "single", id: activeId });
          }
        }
      } else if (key === "escape") {
        if (activeId) {
          e.preventDefault();
          setActiveId(null);
        }
      } else if (key === "backspace" || key === "delete") {
        if (selected.size > 0) {
          e.preventDefault();
          setSelected(new Set());
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // The mutate functions are stable references from react-query;
    // dependency list is deliberately compact to avoid re-binding on every
    // `now` tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, activeId, visibleRows, denyDialog]);

  // ─── render ───────────────────────────────────────────────────────────
  const filterOptions = [
    {
      value: "all",
      label: t("approvals.tp.filterAll"),
      count: counts.all,
      tone: "neutral" as const,
    },
    {
      value: "pending",
      label: t("approvals.tp.filterPending"),
      count: counts.pending,
      tone: counts.pending > 0 ? ("warn" as const) : ("neutral" as const),
    },
    {
      value: "decided",
      label: t("approvals.tp.filterDecided"),
      count: counts.decided,
      tone: "neutral" as const,
    },
  ];

  const listIsEmpty =
    !query.isPending && !query.isError && visibleRows.length === 0;

  return (
    <motion.div
      className="flex flex-col gap-5"
      initial={reduced ? undefined : { opacity: 0, y: 6 }}
      animate={reduced ? undefined : { opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
    >
      <PageHeader pendingCount={pendingCount} oldestHeldMs={oldestHeldMs} />

      <StatsRow pendingCount={pendingCount} pendingLive={pendingLive} />

      {/* Banners — lag / error / offline ──────────────────────────────── */}
      {lagBanner ? (
        <Banner
          tone="warn"
          text={lagBanner}
          onDismiss={() => setLagBanner(null)}
          dismissAria={t("approvals.closeLagAria")}
        />
      ) : null}
      {errorBanner ? (
        <Banner
          tone="err"
          text={errorBanner}
          onDismiss={() => setErrorBanner(null)}
          dismissAria={t("approvals.closeErrorAria")}
        />
      ) : null}
      {query.isError && !lagBanner && !errorBanner ? (
        <Banner
          tone="info"
          text={t("approvals.tp.endpointOfflineBanner")}
        />
      ) : null}

      <FilterChipGroup
        label={t("approvals.tabsAria")}
        options={filterOptions}
        value={filter}
        onChange={(next) => {
          setFilter(next as Filter);
          setSelected(new Set());
        }}
      />

      {/* Two-column layout: list + detail drawer ──────────────────────── */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-2.5">
          {query.isPending ? (
            <ListSkeleton />
          ) : listIsEmpty ? (
            <ApprovalsEmptyState tab={tab} />
          ) : (
            visibleRows.map((row) => (
              <ApprovalCard
                key={row.id}
                approval={row}
                now={now}
                isPending={row.decision === null}
                isSelected={selected.has(row.id)}
                isActive={activeId === row.id}
                isHighlighted={highlightIds.has(row.id)}
                isFading={fadingIds.has(row.id)}
                onToggleSelect={toggleOne}
                onActivate={(id) =>
                  setActiveId((prev) => (prev === id ? null : id))
                }
                onApprove={(id) =>
                  singleMutation.mutate({ id, approve: true })
                }
                onDeny={(id) => setDenyDialog({ kind: "single", id })}
                disabled={anyMutating}
                showShortcuts={activeId === row.id && selected.size === 0}
              />
            ))
          )}
        </div>
        <aside className="lg:sticky lg:top-4 lg:self-start">
          <DetailDrawerContent approval={activeApproval} />
        </aside>
      </section>

      <BatchToolbar
        selectedCount={selected.size}
        onApproveAll={confirmAndBatchApprove}
        onDenyAll={openBatchDeny}
        onClear={() => setSelected(new Set())}
        disabled={anyMutating}
      />

      <DenyReasonDialog
        open={denyDialog !== null}
        onOpenChange={(open) => {
          if (!open) setDenyDialog(null);
        }}
        targetLabel={
          denyDialog?.kind === "batch"
            ? t("approvals.batchTarget", { n: denyDialog.ids.length })
            : t("approvals.singleTarget")
        }
        onConfirm={handleDenyConfirm}
        submitting={anyMutating}
      />
    </motion.div>
  );
}

// ─── atomic pieces ────────────────────────────────────────────────────────

function Banner({
  tone,
  text,
  onDismiss,
  dismissAria,
}: {
  tone: "warn" | "err" | "info";
  text: string;
  onDismiss?: () => void;
  dismissAria?: string;
}) {
  const { t } = useTranslation();
  const cls = {
    warn: "border-sg-warn/30 bg-sg-warn-soft text-sg-warn",
    err: "border-sg-err/40 bg-sg-err-soft text-sg-err",
    info: "border-sg-border bg-sg-inset text-sg-ink-3",
  }[tone];
  return (
    <div
      role="alert"
      className={cn(
        "flex items-center justify-between gap-3 rounded-xl border px-3 py-2 text-[12.5px]",
        cls,
      )}
    >
      <span>{text}</span>
      {onDismiss ? (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={dismissAria}
          className={cn(
            "rounded-md px-2 py-1 text-[11px] font-medium",
            "bg-transparent hover:bg-sg-inset-hover",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
          )}
        >
          {t("common.close")}
        </button>
      ) : null}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-2.5">
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "h-[84px] animate-pulse rounded-2xl border border-sg-border",
            "bg-sg-inset/70",
          )}
        />
      ))}
    </div>
  );
}
