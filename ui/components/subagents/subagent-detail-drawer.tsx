"use client";

/**
 * `<SubagentDetailDrawer>` — right-side drawer with per-subagent
 * timeline + summary + error. Opens when the operator clicks a row in
 * `/admin/subagents`.
 *
 * EventTimeline integration: the existing `<EventTimeline>` in
 * `components/sessions/` opens its SSE stream by *session key*
 * (`openLiveEventStream(sessionKey)`). The subagent's child run owns
 * its own `child_session_key` — and the supervisor's `BubbleEmitter`
 * already publishes the child's events under that key. We can
 * therefore mount `<EventTimeline mode="live"
 * sessionKey={status.child_session_key}>` directly — no adapter
 * needed.
 *
 * Until the child agent boots, `child_session_key` is null; we show a
 * pending placeholder in the Timeline tab instead.
 */

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "@/components/icons";

import { cn } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Drawer } from "@/components/ui/drawer";
import { EventTimeline } from "@/components/sessions/event-timeline";
import {
  fetchSubagentStatus,
  type SubagentStatusResponse,
} from "@/lib/api";

export interface SubagentDetailDrawerProps {
  /** When non-null, the drawer is open and bound to this id. */
  requestId: string | null;
  /** Optional optimistic snapshot from the overview list — used as
   * placeholder data while the per-row poll catches up. */
  initial?: SubagentStatusResponse | null;
  onClose: () => void;
}

type TabId = "timeline" | "summary" | "error";

export function SubagentDetailDrawer({
  requestId,
  initial,
  onClose,
}: SubagentDetailDrawerProps): React.JSX.Element {
  const { t } = useTranslation();
  const [tab, setTab] = React.useState<TabId>("timeline");

  // Reset to the timeline tab whenever a new row is opened.
  React.useEffect(() => {
    if (requestId != null) setTab("timeline");
  }, [requestId]);

  // Polled status — 3s while the drawer is open so the Summary / Error
  // tabs reflect terminal transitions. The Timeline itself is driven
  // by the SSE stream the `<EventTimeline>` opens internally.
  const statusQuery = useQuery<SubagentStatusResponse>({
    queryKey: ["subagent-status", requestId],
    queryFn: () => fetchSubagentStatus(requestId as string),
    enabled: requestId != null,
    initialData: initial ?? undefined,
    refetchInterval: (q) => {
      const s = q.state.data?.state;
      if (s === "succeeded" || s === "failed" || s === "killed" || s === "timeout")
        return false;
      return 3000;
    },
  });
  const status = statusQuery.data ?? initial ?? null;

  const title = requestId
    ? t("subagents.detail.title", { id: requestId.slice(0, 8) })
    : t("subagents.title");

  return (
    <Drawer
      open={requestId != null}
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={title}
      width="xl"
    >
      {status ? (
        <div className="flex h-full flex-col">
          {/* Parent session deep-link */}
          <div className="border-b border-border px-5 py-3 text-xs text-muted-foreground">
            <Link
              href={`/sessions/detail?key=${encodeURIComponent(
                status.parent_session_key,
              )}`}
              className="font-mono text-foreground/80 underline-offset-2 hover:underline"
              onClick={onClose}
            >
              {status.parent_session_key}
            </Link>
            <span className="mx-2 opacity-40">·</span>
            <span className="font-mono">{status.subagent_type}</span>
            <span className="mx-2 opacity-40">·</span>
            <span>{t(`subagents.state.${status.state}`)}</span>
          </div>

          {/* Tab strip */}
          <div
            role="tablist"
            aria-label={title}
            className="flex shrink-0 gap-1 border-b border-border px-3 pt-2"
          >
            {(
              [
                ["timeline", t("subagents.detail.timeline")],
                ["summary", t("subagents.detail.summary")],
                ["error", t("subagents.detail.error")],
              ] as const
            )
              // Hide the Error tab unless we actually have one to show.
              .filter(([id]) => id !== "error" || Boolean(status.error))
              .map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={tab === id}
                  data-testid={`subagent-tab-${id}`}
                  onClick={() => setTab(id)}
                  className={cn(
                    "rounded-t-md border-b-2 px-3 py-1.5 text-xs font-medium transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
                    tab === id
                      ? "border-sg-accent text-foreground"
                      : "border-transparent text-muted-foreground hover:text-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
          </div>

          {/* Tab body */}
          <div className="flex-1 overflow-y-auto px-5 py-4">
            {tab === "timeline" ? (
              status.child_session_key ? (
                <EventTimeline
                  sessionKey={status.child_session_key}
                  mode="live"
                />
              ) : (
                <div className="rounded-sg-md border border-dashed border-border bg-muted/20 p-4 text-center text-xs italic text-muted-foreground">
                  <Loader2 className="mx-auto mb-2 size-4 animate-spin" />
                  {t("subagents.state.queued")}…
                </div>
              )
            ) : null}
            {tab === "summary" ? (
              <pre
                data-testid="subagent-summary"
                className={cn(
                  "whitespace-pre-wrap break-words rounded-sg-md border border-border",
                  "bg-muted/20 p-3 font-mono text-[12px] text-foreground",
                )}
              >
                {status.summary || `(${t("sessions.timeline.empty")})`}
              </pre>
            ) : null}
            {tab === "error" && status.error ? (
              <Alert
                data-testid="subagent-error"
                variant="danger"
                icon={null}
              >
                <span className="font-mono text-[12px] text-sg-err">
                  {status.error}
                </span>
              </Alert>
            ) : null}
          </div>

          {/* No explicit footer — the drawer ships an X close affordance
           * in its header. */}
        </div>
      ) : null}
    </Drawer>
  );
}
