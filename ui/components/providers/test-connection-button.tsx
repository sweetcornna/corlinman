"use client";

/**
 * Inline "Test connection" button for one provider row (W2.3 / W1.1).
 *
 * On click → POST /admin/providers/{name}/test (the W1.1 zero-cost probe
 * backport). Surfaces the result via a sonner toast and flashes the button
 * itself green/red for 3s so the operator can scan a long table without
 * reading every toast.
 *
 * States:
 *   idle    → "Test" with the default outline button styling
 *   testing → spinner + "Testing…" copy, disabled
 *   success → "OK" + green checkmark for 3s, then back to idle
 *   fail    → "Failed" + red, hover-tooltip carries the error message,
 *             stays sticky until the operator clicks again
 *
 * Toast hook: shared `toast` singleton from `sonner` (the same one the
 * Add/Edit/Delete flows on this page already use; there's no project-local
 * `useToast` wrapper).
 */

import * as React from "react";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { CorlinmanApiError, testProvider } from "@/lib/api";
import { cn } from "@/lib/utils";

type State =
  | { kind: "idle" }
  | { kind: "testing" }
  | { kind: "success" }
  | { kind: "fail"; error: string };

const SUCCESS_FLASH_MS = 3000;

export interface TestConnectionButtonProps {
  name: string;
  className?: string;
}

export function TestConnectionButton({
  name,
  className,
}: TestConnectionButtonProps) {
  const { t } = useTranslation();
  const [state, setState] = React.useState<State>({ kind: "idle" });
  const successResetTimer = React.useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  // Clear any pending success-flash timer on unmount or before scheduling a
  // new one.
  React.useEffect(() => {
    return () => {
      if (successResetTimer.current) {
        clearTimeout(successResetTimer.current);
        successResetTimer.current = null;
      }
    };
  }, []);

  async function runTest() {
    if (state.kind === "testing") return;
    if (successResetTimer.current) {
      clearTimeout(successResetTimer.current);
      successResetTimer.current = null;
    }
    setState({ kind: "testing" });
    try {
      const res = await testProvider(name);
      if (res.ok) {
        const note = res.note ? ` · ${res.note}` : "";
        toast.success(
          `✓ ${name} ${t("providers.test.success")} · ${res.latency_ms}ms${note}`,
        );
        setState({ kind: "success" });
        successResetTimer.current = setTimeout(() => {
          setState({ kind: "idle" });
          successResetTimer.current = null;
        }, SUCCESS_FLASH_MS);
      } else {
        const err = res.error ?? t("providers.test.fail");
        toast.error(`✗ ${name}: ${err}`);
        setState({ kind: "fail", error: err });
      }
    } catch (e) {
      const err =
        e instanceof CorlinmanApiError
          ? `${e.status ?? "?"} · ${e.message}`
          : e instanceof Error
            ? e.message
            : String(e);
      toast.error(`✗ ${name}: ${err}`);
      setState({ kind: "fail", error: err });
    }
  }

  const label =
    state.kind === "testing"
      ? t("providers.test.testing")
      : state.kind === "success"
        ? t("providers.test.success")
        : state.kind === "fail"
          ? t("providers.test.fail")
          : t("providers.test.button");

  const icon =
    state.kind === "testing" ? (
      <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
    ) : state.kind === "success" ? (
      <CheckCircle2 className="h-3 w-3 text-ok" aria-hidden="true" />
    ) : state.kind === "fail" ? (
      <XCircle className="h-3 w-3 text-destructive" aria-hidden="true" />
    ) : null;

  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      onClick={runTest}
      disabled={state.kind === "testing"}
      data-testid={`provider-test-btn-${name}`}
      data-test-state={state.kind}
      title={state.kind === "fail" ? state.error : undefined}
      aria-label={`${t("providers.test.button")} ${name}`}
      className={cn(
        "h-7 px-2 text-[11px]",
        state.kind === "success" && "border-ok/50 text-ok",
        state.kind === "fail" && "border-destructive/60 text-destructive",
        className,
      )}
    >
      {icon}
      {label}
    </Button>
  );
}

export default TestConnectionButton;
