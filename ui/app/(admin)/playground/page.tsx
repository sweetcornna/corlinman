"use client";

import * as React from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Send, Square, Trash2, Wrench } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  GATEWAY_BASE_URL,
  apiFetch,
  fetchHealth,
  listPendingApprovals,
  type AgentSummary,
  type HealthStatus,
  type PluginSummary,
} from "@/lib/api";
import {
  fetchPersonas,
  fetchQqHumanlike,
  type Persona,
  type QqHumanlikeState,
} from "@/lib/api/personas";
import { openEventStream } from "@/lib/sse";
import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import { LogRow, type LogSeverity } from "@/components/ui/log-row";
import { AgentPicker } from "@/components/playground/agent-picker";

/**
 * /admin/playground — system overview + working chat surface.
 *
 * Replaces the deleted protocol-comparison playground. This page now
 * serves two jobs:
 *
 *   1. **System overview** — a row of stat chips reading the same admin
 *      queries the dashboard uses (plugins, agents, personas + QQ
 *      humanlike binding, pending approvals) plus a small tail of recent
 *      log events streamed from ``/admin/logs/stream``.
 *
 *   2. **Chat** — a transcript + composer that POSTs to
 *      ``/v1/chat/completions`` with ``stream: true``. Token deltas are
 *      appended to the assistant turn live; ``tool_calls`` deltas surface
 *      as collapsible "🔧 tool" chips inside the assistant turn. Tool
 *      dispatch + the final answer round-trip are owned by the gateway —
 *      the UI only renders deltas.
 *
 * Layout (1440w reference):
 *
 *   ┌─────────────────── overview ────────────────────┐
 *   │  Plugins · Agents · Persona · Approvals         │
 *   │  Recent activity tail (last 5 log events SSE)   │
 *   └─────────────────────────────────────────────────┘
 *   ┌─────────────────── chat ────────────────────────┐
 *   │  [model picker] [agent picker]                  │
 *   │  ┌────────────── transcript ──────────────┐     │
 *   │  │ user / assistant rounds; tool_call     │     │
 *   │  │ blocks rendered as collapsible chips   │     │
 *   │  └─────────────────────────────────────────┘    │
 *   │  [textarea + ⌘↵ Send + Stop]                    │
 *   └─────────────────────────────────────────────────┘
 */

// ─── activity row shape (matches /admin/logs/stream SSE payload) ─────
interface LogEvent {
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  subsystem: string;
  trace_id: string;
  message: string;
}

// ─── chat transcript shapes ──────────────────────────────────────────
interface ToolCallChip {
  /** Stable per-message tool-call index from the SSE delta. */
  index: number;
  /** ``call_<hex>`` from the gateway, or ``undefined`` until a chunk
   * supplies it. */
  id?: string;
  name?: string;
  arguments: string;
}

interface ChatTurn {
  /** Local-only id; the gateway's ``chatcmpl-*`` id arrives mid-stream. */
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  toolCalls?: ToolCallChip[];
  /** ``true`` while the assistant turn is still being streamed. */
  pending?: boolean;
  /** When the SSE stream surfaces an upstream error envelope. */
  error?: string;
}

// ─── sparkline geometry (deterministic so SSR + CSR match) ───────────
const SPARK_PRIMARY =
  "M0 28 L30 24 L60 26 L90 20 L120 22 L150 16 L180 18 L210 12 L240 14 L270 8 L300 10 L300 36 L0 36 Z";
const SPARK_FLAT =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const SPARK_ASCENDING =
  "M0 28 L30 26 L60 24 L90 22 L120 20 L150 16 L180 14 L210 10 L240 8 L270 6 L300 4 L300 36 L0 36 Z";
const SPARK_DESCENDING =
  "M0 10 L30 14 L60 16 L90 20 L120 22 L150 24 L180 26 L210 28 L240 30 L270 30 L300 32 L300 36 L0 36 Z";

const ACTIVITY_TAIL_MAX = 5;
const TRANSCRIPT_MAX = 200;
const DEFAULT_MODEL = "gpt-4o";

// ─── Page ────────────────────────────────────────────────────────────

export default function PlaygroundPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();

  // ── system overview queries ────────────────────────────────
  const plugins = useQuery<PluginSummary[]>({
    queryKey: ["admin", "plugins"],
    queryFn: () => apiFetch<PluginSummary[]>("/admin/plugins"),
    retry: false,
  });
  const agents = useQuery<AgentSummary[]>({
    queryKey: ["admin", "agents"],
    queryFn: () => apiFetch<AgentSummary[]>("/admin/agents"),
    retry: false,
  });
  const personasQ = useQuery<Persona[]>({
    queryKey: ["admin", "personas"],
    queryFn: fetchPersonas,
    retry: false,
  });
  const qqHumanlike = useQuery<QqHumanlikeState>({
    queryKey: ["admin", "channels", "qq", "humanlike"],
    queryFn: fetchQqHumanlike,
    retry: false,
  });
  const approvals = useQuery({
    queryKey: ["admin", "approvals", "pending"],
    queryFn: () => listPendingApprovals(),
    refetchInterval: 30_000,
    retry: false,
  });
  const health = useQuery<HealthStatus>({
    queryKey: ["admin", "health"],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
    retry: false,
  });

  // ── recent activity tail ──────────────────────────────────
  const [events, setEvents] = React.useState<LogEvent[]>([]);
  React.useEffect(() => {
    const close = openEventStream<LogEvent>("/admin/logs/stream", {
      events: ["log", "message"],
      onMessage: ({ data }) => {
        if (!data || typeof data !== "object") return;
        const ev = data as LogEvent;
        if (ev.level === "debug") return;
        setEvents((prev) => {
          const next = [ev, ...prev];
          if (next.length > ACTIVITY_TAIL_MAX) next.length = ACTIVITY_TAIL_MAX;
          return next;
        });
      },
    });
    return close;
  }, []);

  // ── derived stats ─────────────────────────────────────────
  const pluginsTotal = plugins.data?.length;
  const pluginsLoaded = plugins.data?.filter((p) => p.status === "loaded").length;
  const agentsCount = agents.data?.length;
  const personaCount = personasQ.data?.length ?? 0;
  const pendingApprovals = approvals.data?.length ?? 0;

  // ── chat state ────────────────────────────────────────────
  const [model, setModel] = React.useState<string>(DEFAULT_MODEL);
  const [explicitAgent, setExplicitAgent] = React.useState<string | null>(null);
  const [composer, setComposer] = React.useState("");
  const [transcript, setTranscript] = React.useState<ChatTurn[]>([]);
  const [streaming, setStreaming] = React.useState(false);
  const abortRef = React.useRef<AbortController | null>(null);
  const transcriptRef = React.useRef<HTMLDivElement | null>(null);

  // Scroll-pin transcript to bottom on new content.
  React.useEffect(() => {
    const el = transcriptRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [transcript]);

  React.useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const send = React.useCallback(async () => {
    const text = composer.trim();
    if (!text || streaming) return;
    if (!model.trim()) return;

    // Compose the next transcript: append the user turn + a pending
    // assistant turn. The assistant turn is mutated in-place as deltas
    // arrive so the React tree only re-renders the tail.
    const userTurn: ChatTurn = {
      id: `local-user-${Date.now()}`,
      role: "user",
      content: text,
    };
    const assistantId = `local-asst-${Date.now()}`;
    const assistantTurn: ChatTurn = {
      id: assistantId,
      role: "assistant",
      content: "",
      pending: true,
    };

    // Build the wire history from the existing transcript + the new
    // user turn. Skip pending / errored assistant turns so the gateway
    // doesn't see half-finished conversations.
    const history = [...transcript, userTurn]
      .filter((t) => !t.pending && !t.error)
      .map((t) => ({ role: t.role, content: t.content }));

    const body: Record<string, unknown> = {
      model,
      messages: history,
      stream: true,
    };
    if (explicitAgent) body.agent_id = explicitAgent;

    setTranscript((prev) => {
      const next = [...prev, userTurn, assistantTurn];
      if (next.length > TRANSCRIPT_MAX) next.splice(0, next.length - TRANSCRIPT_MAX);
      return next;
    });
    setComposer("");
    setStreaming(true);

    const ac = new AbortController();
    abortRef.current = ac;

    const patchAssistant = (
      patch: (turn: ChatTurn) => ChatTurn,
    ): void => {
      setTranscript((prev) =>
        prev.map((t) => (t.id === assistantId ? patch(t) : t)),
      );
    };

    try {
      const res = await fetch(`${GATEWAY_BASE_URL}/v1/chat/completions`, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: ac.signal,
      });
      if (!res.ok || !res.body) {
        const txt = await res.text().catch(() => "");
        patchAssistant((t) => ({
          ...t,
          pending: false,
          error: txt || `HTTP ${res.status}`,
        }));
        return;
      }
      await consumeChatSse(res.body, ac.signal, (frame) => {
        if (frame.kind === "delta") {
          patchAssistant((t) => ({
            ...t,
            content: t.content + frame.text,
          }));
        } else if (frame.kind === "tool_call") {
          patchAssistant((t) => mergeToolCall(t, frame));
        } else if (frame.kind === "error") {
          patchAssistant((t) => ({
            ...t,
            pending: false,
            error: frame.message,
          }));
        } else if (frame.kind === "done") {
          patchAssistant((t) => ({ ...t, pending: false }));
        }
      });
      // The server may close the stream without emitting [DONE] — make
      // sure the pending flag clears either way.
      patchAssistant((t) => (t.pending ? { ...t, pending: false } : t));
    } catch (err) {
      if (ac.signal.aborted) {
        patchAssistant((t) => ({
          ...t,
          pending: false,
        }));
      } else {
        const detail = err instanceof Error ? err.message : String(err);
        patchAssistant((t) => ({
          ...t,
          pending: false,
          error: detail,
        }));
      }
    } finally {
      setStreaming(false);
      if (abortRef.current === ac) abortRef.current = null;
    }
  }, [composer, model, explicitAgent, streaming, transcript]);

  const stop = React.useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const clear = React.useCallback(() => {
    abortRef.current?.abort();
    setTranscript([]);
  }, []);

  // ⌘↵ to send; Esc to stop while streaming.
  React.useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const meta = ev.metaKey || ev.ctrlKey;
      if (meta && ev.key === "Enter") {
        ev.preventDefault();
        void send();
        return;
      }
      if (ev.key === "Escape" && streaming) {
        ev.preventDefault();
        stop();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [send, stop, streaming]);

  // ── persona stat content (label depends on QQ binding) ────
  const boundChannel = qqHumanlike.data?.enabled && qqHumanlike.data?.persona_id
    ? "QQ"
    : null;
  const personaFoot = personasQ.isError
    ? t("playground.overview.endpointOffline")
    : personaCount === 0
      ? t("playground.overview.statPersonasNone")
      : boundChannel
        ? t("playground.overview.statPersonasBound", { channel: boundChannel })
        : t("playground.overview.statPersonasUnbound");

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
      data-testid="playground-page"
    >
      {/* ─── HERO ──────────────────────────────────────────── */}
      <GlassPanel
        as="header"
        variant="strong"
        className="relative overflow-hidden p-5 sm:p-7"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute bottom-[-80px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-70 blur-3xl"
          style={{
            background:
              "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
          }}
        />
        <div className="relative flex min-w-0 flex-col gap-2.5">
          <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-tp-glass-edge bg-tp-glass-inner-strong py-1 pl-2 pr-3 font-mono text-[11px] text-tp-ink-2">
            <span className="h-1.5 w-1.5 rounded-full bg-tp-amber tp-breathe-amber" />
            {t("playground.overview.heroLead")}
          </div>
          <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.12] tracking-[-0.025em] text-tp-ink sm:text-[32px]">
            {t("playground.overview.heroTitle")}
          </h1>
          <p className="max-w-[70ch] text-[14px] leading-[1.6] text-tp-ink-2">
            {t("playground.overview.heroSub")}
          </p>
        </div>
      </GlassPanel>

      {/* ─── STAT CHIPS (3-up + approvals = 4) ──────────────── */}
      <section className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
        <StatChip
          variant="primary"
          live
          label={t("playground.overview.statPlugins")}
          value={
            plugins.isError || pluginsTotal === undefined ? "—" : pluginsTotal
          }
          foot={
            plugins.isError
              ? t("playground.overview.endpointOffline")
              : t("playground.overview.statPluginsFoot", {
                  loaded: pluginsLoaded ?? 0,
                  total: pluginsTotal ?? 0,
                })
          }
          sparkPath={SPARK_PRIMARY}
          sparkTone="amber"
          data-testid="stat-plugins"
        />
        <StatChip
          label={t("playground.overview.statAgents")}
          value={agents.isError || agentsCount === undefined ? "—" : agentsCount}
          foot={
            agents.isError
              ? t("playground.overview.endpointOffline")
              : t("playground.overview.statAgentsFoot")
          }
          sparkPath={SPARK_FLAT}
          sparkTone="ember"
          data-testid="stat-agents"
        />
        <StatChip
          label={t("playground.overview.statPersonas")}
          value={personasQ.isError ? "—" : personaCount}
          foot={personaFoot}
          sparkPath={SPARK_ASCENDING}
          sparkTone="peach"
          data-testid="stat-personas"
        />
        <StatChip
          label={t("playground.overview.statApprovals")}
          value={approvals.isError ? "—" : pendingApprovals}
          delta={
            pendingApprovals === 0
              ? { label: t("playground.overview.statApprovalsCaughtUp"), tone: "up" }
              : undefined
          }
          foot={
            approvals.isError
              ? t("playground.overview.endpointOffline")
              : t("playground.overview.statApprovalsFoot")
          }
          sparkPath={SPARK_DESCENDING}
          sparkTone="ember"
          data-testid="stat-approvals"
        />
      </section>

      {/* ─── RECENT ACTIVITY ──────────────────────────────── */}
      <ActivityTail events={events} />

      {/* ─── CHAT ─────────────────────────────────────────── */}
      <ChatPanel
        model={model}
        onModelChange={setModel}
        explicitAgent={explicitAgent}
        onAgentChange={setExplicitAgent}
        composer={composer}
        onComposerChange={setComposer}
        transcript={transcript}
        streaming={streaming}
        onSend={send}
        onStop={stop}
        onClear={clear}
        transcriptRef={transcriptRef}
      />

      {/* unused refs hush the linter when health goes unread above */}
      <span aria-hidden className="sr-only">
        {health.data?.checks?.length ?? 0}
      </span>
    </motion.div>
  );
}

// ─── Activity tail ───────────────────────────────────────────────────

function ActivityTail({ events }: { events: LogEvent[] }) {
  const { t } = useTranslation();
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col p-5"
      data-testid="activity-tail"
    >
      <div className="flex items-center justify-between border-b border-tp-glass-edge pb-3">
        <div className="inline-flex items-center gap-2.5 text-[14px] font-semibold text-tp-ink">
          <span className="h-1.5 w-1.5 rounded-full bg-tp-ok tp-breathe" />
          {t("playground.overview.activityTitle")}
        </div>
        <Link
          href="/logs"
          className="inline-flex min-h-8 items-center rounded-md px-2 text-[12.5px] text-tp-ink-3 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
        >
          {t("playground.overview.activityViewAll")}
        </Link>
      </div>
      {events.length === 0 ? (
        <div
          className="flex items-center justify-center p-6 text-center text-[13px] text-tp-ink-3"
          data-testid="activity-empty"
        >
          {t("playground.overview.activityEmpty")}
        </div>
      ) : (
        <div className="flex flex-col">
          {events.map((e, i) => (
            <LogRow
              key={`${e.trace_id}-${e.ts}-${i}`}
              variant="comfortable"
              ts={safeTime(e.ts)}
              severity={mapSeverity(e.level)}
              subsystem={e.subsystem}
              message={e.message}
              justNow={i === 0}
            />
          ))}
        </div>
      )}
    </GlassPanel>
  );
}

function mapSeverity(level: LogEvent["level"]): LogSeverity {
  if (level === "error") return "err";
  if (level === "warn") return "warn";
  return "info";
}

function safeTime(iso: string): string {
  try {
    return iso.slice(11, 19);
  } catch {
    return "--:--:--";
  }
}

// ─── Chat panel ──────────────────────────────────────────────────────

interface ChatPanelProps {
  model: string;
  onModelChange: (next: string) => void;
  explicitAgent: string | null;
  onAgentChange: (next: string | null) => void;
  composer: string;
  onComposerChange: (next: string) => void;
  transcript: ChatTurn[];
  streaming: boolean;
  onSend: () => void;
  onStop: () => void;
  onClear: () => void;
  transcriptRef: React.RefObject<HTMLDivElement | null>;
}

function ChatPanel({
  model,
  onModelChange,
  explicitAgent,
  onAgentChange,
  composer,
  onComposerChange,
  transcript,
  streaming,
  onSend,
  onStop,
  onClear,
  transcriptRef,
}: ChatPanelProps) {
  const { t } = useTranslation();

  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col gap-3 p-4 sm:p-5"
      data-testid="chat-panel"
    >
      <div className="flex items-center justify-between border-b border-tp-glass-edge pb-3">
        <div className="flex flex-col gap-0.5">
          <h2 className="text-[14px] font-semibold text-tp-ink">
            {t("playground.overview.chatTitle")}
          </h2>
          <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
            {t("playground.overview.chatSubtitle")}
          </span>
        </div>
        <button
          type="button"
          onClick={onClear}
          disabled={transcript.length === 0}
          aria-label={t("playground.overview.chatClear")}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[12px]",
            "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
            "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "disabled:cursor-not-allowed disabled:opacity-50",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
          data-testid="chat-clear"
        >
          <Trash2 className="h-3 w-3" />
          {t("playground.overview.chatClear")}
        </button>
      </div>

      {/* model + agent pickers */}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
            {t("playground.overview.chatModelLabel")}
          </span>
          <input
            type="text"
            value={model}
            onChange={(e) => onModelChange(e.target.value)}
            placeholder={t("playground.overview.chatModelPlaceholder")}
            data-testid="chat-model-input"
            className={cn(
              "w-[260px] rounded-lg border px-3 py-1.5 font-mono text-[12.5px] text-tp-ink",
              "bg-tp-glass-inner border-tp-glass-edge placeholder:text-tp-ink-4",
              "outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 focus-visible:border-tp-amber/40",
            )}
          />
        </label>
        <AgentPicker value={explicitAgent} onChange={onAgentChange} />
      </div>

      {/* transcript */}
      <div
        ref={transcriptRef}
        className="flex max-h-[420px] min-h-[200px] flex-1 flex-col gap-3 overflow-y-auto rounded-lg border border-tp-glass-edge bg-tp-glass-inner p-3"
        data-testid="chat-transcript"
      >
        {transcript.length === 0 ? (
          <div
            className="flex flex-1 flex-col items-center justify-center gap-1 p-6 text-center"
            data-testid="chat-empty"
          >
            <div className="text-[14px] font-medium text-tp-ink-2">
              {t("playground.overview.chatEmptyTitle")}
            </div>
            <div className="max-w-[44ch] text-[12.5px] text-tp-ink-3">
              {t("playground.overview.chatEmptyHint")}
            </div>
          </div>
        ) : (
          transcript.map((turn) => <ChatTurnView key={turn.id} turn={turn} />)
        )}
      </div>

      {/* composer */}
      <label className="flex flex-col gap-1.5">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
          {t("playground.overview.chatComposerLabel")}
        </span>
        <textarea
          value={composer}
          onChange={(e) => onComposerChange(e.target.value)}
          rows={3}
          data-testid="chat-composer"
          placeholder={t("playground.overview.chatComposerPlaceholder")}
          aria-label={t("playground.overview.chatComposerLabel")}
          className={cn(
            "w-full resize-none rounded-lg border px-3 py-2 font-mono text-[13px] text-tp-ink",
            "bg-tp-glass-inner border-tp-glass-edge placeholder:text-tp-ink-4",
            "outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 focus-visible:border-tp-amber/40",
          )}
        />
      </label>

      <div className="flex items-center justify-between gap-2">
        <span className="hidden font-mono text-[10.5px] text-tp-ink-4 sm:inline">
          {t("playground.overview.chatHint")}
        </span>
        <div className="flex items-center gap-2">
          {streaming ? (
            <button
              type="button"
              onClick={onStop}
              data-testid="chat-stop"
              className={cn(
                "inline-flex items-center gap-2 rounded-lg px-3 py-2 text-[13px] font-medium",
                "border border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
                "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
              )}
            >
              <Square className="h-3 w-3" />
              {t("playground.overview.chatStop")}
            </button>
          ) : null}
          <button
            type="button"
            onClick={onSend}
            disabled={streaming || composer.trim().length === 0 || !model.trim()}
            data-testid="chat-send"
            className={cn(
              "inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-[13px] font-medium",
              "border border-tp-amber/35 bg-tp-amber-soft text-tp-amber",
              "shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_6px_14px_-8px_color-mix(in_oklch,var(--tp-amber)_55%,transparent)]",
              "transition-transform duration-200 hover:-translate-y-px",
              "hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
              "disabled:translate-y-0 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:translate-y-0",
            )}
          >
            <Send className="h-3 w-3" />
            {streaming
              ? t("playground.overview.chatSending")
              : t("playground.overview.chatSend")}
            <kbd className="ml-1 rounded bg-tp-amber/10 px-1.5 py-0.5 font-mono text-[10px] text-tp-amber/80">
              ⌘↵
            </kbd>
          </button>
        </div>
      </div>
    </GlassPanel>
  );
}

// ─── Transcript row + tool chip ──────────────────────────────────────

function ChatTurnView({ turn }: { turn: ChatTurn }) {
  const { t } = useTranslation();
  const roleLabel =
    turn.role === "user"
      ? t("playground.overview.chatRoleUser")
      : turn.role === "assistant"
        ? t("playground.overview.chatRoleAssistant")
        : t("playground.overview.chatRoleSystem");
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-md border px-3 py-2",
        turn.role === "user"
          ? "border-tp-glass-edge bg-tp-glass-inner-strong"
          : "border-tp-amber/20 bg-tp-amber-soft/40",
      )}
      data-testid={`chat-turn-${turn.role}`}
      data-pending={turn.pending ? "true" : undefined}
    >
      <div className="flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
        <span
          aria-hidden
          className={cn(
            "h-1.5 w-1.5 rounded-full",
            turn.role === "user"
              ? "bg-tp-ink-4"
              : turn.pending
                ? "bg-tp-amber tp-breathe-amber"
                : "bg-tp-amber",
          )}
        />
        {roleLabel}
      </div>
      {turn.content.length > 0 ? (
        <div className="whitespace-pre-wrap text-[13.5px] leading-[1.55] text-tp-ink">
          {turn.content}
        </div>
      ) : turn.pending && (turn.toolCalls?.length ?? 0) === 0 ? (
        <div className="font-mono text-[11px] text-tp-ink-4">…</div>
      ) : null}
      {turn.toolCalls && turn.toolCalls.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          {turn.toolCalls.map((tc) => (
            <ToolChip key={`${tc.index}-${tc.id ?? "pending"}`} tc={tc} />
          ))}
        </div>
      ) : null}
      {turn.error ? (
        <div className="flex flex-col gap-0.5 rounded-md border border-tp-err/30 bg-tp-err-soft px-2 py-1.5 text-[12px] text-tp-err">
          <span className="font-mono text-[10px] uppercase tracking-[0.08em]">
            {t("playground.overview.chatErrorTitle")}
          </span>
          <span>{t("playground.overview.chatErrorBody", { detail: turn.error })}</span>
        </div>
      ) : null}
    </div>
  );
}

function ToolChip({ tc }: { tc: ToolCallChip }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const label = tc.name
    ? t("playground.overview.chatToolChipLabel", { name: tc.name })
    : t("playground.overview.chatToolChipEmpty");
  return (
    <div
      className="rounded-md border border-tp-glass-edge bg-tp-glass-inner"
      data-testid="chat-tool-chip"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left",
          "font-mono text-[11.5px] text-tp-ink-2",
          "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        )}
      >
        <Wrench className="h-3 w-3" />
        <span className="truncate">{label}</span>
      </button>
      {open ? (
        <pre className="max-h-[200px] overflow-auto border-t border-tp-glass-edge bg-tp-glass-inner-strong p-2 font-mono text-[11px] text-tp-ink-2">
          {tc.arguments || "{}"}
        </pre>
      ) : null}
    </div>
  );
}

// ─── SSE consumer ────────────────────────────────────────────────────

interface DeltaFrame {
  kind: "delta";
  text: string;
}
interface ToolCallFrame {
  kind: "tool_call";
  index: number;
  id?: string;
  name?: string;
  arguments?: string;
}
interface DoneFrame {
  kind: "done";
  reason?: string;
}
interface ErrorFrame {
  kind: "error";
  message: string;
}
type ChatFrame = DeltaFrame | ToolCallFrame | DoneFrame | ErrorFrame;

/**
 * Read an OpenAI-shaped SSE response body and dispatch each parsed
 * frame to the caller. Exits cleanly on stream end, abort, or the
 * ``data: [DONE]`` sentinel. Malformed JSON chunks are dropped — the
 * server's contract is a JSON-per-chunk so a parse failure means the
 * server lied; we don't try to recover.
 */
export async function consumeChatSse(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onFrame: (frame: ChatFrame) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  try {
    while (true) {
      if (signal.aborted) {
        await reader.cancel().catch(() => undefined);
        return;
      }
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line. Parse one frame at a
      // time so partial chunks stay in the buffer for the next read.
      while (true) {
        const sep = buf.indexOf("\n\n");
        if (sep < 0) break;
        const raw = buf.slice(0, sep);
        buf = buf.slice(sep + 2);

        // Each frame may have multiple ``data:`` lines per the SSE spec.
        // Concatenate them with newlines (matches EventSource semantics).
        const dataLines: string[] = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trimStart());
          }
        }
        if (dataLines.length === 0) continue;
        const payload = dataLines.join("\n");
        if (payload === "[DONE]") {
          onFrame({ kind: "done" });
          return;
        }

        let parsed: unknown;
        try {
          parsed = JSON.parse(payload);
        } catch {
          continue;
        }
        for (const frame of extractFrames(parsed)) onFrame(frame);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** Translate one ``chat.completion.chunk`` JSON into zero-or-more frames. */
function extractFrames(payload: unknown): ChatFrame[] {
  if (!payload || typeof payload !== "object") return [];
  const obj = payload as Record<string, unknown>;

  // Upstream error envelope from chat.py — ``{"error": {...}}``.
  const errOuter = obj.error;
  if (errOuter && typeof errOuter === "object") {
    const e = errOuter as Record<string, unknown>;
    const message =
      (typeof e.message === "string" && e.message) ||
      (typeof e.reason === "string" && e.reason) ||
      "stream error";
    return [{ kind: "error", message }];
  }

  const choices = obj.choices;
  if (!Array.isArray(choices) || choices.length === 0) return [];
  const choice = choices[0] as Record<string, unknown>;
  const frames: ChatFrame[] = [];

  const delta = choice.delta as Record<string, unknown> | undefined;
  if (delta) {
    if (typeof delta.content === "string" && delta.content.length > 0) {
      frames.push({ kind: "delta", text: delta.content });
    }
    const toolCalls = delta.tool_calls;
    if (Array.isArray(toolCalls)) {
      for (const tc of toolCalls as Array<Record<string, unknown>>) {
        const index = typeof tc.index === "number" ? tc.index : 0;
        const id = typeof tc.id === "string" ? tc.id : undefined;
        const fn = (tc.function ?? {}) as Record<string, unknown>;
        const name = typeof fn.name === "string" ? fn.name : undefined;
        const args = typeof fn.arguments === "string" ? fn.arguments : undefined;
        frames.push({ kind: "tool_call", index, id, name, arguments: args });
      }
    }
  }

  const finish = choice.finish_reason;
  if (typeof finish === "string" && finish.length > 0) {
    frames.push({ kind: "done", reason: finish });
  }
  return frames;
}

/** Merge an incoming tool-call frame into the assistant turn's chip list. */
function mergeToolCall(turn: ChatTurn, frame: ToolCallFrame): ChatTurn {
  const existing = turn.toolCalls ?? [];
  const idx = existing.findIndex((c) => c.index === frame.index);
  if (idx === -1) {
    const next: ToolCallChip = {
      index: frame.index,
      id: frame.id,
      name: frame.name,
      arguments: frame.arguments ?? "",
    };
    return { ...turn, toolCalls: [...existing, next] };
  }
  const updated: ToolCallChip = {
    ...existing[idx]!,
    id: frame.id ?? existing[idx]!.id,
    name: frame.name ?? existing[idx]!.name,
    arguments: (existing[idx]!.arguments ?? "") + (frame.arguments ?? ""),
  };
  const nextList = existing.slice();
  nextList[idx] = updated;
  return { ...turn, toolCalls: nextList };
}
