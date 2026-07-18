"use client";

/**
 * `<ChatLiveAgents>` — Codex-Desktop-style right rail in `/admin/chat` that
 * shows the sub-agents spawned by the CURRENT chat session, live.
 *
 * Subscribes to the same global `/admin/subagents/events/live` SSE the
 * `/admin/subagents` page uses, then filters to rows belonging to this
 * session (inline children are keyed as `{session}::child::…`, background
 * children carry `parent_session_key === session`). Renders the shared
 * `<LiveAgentsPanel>` (dense). Collapsible; auto-tucks to a thin strip when
 * the operator hides it (persisted per browser). Desktop-only (lg+) — the
 * narrow viewport keeps the transcript unobstructed on phones.
 *
 * Clicking a card drills into the session-detail timeline (where the inline
 * child's full live sub-tree renders); kill is wired through to
 * `killSubagent` for background rows (inline rows hide the affordance).
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Bot, PanelRightClose, PanelRightOpen } from "@/components/icons";

import { cn } from "@/lib/utils";
import {
  CorlinmanApiError,
  killSubagent,
  streamSubagentsOverview,
  type SubagentStatusResponse,
} from "@/lib/api";
import { LiveAgentsPanel } from "@/components/subagents/live-agents-panel";

const IN_FLIGHT: ReadonlySet<SubagentStatusResponse["state"]> = new Set([
  "queued",
  "running",
  "stalled",
]);

const COLLAPSE_KEY = "corlinman:chat:agents-rail-collapsed";

function belongsToSession(row: SubagentStatusResponse, sessionKey: string): boolean {
  if (!sessionKey) return false;
  return (
    row.parent_session_key === sessionKey ||
    row.request_id.startsWith(`${sessionKey}::`) ||
    (row.child_session_key?.startsWith(`${sessionKey}::`) ?? false)
  );
}

export interface ChatLiveAgentsProps {
  sessionKey: string;
}

export function ChatLiveAgents({ sessionKey }: ChatLiveAgentsProps): React.JSX.Element {
  const { t } = useTranslation();
  const [rows, setRows] = React.useState<Map<string, SubagentStatusResponse>>(
    () => new Map(),
  );
  const [collapsed, setCollapsed] = React.useState(false);

  React.useEffect(() => {
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
    } catch {
      /* ignore */
    }
  }, []);

  const toggleCollapsed = React.useCallback(() => {
    setCollapsed((v) => {
      const next = !v;
      try {
        localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  // Reset the row set when switching sessions so a prior run's agents don't
  // linger in the new conversation's rail.
  React.useEffect(() => {
    setRows(new Map());
  }, [sessionKey]);

  // Live overview SSE — upsert every frame, keep only this session's rows.
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
      if (!belongsToSession(parsed, sessionKey)) return;
      const snapshot = parsed;
      setRows((prev) => {
        const next = new Map(prev);
        next.set(snapshot.request_id, snapshot);
        return next;
      });
    };
    es.addEventListener("subagent", onMessage as EventListener);
    return () => {
      es.removeEventListener("subagent", onMessage as EventListener);
      es.close();
    };
  }, [sessionKey]);

  const allRows = React.useMemo(() => Array.from(rows.values()), [rows]);
  const activeCount = React.useMemo(
    () => allRows.filter((r) => IN_FLIGHT.has(r.state)).length,
    [allRows],
  );

  const handleKill = React.useCallback(
    async (requestId: string) => {
      try {
        const killed = await killSubagent(requestId);
        setRows((prev) => {
          const next = new Map(prev);
          next.set(killed.request_id, killed);
          return next;
        });
      } catch (err) {
        toast.error(
          err instanceof CorlinmanApiError ? err.message : String(err),
        );
      }
    },
    [],
  );

  // Cards expand inline (expandable below) to show the agent's current
  // status — no navigation needed, so onSelect is a no-op here.
  const noopSelect = React.useCallback(() => {}, []);

  if (collapsed) {
    return (
      <aside className="relative hidden shrink-0 lg:flex">
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-label={t("subagents.chatPanel.expand")}
          data-testid="chat-agents-rail-expand"
          className="flex h-9 items-center gap-1.5 self-start rounded-sg-md border border-sg-border bg-sg-card px-2 text-[11px] text-sg-ink-2 shadow-sg-1 hover:bg-sg-inset"
        >
          <PanelRightOpen className="h-3.5 w-3.5" />
          <Bot className="h-3.5 w-3.5" />
          {activeCount > 0 ? (
            <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-sg-warn-soft px-1 text-[10px] font-medium text-sg-warn">
              {activeCount}
            </span>
          ) : null}
        </button>
      </aside>
    );
  }

  return (
    <aside
      data-testid="chat-agents-rail"
      className={cn(
        "hidden w-[300px] shrink-0 flex-col overflow-hidden lg:flex",
        "rounded-xl border border-sg-border bg-sg-card shadow-sg-2",
      )}
    >
      <header className="flex items-center gap-2 border-b border-sg-border px-3 py-2.5">
        <Bot className="h-4 w-4 text-sg-ink-3" />
        <span className="text-sm font-semibold tracking-tight text-sg-ink">
          {t("subagents.chatPanel.title")}
        </span>
        {activeCount > 0 ? (
          <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-sg-warn-soft px-1 text-[10px] font-medium text-sg-warn">
            {activeCount}
          </span>
        ) : null}
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-label={t("subagents.chatPanel.collapse")}
          data-testid="chat-agents-rail-collapse"
          className="ml-auto rounded-sg-sm p-1 text-sg-ink-3 hover:bg-sg-inset hover:text-sg-ink"
        >
          <PanelRightClose className="h-4 w-4" />
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {allRows.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-1 px-4 py-8 text-center">
            <Bot className="h-5 w-5 text-sg-ink-4" />
            <p className="text-[12px] text-sg-ink-3">
              {t("subagents.chatPanel.empty")}
            </p>
          </div>
        ) : (
          <LiveAgentsPanel
            rows={allRows}
            onSelect={noopSelect}
            onKill={handleKill}
            dense
            expandable
          />
        )}
      </div>
    </aside>
  );
}

export default ChatLiveAgents;
