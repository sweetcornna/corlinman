"use client";

/**
 * `<AuditCard>` — paginated audit log of system upgrade events.
 *
 * Mounted at the bottom of `/admin/system`. Reads
 * `GET /admin/system/audit` and renders newest-first with a "Load more"
 * cursor (`before_ts`). Each row shows ts (relative + tooltip absolute),
 * event-type badge (color-coded), tag, actor; expanding the row reveals
 * the structured `details` JSON.
 *
 * Self-contained: no props. The page mounts it unconditionally — the
 * empty state handles fresh installs gracefully.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  listSystemAudit,
  type AuditEntry,
  type AuditTailResponse,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;

/** Format an ISO timestamp into a `Xm ago` relative string with the
 * absolute value as a tooltip via `title` attribute. */
function relativeTime(iso: string, now: number): { rel: string; abs: string } {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return { rel: "—", abs: iso };
  const diff = Math.max(0, now - t);
  const s = Math.floor(diff / 1000);
  let rel: string;
  if (s < 60) rel = `${s}s ago`;
  else if (s < 3600) rel = `${Math.floor(s / 60)}m ago`;
  else if (s < 86400) rel = `${Math.floor(s / 3600)}h ago`;
  else rel = `${Math.floor(s / 86400)}d ago`;
  return { rel, abs: formatDateTime(new Date(t)) };
}

/** Map an event-type string to a short label + color. */
function eventBadge(t: (k: string) => string, event: string): {
  label: string;
  tone: "default" | "success" | "warn" | "error";
} {
  if (event.endsWith(".completed")) {
    return { label: t("system.audit.event.completed"), tone: "success" };
  }
  if (event.endsWith(".failed")) {
    return { label: t("system.audit.event.failed"), tone: "error" };
  }
  if (event.endsWith(".started")) {
    return { label: t("system.audit.event.started"), tone: "warn" };
  }
  if (event.endsWith(".requested")) {
    return { label: t("system.audit.event.requested"), tone: "default" };
  }
  // Unknown event type — surface verbatim so operators see what was logged.
  return { label: event, tone: "default" };
}

export function AuditCard() {
  const { t } = useTranslation();
  const [extraPages, setExtraPages] = React.useState<AuditTailResponse[]>([]);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [now, setNow] = React.useState(() => Date.now());
  const [expanded, setExpanded] = React.useState<Set<string>>(new Set());

  // Tick "now" every 30s so relative times stay fresh.
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const firstPage = useQuery({
    queryKey: ["system", "audit"],
    queryFn: () => listSystemAudit({ limit: PAGE_SIZE }),
    refetchOnWindowFocus: false,
    retry: false,
  });

  // Combine first page + paginated extras.
  const entries = React.useMemo<AuditEntry[]>(() => {
    const all: AuditEntry[] = [];
    if (firstPage.data) all.push(...firstPage.data.entries);
    for (const page of extraPages) all.push(...page.entries);
    return all;
  }, [firstPage.data, extraPages]);

  const lastCursor =
    extraPages.length > 0
      ? extraPages[extraPages.length - 1]!.next_before_ts
      : firstPage.data?.next_before_ts ?? null;

  async function handleLoadMore() {
    if (!lastCursor || loadingMore) return;
    setLoadingMore(true);
    setLoadError(null);
    try {
      const page = await listSystemAudit({
        limit: PAGE_SIZE,
        before_ts: lastCursor,
      });
      setExtraPages((p) => [...p, page]);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingMore(false);
    }
  }

  function handleRefresh() {
    setExtraPages([]);
    setLoadError(null);
    firstPage.refetch();
  }

  function toggleExpanded(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <section
      data-testid="system-audit-card"
      className="rounded-lg border border-sg-border bg-sg-card p-4 sm:p-6"
    >
      <header className="mb-4 flex items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight">
            {t("system.audit.title")}
          </h2>
          <p className="text-sm text-sg-ink-3">
            {t("system.audit.subtitle")}
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={handleRefresh}
          disabled={firstPage.isFetching}
        >
          <RefreshCw
            className={cn(
              "mr-1.5 h-3.5 w-3.5",
              firstPage.isFetching && "animate-spin",
            )}
            aria-hidden="true"
          />
          {t("system.audit.refresh")}
        </Button>
      </header>

      {firstPage.isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
        </div>
      ) : entries.length === 0 ? (
        <p
          className="text-sm text-sg-ink-3"
          data-testid="system-audit-empty"
        >
          {t("system.audit.empty")}
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border border-sg-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10" aria-label="expand" />
                <TableHead>{t("system.audit.column.ts")}</TableHead>
                <TableHead>{t("system.audit.column.event")}</TableHead>
                <TableHead>{t("system.audit.column.tag")}</TableHead>
                <TableHead>{t("system.audit.column.actor")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map((entry, i) => {
                const key = `${entry.ts}-${i}`;
                const tone = eventBadge(t, entry.event);
                const time = relativeTime(entry.ts, now);
                const isOpen = expanded.has(key);
                return (
                  <React.Fragment key={key}>
                    <TableRow
                      data-testid="system-audit-row"
                      className="cursor-pointer hover:bg-sg-inset"
                      onClick={() => toggleExpanded(key)}
                    >
                      <TableCell className="px-2 text-sg-ink-3">
                        {isOpen ? (
                          <ChevronDown
                            className="h-3.5 w-3.5"
                            aria-hidden="true"
                          />
                        ) : (
                          <ChevronRight
                            className="h-3.5 w-3.5"
                            aria-hidden="true"
                          />
                        )}
                      </TableCell>
                      <TableCell title={time.abs}>{time.rel}</TableCell>
                      <TableCell>
                        <Badge
                          variant={
                            tone.tone === "success"
                              ? "default"
                              : tone.tone === "error"
                                ? "destructive"
                                : "secondary"
                          }
                        >
                          {tone.label}
                        </Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {entry.tag ?? "—"}
                      </TableCell>
                      <TableCell className="text-xs text-sg-ink-3">
                        {entry.actor ?? "—"}
                      </TableCell>
                    </TableRow>
                    {isOpen ? (
                      <TableRow>
                        <TableCell />
                        <TableCell colSpan={4} className="bg-sg-inset">
                          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-sg-card p-2 font-mono text-[11px]">
                            {JSON.stringify(
                              {
                                event: entry.event,
                                request_id: entry.request_id ?? null,
                                details: entry.details,
                              },
                              null,
                              2,
                            )}
                          </pre>
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </React.Fragment>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {loadError ? (
        <p
          role="alert"
          className="mt-3 text-xs text-sg-err"
          data-testid="system-audit-load-error"
        >
          {loadError}
        </p>
      ) : null}

      {lastCursor ? (
        <div className="mt-3 flex justify-center">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={handleLoadMore}
            disabled={loadingMore}
            data-testid="system-audit-load-more"
          >
            {loadingMore ? "…" : t("system.audit.loadMore")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
