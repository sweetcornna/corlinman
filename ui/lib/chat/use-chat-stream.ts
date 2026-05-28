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

import {
  cancelChatSession,
  streamChatCompletions,
  submitApproval,
  type ChatCompletionMessage,
  type ChatCompletionRequest,
  type ChatCompletionToolDef,
} from "@/lib/api/chat";
import { openLiveEventStream } from "@/lib/sessions/event-stream";
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
    case "turn-complete":
      draft.usage = { ...(draft.usage ?? {}), ...ev.usage };
      draft.pending = false;
      break;
    case "turn-errored":
      draft.error = ev.error;
      draft.pending = false;
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
  const dedupRef = React.useRef<EventDedupSet>(new EventDedupSet());
  const lastUserMessageRef = React.useRef<{
    text: string;
    attachments?: ChatAttachment[];
  } | null>(null);

  const hydrate = React.useCallback((history: ChatMessage[]) => {
    setMessages(history);
    setPendingMessage(null);
    setIsStreaming(false);
    dedupRef.current.reset();
  }, []);

  const reduceEvent = React.useCallback((ev: ChatEvent) => {
    if (!dedupRef.current.shouldEmit(ev.turnId, ev.sequence)) return;
    setPendingMessage((prev) => {
      if (!prev) return prev;
      const next: ChatMessage = {
        ...prev,
        toolCalls: prev.toolCalls ? prev.toolCalls.map((t) => ({ ...t })) : undefined,
        subagents: prev.subagents ? prev.subagents.map((s) => ({ ...s, events: s.events ? [...s.events] : [] })) : undefined,
        approvals: prev.approvals ? prev.approvals.map((a) => ({ ...a })) : undefined,
        usage: prev.usage ? { ...prev.usage } : undefined,
      };
      applyEvent(next, ev);
      return next;
    });
  }, []);

  /** Run one turn end-to-end: open both streams, merge, settle. */
  const runTurn = React.useCallback(
    async (userMsg: ChatMessage) => {
      // Open the assistant draft.
      const draft: ChatMessage = {
        id: genId(),
        role: "assistant",
        content: "",
        createdAt: Date.now(),
        pending: true,
      };
      setPendingMessage(draft);
      setIsStreaming(true);
      dedupRef.current = new EventDedupSet();

      // Subscribe to journal events. We don't await here — events arrive
      // asynchronously alongside the token stream.
      closeLiveRef.current = openLiveEventStream(args.sessionKey, {
        onEvent: (live) => {
          const chatEvent = liveEventToChatEvent(live);
          if (chatEvent) reduceEvent(chatEvent);
        },
      });

      // Build the OpenAI-compatible request body.
      const messagesPayload: ChatCompletionMessage[] = [];
      if (args.systemPrompt) {
        messagesPayload.push({ role: "system", content: args.systemPrompt });
      }
      for (const m of messages) {
        if (m.role === "user" || m.role === "assistant" || m.role === "system") {
          messagesPayload.push({ role: m.role, content: m.content });
        }
      }
      messagesPayload.push({ role: "user", content: userMsg.content });

      const reqBody: ChatCompletionRequest = {
        model: args.model,
        stream: true,
        messages: messagesPayload,
        metadata: {
          session_key: args.sessionKey,
          ...(args.agentId ? { agent_id: args.agentId } : {}),
          ...(args.personaId ? { persona_id: args.personaId } : {}),
        },
        ...(args.tools ? { tools: args.tools } : {}),
      };

      abortRef.current = new AbortController();

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
            reduceEvent(ev);
          }
        }
      } catch (err) {
        if ((err as DOMException)?.name === "AbortError") {
          reduceEvent({
            kind: "turn-errored",
            turnId: draft.turnId ?? draft.id,
            sequence: -1,
            error: "cancelled",
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
        setIsStreaming(false);
        abortRef.current = null;
        // Commit the pending message into history.
        setPendingMessage((current) => {
          if (current) {
            setMessages((prev) => [...prev, { ...current, pending: false }]);
          }
          return null;
        });
        // Live stream stays open briefly so trailing events can land, then
        // close on the next tick.
        const close = closeLiveRef.current;
        closeLiveRef.current = null;
        setTimeout(() => close?.(), 500);
      }
    },
    [args, messages, reduceEvent],
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
    abortRef.current?.abort();
    try {
      await cancelChatSession(args.sessionKey);
    } catch {
      // best-effort
    }
  }, [args.sessionKey]);

  const editAndRerun = React.useCallback(
    async (messageId: string, newContent: string) => {
      // Locate the message + every subsequent message gets dropped.
      let edited: ChatMessage | null = null;
      setMessages((prev) => {
        const idx = prev.findIndex((m) => m.id === messageId);
        if (idx < 0) {
          return prev;
        }
        const next: ChatMessage = {
          ...prev[idx],
          content: newContent,
          createdAt: Date.now(),
        };
        edited = next;
        return [...prev.slice(0, idx), next];
      });
      const editedUser = edited as ChatMessage | null;
      if (!editedUser) return;
      lastUserMessageRef.current = {
        text: newContent,
        attachments: editedUser.attachments,
      };
      // Wait a tick so the messages state is observed by runTurn closure.
      await Promise.resolve();
      await runTurn(editedUser);
    },
    [runTurn],
  );

  const sliceUntil = React.useCallback(
    (messageId: string): ChatMessage[] => {
      const idx = messages.findIndex((m) => m.id === messageId);
      if (idx < 0) return messages;
      return messages.slice(0, idx + 1);
    },
    [messages],
  );

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
      await submitApproval(turnId, {
        approved: decision === "approved",
        scope,
      });
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

  React.useEffect(
    () => () => {
      abortRef.current?.abort();
      closeLiveRef.current?.();
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
    totals,
  };
}
