/**
 * `ask_user` — the agent-side clarification-question tool.
 *
 * The backend advertises `ask_user` to every model; when called, the tool
 * result tells the model to finalize the turn with the question text. The
 * question + canned options therefore live only in the tool-call args —
 * Telegram renders them as inline-keyboard buttons, QQ-family channels as a
 * bulleted list. This module is the web-side equivalent: it recognises the
 * call in `ChatMessage.toolCalls` so the bubble can render a question card
 * with clickable options instead of a generic tool-trace row.
 *
 * The parse mirrors `corlinman_agent.interactive.ask_user.parse_ask_user_args`
 * (max 8 options, newlines stripped, labels capped) so both surfaces show
 * the same thing.
 */

import type { ToolCallState } from "@/lib/chat/types";

export const ASK_USER_TOOL_NAME = "ask_user";

const MAX_OPTIONS = 8;
const MAX_OPTION_LEN = 120;
const MAX_QUESTION_LEN = 1000;

export interface AskUserQuestion {
  question: string;
  options: string[];
  multiple: boolean;
}

/** Lenient parse of the `ask_user` args JSON. Returns `null` when the JSON
 *  is unparsable (still streaming) or carries no question. Never throws. */
export function parseAskUserArgs(argsJson: string): AskUserQuestion | null {
  let obj: unknown;
  try {
    obj = JSON.parse(argsJson || "{}");
  } catch {
    return null;
  }
  if (typeof obj !== "object" || obj === null) return null;
  const rec = obj as Record<string, unknown>;

  let question = typeof rec.question === "string" ? rec.question.trim() : "";
  if (!question) return null;
  if (question.length > MAX_QUESTION_LEN) {
    question = `${question.slice(0, MAX_QUESTION_LEN - 1)}…`;
  }

  const options: string[] = [];
  if (Array.isArray(rec.options)) {
    for (const raw of rec.options.slice(0, MAX_OPTIONS)) {
      let label = String(raw).replace(/\n/g, " ").trim();
      if (!label) continue;
      if (label.length > MAX_OPTION_LEN) {
        label = `${label.slice(0, MAX_OPTION_LEN - 1)}…`;
      }
      options.push(label);
    }
  }

  return { question, options, multiple: Boolean(rec.multiple) };
}

/** The parsed questions carried by a message's tool calls, in call order. */
export function askUserQuestions(
  toolCalls: ToolCallState[] | undefined,
): AskUserQuestion[] {
  if (!toolCalls?.length) return [];
  const out: AskUserQuestion[] = [];
  for (const tc of toolCalls) {
    if (tc.toolName !== ASK_USER_TOOL_NAME) continue;
    const parsed = parseAskUserArgs(tc.argsJson);
    if (parsed) out.push(parsed);
  }
  return out;
}
