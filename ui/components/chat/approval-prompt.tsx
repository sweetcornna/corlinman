"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Check, ShieldAlert, Shield, X } from "lucide-react";

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
        "my-2 overflow-hidden rounded-md border-2 bg-tp-glass-inner",
        decided ? "border-tp-glass-edge opacity-70" : "border-tp-amber/60 shadow-sm",
      )}
      role={decided ? undefined : "alertdialog"}
      aria-label={t("chat.approvalAriaLabel", { tool: prompt.tool })}
      data-testid="approval-prompt"
    >
      <div className="flex items-center gap-2 border-b border-tp-glass-edge px-3 py-2 text-[12px]">
        <ShieldAlert
          className={cn(
            "h-4 w-4",
            decided ? "text-tp-ink-3" : "text-tp-amber",
          )}
          aria-hidden="true"
        />
        <span className="font-medium text-tp-ink">
          {prompt.plugin}.{prompt.tool}
        </span>
        <span className="ml-auto text-[11px] text-tp-ink-3">
          {decided
            ? t("chat.approvalDecidedSuffix", { decision: decisionLabel, scope: scopeLabel })
            : t("chat.approvalRequired")}
        </span>
      </div>
      <div className="space-y-2 px-3 py-2 text-[12px]">
        {prompt.reason ? (
          <div className="text-tp-ink-2 italic">{prompt.reason}</div>
        ) : null}
        <pre className="max-h-[160px] overflow-auto rounded bg-tp-glass-inner/80 p-2 font-mono text-[11px] leading-snug text-tp-ink">
          {prompt.argsPreviewJson || t("chat.approvalNoArgs")}
        </pre>
        {!decided ? (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              onClick={() => onDecide("denied", "once")}
              className="inline-flex items-center gap-1 rounded border border-tp-glass-edge px-2 py-1 text-[12px] text-tp-ink hover:border-tp-err hover:bg-tp-err/10"
              data-testid="approval-deny"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
              {t("chat.approvalDeny")}
            </button>
            <button
              type="button"
              onClick={() => onDecide("approved", "once")}
              className="inline-flex items-center gap-1 rounded border border-tp-amber/60 bg-tp-amber/10 px-2 py-1 text-[12px] text-tp-ink hover:bg-tp-amber/20"
              data-testid="approval-once"
            >
              <Check className="h-3.5 w-3.5" aria-hidden="true" />
              {t("chat.approvalApproveOnce")}
            </button>
            <button
              type="button"
              onClick={() => onDecide("approved", "session")}
              className="inline-flex items-center gap-1 rounded border border-tp-glass-edge px-2 py-1 text-[12px] text-tp-ink hover:bg-tp-glass-inner"
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
