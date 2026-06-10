"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

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
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl border-tp-glass-edge bg-tp-glass-2 backdrop-blur-glass-strong backdrop-saturate-glass-strong">
        <DialogHeader>
          <DialogTitle className="text-tp-ink">
            {t("channels.qq.scanLogin.title")}
          </DialogTitle>
          <DialogDescription className="text-tp-ink-3">
            {t("channels.qq.scanLogin.subtitle")}
          </DialogDescription>
        </DialogHeader>

        {open ? (
          <>
            <NapcatDiagnosticsStrip
              diagnostics={diagnostics}
              error={diagnosticsError}
            />
            <iframe
              data-testid="qq-napcat-webui"
              src="/webui"
              title="NapCat WebUI"
              className="h-[620px] w-full rounded-xl border border-tp-glass-edge bg-white"
            />
          </>
        ) : null}
      </DialogContent>
    </Dialog>
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
      <div className="rounded-lg border border-tp-err/25 bg-tp-err-soft px-3 py-2 text-[12px] text-tp-err">
        {error}
      </div>
    );
  }
  if (!diagnostics) {
    return (
      <div className="rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[12px] text-tp-ink-3">
        NapCat diagnostics: checking
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[12px] text-tp-ink-2 sm:grid-cols-4">
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
  return (
    <div className="min-w-0">
      <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-tp-ink-4">
        {label}
      </div>
      <div className="truncate font-mono text-[12px]" data-testid={testId}>
        {value}
      </div>
    </div>
  );
}
