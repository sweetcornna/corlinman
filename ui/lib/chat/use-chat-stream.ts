"use client";

/**
 * React hook that owns the lifecycle of one chat conversation:
 *
 *   - `messages` state (user + assistant + tool history)
 *   - in-flight assistant turn (token stream + journal stream merged)
 *   - send / stop / retry / approve actions
 *   - resume-on-mount (live SSE picks up Last-Event-ID; history is the
 *     responsibility of the caller via React Query)
 *
 * Single React component (the chat page) wires this hook up once per
 * `sessionKey` it renders.
 */

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";

import { CorlinmanApiError, GATEWAY_BASE_URL } from "@/lib/api";
import {
  attachmentKindFromMime,
  cancelChatSession,
  streamChatCompletions,
  submitApproval,
  type ChatCompletionMessage,
  type ChatCompletionRequest,
  type ChatCompletionToolDef,
  type ReasoningEffort,
} from "@/lib/api/chat";
import { fetchTurnEvents, listSessionTurns } from "@/lib/api/sessions";
import { buildMessageContent } from "@/lib/chat/content-parts";
import {
  openLiveEventStream,
  type LiveEvent,
} from "@/lib/sessions/event-stream";
import {
  EventDedupSet,
  chunkToChatEvents,
  liveEventToChatEvent,
} from "@/lib/chat/event-merger";
import type {
  ApprovalDecision,
  ApprovalScope,
  ChatAttachment,
  ChatEvent,
  ChatMessage,
} from "@/lib/chat/types";

interface UseChatStreamArgs {
  sessionKey: string;
  model: string;
  /** Optional system prompt to lead the conversation with. */
  systemPrompt?: string;
  /** Tools enabled this turn (forwarded to /v1/chat/completions). */
  tools?: ChatCompletionToolDef[];
  /** Provider reasoning budget for models that expose one. */
  reasoningEffort?: ReasoningEffort;
  /** Persona / agent id to bind this conversation to. Goes in metadata. */
  agentId?: string;
  personaId?: string;
}

export interface UseChatStreamResult {
  messages: ChatMessage[];
  pendingMessage: ChatMessage | null;
  isStreaming: boolean;
  sendMessage: (text: string, attachments?: ChatAttachment[]) => Promise<void>;
  retryLast: () => Promise<void>;
  stop: () => Promise<void>;
  hydrate: (history: ChatMessage[]) => void;
  approve: (
    turnId: string,
    callId: string,
    decision: ApprovalDecision,
    scope?: ApprovalScope,
  ) => Promise<void>;
  /**
   * Replace a user message's content and re-run from that point — drops
   * every message after `messageId`, then triggers a new turn with the
   * updated user content.
   */
  editAndRerun: (messageId: string, newContent: string) => Promise<void>;
  /**
   * Returns the slice of messages up to and including `messageId` so the
   * caller can branch into a new conversation pre-loaded with that
   * history. Does not mutate state.
   */
  sliceUntil: (messageId: string) => ChatMessage[];
  /**
   * Reattach to a turn that is still generating server-side (the user
   * navigated away mid-turn and came back). Detects an `in_progress`
   * latest turn, rebuilds the pending bubble from the journal event
   * backlog, then tails the live SSE from where the backlog ended.
   * No-op when nothing is in flight. Caller invokes it after hydrate.
   */
  resumeInFlight: () => Promise<void>;
  /**
   * Aggregated input/output tokens + estimated cost across all completed
   * assistant turns in this session.
   */
  totals: { inputTokens: number; outputTokens: number; costUsd: number };
}

function genId(): string {
  return `m_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

/**
 * Reduce a `ChatEvent` into a `ChatMessage` (mutates a draft for speed —
 * caller wraps in immer-style copy at the React layer).
 */
function applyEvent(draft: ChatMessage, ev: ChatEvent): void {
  switch (ev.kind) {
    case "turn-start":
      draft.turnId = ev.turnId;
      break;
    case "text-delta":
      draft.content += ev.text;
      break;
    case "reasoning-delta":
      draft.reasoning = (draft.reasoning ?? "") + ev.text;
      break;
    case "tool-input-delta": {
      const tc = (draft.toolCalls ??= []).find((t) => t.callId === ev.callId);
      if (tc) tc.argsJson += ev.delta;
      else {
        draft.toolCalls.push({
          callId: ev.callId,
          toolName: "(pending)",
          argsJson: ev.delta,
          status: "running",
        });
      }
      break;
    }
    case "tool-running": {
      const list = (draft.toolCalls ??= []);
      const tc = list.find((t) => t.callId === ev.callId);
      if (tc) {
        tc.toolName = ev.toolName;
        tc.pluginName = ev.pluginName;
        tc.argsJson = ev.argsJson || tc.argsJson;
        tc.startedAt = ev.startedAtMs;
        tc.status = "running";
      } else {
        list.push({
          callId: ev.callId,
          toolName: ev.toolName,
          pluginName: ev.pluginName,
          argsJson: ev.argsJson,
          startedAt: ev.startedAtMs,
          status: "running",
        });
      }
      break;
    }
    case "tool-completed": {
      const tc = (draft.toolCalls ??= []).find((t) => t.callId === ev.callId);
      if (tc) {
        tc.resultPreview = ev.resultPreview;
        tc.durationMs = ev.durationMs;
        tc.status = ev.isError ? "error" : "ok";
      }
      break;
    }
    case "subagent-spawned": {
      (draft.subagents ??= []).push({
        childSessionKey: ev.childSessionKey,
        childAgentId: ev.childAgentId,
        depth: ev.depth,
        promptPreview: ev.promptPreview,
        status: "spawned",
        events: [],
      });
      break;
    }
    case "subagent-event": {
      const sa = (draft.subagents ??= []).find(
        (s) => s.childSessionKey === ev.childSessionKey,
      );
      if (sa) {
        sa.status = "running";
        (sa.events ??= []).push(ev.envelope);
      }
      break;
    }
    case "subagent-completed": {
      const sa = (draft.subagents ??= []).find(
        (s) => s.childSessionKey === ev.childSessionKey,
      );
      if (sa) {
        sa.status = "completed";
        sa.finishReason = ev.finishReason;
        sa.toolCallsMade = ev.toolCallsMade;
        sa.elapsedMs = ev.elapsedMs;
        sa.summary = ev.summary;
      }
      break;
    }
    case "awaiting-approval": {
      (draft.approvals ??= []).push({
        callId: ev.callId,
        plugin: ev.plugin,
        tool: ev.tool,
        argsPreviewJson: ev.argsPreviewJson,
        reason: ev.reason,
      });
      break;
    }
    case "attachment": {
      // Same attachment can arrive twice (token-stream `corlinman`
      // extension AND journal `AttachmentAdded`) — dedup on the resolved
      // remote url so it renders exactly once.
      const list = (draft.attachments ??= []);
      const remoteUrl = ev.attachment.url.startsWith("/")
        ? `${GATEWAY_BASE_URL}${ev.attachment.url}`
        : ev.attachment.url;
      if (list.some((a) => a.remoteUrl === remoteUrl)) break;
      const k = ev.attachment.kind;
      list.push({
        id: `att_${ev.turnId}_${ev.sequence}_${list.length}`,
        kind:
          k === "image" || k === "audio" || k === "video"
            ? k
            : attachmentKindFromMime(ev.attachment.mime ?? ""),
        name: ev.attachment.name,
        mime: ev.attachment.mime,
        sizeBytes: ev.attachment.size ?? 0,
        remoteUrl,
      });
      break;
    }
    case "turn-complete":
      draft.usage = { ...(draft.usage ?? {}), ...ev.usage };
      draft.pending = false;
      draft.cancelling = false;
      break;
    case "turn-errored":
      draft.error = ev.error;
      draft.pending = false;
      draft.cancelling = false;
      break;
    case "cancelling":
      draft.cancelling = true;
      break;
    case "tools-settle":
      // OpenAI-compat stream-end: any tool still "running" gets
      // demoted to "settled" so the UI stops spinning. Tools that
      // already received a journal-driven ToolStateCompleted stay
      // "ok"/"error".
      if (draft.toolCalls && draft.toolCalls.length > 0) {
        for (const tc of draft.toolCalls) {
          if (tc.status === "running") tc.status = "settled";
        }
      }
      break;
  }
}

export function useChatStream(args: UseChatStreamArgs): UseChatStreamResult {
  const [messages, setMessages] = React.useState<ChatMessage[]>([]);
  const [pendingMessage, setPendingMessage] =
    React.useState<ChatMessage | null>(null);
  const [isStreaming, setIsStreaming] = React.useState(false);

  const abortRef = React.useRef<AbortController | null>(null);
  const closeLiveRef = React.useRef<(() => void) | null>(null);
  const closeTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const dedupRef = React.useRef<EventDedupSet>(new EventDedupSet());
  const lastUserMessageRef = React.useRef<{
    text: string;
    attachments?: ChatAttachment[];
  } | null>(null);
  // Identity of the turn that currently owns `pendingMessage`. A new
  // `runTurn` (rapid resend / edit-rerun) or a `hydrate` (session switch)
  // supersedes the previous turn: its draft object is replaced here and
  // its ids land in `retiredTurnIdsRef`, so late events from the dying
  // streams can never reduce into the new draft (cross-turn pollution).
  const activeDraftRef = React.useRef<ChatMessage | null>(null);
  // Live mirror of `pendingMessage` for synchronous reads (state itself
  // is only available asynchronously inside updater callbacks).
  const pendingRef = React.useRef<ChatMessage | null>(null);
  const retiredTurnIdsRef = React.useRef<Set<string>>(new Set());
  // True while the POST /v1/chat/completions fetch owns the turn. The
  // journal live stream emits the SAME text tokens as the fetch (both
  // tee off the reasoning loop); while the fetch is live it is the sole
  // text authority and journal text deltas are dropped — otherwise the
  // reply doubles. Tool/attachment/lifecycle journal events still apply.
  const fetchActiveRef = React.useRef(false);
  // Turn id adopted from the journal by `resumeInFlight` (no local
  // fetch). Its terminal event — or the status-poll safety net —
  // finalizes the pending bubble instead of `runTurn`'s `finally`.
  const journalTurnRef = React.useRef<string | null>(null);
  const statusPollRef = React.useRef<ReturnType<typeof setInterval> | null>(
    null,
  );
  const qc = useQueryClient();

  /** Retire every id the current pending draft is known under. */
  const retirePending = React.useCallback(() => {
    const prev = pendingRef.current;
    if (!prev) return;
    retiredTurnIdsRef.current.add(prev.id);
    if (prev.turnId) retiredTurnIdsRef.current.add(prev.turnId);
  }, []);

  const hydrate = React.useCallback(
    (history: ChatMessage[]) => {
      // Hydration replaces the whole thread (session switch / clear).
      // Kill any in-flight turn first so its late events and its
      // `finally` commit can't leak into the freshly-hydrated view.
      retirePending();
      abortRef.current?.abort();
      abortRef.current = null;
      closeLiveRef.current?.();
      closeLiveRef.current = null;
      activeDraftRef.current = null;
      pendingRef.current = null;
      fetchActiveRef.current = false;
      journalTurnRef.current = null;
      if (statusPollRef.current) {
        clearInterval(statusPollRef.current);
        statusPollRef.current = null;
      }
      setMessages(history);
      setPendingMessage(null);
      setIsStreaming(false);
      dedupRef.current.reset();
    },
    [retirePending],
  );

  /** Commit a journal-owned (resumed) turn: move the pending bubble into
   *  history and refresh the canonical transcript. The transcript refetch
   *  re-hydrates the thread with the authoritative journal rows, so a
   *  partial reconstruction (e.g. the agent process journaled no events)
   *  self-heals instead of freezing a half-empty bubble. */
  const finalizeJournalTurn = React.useCallback(() => {
    journalTurnRef.current = null;
    if (statusPollRef.current) {
      clearInterval(statusPollRef.current);
      statusPollRef.current = null;
    }
    setIsStreaming(false);
    activeDraftRef.current = null;
    setPendingMessage((current) => {
      if (current) {
        setMessages((prev) => [
          ...prev,
          { ...current, pending: false, cancelling: false },
        ]);
      }
      return null;
    });
    pendingRef.current = null;
    void qc.invalidateQueries({ queryKey: ["chat", "sessions"] });
    void qc.invalidateQueries({
      queryKey: ["chat", "transcript", args.sessionKey],
    });
    // Same trailing-event grace the fetch path uses before closing.
    const close = closeLiveRef.current;
    closeLiveRef.current = null;
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
    closeTimerRef.current = setTimeout(() => close?.(), 500);
  }, [qc, args.sessionKey]);

  const reduceEvent = React.useCallback((ev: ChatEvent) => {
    // Events from a superseded turn (stopped, re-sent over, or left
    // behind on session switch) are dropped wholesale — applying them
    // to the *current* draft was the cross-turn pollution bug.
    if (retiredTurnIdsRef.current.has(ev.turnId)) return;
    if (!dedupRef.current.shouldEmit(ev.turnId, ev.sequence)) return;
    // Journal deltas (seq >= 0) duplicate what the fetch stream already
    // carries — text tokens AND tool-args fragments (appending both
    // garbles argsJson) — so drop them while the fetch owns the turn.
    // Journal tool STATE events still apply: they carry the real
    // completion status the OpenAI-shaped stream has no frame for.
    if (
      fetchActiveRef.current &&
      ev.sequence >= 0 &&
      (ev.kind === "text-delta" ||
        ev.kind === "reasoning-delta" ||
        ev.kind === "tool-input-delta")
    ) {
      return;
    }
    setPendingMessage((prev) => {
      if (!prev) return prev;
      // PERF-010: clone ONLY the sub-structure the incoming event actually
      // mutates and reuse the references for everything else. The old code
      // deep-copied toolCalls/subagents (incl. each nested events array)/
      // approvals/usage on *every* event — so a pure token-content delta
      // re-allocated the whole tool/subagent graph and forced every
      // downstream card to re-render. We always spread `prev` into a fresh
      // top-level object (React needs a new reference), but the untouched
      // array/object branches stay reference-equal.
      const next: ChatMessage = { ...prev };
      switch (ev.kind) {
        case "tool-input-delta":
        case "tool-running":
        case "tool-completed":
        case "tools-settle":
          // These mutate (or append to) `toolCalls`. Clone the array and its
          // elements so `applyEvent`'s in-place mutations don't touch `prev`.
          next.toolCalls = prev.toolCalls
            ? prev.toolCalls.map((t) => ({ ...t }))
            : undefined;
          break;
        case "subagent-spawned":
        case "subagent-event":
        case "subagent-completed":
          // These mutate `subagents` (and a subagent's nested `events`).
          next.subagents = prev.subagents
            ? prev.subagents.map((s) => ({
                ...s,
                events: s.events ? [...s.events] : [],
              }))
            : undefined;
          break;
        case "awaiting-approval":
          next.approvals = prev.approvals
            ? prev.approvals.map((a) => ({ ...a }))
            : undefined;
          break;
        case "attachment":
          next.attachments = prev.attachments
            ? prev.attachments.map((a) => ({ ...a }))
            : undefined;
          break;
        case "turn-complete":
          next.usage = prev.usage ? { ...prev.usage } : undefined;
          break;
        // text-delta / reasoning-delta / turn-start / turn-errored only
        // touch scalar fields already covered by the `...prev` spread.
      }
      applyEvent(next, ev);
      pendingRef.current = next;
      return next;
    });
    // A journal-owned (resumed) turn has no local fetch whose `finally`
    // commits the bubble — its terminal event does it instead.
    if (
      (ev.kind === "turn-complete" || ev.kind === "turn-errored") &&
      !fetchActiveRef.current &&
      journalTurnRef.current !== null &&
      ev.turnId === journalTurnRef.current
    ) {
      finalizeJournalTurn();
    }
  }, [finalizeJournalTurn]);

  /**
   * Run one turn end-to-end: open both streams, merge, settle.
   *
   * `baseHistory`, when provided, is the explicit list of prior messages
   * the request body should be built from (e.g. the truncated history from
   * `editAndRerun`). When omitted we fall back to the hook's `messages`
   * state. Relying on the closure's `messages` alone is unsafe for callers
   * that mutate state and immediately re-run, since the freshly-mutated
   * state is not yet committed into this `useCallback`'s captured value.
   */
  const runTurn = React.useCallback(
    async (userMsg: ChatMessage, baseHistory?: ChatMessage[]) => {
      // Supersede any turn still in flight (rapid resend / edit-rerun):
      // retire its ids and abort its token stream BEFORE the new draft
      // exists, so its late chunks, its AbortError handler and its
      // `finally` commit all become no-ops instead of corrupting the
      // new turn's state.
      retirePending();
      abortRef.current?.abort();
      abortRef.current = null;
      // A journal-owned resumed turn (if any) is superseded too — its
      // ids were just retired; drop its finalize hooks.
      journalTurnRef.current = null;
      if (statusPollRef.current) {
        clearInterval(statusPollRef.current);
        statusPollRef.current = null;
      }

      // Open the assistant draft.
      const draft: ChatMessage = {
        id: genId(),
        role: "assistant",
        content: "",
        createdAt: Date.now(),
        pending: true,
      };
      activeDraftRef.current = draft;
      pendingRef.current = draft;
      setPendingMessage(draft);
      setIsStreaming(true);
      dedupRef.current = new EventDedupSet();

      // Subscribe to journal events. We don't await here — events arrive
      // asynchronously alongside the token stream.
      //
      // Close any prior live stream first. If the user re-sends (or
      // `editAndRerun` fires) while the previous turn is still in-flight
      // — or during the 500ms grace window in the `finally` block below
      // — `closeLiveRef.current` still points at the previous turn's
      // EventSource. Reassigning without closing first leaks that
      // EventSource and keeps a second `onEvent` consumer reducing into
      // a stale `pendingMessage` until the page unmounts (R1-005).
      closeLiveRef.current?.();
      closeLiveRef.current = openLiveEventStream(args.sessionKey, {
        onEvent: (live) => {
          const chatEvent = liveEventToChatEvent(live);
          if (chatEvent) reduceEvent(chatEvent);
        },
      });

      // Defensive: if the page somehow renders us without a session
      // key (the conditional guard in /chat/page.tsx should prevent
      // this, but defensive belt-and-braces) generate one on the fly
      // so the journal never gets an empty key. Empty keys collapse
      // every web turn into a single un-resumable aggregate row in
      // /admin/sessions, which is the bug we're guarding against.
      let effectiveSessionKey = (args.sessionKey ?? "").trim();
      if (!effectiveSessionKey) {
        const r = Math.random().toString(36).slice(2, 10);
        effectiveSessionKey = `corlinman:${Date.now().toString(36)}:${r}`;
        if (typeof console !== "undefined") {
          console.warn(
            "useChatStream: missing sessionKey arg; generated fallback",
            effectiveSessionKey,
          );
        }
      }

      // Build the OpenAI-compatible request body.
      const messagesPayload: ChatCompletionMessage[] = [];
      if (args.systemPrompt) {
        messagesPayload.push({ role: "system", content: args.systemPrompt });
      }
      const history = baseHistory ?? messages;
      for (const m of history) {
        if (m.role === "user" || m.role === "assistant" || m.role === "system") {
          messagesPayload.push({
            role: m.role,
            content: buildMessageContent(m.content, m.attachments),
          });
        }
      }
      // Attachments ride as OpenAI content-parts (W3). Pre-fix they were
      // collected by the composer and then silently dropped here — the
      // model never saw them and history couldn't re-render them.
      messagesPayload.push({
        role: "user",
        content: buildMessageContent(userMsg.content, userMsg.attachments),
      });

      const reqBody: ChatCompletionRequest = {
        model: args.model,
        stream: true,
        messages: messagesPayload,
        // Pin the conversation id at the top level — the gateway's
        // ChatRequest pydantic model reads it from there, not from
        // the metadata bag.
        session_key: effectiveSessionKey,
        ...((args.agentId || args.personaId) && {
          metadata: {
            ...(args.agentId ? { agent_id: args.agentId } : {}),
            ...(args.personaId ? { persona_id: args.personaId } : {}),
          },
        }),
        ...(args.reasoningEffort
          ? { reasoning_effort: args.reasoningEffort }
          : {}),
        ...(args.tools ? { tools: args.tools } : {}),
      };

      abortRef.current = new AbortController();
      fetchActiveRef.current = true;
      let finishReceived = false;

      try {
        for await (const chunk of streamChatCompletions(
          reqBody,
          abortRef.current.signal,
        )) {
          const turnId = chunk.corlinman?.turn_id ?? draft.turnId ?? draft.id;
          if (!draft.turnId && chunk.corlinman?.turn_id) {
            draft.turnId = chunk.corlinman.turn_id;
          }
          for (const ev of chunkToChatEvents(chunk, turnId)) {
            if (ev.kind === "turn-complete") finishReceived = true;
            reduceEvent(ev);
          }
        }
      } catch (err) {
        // OpenAI-compat servers don't always close the SSE cleanly
        // after `data: [DONE]` — the reader may then throw a "network
        // error" / "TypeError" even though the turn finished
        // successfully. If we already observed a finish_reason chunk,
        // swallow the error rather than surface a misleading toast.
        if (finishReceived) {
          // intentional no-op
        } else if ((err as DOMException)?.name === "AbortError") {
          reduceEvent({
            kind: "turn-errored",
            turnId: draft.turnId ?? draft.id,
            sequence: -1,
            error: "cancelled",
          });
        } else if (err instanceof CorlinmanApiError && err.status === 401) {
          // Session expired mid-conversation. The sentinel gets a
          // dedicated bubble rendering with a re-login affordance —
          // the raw 401 body was meaningless to users.
          reduceEvent({
            kind: "turn-errored",
            turnId: draft.turnId ?? draft.id,
            sequence: -1,
            error: "session_expired",
          });
        } else {
          reduceEvent({
            kind: "turn-errored",
            turnId: draft.turnId ?? draft.id,
            sequence: -1,
            error:
              err instanceof Error ? err.message : "stream failed",
          });
        }
      } finally {
        // A superseding turn (or hydrate) owns the shared state now —
        // this dying turn must not flip isStreaming, commit its draft
        // over the new pending message, or null the new AbortController.
        const superseded = activeDraftRef.current !== draft;
        if (!superseded) fetchActiveRef.current = false;
        if (!superseded) {
          setIsStreaming(false);
          abortRef.current = null;
          activeDraftRef.current = null;
          // Commit the pending message into history.
          setPendingMessage((current) => {
            if (current) {
              setMessages((prev) => [...prev, { ...current, pending: false }]);
            }
            return null;
          });
          pendingRef.current = null;
          // Refresh the sidebar conversation list so the just-finished
          // turn is reflected immediately (refetchInterval would catch
          // it eventually; this makes the UX feel live).
          void qc.invalidateQueries({ queryKey: ["chat", "sessions"] });
          // Live stream stays open briefly so trailing events can land,
          // then close on the next tick (timer tracked for unmount).
          const close = closeLiveRef.current;
          closeLiveRef.current = null;
          if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
          closeTimerRef.current = setTimeout(() => close?.(), 500);
        }
      }
    },
    [args, messages, qc, reduceEvent, retirePending],
  );

  const sendMessage = React.useCallback(
    async (text: string, attachments?: ChatAttachment[]) => {
      const userMsg: ChatMessage = {
        id: genId(),
        role: "user",
        content: text,
        createdAt: Date.now(),
        attachments,
      };
      setMessages((prev) => [...prev, userMsg]);
      lastUserMessageRef.current = { text, attachments };
      await runTurn(userMsg);
    },
    [runTurn],
  );

  const retryLast = React.useCallback(async () => {
    const last = lastUserMessageRef.current;
    if (!last) return;
    // Drop the previous failed assistant turn if it exists.
    setMessages((prev) => {
      if (prev.length === 0) return prev;
      const tail = prev[prev.length - 1];
      if (tail.role === "assistant" && tail.error) return prev.slice(0, -1);
      return prev;
    });
    const userMsg: ChatMessage = {
      id: genId(),
      role: "user",
      content: last.text,
      createdAt: Date.now(),
      attachments: last.attachments,
    };
    await runTurn(userMsg);
  }, [runTurn]);

  const stop = React.useCallback(async () => {
    // Optimistic: flag "stopping" the instant the user clicks so the
    // click visibly took; the journal `Cancelling` event (and finally
    // `TurnErrored`) confirm it server-side.
    setPendingMessage((prev) => {
      if (!prev) return prev;
      const next = { ...prev, cancelling: true };
      pendingRef.current = next;
      return next;
    });
    abortRef.current?.abort();
    try {
      await cancelChatSession(args.sessionKey);
    } catch {
      // best-effort
    }
  }, [args.sessionKey]);

  const editAndRerun = React.useCallback(
    async (messageId: string, newContent: string) => {
      // Locate the message; everything from it onward gets dropped. We
      // compute the truncated history synchronously from the current
      // `messages` rather than reading a value captured inside a
      // `setMessages` updater — that updater runs lazily (and may be
      // batched), so its captured value is not reliably available here,
      // and `runTurn`'s closure over `messages` would otherwise still
      // hold the PRE-truncation history.
      const idx = messages.findIndex((m) => m.id === messageId);
      if (idx < 0) return;
      const editedUser: ChatMessage = {
        ...messages[idx],
        content: newContent,
        createdAt: Date.now(),
      };
      // History the rerun request must be built from: everything strictly
      // before the edited message. The edited user message itself is
      // appended by `runTurn`, so it is intentionally excluded here.
      const truncatedHistory = messages.slice(0, idx);
      // Reflect the truncation in the UI: drop the edited message and
      // everything after it, then re-add the edited user message.
      setMessages([...truncatedHistory, editedUser]);
      lastUserMessageRef.current = {
        text: newContent,
        attachments: editedUser.attachments,
      };
      // Pass the truncated history explicitly so the request body is built
      // from it, not from the stale closure's `messages`.
      await runTurn(editedUser, truncatedHistory);
    },
    [messages, runTurn],
  );

  const sliceUntil = React.useCallback(
    (messageId: string): ChatMessage[] => {
      const idx = messages.findIndex((m) => m.id === messageId);
      if (idx < 0) return messages;
      return messages.slice(0, idx + 1);
    },
    [messages],
  );

  const resumeInFlight = React.useCallback(async () => {
    const key = (args.sessionKey ?? "").trim();
    if (!key) return;
    // Something already owns the pending bubble — nothing to reattach.
    if (fetchActiveRef.current || pendingRef.current || journalTurnRef.current)
      return;

    const turns = await listSessionTurns(key, 1);
    const latest = turns[0];
    if (!latest || latest.status !== "in_progress") return;
    const turnId = latest.turn_id;
    if (retiredTurnIdsRef.current.has(turnId)) return;
    // Re-check after the await: a runTurn / second resume may have won.
    if (fetchActiveRef.current || pendingRef.current || journalTurnRef.current)
      return;

    journalTurnRef.current = turnId;
    const draft: ChatMessage = {
      id: `resume_${turnId}`,
      turnId,
      role: "assistant",
      content: "",
      createdAt: Date.now(),
      pending: true,
    };
    activeDraftRef.current = draft;
    pendingRef.current = draft;
    setPendingMessage(draft);
    setIsStreaming(true);
    dedupRef.current = new EventDedupSet();

    // Backlog first (JSON replay returns sequence 0 onward — the live
    // SSE catch-up can't deliver sequence 0), then tail the live stream
    // from exactly where the backlog ended. The (turn, sequence) dedup
    // absorbs any overlap.
    const backlog = await fetchTurnEvents(key, turnId);
    let lastSeq = -1;
    for (const env of backlog) {
      if (env.sequence > lastSeq) lastSeq = env.sequence;
      const chatEvent = liveEventToChatEvent(env as LiveEvent);
      if (chatEvent) reduceEvent(chatEvent);
    }
    // A terminal event in the backlog already finalized the turn.
    if (journalTurnRef.current !== turnId) return;

    closeLiveRef.current?.();
    closeLiveRef.current = openLiveEventStream(key, {
      // Always the composite id — `turn:-1` when the backlog was empty.
      // A bare fresh stream (no id) gets live-only semantics server-side
      // (the poll cursor seeds at the latest sequence); naming the turn
      // keeps full delivery for the in-flight turn we are reattaching to.
      initialLastEventId: `${turnId}:${lastSeq}`,
      onEvent: (live) => {
        const chatEvent = liveEventToChatEvent(live);
        if (chatEvent) reduceEvent(chatEvent);
      },
    });

    // Safety net: an agent process that journals no events (or a stalled
    // tail) would otherwise leave the bubble pending forever. Poll the
    // turn status; on a terminal flip, finalize — the transcript refetch
    // inside finalize replaces the partial bubble with journal truth.
    statusPollRef.current = setInterval(() => {
      if (journalTurnRef.current !== turnId) return;
      void listSessionTurns(key, 1).then((ts) => {
        if (journalTurnRef.current !== turnId) return;
        const t = ts.find((x) => x.turn_id === turnId);
        if (t && t.status !== "in_progress") finalizeJournalTurn();
      });
    }, 5_000);
  }, [args.sessionKey, reduceEvent, finalizeJournalTurn]);

  const totals = React.useMemo(() => {
    let inputTokens = 0;
    let outputTokens = 0;
    let costUsd = 0;
    for (const m of messages) {
      if (m.role !== "assistant" || !m.usage) continue;
      inputTokens += m.usage.inputTokens ?? 0;
      outputTokens += m.usage.outputTokens ?? 0;
      costUsd += m.usage.estimatedCostUsd ?? 0;
    }
    return { inputTokens, outputTokens, costUsd };
  }, [messages]);

  const approve = React.useCallback(
    async (
      turnId: string,
      callId: string,
      decision: ApprovalDecision,
      scope: ApprovalScope = "once",
    ) => {
      try {
        await submitApproval(turnId, {
          approved: decision === "approved",
          scope,
        });
      } catch (err) {
        // Leave the prompt open so the user can retry the decision —
        // collapsing it on a failed POST silently dropped the approval.
        console.warn("chat approval submit failed", err);
        return;
      }
      // Reflect locally so the prompt collapses.
      setPendingMessage((prev) => {
        if (!prev) return prev;
        if (!prev.approvals) return prev;
        return {
          ...prev,
          approvals: prev.approvals.map((a) =>
            a.callId === callId
              ? { ...a, decision, decidedScope: scope }
              : a,
          ),
        };
      });
    },
    [],
  );

  // Session switch: the hook instance survives a `sessionKey` change, but
  // any in-flight turn belongs to the *old* session — abort it and close
  // its live stream so it can't reduce into the new session's thread.
  // (`hydrate` does this too; this is the belt-and-braces for callers
  // that change the key without hydrating.)
  React.useEffect(
    () => () => {
      retirePending();
      abortRef.current?.abort();
      closeLiveRef.current?.();
      closeLiveRef.current = null;
      fetchActiveRef.current = false;
      journalTurnRef.current = null;
      if (statusPollRef.current) {
        clearInterval(statusPollRef.current);
        statusPollRef.current = null;
      }
    },
    [args.sessionKey, retirePending],
  );

  React.useEffect(
    () => () => {
      abortRef.current?.abort();
      closeLiveRef.current?.();
      if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
      if (statusPollRef.current) clearInterval(statusPollRef.current);
    },
    [],
  );

  return {
    messages,
    pendingMessage,
    isStreaming,
    sendMessage,
    retryLast,
    stop,
    hydrate,
    approve,
    editAndRerun,
    sliceUntil,
    resumeInFlight,
    totals,
  };
}
