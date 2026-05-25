/**
 * Session timeline store (reducer + React context).
 *
 * Holds per-turn state assembled from the live SSE stream. We deliberately
 * use `useReducer` + `useContext` here instead of Zustand to keep the
 * dependency footprint flat (Wave 2 doesn't need cross-component selectors).
 *
 * Streaming text/reasoning deltas can arrive at 100+ Hz; calling
 * `dispatch(applyDelta)` on every byte trashes React. So `useLiveTimeline`
 * (in `event-timeline.tsx`) accumulates incoming events into a small queue
 * and flushes via `requestAnimationFrame`. This file exposes the reducer
 * and the public action types only — the rAF batching lives at the
 * consumer.
 */

import * as React from "react";
import type { LiveEvent, LiveEventType } from "./event-stream";

/* -------------------------------------------------------------- */
/*                            Part union                          */
/* -------------------------------------------------------------- */

export interface TextPart {
  kind: "text";
  block_id: string;
  text: string;
  done: boolean;
}

export interface ReasoningPart {
  kind: "reasoning";
  block_id: string;
  text: string;
  done: boolean;
}

export type ToolPartState =
  | { kind: "pending" }
  | { kind: "running"; startedAt: number; lastHeartbeatAt?: number; statusText?: string }
  | { kind: "completed"; startedAt: number; completedAt: number; isError: boolean; output?: string }
  | { kind: "error"; startedAt: number; completedAt: number; message: string };

export interface ToolPart {
  kind: "tool_use";
  block_id: string;
  tool_name: string;
  /** Accumulated tool args (streamed as JSON-ish text deltas). */
  input_json: string;
  state: ToolPartState;
  /**
   * W3.2 — subagent sessions spawned by this tool call. Most tool calls
   * will have zero; ``subagent.spawn`` / ``subagent.spawn_many`` push
   * one entry per child here. The frontend renders these as a nested
   * ``<SubagentTree>`` under the widget's expanded body.
   */
  subagentSessions?: SubagentSession[];
}

/**
 * W3.2 — one nested subagent run under a parent tool call.
 *
 * The reducer creates this on a ``SubagentSpawned`` envelope and pushes
 * the child's bubbled events (``SubagentEvent.envelope``) into
 * ``parts`` via the same reducer logic the parent uses, so the nested
 * timeline supports text / reasoning / tool widgets identically.
 *
 * ``depth`` is bounded by the supervisor (W3.2 risk mitigation caps at
 * 3); depths above that render as a single collapsed placeholder
 * instead of recursing further.
 */
export interface SubagentSession {
  kind: "subagent_session";
  child_session_key: string;
  child_agent_id: string;
  prompt_preview: string;
  depth: number;
  status: "running" | "complete" | "errored";
  finish_reason?: string;
  tool_calls_made?: number;
  elapsed_ms?: number;
  summary?: string;
  parts: Part[];
}

export type Part = TextPart | ReasoningPart | ToolPart;

/**
 * W3.2 — subagent trees deeper than this are rendered as a collapsed
 * "(deeper subagent)" placeholder so the UI doesn't have to recurse
 * unbounded. Matches the plan's §5 risk mitigation.
 */
export const SUBAGENT_MAX_RENDER_DEPTH = 3;

/* -------------------------------------------------------------- */
/*                          Turn shape                            */
/* -------------------------------------------------------------- */

export type TurnStatus = "streaming" | "complete" | "errored" | "cancelling";

export interface TurnUsage {
  input_tokens?: number;
  output_tokens?: number;
}

export interface Turn {
  turn_id: string;
  status: TurnStatus;
  parts: Part[];
  startedAt: number;
  endedAt?: number;
  usage?: TurnUsage;
  costUsd?: number;
  /** Highest sequence we have ingested for this turn (for replay-after). */
  highestSequence: number;
  /** Optional last error message when status === "errored". */
  errorMessage?: string;
}

export interface TimelineState {
  /** Insertion-ordered list of turn ids. */
  turnOrder: string[];
  turns: Record<string, Turn>;
}

export const initialTimelineState: TimelineState = {
  turnOrder: [],
  turns: {},
};

/* -------------------------------------------------------------- */
/*                            Actions                             */
/* -------------------------------------------------------------- */

export type TimelineAction =
  | { type: "events"; events: LiveEvent[] }
  | { type: "reset" };

/* -------------------------------------------------------------- */
/*                            Reducer                             */
/* -------------------------------------------------------------- */

function ensureTurn(state: TimelineState, turn_id: string, timestamp_ms: number): TimelineState {
  if (state.turns[turn_id]) return state;
  return {
    turnOrder: [...state.turnOrder, turn_id],
    turns: {
      ...state.turns,
      [turn_id]: {
        turn_id,
        status: "streaming",
        parts: [],
        startedAt: timestamp_ms,
        highestSequence: 0,
      },
    },
  };
}

function updateTurn(
  state: TimelineState,
  turn_id: string,
  mutate: (t: Turn) => Turn,
): TimelineState {
  const t = state.turns[turn_id];
  if (!t) return state;
  return {
    ...state,
    turns: { ...state.turns, [turn_id]: mutate(t) },
  };
}

function replacePart(turn: Turn, block_id: string, replacer: (p: Part | undefined) => Part): Turn {
  const idx = turn.parts.findIndex((p) => p.block_id === block_id);
  const existing = idx >= 0 ? turn.parts[idx] : undefined;
  const next = replacer(existing);
  const parts =
    idx >= 0
      ? turn.parts.map((p, i) => (i === idx ? next : p))
      : [...turn.parts, next];
  return { ...turn, parts };
}

interface BlockStartPayload {
  block_id: string;
  block_kind: "text" | "reasoning" | "tool_use";
  tool_name?: string;
}

interface TextDeltaPayload {
  block_id: string;
  delta: string;
}

interface ToolInputDeltaPayload {
  block_id: string;
  delta: string;
}

interface BlockStopPayload {
  block_id: string;
}

interface ToolStateRunningPayload {
  block_id: string;
  status_text?: string;
}

interface ToolStateHeartbeatPayload {
  block_id: string;
  status_text?: string;
}

interface ToolStateCompletedPayload {
  block_id: string;
  is_error?: boolean;
  output?: string;
  error_message?: string;
}

interface TurnCompletePayload {
  usage?: TurnUsage;
  cost_usd?: number;
}

interface TurnErroredPayload {
  error_message?: string;
}

/** W3.2 — payload shape for ``SubagentSpawned`` envelopes. */
interface SubagentSpawnedPayload {
  parent_session_key: string;
  child_session_key: string;
  child_agent_id: string;
  depth: number;
  prompt_preview: string;
  /** Optional — the dispatch can correlate a spawned child to the
   *  tool call that produced it via the parent's ``tool_call_id``.
   *  Backend doesn't currently set this; we keep the field so a
   *  future emit can route deterministically rather than by inference. */
  tool_call_id?: string;
}

/** W3.2 — payload shape for ``SubagentEvent`` envelopes (the wrapped
 *  child envelope). */
interface SubagentEventPayload {
  child_session_key: string;
  envelope: LiveEvent;
}

/** W3.2 — payload shape for ``SubagentCompleted`` envelopes. */
interface SubagentCompletedPayload {
  child_session_key: string;
  finish_reason: string;
  tool_calls_made: number;
  elapsed_ms: number;
  summary: string;
}

function applyEvent(state: TimelineState, ev: LiveEvent): TimelineState {
  const { turn_id, sequence, timestamp_ms, event_type, payload } = ev;
  // Drop stale/duplicate events
  const existing = state.turns[turn_id];
  if (existing && sequence <= existing.highestSequence && event_type !== "TurnStart") {
    return state;
  }

  const next = ensureTurn(state, turn_id, timestamp_ms);

  const bump = (t: Turn): Turn => ({
    ...t,
    highestSequence: Math.max(t.highestSequence, sequence),
  });

  switch (event_type as LiveEventType) {
    case "TurnStart":
      return updateTurn(next, turn_id, (t) =>
        bump({ ...t, status: "streaming", startedAt: timestamp_ms }),
      );

    case "BlockStart": {
      const p = payload as BlockStartPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            if (existingPart) return existingPart;
            if (p.block_kind === "text") {
              return { kind: "text", block_id: p.block_id, text: "", done: false };
            }
            if (p.block_kind === "reasoning") {
              return { kind: "reasoning", block_id: p.block_id, text: "", done: false };
            }
            return {
              kind: "tool_use",
              block_id: p.block_id,
              tool_name: p.tool_name ?? "tool",
              input_json: "",
              state: { kind: "pending" },
            };
          }),
        ),
      );
    }

    case "TextDelta": {
      const p = payload as TextDeltaPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            const base: TextPart =
              existingPart && existingPart.kind === "text"
                ? existingPart
                : { kind: "text", block_id: p.block_id, text: "", done: false };
            return { ...base, text: base.text + (p.delta ?? "") };
          }),
        ),
      );
    }

    case "ReasoningDelta": {
      const p = payload as TextDeltaPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            const base: ReasoningPart =
              existingPart && existingPart.kind === "reasoning"
                ? existingPart
                : { kind: "reasoning", block_id: p.block_id, text: "", done: false };
            return { ...base, text: base.text + (p.delta ?? "") };
          }),
        ),
      );
    }

    case "ToolInputDelta": {
      const p = payload as ToolInputDeltaPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            const base: ToolPart =
              existingPart && existingPart.kind === "tool_use"
                ? existingPart
                : {
                    kind: "tool_use",
                    block_id: p.block_id,
                    tool_name: "tool",
                    input_json: "",
                    state: { kind: "pending" },
                  };
            return { ...base, input_json: base.input_json + (p.delta ?? "") };
          }),
        ),
      );
    }

    case "BlockStop": {
      const p = payload as BlockStopPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            if (!existingPart) {
              return { kind: "text", block_id: p.block_id, text: "", done: true };
            }
            if (existingPart.kind === "text" || existingPart.kind === "reasoning") {
              return { ...existingPart, done: true };
            }
            return existingPart;
          }),
        ),
      );
    }

    case "ToolStateRunning": {
      const p = payload as ToolStateRunningPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            if (!existingPart || existingPart.kind !== "tool_use") {
              return (
                existingPart ?? {
                  kind: "tool_use",
                  block_id: p.block_id,
                  tool_name: "tool",
                  input_json: "",
                  state: { kind: "running", startedAt: timestamp_ms, statusText: p.status_text },
                }
              );
            }
            return {
              ...existingPart,
              state: { kind: "running", startedAt: timestamp_ms, statusText: p.status_text },
            };
          }),
        ),
      );
    }

    case "ToolStateHeartbeat": {
      const p = payload as ToolStateHeartbeatPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            if (!existingPart || existingPart.kind !== "tool_use") return existingPart!;
            const prev =
              existingPart.state.kind === "running"
                ? existingPart.state
                : { kind: "running" as const, startedAt: timestamp_ms };
            return {
              ...existingPart,
              state: {
                kind: "running",
                startedAt: prev.startedAt,
                lastHeartbeatAt: timestamp_ms,
                statusText: p.status_text ?? prev.statusText,
              },
            };
          }),
        ),
      );
    }

    case "ToolStateCompleted": {
      const p = payload as ToolStateCompletedPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(
          replacePart(t, p.block_id, (existingPart) => {
            if (!existingPart || existingPart.kind !== "tool_use") return existingPart!;
            const startedAt =
              existingPart.state.kind === "running" || existingPart.state.kind === "completed"
                ? existingPart.state.startedAt
                : timestamp_ms;
            if (p.is_error || p.error_message) {
              return {
                ...existingPart,
                state: {
                  kind: "error",
                  startedAt,
                  completedAt: timestamp_ms,
                  message: p.error_message ?? p.output ?? "tool error",
                },
              };
            }
            return {
              ...existingPart,
              state: {
                kind: "completed",
                startedAt,
                completedAt: timestamp_ms,
                isError: false,
                output: p.output,
              },
            };
          }),
        ),
      );
    }

    case "TurnComplete": {
      const p = (payload ?? {}) as TurnCompletePayload;
      return updateTurn(next, turn_id, (t) =>
        bump({
          ...t,
          status: "complete",
          endedAt: timestamp_ms,
          usage: p.usage ?? t.usage,
          costUsd: p.cost_usd ?? t.costUsd,
        }),
      );
    }

    case "TurnErrored": {
      const p = (payload ?? {}) as TurnErroredPayload;
      return updateTurn(next, turn_id, (t) =>
        bump({
          ...t,
          status: "errored",
          endedAt: timestamp_ms,
          errorMessage: p.error_message,
        }),
      );
    }

    case "Cancelling":
      return updateTurn(next, turn_id, (t) => bump({ ...t, status: "cancelling" }));

    case "SubagentSpawned": {
      const p = payload as SubagentSpawnedPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(attachSubagentSpawn(t, p, timestamp_ms)),
      );
    }

    case "SubagentEvent": {
      const p = payload as SubagentEventPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(applySubagentEvent(t, p, timestamp_ms)),
      );
    }

    case "SubagentCompleted": {
      const p = payload as SubagentCompletedPayload;
      return updateTurn(next, turn_id, (t) =>
        bump(applySubagentCompleted(t, p)),
      );
    }

    default:
      return next;
  }
}

/* -------------------------------------------------------------- */
/*                       Subagent helpers                         */
/* -------------------------------------------------------------- */

/**
 * Attach a freshly-spawned subagent session to a tool_use part.
 *
 * Routing strategy (W3.2 inference path): the backend doesn't tag
 * ``SubagentSpawned`` with the originating ``tool_call_id``, so we
 * find the most recent ``tool_use`` part on the turn that doesn't
 * already carry the same ``child_session_key`` (so re-attaches across
 * SSE catch-up replays stay idempotent). Falls back to appending a
 * synthetic detached part when no tool_use is present (rare — only
 * fires when ``SubagentSpawned`` lands before its preceding tool_use
 * BlockStart did, which violates ordering but we degrade gracefully).
 */
function attachSubagentSpawn(
  turn: Turn,
  payload: SubagentSpawnedPayload,
  _timestamp_ms: number,
): Turn {
  // Idempotent: if this child_session_key is already attached anywhere
  // on the turn (live replay), no-op.
  if (findSubagentSessionLocation(turn.parts, payload.child_session_key)) {
    return turn;
  }

  const newSession: SubagentSession = {
    kind: "subagent_session",
    child_session_key: payload.child_session_key,
    child_agent_id: payload.child_agent_id,
    prompt_preview: payload.prompt_preview,
    depth: payload.depth,
    status: "running",
    parts: [],
  };

  // Walk parts in reverse to find the most-recent tool_use that can
  // host this child. Avoid attaching to a tool that's already error /
  // completed if there's a more recent running one (preference: the
  // tool the spawn was emitted right after).
  for (let i = turn.parts.length - 1; i >= 0; i--) {
    const part = turn.parts[i];
    if (part.kind === "tool_use") {
      const sessions = part.subagentSessions ?? [];
      const nextParts = [...turn.parts];
      nextParts[i] = {
        ...part,
        subagentSessions: [...sessions, newSession],
      };
      return { ...turn, parts: nextParts };
    }
  }

  // No host found — best-effort fallback: stash the session on a
  // synthetic tool_use stub so the UI still renders something.
  const stub: ToolPart = {
    kind: "tool_use",
    block_id: `subagent_${payload.child_session_key}`,
    tool_name: "subagent.spawn",
    input_json: "",
    state: { kind: "running", startedAt: _timestamp_ms },
    subagentSessions: [newSession],
  };
  return { ...turn, parts: [...turn.parts, stub] };
}

/**
 * Apply a bubbled ``SubagentEvent`` to the right nested session.
 *
 * The wrapped inner envelope is fed through the same reducer scoped
 * to the sub-tree's ``parts`` array. We use a lightweight local
 * reducer (``applyChildEvent``) instead of recursing into
 * ``applyEvent`` so we don't allocate a fake Turn for the child.
 */
function applySubagentEvent(
  turn: Turn,
  payload: SubagentEventPayload,
  _timestamp_ms: number,
): Turn {
  const inner = payload.envelope;
  if (!inner) return turn;
  return mutateSubagentSession(turn, payload.child_session_key, (session) => ({
    ...session,
    parts: applyChildEvent(session.parts, inner, session.depth),
  }));
}

/**
 * Mark a subagent session ``complete`` and stamp the terminal fields.
 */
function applySubagentCompleted(
  turn: Turn,
  payload: SubagentCompletedPayload,
): Turn {
  return mutateSubagentSession(turn, payload.child_session_key, (session) => ({
    ...session,
    status: payload.finish_reason === "error" ? "errored" : "complete",
    finish_reason: payload.finish_reason,
    tool_calls_made: payload.tool_calls_made,
    elapsed_ms: payload.elapsed_ms,
    summary: payload.summary,
  }));
}

/**
 * Walk ``parts`` (including nested subagent sessions one level deep)
 * for a session matching ``childSessionKey``. Returns a small location
 * tuple the mutator helpers use to rebuild immutable copies.
 *
 * We deliberately bound the search to one level of nesting per call —
 * the recursive routing for grandchildren works because each level
 * dispatches its own ``SubagentEvent`` envelope through this routing
 * helper from the top level. So a depth-3 grandchild's events arrive
 * via two stacked SubagentEvent wrappings on the wire, and each level
 * peels one wrapper as it reaches the right session — see
 * ``applyChildEvent`` for the recursive case.
 */
type SessionLocation =
  | { kind: "direct"; toolIndex: number; sessionIndex: number };

function findSubagentSessionLocation(
  parts: Part[],
  childSessionKey: string,
): SessionLocation | undefined {
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (part.kind !== "tool_use") continue;
    const sessions = part.subagentSessions ?? [];
    for (let j = 0; j < sessions.length; j++) {
      if (sessions[j].child_session_key === childSessionKey) {
        return { kind: "direct", toolIndex: i, sessionIndex: j };
      }
    }
  }
  return undefined;
}

function mutateSubagentSession(
  turn: Turn,
  childSessionKey: string,
  mutate: (session: SubagentSession) => SubagentSession,
): Turn {
  const loc = findSubagentSessionLocation(turn.parts, childSessionKey);
  if (!loc) return turn;
  const tool = turn.parts[loc.toolIndex] as ToolPart;
  const sessions = tool.subagentSessions ?? [];
  const nextSessions = sessions.map((s, idx) =>
    idx === loc.sessionIndex ? mutate(s) : s,
  );
  const nextParts = [...turn.parts];
  nextParts[loc.toolIndex] = { ...tool, subagentSessions: nextSessions };
  return { ...turn, parts: nextParts };
}

/**
 * Apply one bubbled child envelope to a subagent session's ``parts``
 * array. Mirrors the top-level reducer's switch but operates on a
 * flat ``Part[]`` instead of a Turn. Handles nested SubagentEvent so
 * grandchildren bubble correctly.
 */
function applyChildEvent(
  parts: Part[],
  envelope: LiveEvent,
  sessionDepth: number,
): Part[] {
  const { event_type, payload, timestamp_ms } = envelope;

  switch (event_type) {
    case "BlockStart": {
      const p = payload as BlockStartPayload;
      return replacePartIn(parts, p.block_id, (existing) => {
        if (existing) return existing;
        if (p.block_kind === "text") {
          return { kind: "text", block_id: p.block_id, text: "", done: false };
        }
        if (p.block_kind === "reasoning") {
          return { kind: "reasoning", block_id: p.block_id, text: "", done: false };
        }
        return {
          kind: "tool_use",
          block_id: p.block_id,
          tool_name: p.tool_name ?? "tool",
          input_json: "",
          state: { kind: "pending" },
        };
      });
    }
    case "TextDelta": {
      const p = payload as TextDeltaPayload;
      return replacePartIn(parts, p.block_id, (existing) => {
        const base: TextPart =
          existing && existing.kind === "text"
            ? existing
            : { kind: "text", block_id: p.block_id, text: "", done: false };
        return { ...base, text: base.text + (p.delta ?? "") };
      });
    }
    case "ReasoningDelta": {
      const p = payload as TextDeltaPayload;
      return replacePartIn(parts, p.block_id, (existing) => {
        const base: ReasoningPart =
          existing && existing.kind === "reasoning"
            ? existing
            : { kind: "reasoning", block_id: p.block_id, text: "", done: false };
        return { ...base, text: base.text + (p.delta ?? "") };
      });
    }
    case "ToolInputDelta": {
      const p = payload as ToolInputDeltaPayload;
      return replacePartIn(parts, p.block_id, (existing) => {
        const base: ToolPart =
          existing && existing.kind === "tool_use"
            ? existing
            : {
                kind: "tool_use",
                block_id: p.block_id,
                tool_name: "tool",
                input_json: "",
                state: { kind: "pending" },
              };
        return { ...base, input_json: base.input_json + (p.delta ?? "") };
      });
    }
    case "BlockStop": {
      const p = payload as BlockStopPayload;
      return replacePartIn(parts, p.block_id, (existing) => {
        if (!existing) {
          return { kind: "text", block_id: p.block_id, text: "", done: true };
        }
        if (existing.kind === "text" || existing.kind === "reasoning") {
          return { ...existing, done: true };
        }
        return existing;
      });
    }
    case "ToolStateRunning": {
      const p = payload as ToolStateRunningPayload;
      return replacePartIn(parts, p.block_id, (existing) => {
        if (!existing || existing.kind !== "tool_use") {
          return (
            existing ?? {
              kind: "tool_use",
              block_id: p.block_id,
              tool_name: "tool",
              input_json: "",
              state: { kind: "running", startedAt: timestamp_ms, statusText: p.status_text },
            }
          );
        }
        return {
          ...existing,
          state: { kind: "running", startedAt: timestamp_ms, statusText: p.status_text },
        };
      });
    }
    case "ToolStateCompleted": {
      const p = payload as ToolStateCompletedPayload;
      return replacePartIn(parts, p.block_id, (existing) => {
        if (!existing || existing.kind !== "tool_use") return existing!;
        const startedAt =
          existing.state.kind === "running" || existing.state.kind === "completed"
            ? existing.state.startedAt
            : timestamp_ms;
        if (p.is_error || p.error_message) {
          return {
            ...existing,
            state: {
              kind: "error",
              startedAt,
              completedAt: timestamp_ms,
              message: p.error_message ?? p.output ?? "tool error",
            },
          };
        }
        return {
          ...existing,
          state: {
            kind: "completed",
            startedAt,
            completedAt: timestamp_ms,
            isError: false,
            output: p.output,
          },
        };
      });
    }
    case "SubagentSpawned": {
      // Grandchild — only attach if we're below the render-depth cap.
      // Above the cap the placeholder renderer in the tree shows a
      // collapsed "(deeper subagent)" line instead.
      if (sessionDepth + 1 >= SUBAGENT_MAX_RENDER_DEPTH) {
        return parts;
      }
      const p = payload as SubagentSpawnedPayload;
      const newSession: SubagentSession = {
        kind: "subagent_session",
        child_session_key: p.child_session_key,
        child_agent_id: p.child_agent_id,
        prompt_preview: p.prompt_preview,
        depth: p.depth,
        status: "running",
        parts: [],
      };
      // Attach under most-recent tool_use in this session, same rule
      // as the top-level reducer.
      for (let i = parts.length - 1; i >= 0; i--) {
        const part = parts[i];
        if (part.kind === "tool_use") {
          const sessions = part.subagentSessions ?? [];
          const nextParts = [...parts];
          nextParts[i] = {
            ...part,
            subagentSessions: [...sessions, newSession],
          };
          return nextParts;
        }
      }
      return parts;
    }
    case "SubagentEvent": {
      const p = payload as SubagentEventPayload;
      if (!p.envelope) return parts;
      // Locate and recurse one level deeper.
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i];
        if (part.kind !== "tool_use") continue;
        const sessions = part.subagentSessions ?? [];
        for (let j = 0; j < sessions.length; j++) {
          if (sessions[j].child_session_key !== p.child_session_key) continue;
          const nestedDepth = sessions[j].depth;
          const nextSessions = sessions.map((s, idx) =>
            idx === j
              ? { ...s, parts: applyChildEvent(s.parts, p.envelope, nestedDepth) }
              : s,
          );
          const nextParts = [...parts];
          nextParts[i] = { ...part, subagentSessions: nextSessions };
          return nextParts;
        }
      }
      return parts;
    }
    case "SubagentCompleted": {
      const p = payload as SubagentCompletedPayload;
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i];
        if (part.kind !== "tool_use") continue;
        const sessions = part.subagentSessions ?? [];
        for (let j = 0; j < sessions.length; j++) {
          if (sessions[j].child_session_key !== p.child_session_key) continue;
          const completed: SubagentSession = {
            ...sessions[j],
            status: p.finish_reason === "error" ? "errored" : "complete",
            finish_reason: p.finish_reason,
            tool_calls_made: p.tool_calls_made,
            elapsed_ms: p.elapsed_ms,
            summary: p.summary,
          };
          const nextSessions = sessions.map((s, idx) => (idx === j ? completed : s));
          const nextParts = [...parts];
          nextParts[i] = { ...part, subagentSessions: nextSessions };
          return nextParts;
        }
      }
      return parts;
    }
    default:
      return parts;
  }
}

function replacePartIn(parts: Part[], block_id: string, replacer: (p: Part | undefined) => Part): Part[] {
  const idx = parts.findIndex((p) => p.block_id === block_id);
  const existing = idx >= 0 ? parts[idx] : undefined;
  const next = replacer(existing);
  return idx >= 0
    ? parts.map((p, i) => (i === idx ? next : p))
    : [...parts, next];
}

export function timelineReducer(state: TimelineState, action: TimelineAction): TimelineState {
  switch (action.type) {
    case "reset":
      return initialTimelineState;
    case "events": {
      let next = state;
      for (const ev of action.events) {
        next = applyEvent(next, ev);
      }
      return next;
    }
  }
}

/* -------------------------------------------------------------- */
/*                            Context                             */
/* -------------------------------------------------------------- */

export interface TimelineContextValue {
  state: TimelineState;
  dispatch: React.Dispatch<TimelineAction>;
}

const TimelineContext = React.createContext<TimelineContextValue | null>(null);

export function TimelineProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = React.useReducer(timelineReducer, initialTimelineState);
  const value = React.useMemo(() => ({ state, dispatch }), [state]);
  return React.createElement(TimelineContext.Provider, { value }, children);
}

export function useTimeline(): TimelineContextValue {
  const ctx = React.useContext(TimelineContext);
  if (!ctx) {
    throw new Error("useTimeline must be used inside <TimelineProvider>");
  }
  return ctx;
}
