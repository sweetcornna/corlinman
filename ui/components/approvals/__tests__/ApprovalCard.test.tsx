import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ApprovalCard } from "../ApprovalCard";
import type { Approval } from "../types";

/**
 * W3-4 field-contract test: the UI `Approval` type maps 1:1 onto the
 * Python gateway's `ApprovalOut` (call_id / args_preview / created_at /
 * decision_reason — NOT the Rust-era id / args_json / requested_at).
 * A wire-shaped fixture rendering correctly pins that contract; the
 * callback assertions pin that actions carry the `call_id`.
 */
const WIRE_APPROVAL: Approval = {
  call_id: "call_w34",
  plugin: "github",
  tool: "create_issue",
  session_key: "acme::s1",
  args_preview: '{"title": "hi"}',
  reason: "permission rule requires approval",
  created_at: Date.now() / 1000 - 5,
  decision: null,
  decided_at: null,
  decision_reason: null,
};

describe("ApprovalCard (wire contract)", () => {
  it("renders the ApprovalOut fields and passes call_id to actions", () => {
    const onApprove = vi.fn();
    const onActivate = vi.fn();
    render(
      <ApprovalCard
        approval={WIRE_APPROVAL}
        now={Date.now()}
        isPending
        isSelected={false}
        isActive={false}
        onToggleSelect={() => {}}
        onActivate={onActivate}
        onApprove={onApprove}
        onDeny={() => {}}
        showShortcuts={false}
      />,
    );

    // plugin.tool + args preview come from the wire field names.
    expect(screen.getByText("create_issue")).toBeInTheDocument();
    expect(screen.getByText('{"title":"hi"}')).toBeInTheDocument();
    expect(screen.getByText("acme::s1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onApprove).toHaveBeenCalledWith("call_w34");

    fireEvent.click(screen.getByRole("article"));
    expect(onActivate).toHaveBeenCalledWith("call_w34");
  });

  it("shows a decision tag for decided rows (timeout distinguishable)", () => {
    render(
      <ApprovalCard
        approval={{
          ...WIRE_APPROVAL,
          decision: "timeout",
          decided_at: WIRE_APPROVAL.created_at + 300,
          decision_reason: "stream ended before a decision arrived",
        }}
        now={Date.now()}
        isPending={false}
        isSelected={false}
        isActive={false}
        onToggleSelect={() => {}}
        onActivate={() => {}}
        onApprove={() => {}}
        onDeny={() => {}}
        showShortcuts={false}
      />,
    );
    expect(screen.getByText(/timeout/i)).toBeInTheDocument();
  });
});
