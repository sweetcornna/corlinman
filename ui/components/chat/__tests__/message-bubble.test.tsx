import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage } from "@/lib/chat/types";

function ulWrap(child: React.ReactNode) {
  return <ul>{child}</ul>;
}

function makeMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "user",
    content: "hello",
    createdAt: Date.UTC(2026, 0, 1, 12, 30),
    ...overrides,
  };
}

describe("MessageBubble", () => {
  it("aligns user messages right and assistant left via data-role", () => {
    const { rerender } = render(
      ulWrap(<MessageBubble message={makeMsg({ role: "user" })} />),
    );
    expect(screen.getByTestId("chat-bubble").getAttribute("data-role")).toBe("user");

    rerender(
      ulWrap(
        <MessageBubble
          message={makeMsg({ role: "assistant", content: "hi" })}
        />,
      ),
    );
    expect(screen.getByTestId("chat-bubble").getAttribute("data-role")).toBe(
      "assistant",
    );
  });

  it("renders markdown for assistant messages (code blocks shown)", () => {
    render(
      ulWrap(
        <MessageBubble
          message={makeMsg({
            role: "assistant",
            content: "```py\nprint('hi')\n```",
          })}
        />,
      ),
    );
    expect(screen.getByTestId("md-codeblock")).toBeInTheDocument();
  });

  it("renders tool-call cards from message.toolCalls", () => {
    render(
      ulWrap(
        <MessageBubble
          message={makeMsg({
            role: "assistant",
            content: "",
            toolCalls: [
              {
                callId: "c1",
                toolName: "read_file",
                argsJson: '{"path":"x"}',
                status: "ok",
                resultPreview: "ok",
              },
            ],
          })}
        />,
      ),
    );
    expect(screen.getByTestId("tool-call-card")).toBeInTheDocument();
    expect(
      screen.getByTestId("tool-call-card").getAttribute("data-tool-name"),
    ).toBe("read_file");
  });

  it("hides reasoning, tool calls, and subagents when action trace is disabled", () => {
    render(
      ulWrap(
        <MessageBubble
          showActionTrace={false}
          message={makeMsg({
            role: "assistant",
            content: "done",
            reasoning: "private reasoning",
            toolCalls: [
              {
                callId: "c1",
                toolName: "read_file",
                argsJson: '{"path":"x"}',
                status: "ok",
              },
            ],
            subagents: [
              {
                childSessionKey: "child_1",
                childAgentId: "researcher",
                depth: 1,
                status: "completed",
              },
            ],
          })}
        />,
      ),
    );

    expect(screen.queryByTestId("reasoning-block")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-call-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("subagent-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bubble-tools-toggle")).not.toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
  });

  it("shows error envelope when set", () => {
    render(
      ulWrap(
        <MessageBubble
          message={makeMsg({
            role: "assistant",
            content: "partial",
            error: "stream failed",
          })}
        />,
      ),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("stream failed");
  });

  it("fires onRegenerate when assistant copy/regenerate clicked", () => {
    const onRegenerate = vi.fn();
    render(
      ulWrap(
        <MessageBubble
          message={makeMsg({ role: "assistant", content: "answer" })}
          onRegenerate={onRegenerate}
        />,
      ),
    );
    fireEvent.click(screen.getByLabelText("重新生成回复"));
    expect(onRegenerate).toHaveBeenCalledOnce();
  });

  it("fires onApprove with decision + scope when approval clicked", () => {
    const onApprove = vi.fn();
    render(
      ulWrap(
        <MessageBubble
          message={makeMsg({
            role: "assistant",
            content: "",
            turnId: "turn_1",
            approvals: [
              {
                callId: "call_1",
                plugin: "shell",
                tool: "run_shell",
                argsPreviewJson: '{"cmd":"ls"}',
              },
            ],
          })}
          onApprove={onApprove}
        />,
      ),
    );
    fireEvent.click(screen.getByTestId("approval-once"));
    expect(onApprove).toHaveBeenCalledWith(
      "turn_1",
      "call_1",
      "approved",
      "once",
    );
  });
});
