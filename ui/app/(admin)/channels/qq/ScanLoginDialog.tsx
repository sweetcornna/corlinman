"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  fetchNapcatDiagnostics,
  type NapcatDiagnostics,
} from "@/lib/api";

/**
 * QQ scan-login dialog.
 *
 * Embeds NapCat's own WebUI — reverse-proxied same-origin at `/webui` —
 * in an iframe. NapCat's native WebUI owns the QR lifecycle: it refreshes
 * the code live over its own websocket and reports login state itself,
 * which stays reliable across NapCat releases.
 *
 * This replaces the former relay flow (`POST /admin/channels/qq/qrcode`
 * snapshot + 2s status poll). That relay raced NapCat's ~120s QR rotation
 * and silently served stale codes, so scans never landed — it was dropped
 * in favour of embedding NapCat's first-party UI directly.
 *
 * `/webui` is expected to resolve to the NapCat WebUI; the deployment's
 * reverse proxy is responsible for injecting the WebUI access token.
 * Nothing here is NapCat-version specific — NapCat drives its own login.
 */
export function ScanLoginDialog({
  open,
  onOpenChange,
  onClosed,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onClosed?: () => void;
}) {
  const { t } = useTranslation();
  const [diagnostics, setDiagnostics] =
    React.useState<NapcatDiagnostics | null>(null);
  const [diagnosticsError, setDiagnosticsError] = React.useState<string | null>(
    null,
  );

  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setDiagnostics(null);
    setDiagnosticsError(null);
    void fetchNapcatDiagnostics()
      .then((out) => {
        if (!cancelled) setDiagnostics(out);
      })
      .catch((err) => {
        if (!cancelled) {
          setDiagnosticsError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const handleOpenChange = React.useCallback(
    (next: boolean) => {
      onOpenChange(next);
      if (!next) onClosed?.();
    },
    [onClosed, onOpenChange],
  );

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t("channels.qq.scanLogin.title")}</DialogTitle>
          <DialogDescription>
            {t("channels.qq.scanLogin.subtitle")}
          </DialogDescription>
        </DialogHeader>

        {open ? (
          <>
            <NapcatDiagnosticsStrip
              diagnostics={diagnostics}
              error={diagnosticsError}
            />
            {/* QR well — sunken glass-inset frame with cyan corner accents
                bracketing NapCat's first-party WebUI / live QR. */}
            <div className="relative rounded-sg-lg border border-sg-border bg-sg-inset p-2 shadow-[inset_0_1px_3px_oklch(0_0_0/0.18)]">
              <CornerAccents />
              <iframe
                data-testid="qq-napcat-webui"
                src="/webui"
                title="NapCat WebUI"
                className="relative h-[620px] w-full rounded-sg-md border border-sg-border bg-white"
              />
            </div>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

/**
 * Four L-shaped cyan corner brackets framing the QR well — purely decorative,
 * token-driven, and pointer-transparent so the iframe stays interactive.
 */
function CornerAccents() {
  const base =
    "pointer-events-none absolute h-4 w-4 border-sg-accent/60";
  return (
    <span aria-hidden>
      <span className={`${base} left-1 top-1 rounded-tl-sg-sm border-l-2 border-t-2`} />
      <span className={`${base} right-1 top-1 rounded-tr-sg-sm border-r-2 border-t-2`} />
      <span className={`${base} bottom-1 left-1 rounded-bl-sg-sm border-b-2 border-l-2`} />
      <span className={`${base} bottom-1 right-1 rounded-br-sg-sm border-b-2 border-r-2`} />
    </span>
  );
}

function NapcatDiagnosticsStrip({
  diagnostics,
  error,
}: {
  diagnostics: NapcatDiagnostics | null;
  error: string | null;
}) {
  if (error) {
    return (
      <div className="rounded-sg-md border border-sg-err/30 bg-sg-err-soft px-3 py-2 text-[12px] text-sg-err">
        {error}
      </div>
    );
  }
  if (!diagnostics) {
    return (
      <div className="rounded-sg-md border border-sg-border bg-sg-inset px-3 py-2 text-[12px] text-sg-ink-3">
        NapCat diagnostics: checking
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-2 rounded-sg-md border border-sg-border bg-sg-inset px-3 py-2 text-[12px] text-sg-ink-2 sm:grid-cols-4">
      <DiagnosticItem
        label="mode"
        value={diagnostics.mode}
        testId="qq-napcat-diagnostics-mode"
      />
      <DiagnosticItem
        label="credential"
        value={diagnostics.credential}
        testId="qq-napcat-diagnostics-credential"
      />
      <DiagnosticItem
        label="QR"
        value={diagnostics.qrcode_api}
        testId="qq-napcat-diagnostics-qrcode"
      />
      <DiagnosticItem
        label="OB11"
        value={diagnostics.onebot_config_api}
        testId="qq-napcat-diagnostics-onebot"
      />
    </div>
  );
}

function DiagnosticItem({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}) {
  // Tint the status line with sg status tokens: green for "ok", red for known
  // failure words, neutral ink otherwise (urls, mode names, etc.).
  const v = value.toLowerCase();
  const tone =
    v === "ok" || v === "true"
      ? "text-sg-ok"
      : v === "error" || v === "missing" || v === "false" || v === "fail"
        ? "text-sg-err"
        : "text-sg-ink-2";
  return (
    <div className="min-w-0">
      <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-sg-ink-4">
        {label}
      </div>
      <div
        className={cn("truncate font-mono text-[12px]", tone)}
        data-testid={testId}
      >
        {value}
      </div>
    </div>
  );
}
