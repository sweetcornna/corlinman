"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Check, ShieldAlert, Shield, X } from "@/components/icons";

import { cn } from "@/lib/utils";
import type {
  ApprovalDecision,
  ApprovalPromptState,
  ApprovalScope,
} from "@/lib/chat/types";

interface ApprovalPromptProps {
  prompt: ApprovalPromptState;
  onDecide: (decision: ApprovalDecision, scope: ApprovalScope) => void;
}

export function ApprovalPrompt({ prompt, onDecide }: ApprovalPromptProps) {
  const { t } = useTranslation();
  const decided = prompt.decision !== undefined;

  const decisionLabel = prompt.decision === "approved"
    ? t("chat.approvalDecidedApproved")
    : t("chat.approvalDecidedDenied");
  const scopeLabel = prompt.decidedScope === "session"
    ? t("chat.approvalDecisionScopeSession")
    : prompt.decidedScope === "always"
      ? t("chat.approvalDecisionScopeAlways")
      : t("chat.approvalDecisionScopeOnce");

  return (
    <div
      className={cn(
        "my-2 overflow-hidden rounded-sg-md border",
        // Undecided = the heaviest inline layer: opaque matte + strong moon
        // edge + elevation (the .c-sheet treatment, rendered in-thread).
        decided
          ? "border-sg-border bg-sg-inset opacity-70"
          : "border-sg-border-strong bg-sg-opaque shadow-[var(--sg-edge-top-strong),var(--sg-elev-2)]",
      )}
      role={decided ? undefined : "alertdialog"}
      aria-label={t("chat.approvalAriaLabel", { tool: prompt.tool })}
      data-testid="approval-prompt"
    >
      <div className="flex items-center gap-2 border-b border-sg-border px-3 py-2 text-[12px]">
        <ShieldAlert
          className={cn(
            "h-4 w-4",
            decided ? "text-sg-ink-4" : "text-sg-warn",
          )}
          aria-hidden="true"
        />
        <span className="font-medium text-sg-ink">
          {prompt.plugin}.{prompt.tool}
        </span>
        <span className="ml-auto text-[11px] text-sg-ink-4">
          {decided
            ? t("chat.approvalDecidedSuffix", { decision: decisionLabel, scope: scopeLabel })
            : t("chat.approvalRequired")}
        </span>
      </div>
      <div className="space-y-2 px-3 py-2 text-[12px]">
        {prompt.reason ? (
          <div className="text-sg-ink-3 italic">{prompt.reason}</div>
        ) : null}
        <pre className="max-h-[160px] overflow-auto rounded-sg-sm bg-sg-inset p-2 font-mono text-[11px] leading-snug text-sg-ink">
          {prompt.argsPreviewJson || t("chat.approvalNoArgs")}
        </pre>
        {!decided ? (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              onClick={() => onDecide("denied", "once")}
              className="inline-flex items-center gap-1 rounded-full border border-sg-border-ghost bg-transparent px-2.5 py-1 text-[12px] text-sg-ink-3 hover:border-sg-err/50 hover:bg-sg-err-soft hover:text-sg-err"
              data-testid="approval-deny"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
              {t("chat.approvalDeny")}
            </button>
            <button
              type="button"
              onClick={() => onDecide("approved", "once")}
              className="inline-flex items-center gap-1 rounded-full bg-sg-tint px-2.5 py-1 text-[12px] font-medium text-sg-tint-ink hover:bg-sg-tint/90"
              data-testid="approval-once"
            >
              <Check className="h-3.5 w-3.5" aria-hidden="true" />
              {t("chat.approvalApproveOnce")}
            </button>
            <button
              type="button"
              onClick={() => onDecide("approved", "session")}
              className="inline-flex items-center gap-1 rounded-full border border-sg-border-ghost bg-transparent px-2.5 py-1 text-[12px] text-sg-ink hover:bg-sg-inset-hover"
              data-testid="approval-session"
            >
              <Shield className="h-3.5 w-3.5" aria-hidden="true" />
              {t("chat.approvalApproveAlways")}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
