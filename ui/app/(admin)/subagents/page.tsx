"use client";

/**
 * `/admin/subagents` — live activity panel for background sub-agents
 * (W2.2 of `docs/PLAN_MULTI_AGENT.md`).
 *
 * Layout (Tidepool):
 *   - Header — title + subtitle + "Include completed" toggle.
 *   - Activity table — one row per sub-agent (active by default).
 *   - Drawer — opens on row click, shows live timeline + summary +
 *     error tabs.
 *
 * Data plumbing:
 *   - `listSubagents()` for the initial warm-up snapshot.
 *   - `streamSubagentsOverview()` SSE feed merges live state transitions
 *     into the same Map (keyed by `request_id`). The feed also emits a
 *     snapshot of all current rows on connect, so we end up with two
 *     warm-up paths — the merge logic just upserts and lets the most
 *     recent state win.
 *
 * The "Include completed" toggle flips the `include_terminal=` query
 * parameter on the REST list. The SSE feed is store-driven and always
 * relays terminal transitions, so terminal rows still flow in when the
 * toggle is off — we just hide them in the rendered table.
 */

import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import {
  CorlinmanApiError,
  killSubagent,
  listSubagents,
  streamSubagentsOverview,
  type SubagentStatusResponse,
} from "@/lib/api";
import { LiveAgentsPanel } from "@/components/subagents/live-agents-panel";
import { SubagentDetailDrawer } from "@/components/subagents/subagent-detail-drawer";

const IN_FLIGHT: ReadonlySet<SubagentStatusResponse["state"]> = new Set([
  "queued",
  "running",
  "stalled",
]);

/** Stable ordering for the table — in-flight first (newest started
 * first), then terminal rows (newest finished first). Lets the
 * operator see the "currently running" set without scrolling. */
function sortRows(
  rows: SubagentStatusResponse[],
): SubagentStatusResponse[] {
  return [...rows].sort((a, b) => {
    const aActive = IN_FLIGHT.has(a.state);
    const bActive = IN_FLIGHT.has(b.state);
    if (aActive !== bActive) return aActive ? -1 : 1;
    const aKey = a.started_at ?? a.finished_at ?? 0;
    const bKey = b.started_at ?? b.finished_at ?? 0;
    return bKey - aKey;
  });
}

export default function SubagentsPage(): React.JSX.Element {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [includeTerminal, setIncludeTerminal] = React.useState<boolean>(false);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  // Local Map keyed by request_id — owns the rendered set. SSE upserts
  // and the initial REST list both write into this.
  const [rows, setRows] = React.useState<
    Map<string, SubagentStatusResponse>
  >(() => new Map());

  // Initial warm-up snapshot. Re-runs when the toggle flips so the
  // operator gets the matching set immediately rather than waiting on
  // the next SSE tick.
  const listQuery = useQuery({
    queryKey: ["subagents-list", includeTerminal],
    queryFn: () => listSubagents({ include_terminal: includeTerminal }),
    refetchOnWindowFocus: false,
  });

  // Seed the local map from the REST snapshot whenever it lands.
  React.useEffect(() => {
    const list = listQuery.data?.rows;
    if (!list) return;
    setRows((prev) => {
      const next = new Map(prev);
      for (const row of list) next.set(row.request_id, row);
      return next;
    });
  }, [listQuery.data]);

  // Subscribe to the global overview SSE — upserts rows on every
  // `event: subagent` frame. The feed emits a connect-time snapshot
  // (every active + terminal row) so it warms up by itself; the
  // separate REST list above just shortens the first-paint window.
  React.useEffect(() => {
    const es = streamSubagentsOverview();
    const onMessage = (raw: MessageEvent) => {
      let parsed: SubagentStatusResponse | null = null;
      try {
        parsed = JSON.parse(raw.data) as SubagentStatusResponse;
      } catch {
        return;
      }
      if (!parsed || typeof parsed.request_id !== "string") return;
      const snapshot: SubagentStatusResponse = parsed;
      setRows((prev) => {
        const next = new Map(prev);
        next.set(snapshot.request_id, snapshot);
        return next;
      });
      // If the drawer is open on this row, freshen its react-query
      // cache so the Summary / Error tabs reflect the new state
      // without waiting on the next 3s poll.
      queryClient.setQueryData(
        ["subagent-status", snapshot.request_id],
        snapshot,
      );
    };
    es.addEventListener("subagent", onMessage as EventListener);

    return () => {
      es.removeEventListener("subagent", onMessage as EventListener);
      es.close();
    };
  }, [queryClient]);

  // Visible set — apply the toggle, then sort.
  const visibleRows = React.useMemo(() => {
    const all = Array.from(rows.values());
    const filtered = includeTerminal
      ? all
      : all.filter((r) => IN_FLIGHT.has(r.state));
    return sortRows(filtered);
  }, [rows, includeTerminal]);

  async function handleKill(requestId: string) {
    try {
      const killed = await killSubagent(requestId);
      setRows((prev) => {
        const next = new Map(prev);
        next.set(killed.request_id, killed);
        return next;
      });
      queryClient.setQueryData(
        ["subagent-status", killed.request_id],
        killed,
      );
      toast.success(t("subagents.action.killSuccess"));
    } catch (err) {
      const msg =
        err instanceof CorlinmanApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : String(err);
      toast.error(t("subagents.action.killFailed", { msg }));
    }
  }

  const selectedRow = selectedId ? rows.get(selectedId) ?? null : null;
  const loadError = listQuery.error
    ? listQuery.error instanceof CorlinmanApiError
      ? listQuery.error.message
      : (listQuery.error as Error).message
    : null;

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
            {t("subagents.title")}
          </h1>
          <p className="text-xs text-sg-ink-3">{t("subagents.subtitle")}</p>
        </div>
        <label
          htmlFor="subagents-include-terminal"
          className="inline-flex cursor-pointer items-center gap-2 text-xs text-sg-ink-2"
        >
          <Switch
            id="subagents-include-terminal"
            checked={includeTerminal}
            onCheckedChange={setIncludeTerminal}
            data-testid="subagents-include-terminal-toggle"
          />
          {t("subagents.detail.includeTerminal")}
        </label>
      </header>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">{t("subagents.title")}</CardTitle>
          <CardDescription className="text-xs">
            {t("subagents.subtitle")}
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {loadError ? (
            <Alert variant="danger" className="mx-4 mb-4">
              {t("subagents.loadFailed", { msg: loadError })}
            </Alert>
          ) : null}
          {visibleRows.length === 0 ? (
            <EmptyState />
          ) : (
            <LiveAgentsPanel
              rows={visibleRows}
              onSelect={setSelectedId}
              onKill={handleKill}
              className="px-4 pb-4"
            />
          )}
        </CardContent>
      </Card>

      <SubagentDetailDrawer
        requestId={selectedId}
        initial={selectedRow}
        onClose={() => setSelectedId(null)}
      />
    </main>
  );
}

function EmptyState(): React.JSX.Element {
  const { t } = useTranslation();
  return (
    <div
      data-testid="subagents-empty"
      className={cn(
        "mx-4 mb-4 rounded-sg-md border border-dashed border-sg-border",
        "bg-sg-inset px-6 py-10 text-center",
      )}
    >
      <div className="text-sm font-medium text-sg-ink-2">
        {t("subagents.empty")}
      </div>
      <div className="mt-2 font-mono text-[11px] text-sg-ink-4">
        subagent.spawn
      </div>
    </div>
  );
}
