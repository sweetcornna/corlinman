import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage, ToolCallState } from "@/lib/chat/types";

function askUserCall(args: object): ToolCallState {
  return {
    callId: "call-ask",
    toolName: "ask_user",
    argsJson: JSON.stringify(args),
    status: "ok",
  };
}

function makeMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "侧重哪方面?",
    createdAt: Date.UTC(2026, 0, 1, 12, 30),
    toolCalls: [
      askUserCall({ question: "侧重哪方面?", options: ["代码质量", "安全"] }),
    ],
    ...overrides,
  };
}

function renderBubble(msg: ChatMessage, onQuestionAnswer = vi.fn()) {
  render(
    <ul>
      <MessageBubble
        message={msg}
        isLatest
        onQuestionAnswer={onQuestionAnswer}
      />
    </ul>,
  );
  return onQuestionAnswer;
}

describe("MessageBubble × ask_user", () => {
  it("renders the question card with clickable options on the latest turn", () => {
    const onAnswer = renderBubble(makeMsg());
    expect(screen.getByTestId("question-card")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "安全" }));
    expect(onAnswer).toHaveBeenCalledWith("安全");
  });

  it("keeps ask_user out of the generic tool trace", () => {
    renderBubble(makeMsg());
    // No collapsed-trace chip: ask_user was the only tool call.
    expect(screen.queryByTestId("bubble-tools-toggle")).toBeNull();
  });

  it("still shows the trace chip for real tool calls alongside ask_user", () => {
    renderBubble(
      makeMsg({
        toolCalls: [
          {
            callId: "c1",
            toolName: "read_file",
            argsJson: "{}",
            status: "ok",
          },
          askUserCall({ question: "侧重哪方面?", options: ["代码质量"] }),
        ],
      }),
    );
    expect(screen.getByTestId("bubble-tools-toggle")).toBeInTheDocument();
    expect(screen.getByTestId("question-card")).toBeInTheDocument();
  });

  it("does not repeat the question line when the bubble text already carries it", () => {
    renderBubble(makeMsg());
    // Bubble markdown + card would give two copies — the card must skip it.
    expect(screen.getAllByText("侧重哪方面?")).toHaveLength(1);
  });

  it("shows the question line when the bubble text omitted it", () => {
    renderBubble(makeMsg({ content: "(等待你的回复)" }));
    expect(screen.getByText("侧重哪方面?")).toBeInTheDocument();
  });

  it("renders inert options on non-latest turns", () => {
    const onAnswer = vi.fn();
    render(
      <ul>
        <MessageBubble
          message={makeMsg()}
          isLatest={false}
          onQuestionAnswer={onAnswer}
        />
      </ul>,
    );
    const option = screen.getAllByTestId("question-option")[0]!;
    expect(option).toBeDisabled();
  });

  it("renders no card while the args are still streaming", () => {
    renderBubble(
      makeMsg({
        pending: true,
        toolCalls: [
          {
            callId: "call-ask",
            toolName: "ask_user",
            argsJson: '{"question": "侧重',
            status: "running",
          },
        ],
      }),
    );
    expect(screen.queryByTestId("question-card")).toBeNull();
  });
});
