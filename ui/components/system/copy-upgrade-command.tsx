"use client";

/**
 * `<CopyUpgradeCommand>` — monospace code block + one-click copy (W2.1).
 *
 * Surface contract:
 *   - `command` is rendered verbatim in a `<pre>` so the operator can
 *     read it before copying (long shell incantations benefit from being
 *     visually verifiable).
 *   - Click → `navigator.clipboard.writeText(command)` (modern). Falls
 *     back to the legacy hidden-textarea + `document.execCommand('copy')`
 *     dance when the Clipboard API is unavailable (file:// + some older
 *     mobile browsers).
 *   - A `sonner` toast confirms the copy in either path. The button itself
 *     also flashes "Copied" for 1.5s so screen-free / muted-toast users
 *     get inline confirmation.
 *
 * `label` is shown as a non-interactive caption above the code block so
 * the tab headers (Native / Docker / Docker+QQ) read naturally even when
 * the same component is reused across tabs.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Check, Copy } from "@/components/icons";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const FLASH_MS = 1_500;

export interface CopyUpgradeCommandProps {
  /** Visible caption above the code block (e.g. "Native deploy"). */
  label: string;
  /** The exact shell command — copied to clipboard verbatim. */
  command: string;
  className?: string;
}

/**
 * Legacy clipboard fallback. Used when `navigator.clipboard.writeText` is
 * unavailable or rejects (some mobile browsers + insecure contexts). The
 * caller still toasts/flashes on success.
 *
 * Returns `true` on success, `false` if `execCommand('copy')` reported a
 * failure (or if `document.execCommand` itself isn't supported).
 */
function copyViaTextarea(text: string): boolean {
  if (typeof document === "undefined") return false;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  // Position offscreen so the textarea is never visible to the user even
  // for the brief moment it's mounted.
  ta.style.position = "fixed";
  ta.style.top = "-9999px";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

export function CopyUpgradeCommand({
  label,
  command,
  className,
}: CopyUpgradeCommandProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = React.useState(false);
  const flashTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    return () => {
      if (flashTimer.current) {
        clearTimeout(flashTimer.current);
        flashTimer.current = null;
      }
    };
  }, []);

  const flashSuccess = React.useCallback(() => {
    setCopied(true);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => {
      setCopied(false);
      flashTimer.current = null;
    }, FLASH_MS);
    toast.success(t("system.upgrade.copied"));
  }, [t]);

  const handleCopy = React.useCallback(async () => {
    // Modern path — runs in secure contexts (https + localhost).
    if (
      typeof navigator !== "undefined" &&
      navigator.clipboard &&
      typeof navigator.clipboard.writeText === "function"
    ) {
      try {
        await navigator.clipboard.writeText(command);
        flashSuccess();
        return;
      } catch {
        // Fall through to legacy path.
      }
    }
    // Legacy fallback.
    if (copyViaTextarea(command)) {
      flashSuccess();
      return;
    }
    toast.error(t("system.upgrade.copy"));
  }, [command, flashSuccess, t]);

  return (
    <div
      data-testid="copy-upgrade-command"
      className={cn("space-y-2", className)}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-medium uppercase tracking-wide text-sg-ink-3">
          {label}
        </span>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleCopy}
          aria-label={t("system.upgrade.copy")}
          data-testid="copy-upgrade-command-button"
          className="gap-1.5"
        >
          {copied ? (
            <>
              <Check className="h-3.5 w-3.5 text-sg-accent" aria-hidden />
              <span>{t("system.upgrade.copied")}</span>
            </>
          ) : (
            <>
              <Copy className="h-3.5 w-3.5" aria-hidden />
              <span>{t("system.upgrade.copy")}</span>
            </>
          )}
        </Button>
      </div>
      <pre
        data-testid="copy-upgrade-command-pre"
        className="overflow-x-auto rounded-md border border-sg-border bg-sg-inset p-3 font-mono text-[12px] leading-relaxed text-sg-ink"
      >
        {command}
      </pre>
    </div>
  );
}
