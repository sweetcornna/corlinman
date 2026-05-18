"use client";

/**
 * OAuthLoginModal — generic provider PKCE login (W-A2 → W-A3).
 *
 * Mirrors the hermes UX
 * (`hermes-agent/web/src/components/OAuthLoginModal.tsx`) but for
 * corlinman. The Anthropic flow lit up in W-A2 and the xAI flow
 * piggybacks on the same paste-back-code state machine in W-A3 — the
 * only per-provider differences are:
 *
 *   * the start / submit API endpoints (dispatched via `FLOWS` below),
 *   * the i18n keys (title, description, intro copy, button labels),
 *   * the data-testid suffix so Playwright can address them separately.
 *
 * Phase machine (5 + error):
 *   idle → opening-browser → awaiting-code → exchanging → done
 *                                                       ↘ error
 *
 * Notes:
 *   - We never log `code` or `state` (treat them as bearer-like).
 *   - All in-flight requests are aborted on unmount via AbortController.
 *   - The `provider` prop defaults to "anthropic" so the W-A2 callsite
 *     keeps working without churn.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Check, ExternalLink, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  CorlinmanApiError,
  startAnthropicOAuth,
  startXaiOAuth,
  submitAnthropicOAuthCode,
  submitXaiOAuthCode,
  type OAuthStartResponse,
  type OAuthSubmitRequest,
  type OAuthSubmitResponse,
} from "@/lib/api";

export type OAuthLoginPhase =
  | "idle"
  | "opening-browser"
  | "awaiting-code"
  | "exchanging"
  | "done"
  | "error";

export type OAuthLoginProvider = "anthropic" | "xai";

/**
 * Provider-specific bag fed into the modal at render time. Keeping the
 * api functions in this dispatch table (rather than passing the bare
 * `provider` string down to individual call-sites) means the modal
 * stays a pure presentation layer — adding the next provider is a
 * single-line edit here plus the matching `ui/lib/api.ts` helpers.
 */
interface OAuthFlow {
  start: (opts: {
    signal?: AbortSignal;
  }) => Promise<OAuthStartResponse>;
  submit: (
    req: OAuthSubmitRequest,
    opts: { signal?: AbortSignal },
  ) => Promise<OAuthSubmitResponse>;
}

const FLOWS: Record<OAuthLoginProvider, OAuthFlow> = {
  anthropic: {
    start: (opts) => startAnthropicOAuth(opts),
    submit: (req, opts) => submitAnthropicOAuthCode(req, opts),
  },
  xai: {
    start: (opts) => startXaiOAuth(opts),
    submit: (req, opts) => submitXaiOAuthCode(req, opts),
  },
};

/**
 * Per-provider i18n key bundle. Keeping the keys collocated here (instead
 * of computing them inside JSX) makes the typecheck flag a missing key
 * the moment a new provider is added.
 */
interface ProviderCopy {
  title: string;
  description: string;
  intro: string;
  loginButton: string;
  openingBrowser: string;
  awaitingCode: string;
}

const COPY_KEYS: Record<OAuthLoginProvider, ProviderCopy> = {
  anthropic: {
    title: "oauth.modalTitleAnthropic",
    description: "oauth.modalDescriptionAnthropic",
    intro: "oauth.introAnthropic",
    loginButton: "oauth.loginButtonAnthropic",
    openingBrowser: "oauth.openingBrowserAnthropic",
    awaitingCode: "oauth.awaitingCodeAnthropic",
  },
  xai: {
    title: "oauth.modalTitleXai",
    description: "oauth.modalDescriptionXai",
    intro: "oauth.introXai",
    loginButton: "oauth.loginButtonXai",
    openingBrowser: "oauth.openingBrowserXai",
    awaitingCode: "oauth.awaitingCodeXai",
  },
};

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Which provider's PKCE handshake to drive. Defaults to "anthropic" so
   * the existing W-A2 callsite stays source-compatible. */
  provider?: OAuthLoginProvider;
  /** Called after a successful token exchange (and before auto-close). */
  onSuccess?: () => void;
}

/**
 * The reference (hermes) modal accepts both a paste-back code and a
 * state token. The provider's console returns them in the form
 * `<code>#<state>`; we accept either a single concatenated paste OR
 * two separate fields. Splitting here keeps the gateway contract
 * stable (it always receives both fields).
 */
function splitConcatenated(input: string): { code: string; state: string } {
  const trimmed = input.trim();
  const hashIdx = trimmed.indexOf("#");
  if (hashIdx > 0 && hashIdx < trimmed.length - 1) {
    return {
      code: trimmed.slice(0, hashIdx),
      state: trimmed.slice(hashIdx + 1),
    };
  }
  return { code: trimmed, state: "" };
}

export function OAuthLoginModal({
  open,
  onOpenChange,
  provider = "anthropic",
  onSuccess,
}: Props) {
  const { t } = useTranslation();
  const [phase, setPhase] = React.useState<OAuthLoginPhase>("idle");
  const [start, setStart] = React.useState<OAuthStartResponse | null>(null);
  const [code, setCode] = React.useState("");
  const [state, setState] = React.useState("");
  const [errorMsg, setErrorMsg] = React.useState<string | null>(null);
  const abortRef = React.useRef<AbortController | null>(null);

  const flow = FLOWS[provider];
  const copy = COPY_KEYS[provider];

  // Cancel any in-flight request + clear inputs on unmount or close.
  const reset = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPhase("idle");
    setStart(null);
    setCode("");
    setState("");
    setErrorMsg(null);
  }, []);

  // Reset on close (NOT on unmount alone — unmount also fires reset below).
  React.useEffect(() => {
    if (!open) reset();
  }, [open, reset]);

  // Abort on unmount.
  React.useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const beginLogin = React.useCallback(async () => {
    setErrorMsg(null);
    setPhase("opening-browser");
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const resp = await flow.start({ signal: ac.signal });
      if (ac.signal.aborted) return;
      setStart(resp);
      // Best-effort open. Popup blockers may eat this — the fallback
      // link inside the awaiting-code panel covers that case.
      try {
        window.open(resp.auth_url, "_blank", "noopener,noreferrer");
      } catch {
        // ignore
      }
      setPhase("awaiting-code");
    } catch (err) {
      if (ac.signal.aborted) return;
      setPhase("error");
      setErrorMsg(
        err instanceof CorlinmanApiError
          ? err.message ||
            t("oauth.errorStartFailedStatus", {
              status: err.status ?? "?",
            })
          : err instanceof Error
            ? err.message
            : t("oauth.errorStartFailed"),
      );
    }
  }, [flow, t]);

  const submitCode = React.useCallback(async () => {
    if (!start) return;
    // Allow concat paste in the code field.
    const split = splitConcatenated(code);
    const finalCode = split.code;
    const finalState = state.trim() || split.state;
    if (!finalCode || !finalState) {
      setErrorMsg(t("oauth.errorBothRequired"));
      return;
    }
    setErrorMsg(null);
    setPhase("exchanging");
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      await flow.submit(
        { session_id: start.session_id, code: finalCode, state: finalState },
        { signal: ac.signal },
      );
      if (ac.signal.aborted) return;
      setPhase("done");
      onSuccess?.();
      // Auto-close after a beat so the user sees the success state.
      window.setTimeout(() => {
        if (!ac.signal.aborted) onOpenChange(false);
      }, 1200);
    } catch (err) {
      if (ac.signal.aborted) return;
      setPhase("error");
      setErrorMsg(
        err instanceof CorlinmanApiError
          ? err.message ||
            t("oauth.errorExchangeFailedStatus", {
              status: err.status ?? "?",
            })
          : err instanceof Error
            ? err.message
            : t("oauth.errorExchangeFailed"),
      );
    }
  }, [code, state, start, onSuccess, onOpenChange, flow, t]);

  const handleRetry = React.useCallback(() => {
    setErrorMsg(null);
    setStart(null);
    setCode("");
    setState("");
    setPhase("idle");
  }, []);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-md"
        data-testid="oauth-login-modal"
        data-phase={phase}
        data-provider={provider}
      >
        <DialogHeader>
          <DialogTitle>{t(copy.title)}</DialogTitle>
          <DialogDescription>{t(copy.description)}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          {phase === "idle" && (
            <div className="flex flex-col gap-3">
              <p className="text-sm text-tp-ink-2">{t(copy.intro)}</p>
              <Button
                onClick={beginLogin}
                data-testid="oauth-login-start"
                className="self-start"
              >
                {t(copy.loginButton)}
              </Button>
            </div>
          )}

          {phase === "opening-browser" && (
            <div className="flex items-center gap-2 text-sm text-tp-ink-2">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              {t(copy.openingBrowser)}
            </div>
          )}

          {phase === "awaiting-code" && start && (
            <div className="flex flex-col gap-3">
              <p className="text-sm text-tp-ink-2">{t(copy.awaitingCode)}</p>
              <a
                href={start.auth_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 self-start text-xs text-tp-ink-3 underline-offset-2 hover:underline"
                data-testid="oauth-login-manual-link"
              >
                <ExternalLink className="h-3 w-3" aria-hidden />
                {t("oauth.openManually")}
              </a>
              <div className="flex flex-col gap-1">
                <Label htmlFor="oauth-code">{t("oauth.codeLabel")}</Label>
                <Input
                  id="oauth-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder={t("oauth.codePlaceholder")}
                  autoComplete="off"
                  spellCheck={false}
                  data-testid="oauth-login-code"
                />
                <p className="text-[11px] text-tp-ink-3">
                  {t("oauth.codeSplitHint", { example: "CODE#STATE" })}
                </p>
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="oauth-state">{t("oauth.stateLabel")}</Label>
                <Input
                  id="oauth-state"
                  value={state}
                  onChange={(e) => setState(e.target.value)}
                  placeholder={t("oauth.statePlaceholder")}
                  autoComplete="off"
                  spellCheck={false}
                  data-testid="oauth-login-state"
                />
              </div>
              {errorMsg && (
                <p
                  className="text-xs text-destructive"
                  data-testid="oauth-login-inline-error"
                >
                  {errorMsg}
                </p>
              )}
            </div>
          )}

          {phase === "exchanging" && (
            <div className="flex items-center gap-2 text-sm text-tp-ink-2">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              {t("oauth.exchanging")}
            </div>
          )}

          {phase === "done" && (
            <div className="flex items-center gap-2 text-sm text-ok">
              <Check className="h-4 w-4" aria-hidden />
              {t("oauth.done")}
            </div>
          )}

          {phase === "error" && (
            <div
              className="flex flex-col gap-2 rounded border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
              data-testid="oauth-login-error"
              role="alert"
            >
              <span>{errorMsg ?? t("oauth.errorLoginFailed")}</span>
            </div>
          )}
        </div>

        <DialogFooter>
          {phase === "awaiting-code" && (
            <>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                data-testid="oauth-login-cancel"
              >
                {t("oauth.cancel")}
              </Button>
              <Button
                onClick={submitCode}
                disabled={!code.trim()}
                data-testid="oauth-login-submit"
              >
                {t("oauth.submit")}
              </Button>
            </>
          )}
          {phase === "error" && (
            <>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                data-testid="oauth-login-close"
              >
                {t("oauth.close")}
              </Button>
              <Button
                onClick={handleRetry}
                data-testid="oauth-login-retry"
              >
                {t("oauth.tryAgain")}
              </Button>
            </>
          )}
          {(phase === "idle" ||
            phase === "opening-browser" ||
            phase === "exchanging") && (
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={phase === "exchanging"}
              data-testid="oauth-login-dismiss"
            >
              {t("oauth.cancel")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
