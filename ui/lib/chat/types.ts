/**
 * Unified chat types for the in-app /chat surface.
 *
 * The chat window merges two live streams into a single ordered timeline:
 *
 *   1. `/v1/chat/completions` — OpenAI-compatible token deltas (fast path
 *      so the user sees text the moment the model emits it).
 *   2. `/admin/sessions/{key}/events/live` — typed `LiveEvent`s from the
 *      hermes journal (authoritative source for tool calls, sub-agents,
 *      reasoning blocks, approvals, finish reasons).
 *
 * `event-merger.ts` collapses both streams into the `ChatEvent` discriminated
 * union below, deduplicating on `(turn_id, sequence)` from the journal and
 * promoting the token stream into `TextDelta` events when no journal event
 * has arrived yet.
 */

import type { LiveEvent, LiveEventType } from "@/lib/sessions/event-stream";

export type ChatRole = "user" | "assistant" | "system" | "tool";

/** A single message bubble rendered in the thread. */
export interface ChatMessage {
  /** Local id; for assistant messages, swapped to the gateway `turn_id`
   *  once the first journal event arrives. */
  id: string;
  /** Server-side turn id, set as soon as we know it. Stable across reconnects. */
  turnId?: string;
  role: ChatRole;
  /** Rendered markdown text (assistant) or raw user text. */
  content: string;
  /** ms epoch. */
  createdAt: number;
  /** Live while the assistant is still streaming. */
  pending?: boolean;
  /** True between the user pressing Stop and the backend confirming the
   *  cancellation (journal `Cancelling` event / final `TurnErrored`).
   *  Renders as a "stopping…" indicator so the Stop click visibly took. */
  cancelling?: boolean;
  /** Populated when the stream produces an error envelope. The sentinel
   *  values `"cancelled"` (user-initiated stop) and `"session_expired"`
   *  (401 mid-conversation) get dedicated renderings in the bubble. */
  error?: string;
  /** Reasoning blocks (Claude extended thinking). Rendered above content
   *  in a collapsible block. */
  reasoning?: string;
  /** Tool calls and their states, keyed by `call_id`. */
  toolCalls?: ToolCallState[];
  /** Sub-agent spawn cards, in order of spawn. */
  subagents?: SubagentCardState[];
  /** Pending approval requests inline in this message. */
  approvals?: ApprovalPromptState[];
  /** Attachments authored by the user. Server resolves them to provider
   *  inputs before the model sees them. */
  attachments?: ChatAttachment[];
  /** Token / cost accounting from the `TurnComplete` event. */
  usage?: ChatUsage;
}

export interface ChatUsage {
  inputTokens?: number;
  outputTokens?: number;
  cachedInputTokens?: number;
  reasoningTokens?: number;
  estimatedCostUsd?: number;
  finishReason?: string;
  walltimeMs?: number;
}

export type ToolCallStatus = "running" | "ok" | "error" | "cancelled" | "settled";

export interface ToolCallState {
  callId: string;
  toolName: string;
  pluginName?: string;
  /** JSON string (may be partial while streaming `ToolInputDelta`s). */
  argsJson: string;
  /** Final result (string or stringified JSON), if completed. */
  resultPreview?: string;
  status: ToolCallStatus;
  /** ms epoch. */
  startedAt?: number;
  /** ms duration once completed. */
  durationMs?: number;
  /** True when the user collapsed the card. UI state, not persisted. */
  collapsed?: boolean;
}

export interface SubagentCardState {
  childSessionKey: string;
  childAgentId?: string;
  depth: number;
  promptPreview?: string;
  status: "spawned" | "running" | "completed" | "errored";
  finishReason?: string;
  toolCallsMade?: number;
  elapsedMs?: number;
  summary?: string;
  /** Optional nested event log; UI may collapse by default. */
  events?: LiveEvent[];
}

export type ApprovalDecision = "approved" | "denied";

export type ApprovalScope = "once" | "session" | "always";

export interface ApprovalPromptState {
  callId: string;
  plugin: string;
  tool: string;
  argsPreviewJson: string;
  reason?: string;
  /** Decision the user has already taken (in this UI session). */
  decision?: ApprovalDecision;
  decidedScope?: ApprovalScope;
}

/** Attachments authored by the user side. */
export interface ChatAttachment {
  id: string;
  kind: "image" | "audio" | "video" | "document";
  name: string;
  mime?: string;
  sizeBytes: number;
  /** Local object URL for preview while uploading. */
  previewUrl?: string;
  /** Remote URL once uploaded; populated by `uploadAttachment`. */
  remoteUrl?: string;
  /** Server-side file id from `POST /v1/files`; set alongside
   *  `remoteUrl` once the upload completes. */
  fileId?: string;
  uploading?: boolean;
  /** Upload progress 0..1 while `uploading` (when the transport
   *  reports it). Drives the determinate progress bar; absent uploads
   *  fall back to an indeterminate shimmer. */
  progress?: number;
  error?: string;
}

/** What the chat sidebar renders per row. */
export interface ChatConversation {
  sessionKey: string;
  title: string | null;
  pinned: boolean;
  archived: boolean;
  lastMessageAt: number;
  messageCount: number;
}

/** Discriminated event union emitted by `event-merger.ts`. UI subscribes to
 *  this and dispatches to per-message reducers. */
export type ChatEvent =
  | { kind: "text-delta"; turnId: string; sequence: number; text: string }
  | {
      kind: "reasoning-delta";
      turnId: string;
      sequence: number;
      text: string;
    }
  | {
      kind: "tool-input-delta";
      turnId: string;
      sequence: number;
      callId: string;
      delta: string;
    }
  | {
      kind: "tool-running";
      turnId: string;
      sequence: number;
      callId: string;
      toolName: string;
      pluginName?: string;
      argsJson: string;
      startedAtMs: number;
    }
  | {
      kind: "tool-completed";
      turnId: string;
      sequence: number;
      callId: string;
      resultPreview: string;
      durationMs: number;
      isError: boolean;
    }
  | {
      kind: "subagent-spawned";
      turnId: string;
      sequence: number;
      childSessionKey: string;
      childAgentId?: string;
      depth: number;
      promptPreview?: string;
    }
  | {
      kind: "subagent-event";
      turnId: string;
      sequence: number;
      childSessionKey: string;
      envelope: LiveEvent;
    }
  | {
      kind: "subagent-completed";
      turnId: string;
      sequence: number;
      childSessionKey: string;
      finishReason: string;
      toolCallsMade: number;
      elapsedMs: number;
      summary?: string;
    }
  | {
      kind: "awaiting-approval";
      turnId: string;
      sequence: number;
      callId: string;
      plugin: string;
      tool: string;
      argsPreviewJson: string;
      reason?: string;
    }
  | {
      kind: "turn-start";
      turnId: string;
      sequence: number;
      model: string;
      userTextPreview?: string;
    }
  | {
      kind: "turn-complete";
      turnId: string;
      sequence: number;
      usage: ChatUsage;
    }
  | {
      kind: "turn-errored";
      turnId: string;
      sequence: number;
      error: string;
    }
  | {
      // Backend acknowledged a cancel request and is unwinding the turn.
      // Rendered as a "stopping…" state on the pending bubble so the
      // user's Stop click has visible effect before TurnErrored lands.
      kind: "cancelling";
      turnId: string;
      sequence: number;
    }
  | {
      // Emitted on stream end. Any "running" tools should adopt a
      // stable terminal state (no more spinner). Used for the OpenAI-
      // compat path where /v1/chat/completions doesn't carry the
      // journal ToolStateCompleted that hermes' gRPC path emits.
      kind: "tools-settle";
      turnId: string;
      sequence: number;
      finishReason: string;
    };

/** Re-export for ergonomic imports. */
export type { LiveEvent, LiveEventType };
