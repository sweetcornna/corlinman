"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import { Activity, AlertTriangle, Clock3, GitBranch, Loader2 } from "lucide-react";

import {
  loadPublicStatus,
  loadPublicStatusEvents,
  type PublicStatusCurrentStep,
  type PublicStatusResponse,
} from "@/lib/api";
import type { LiveEvent } from "@/lib/sessions/event-stream";
import { TimelineProvider } from "@/lib/sessions/store";
import { EventTimelineBody } from "@/components/sessions/event-timeline";
import { ToolCallCard } from "@/components/chat/tool-call-card";
import { SubagentCard } from "@/components/chat/subagent-card";
import type { SubagentCardState, ToolCallState } from "@/lib/chat/types";
import { cn } from "@/lib/utils";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; card: PublicStatusResponse; events: LiveEvent[] }
  | { status: "error"; message: string; code?: number };

export function StatusCardClient({ initialToken }: { initialToken: string }) {
  const pathname = usePathname();
  const token =
    initialToken === "__token__" ? tokenFromPathname(pathname) : initialToken;
  const [state, setState] = React.useState<LoadState>({ status: "loading" });
  const [attempt, setAttempt] = React.useState(0);

  React.useEffect(() => {
    if (!token) {
      setState({ status: "error", message: "Missing status token." });
      return;
    }
    let cancelled = false;
    const controller = new AbortController();

    async function load() {
      setState({ status: "loading" });
      try {
        const [card, replay] = await Promise.all([
          loadPublicStatus(token, { signal: controller.signal }),
          loadPublicStatusEvents(token, { signal: controller.signal }),
        ]);
        if (cancelled) return;
        setState({
          status: "ready",
          card,
          events: normalizeTimelineEvents(replay.events),
        });
      } catch (err) {
        if (cancelled) return;
        const apiErr = err as Error & { status?: number };
        setState({
          status: "error",
          message: apiErr.message || "Unable to load the status card.",
          code: apiErr.status,
        });
      }
    }

    void load();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [token, attempt]);

  return (
    <main
      data-testid="public-status-page"
      className={cn(
        "min-h-dvh bg-[#f6f1e7] text-stone-950",
        "dark:bg-[#11100e] dark:text-amber-50",
      )}
    >
      <div className="mx-auto flex min-h-dvh max-w-5xl flex-col px-4 py-5 sm:px-6 lg:px-8">
        {state.status === "loading" ? (
          <LoadingView />
        ) : state.status === "error" ? (
          <ErrorView
            message={state.message}
            code={state.code}
            onRetry={() => setAttempt((n) => n + 1)}
          />
        ) : (
          <ReadyView card={state.card} events={state.events} />
        )}
      </div>
    </main>
  );
}

function tokenFromPathname(pathname: string | null): string {
  const raw = (pathname ?? "").split("/").filter(Boolean);
  const statusIdx = raw.indexOf("status");
  if (statusIdx < 0) return "";
  return decodeURIComponent(raw[statusIdx + 1] ?? "");
}

function ReadyView({
  card,
  events,
}: {
  card: PublicStatusResponse;
  events: LiveEvent[];
}) {
  const started = formatTime(card.started_at_ms);
  const last = formatTime(card.last_activity_at_ms);
  const toolCards = deriveToolCards(events);
  const subagentCards = deriveSubagentCards(events);

  return (
    <div className="flex flex-1 flex-col gap-5">
      <header className="flex flex-col gap-4 border-b border-stone-900/10 pb-5 dark:border-white/10 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-wide text-stone-600 dark:text-amber-200/60">
            <span className="inline-flex items-center gap-1">
              <Activity className="size-3.5" aria-hidden />
              Public status
            </span>
            <span className="h-1 w-1 rounded-full bg-stone-400 dark:bg-amber-200/40" />
            <span>{card.turns.length} turns</span>
          </div>
          <h1 className="break-all font-mono text-xl font-semibold tracking-normal sm:text-2xl">
            {card.session_key}
          </h1>
        </div>
        <StatusBadge status={card.status} />
      </header>

      <section className="grid gap-3 sm:grid-cols-3">
        <InfoCell label="Started" value={started} icon={Clock3} />
        <InfoCell label="Last activity" value={last} icon={Clock3} />
        <InfoCell
          label="Current step"
          value={describeCurrentStep(card.current_step)}
          icon={GitBranch}
        />
      </section>

      {toolCards.length > 0 || subagentCards.length > 0 ? (
        <section
          data-testid="public-status-work-cards"
          className="rounded-lg border border-stone-900/10 bg-white/45 p-3 dark:border-white/10 dark:bg-black/20"
        >
          <div className="mb-2 text-[11px] uppercase tracking-wide text-stone-600 dark:text-amber-200/60">
            Recent work
          </div>
          <div className="space-y-2">
            {toolCards.map((tool) => (
              <ToolCallCard key={tool.callId} tool={tool} />
            ))}
            {subagentCards.map((subagent) => (
              <SubagentCard
                key={subagent.childSessionKey}
                subagent={subagent}
              />
            ))}
          </div>
        </section>
      ) : null}

      <section className="flex-1 rounded-lg border border-stone-900/10 bg-white/55 p-3 shadow-sm backdrop-blur sm:p-5 dark:border-white/10 dark:bg-black/25">
        <TimelineProvider>
          <EventTimelineBody
            sessionKey={card.session_key}
            mode="replay"
            seedEvents={events}
          />
        </TimelineProvider>
      </section>
    </div>
  );
}

function LoadingView() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <div
        role="status"
        className="inline-flex items-center gap-2 rounded-md border border-stone-900/10 bg-white/60 px-3 py-2 text-sm dark:border-white/10 dark:bg-black/30"
      >
        <Loader2 className="size-4 animate-spin" aria-hidden />
        Loading status
      </div>
    </div>
  );
}

function ErrorView({
  message,
  code,
  onRetry,
}: {
  message: string;
  code?: number;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-1 items-center justify-center">
      <div
        role="alert"
        className="w-full max-w-lg rounded-lg border border-red-300/60 bg-red-50/80 p-4 text-red-950 dark:border-red-400/30 dark:bg-red-950/35 dark:text-red-100"
      >
        <div className="flex items-center gap-2 font-medium">
          <AlertTriangle className="size-4" aria-hidden />
          Status card unavailable
        </div>
        <p className="mt-2 break-words text-sm opacity-85">
          {code ? `HTTP ${code}: ` : ""}
          {message}
        </p>
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded-md border border-red-400/40 px-2.5 py-1.5 text-sm hover:bg-red-100/70 dark:hover:bg-red-900/30"
        >
          Retry
        </button>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const label = status.replaceAll("_", " ");
  const active = status === "in_progress";
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium uppercase tracking-wide",
        active
          ? "bg-amber-200/70 text-amber-950 dark:bg-amber-500/20 dark:text-amber-100"
          : "bg-emerald-200/70 text-emerald-950 dark:bg-emerald-500/20 dark:text-emerald-100",
      )}
    >
      {active ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
      {label}
    </span>
  );
}

function InfoCell({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
}) {
  return (
    <div className="rounded-lg border border-stone-900/10 bg-white/45 px-3 py-2 dark:border-white/10 dark:bg-black/20">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-stone-600 dark:text-amber-200/60">
        <Icon className="size-3.5" aria-hidden />
        {label}
      </div>
      <div className="mt-1 truncate text-sm font-medium">{value}</div>
    </div>
  );
}

function formatTime(value: number | null): string {
  if (!value) return "Not recorded";
  return new Date(value).toLocaleString();
}

function describeCurrentStep(step: PublicStatusCurrentStep | null): string {
  if (!step) return "Idle";
  if (step.kind === "tool") return step.name || step.call_id || "Running tool";
  return step.child_agent_id || step.child_session_key || "Running subagent";
}

function deriveToolCards(events: LiveEvent[]): ToolCallState[] {
  const tools = new Map<string, ToolCallState>();
  for (const event of events) {
    if (!event.payload || typeof event.payload !== "object") continue;
    const payload = event.payload as Record<string, unknown>;
    if (event.event_type === "ToolStateRunning") {
      const callId =
        stringValue(payload.tool_call_id ?? payload.call_id) ??
        stringValue(payload.block_id) ??
        `${event.turn_id}:${event.sequence}`;
      const toolName =
        stringValue(payload.tool_name ?? payload.name) ?? "tool";
      tools.set(callId, {
        callId,
        toolName,
        argsJson: stringValue(payload.args_json) ?? "",
        status: "running",
        startedAt: numberValue(payload.started_at_ms) ?? event.timestamp_ms,
      });
    }
    if (event.event_type === "ToolStateCompleted") {
      const callId =
        stringValue(payload.tool_call_id ?? payload.call_id) ??
        stringValue(payload.block_id) ??
        `${event.turn_id}:${event.sequence}`;
      const previous = tools.get(callId);
      tools.set(callId, {
        callId,
        toolName: previous?.toolName ?? "tool",
        argsJson: previous?.argsJson ?? "",
        status: payload.is_error ? "error" : "ok",
        startedAt: previous?.startedAt,
        durationMs:
          numberValue(payload.elapsed_ms) ?? numberValue(payload.duration_ms),
        resultPreview:
          stringValue(payload.result_summary) ??
          stringValue(payload.output) ??
          stringValue(payload.error_message),
      });
    }
  }
  return Array.from(tools.values()).slice(-3);
}

function deriveSubagentCards(events: LiveEvent[]): SubagentCardState[] {
  const subagents = new Map<string, SubagentCardState>();
  for (const event of events) {
    if (!event.payload || typeof event.payload !== "object") continue;
    const payload = event.payload as Record<string, unknown>;
    if (event.event_type === "SubagentSpawned") {
      const childSessionKey =
        stringValue(payload.child_session_key) ??
        stringValue(payload.child_agent_id) ??
        `${event.turn_id}:${event.sequence}`;
      subagents.set(childSessionKey, {
        childSessionKey,
        childAgentId: stringValue(payload.child_agent_id),
        depth: numberValue(payload.depth) ?? 0,
        promptPreview: stringValue(payload.prompt_preview),
        status: "running",
      });
    }
    if (event.event_type === "SubagentCompleted") {
      const childSessionKey =
        stringValue(payload.child_session_key) ?? `${event.turn_id}:${event.sequence}`;
      const previous = subagents.get(childSessionKey);
      subagents.set(childSessionKey, {
        childSessionKey,
        childAgentId: previous?.childAgentId,
        depth: previous?.depth ?? 0,
        promptPreview: previous?.promptPreview,
        status: payload.finish_reason === "error" ? "errored" : "completed",
        finishReason: stringValue(payload.finish_reason),
        toolCallsMade: numberValue(payload.tool_calls_made),
        elapsedMs: numberValue(payload.elapsed_ms),
        summary: stringValue(payload.summary),
      });
    }
  }
  return Array.from(subagents.values()).slice(-3);
}

function normalizeTimelineEvents(events: LiveEvent[]): LiveEvent[] {
  const toolBlockByCallId = new Map<string, string>();
  const latestToolBlockByTurn = new Map<string, string>();

  return events.map((event) => {
    const payload = normalizePayload(event, toolBlockByCallId, latestToolBlockByTurn);
    return { ...event, payload };
  });
}

function normalizePayload(
  event: LiveEvent,
  toolBlockByCallId: Map<string, string>,
  latestToolBlockByTurn: Map<string, string>,
): unknown {
  if (!event.payload || typeof event.payload !== "object") return event.payload;
  const payload = event.payload as Record<string, unknown>;
  const byIndex = blockId(event.turn_id, payload.index);

  switch (event.event_type) {
    case "BlockStart": {
      const toolCallId = stringValue(payload.tool_call_id ?? payload.call_id);
      const id = stringValue(payload.block_id) || byIndex;
      if (toolCallId) toolBlockByCallId.set(toolCallId, id);
      if (payload.block_type === "tool_use" || payload.block_kind === "tool_use") {
        latestToolBlockByTurn.set(event.turn_id, id);
      }
      return {
        ...payload,
        block_id: id,
        block_kind: payload.block_kind ?? payload.block_type,
      };
    }
    case "TextDelta":
    case "ReasoningDelta":
      return {
        ...payload,
        block_id: stringValue(payload.block_id) || byIndex,
        delta: payload.delta ?? payload.text ?? "",
      };
    case "ToolInputDelta":
      return {
        ...payload,
        block_id: stringValue(payload.block_id) || byIndex,
        delta: payload.delta ?? payload.partial_json ?? "",
      };
    case "BlockStop":
      return {
        ...payload,
        block_id: stringValue(payload.block_id) || byIndex,
      };
    case "ToolStateRunning": {
      const callId = stringValue(payload.tool_call_id ?? payload.call_id);
      const id =
        stringValue(payload.block_id) ||
        (callId ? toolBlockByCallId.get(callId) : undefined) ||
        latestToolBlockByTurn.get(event.turn_id) ||
        callId ||
        byIndex;
      if (callId) toolBlockByCallId.set(callId, id);
      latestToolBlockByTurn.set(event.turn_id, id);
      return {
        ...payload,
        block_id: id,
        status_text: payload.status_text ?? payload.tool_name,
      };
    }
    case "ToolStateCompleted": {
      const callId = stringValue(payload.tool_call_id ?? payload.call_id);
      const id =
        stringValue(payload.block_id) ||
        (callId ? toolBlockByCallId.get(callId) : undefined) ||
        latestToolBlockByTurn.get(event.turn_id) ||
        callId ||
        byIndex;
      return {
        ...payload,
        block_id: id,
        output: payload.output ?? payload.result_summary,
        error_message: payload.error_message,
      };
    }
    case "SubagentEvent": {
      const inner = payload.envelope;
      if (!inner || typeof inner !== "object") return payload;
      return {
        ...payload,
        envelope: {
          ...(inner as LiveEvent),
          payload: normalizePayload(
            inner as LiveEvent,
            toolBlockByCallId,
            latestToolBlockByTurn,
          ),
        },
      };
    }
    default:
      return payload;
  }
}

function blockId(turnId: string, index: unknown): string {
  const suffix = index == null ? "0" : String(index);
  return `${turnId}:block:${suffix}`;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}
