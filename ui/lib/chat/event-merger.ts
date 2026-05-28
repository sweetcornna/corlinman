/**
 * Merge `/v1/chat/completions` token chunks + `/admin/sessions/.../events/live`
 * journal envelopes into a single ordered `ChatEvent` stream.
 *
 * Strategy:
 *
 *   - Journal `LiveEvent`s are authoritative. Their `(turn_id, sequence)`
 *     forms the key; we never emit two events with the same key.
 *   - Token chunks from `/v1/chat/completions` arrive faster but lack a
 *     `turn_id` until the gateway includes it via the `corlinman` extension.
 *     We emit them as synthetic `text-delta` events with `sequence: -1`
 *     placeholders so the UI renders text live. When the matching journal
 *     `TextDelta` lands later, we mark it as already-rendered to avoid
 *     double-emit.
 *   - Tool calls / sub-agents / approvals / reasoning ONLY arrive via
 *     journal events (token stream doesn't carry them).
 */

import type { LiveEvent } from "@/lib/sessions/event-stream";
import type { ChatCompletionChunk } from "@/lib/api/chat";
import type { ChatEvent } from "@/lib/chat/types";

/* ----------------------- token-stream conversion ----------------------- */

/** Convert one `/v1/chat/completions` chunk into 0-or-more `ChatEvent`s.
 *  These carry no turn_id until the gateway populates `chunk.corlinman`. */
export function chunkToChatEvents(
  chunk: ChatCompletionChunk,
  fallbackTurnId: string,
): ChatEvent[] {
  const events: ChatEvent[] = [];
  const turnId = chunk.corlinman?.turn_id ?? fallbackTurnId;
  for (const choice of chunk.choices ?? []) {
    const delta = choice.delta;
    if (delta.content) {
      events.push({
        kind: "text-delta",
        turnId,
        sequence: -1,
        text: delta.content,
      });
    }
    if (delta.reasoning_content) {
      events.push({
        kind: "reasoning-delta",
        turnId,
        sequence: -1,
        text: delta.reasoning_content,
      });
    }
    if (delta.tool_calls) {
      for (const tc of delta.tool_calls) {
        const callId = tc.id ?? `idx-${tc.index}`;
        const name = tc.function?.name;
        const argsDelta = tc.function?.arguments ?? "";
        // OpenAI streaming tool_calls deltas: the first chunk for a
        // given tool carries `function.name` + (usually empty) args; all
        // subsequent chunks carry only `function.arguments`. Without
        // capturing the name chunk the card stays "(pending)" forever.
        if (name) {
          events.push({
            kind: "tool-running",
            turnId,
            sequence: -1,
            callId,
            toolName: name,
            argsJson: argsDelta,
            startedAtMs: Date.now(),
          });
        } else if (argsDelta) {
          events.push({
            kind: "tool-input-delta",
            turnId,
            sequence: -1,
            callId,
            delta: argsDelta,
          });
        }
      }
    }
    if (choice.finish_reason) {
      // OpenAI-compat /v1/chat/completions doesn't emit per-tool
      // ToolStateCompleted via the journal SSE (only the hermes gRPC
      // path does). On stream end we surface a synthetic settle event
      // so any "running" tools stop spinning and adopt a stable visual
      // state.
      events.push({
        kind: "tools-settle",
        turnId,
        sequence: -1,
        finishReason: choice.finish_reason,
      });
      events.push({
        kind: "turn-complete",
        turnId,
        sequence: -1,
        usage: { finishReason: choice.finish_reason },
      });
    }
  }
  return events;
}

/* ----------------------- live-stream conversion ------------------------ */

interface LivePayloads {
  TurnStart: { model: string; user_text_preview?: string };
  TextDelta: { text: string; cumulative_len?: number; block_index?: number };
  ReasoningDelta: { text: string; block_index?: number };
  ToolInputDelta: { call_id: string; delta: string };
  ToolStateRunning: {
    call_id: string;
    tool_name: string;
    plugin_name?: string;
    args_json: string;
    started_at_ms: number;
  };
  ToolStateCompleted: {
    call_id: string;
    result_summary: string;
    duration_ms: number;
    is_error: boolean;
  };
  ToolStateHeartbeat: { call_id: string; elapsed_ms: number };
  SubagentSpawned: {
    child_session_key: string;
    child_agent_id?: string;
    depth: number;
    prompt_preview?: string;
  };
  SubagentEvent: { child_session_key: string; envelope: LiveEvent };
  SubagentCompleted: {
    child_session_key: string;
    finish_reason: string;
    tool_calls_made: number;
    elapsed_ms: number;
    summary?: string;
  };
  AwaitingApproval: {
    call_id: string;
    plugin: string;
    tool: string;
    args_preview_json: string;
    reason?: string;
  };
  TurnComplete: {
    finish_reason: string;
    usage?: {
      input_tokens?: number;
      output_tokens?: number;
      cached_input_tokens?: number;
      reasoning_tokens?: number;
    };
    estimated_cost_usd?: number;
    elapsed_ms?: number;
  };
  TurnErrored: { message: string };
  Cancelling: Record<string, never>;
  BlockStart: Record<string, never>;
  BlockStop: Record<string, never>;
}

type EventKind = keyof LivePayloads;

function payload<K extends EventKind>(
  ev: LiveEvent,
  _kind: K,
): LivePayloads[K] {
  return (ev.payload ?? {}) as LivePayloads[K];
}

/** Convert one journal LiveEvent into 0-or-1 ChatEvent. We intentionally
 *  drop a few low-signal types (BlockStart/Stop, Heartbeat) at this layer
 *  — they're observability noise the chat UI doesn't render. */
export function liveEventToChatEvent(ev: LiveEvent): ChatEvent | null {
  const baseTurnId = ev.turn_id;
  const baseSeq = ev.sequence;

  switch (ev.event_type) {
    case "TurnStart": {
      const p = payload(ev, "TurnStart");
      return {
        kind: "turn-start",
        turnId: baseTurnId,
        sequence: baseSeq,
        model: p.model,
        userTextPreview: p.user_text_preview,
      };
    }
    case "TextDelta": {
      const p = payload(ev, "TextDelta");
      return {
        kind: "text-delta",
        turnId: baseTurnId,
        sequence: baseSeq,
        text: p.text ?? "",
      };
    }
    case "ReasoningDelta": {
      const p = payload(ev, "ReasoningDelta");
      return {
        kind: "reasoning-delta",
        turnId: baseTurnId,
        sequence: baseSeq,
        text: p.text ?? "",
      };
    }
    case "ToolInputDelta": {
      const p = payload(ev, "ToolInputDelta");
      return {
        kind: "tool-input-delta",
        turnId: baseTurnId,
        sequence: baseSeq,
        callId: p.call_id,
        delta: p.delta ?? "",
      };
    }
    case "ToolStateRunning": {
      const p = payload(ev, "ToolStateRunning");
      return {
        kind: "tool-running",
        turnId: baseTurnId,
        sequence: baseSeq,
        callId: p.call_id,
        toolName: p.tool_name,
        pluginName: p.plugin_name,
        argsJson: p.args_json ?? "",
        startedAtMs: p.started_at_ms,
      };
    }
    case "ToolStateCompleted": {
      const p = payload(ev, "ToolStateCompleted");
      return {
        kind: "tool-completed",
        turnId: baseTurnId,
        sequence: baseSeq,
        callId: p.call_id,
        resultPreview: p.result_summary ?? "",
        durationMs: p.duration_ms ?? 0,
        isError: Boolean(p.is_error),
      };
    }
    case "SubagentSpawned": {
      const p = payload(ev, "SubagentSpawned");
      return {
        kind: "subagent-spawned",
        turnId: baseTurnId,
        sequence: baseSeq,
        childSessionKey: p.child_session_key,
        childAgentId: p.child_agent_id,
        depth: p.depth ?? 0,
        promptPreview: p.prompt_preview,
      };
    }
    case "SubagentEvent": {
      const p = payload(ev, "SubagentEvent");
      return {
        kind: "subagent-event",
        turnId: baseTurnId,
        sequence: baseSeq,
        childSessionKey: p.child_session_key,
        envelope: p.envelope,
      };
    }
    case "SubagentCompleted": {
      const p = payload(ev, "SubagentCompleted");
      return {
        kind: "subagent-completed",
        turnId: baseTurnId,
        sequence: baseSeq,
        childSessionKey: p.child_session_key,
        finishReason: p.finish_reason ?? "",
        toolCallsMade: p.tool_calls_made ?? 0,
        elapsedMs: p.elapsed_ms ?? 0,
        summary: p.summary,
      };
    }
    case "TurnComplete": {
      const p = payload(ev, "TurnComplete");
      return {
        kind: "turn-complete",
        turnId: baseTurnId,
        sequence: baseSeq,
        usage: {
          inputTokens: p.usage?.input_tokens,
          outputTokens: p.usage?.output_tokens,
          cachedInputTokens: p.usage?.cached_input_tokens,
          reasoningTokens: p.usage?.reasoning_tokens,
          estimatedCostUsd: p.estimated_cost_usd,
          finishReason: p.finish_reason,
          walltimeMs: p.elapsed_ms,
        },
      };
    }
    case "TurnErrored": {
      const p = payload(ev, "TurnErrored");
      return {
        kind: "turn-errored",
        turnId: baseTurnId,
        sequence: baseSeq,
        error: p.message ?? "errored",
      };
    }
    // AwaitingApproval is not yet in the LiveEventType union (backend
    // stub), but the merger handles it if/when added. Fall through.
    default:
      // BlockStart / BlockStop / Heartbeat / Cancelling — UI doesn't render.
      if (
        (ev.event_type as string) === "AwaitingApproval"
      ) {
        const p = (ev.payload ?? {}) as LivePayloads["AwaitingApproval"];
        return {
          kind: "awaiting-approval",
          turnId: baseTurnId,
          sequence: baseSeq,
          callId: p.call_id,
          plugin: p.plugin,
          tool: p.tool,
          argsPreviewJson: p.args_preview_json ?? "",
          reason: p.reason,
        };
      }
      return null;
  }
}

/* --------------------------- de-dup tracker ---------------------------- */

/** Tracks `(turn_id, sequence)` keys we've already emitted. Synthetic token
 *  events use `sequence === -1` so they never collide with journal events. */
export class EventDedupSet {
  private readonly seen = new Set<string>();

  shouldEmit(turnId: string, sequence: number): boolean {
    if (sequence < 0) return true;
    const key = `${turnId}:${sequence}`;
    if (this.seen.has(key)) return false;
    this.seen.add(key);
    return true;
  }

  reset(): void {
    this.seen.clear();
  }
}
