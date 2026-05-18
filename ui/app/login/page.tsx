"use client";

/**
 * Admin login page. Two-column layout: brand + dot-grid art on the left,
 * form on the right. Sits outside the `(admin)` group so it doesn't
 * trigger the auth guard.
 *
 * Flow:
 *   1. User types username + password → submits.
 *   2. We POST `/admin/login`; the gateway validates argon2 + sets the
 *      `corlinman_session` HttpOnly cookie on the response.
 *   3. On success, navigate to `?redirect=<path>` if present, else `/`.
 *   4. On failure, render the error inline with a shake animation.
 */

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";

import {
  completePasswordReset,
  getSession,
  login,
  requestPasswordReset,
} from "@/lib/auth";
import { CorlinmanApiError } from "@/lib/api";
import { BrandMark } from "@/components/layout/brand-mark";
import { LanguageToggle } from "@/components/layout/language-toggle";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

// The root body no longer paints bg-background (so the admin aurora can
// show through). Login needs to paint its own background explicitly.

export default function LoginPage() {
  return (
    <div className="relative grid min-h-dvh grid-cols-1 md:grid-cols-[40%_60%]">
      {/* theme + language toggles in top-right regardless of column */}
      <div className="absolute right-4 top-4 z-10 flex items-center gap-2">
        <LanguageToggle />
        <ThemeToggle />
      </div>
      <HeroColumn />
      <div className="flex items-center justify-center p-8">
        <Suspense fallback={<LoginFormShell disabled />}>
          <LoginForm />
        </Suspense>
      </div>
    </div>
  );
}

function HeroColumn() {
  const { t } = useTranslation();
  return (
    <aside className="relative hidden overflow-hidden border-r border-tp-glass-edge bg-tp-glass-inner md:flex md:flex-col md:justify-between md:p-10">
      <div className="flex items-center gap-2">
        <BrandMark />
      </div>
      <div className="relative z-10 space-y-2">
        <h2 className="text-lg font-semibold tracking-tight">
          {t("auth.heroTitle")}
        </h2>
        <p className="max-w-xs text-sm text-tp-ink-3">
          {t("auth.heroBody")}
        </p>
      </div>
      <div className="flex items-center gap-2 text-xs text-tp-ink-3">
        <span className="font-mono">v0.1.1</span>
        <span>·</span>
        <span>M6 admin</span>
      </div>
      {/* decorative dot grid — slowly drifts to add life */}
      <div
        className="pointer-events-none absolute inset-0 dot-grid opacity-60 login-dot-drift"
        aria-hidden
      />
      {/* shimmer glow — slow diagonal sweep over the radial backdrop */}
      <div
        className="pointer-events-none absolute inset-0 login-shimmer-glow"
        aria-hidden
      />
      {/* subtle radial glow */}
      <div
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(600px_300px_at_20%_20%,hsl(var(--primary)/0.15),transparent_60%)]"
        aria-hidden
      />
      {/* Component-scoped keyframes. Intensities are low (≤10px drift, 16s
          period) so the form remains the visual anchor. Reduced-motion is
          honored via @media at the bottom. */}
      <style>{`
        @keyframes login-dot-drift {
          0%   { background-position: 0px 0px; }
          100% { background-position: 18px 18px; }
        }
        .login-dot-drift {
          animation: login-dot-drift 16s linear infinite;
          will-change: background-position;
        }
        @keyframes login-shimmer-sweep {
          0%   { opacity: 0.0; transform: translate3d(-15%, -10%, 0); }
          50%  { opacity: 0.55; }
          100% { opacity: 0.0; transform: translate3d(15%, 10%, 0); }
        }
        .login-shimmer-glow {
          background:
            radial-gradient(
              420px 220px at 30% 30%,
              hsl(var(--primary) / 0.16),
              transparent 70%
            );
          animation: login-shimmer-sweep 9s ease-in-out infinite;
          mix-blend-mode: plus-lighter;
          will-change: opacity, transform;
        }
        @media (prefers-reduced-motion: reduce) {
          .login-dot-drift,
          .login-shimmer-glow {
            animation: none !important;
          }
          .login-shimmer-glow {
            opacity: 0.4;
            transform: none;
          }
        }
      `}</style>
    </aside>
  );
}

function LoginForm() {
  const { t } = useTranslation();
  const router = useRouter();
  const params = useSearchParams();
  const redirect = params.get("redirect") ?? "/";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shakeKey, setShakeKey] = useState(0);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login({ username, password });
      // Wave 1.4 — fetch /admin/me right after the cookie is set so we
      // can honour `must_change_password`. The first-run admin/root seed
      // returns the flag as true; in that case we ignore the `?redirect=`
      // query (which is usually whatever the auth guard captured) and
      // hard-bounce to the security page. The admin layout guard would
      // do this too, but doing it here saves a round-trip flash.
      let forceRotate = false;
      try {
        const me = await getSession();
        forceRotate = me?.must_change_password === true;
      } catch {
        // Swallow — login succeeded so the cookie is good; the admin
        // layout's own getSession() will re-check and recover.
      }
      router.replace(forceRotate ? "/account/security" : redirect);
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 503) {
          // Gateway is in onboarding mode — bounce the operator to the
          // first-run wizard rather than asking them to ssh into the
          // host and seed `[admin]` manually.
          router.replace("/onboard");
          return;
        }
        setError(
          err.status === 401 ? t("auth.invalidCredentials") : err.message,
        );
      } else {
        setError(String(err));
      }
      setShakeKey((k) => k + 1);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="w-full max-w-sm space-y-6">
      <div className="space-y-1.5 md:hidden">
        <BrandMark />
      </div>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.signIn")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("auth.subtitle")}</p>
      </div>
      <form
        onSubmit={onSubmit}
        className="space-y-4"
        key={shakeKey}
        // Trigger the shake via key-remount + the global keyframe (globals.css).
        style={error ? { animation: "login-shake 220ms ease-out" } : undefined}
      >
        <div className="space-y-2">
          <Label htmlFor="username">{t("auth.username")}</Label>
          <Input
            id="username"
            name="username"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={submitting}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">{t("auth.password")}</Label>
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />
        </div>
        {error ? (
          <p
            role="alert"
            className="text-sm text-destructive"
            data-testid="login-error"
          >
            {error}
          </p>
        ) : null}
        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting ? t("auth.submitting") : t("auth.submit")}
        </Button>
      </form>
      <ForgotPasswordPanel />

      <p className="text-center text-xs text-tp-ink-3">
        {t("auth.sessionHint")}
      </p>
    </div>
  );
}

function LoginFormShell({ disabled }: { disabled?: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="w-full max-w-sm space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.signIn")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("auth.subtitle")}</p>
      </div>
      <div className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="username">{t("auth.username")}</Label>
          <Input id="username" disabled={disabled} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">{t("auth.password")}</Label>
          <Input id="password" type="password" disabled={disabled} />
        </div>
        <Button type="button" className="w-full" disabled>
          {t("auth.submit")}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Forgot-password panel — host-token challenge flow.
// ---------------------------------------------------------------------------

type ResetPhase = "idle" | "minted" | "submitting" | "done";

function ForgotPasswordPanel() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<ResetPhase>("idle");
  const [tokenPath, setTokenPath] = useState<string>("");
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [token, setToken] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [minting, setMinting] = useState(false);

  // TTL countdown
  useEffect(() => {
    if (phase !== "minted" || secondsLeft <= 0) return;
    const id = setInterval(() => {
      setSecondsLeft((s) => (s > 0 ? s - 1 : 0));
    }, 1000);
    return () => clearInterval(id);
  }, [phase, secondsLeft]);

  // Reset transient state when the operator closes the panel.
  useEffect(() => {
    if (!open) {
      setPhase("idle");
      setTokenPath("");
      setSecondsLeft(0);
      setToken("");
      setNewPw("");
      setConfirmPw("");
      setError(null);
    }
  }, [open]);

  async function onMint() {
    setError(null);
    setMinting(true);
    try {
      const { token_path, ttl_seconds } = await requestPasswordReset();
      setTokenPath(token_path);
      setSecondsLeft(ttl_seconds);
      setPhase("minted");
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 429) {
          // CorlinmanApiError only carries status/message; the
          // retry_after_seconds payload would need a richer surface to
          // bubble up. 60s matches REQUEST_THROTTLE_SECONDS on the
          // server.
          setError(t("auth.resetRateLimited", { seconds: 60 }));
        } else if (err.status === 503) {
          setError(t("auth.resetUnavailable"));
        } else {
          setError(err.message || t("auth.resetFailed"));
        }
      } else {
        setError(String(err));
      }
    } finally {
      setMinting(false);
    }
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (newPw.length < 8) {
      setError(t("auth.weakPassword", { min: 8 }));
      return;
    }
    if (newPw !== confirmPw) {
      setError(t("account.security.passwordMismatch"));
      return;
    }
    setPhase("submitting");
    try {
      await completePasswordReset({ token: token.trim(), new_password: newPw });
      setPhase("done");
    } catch (err) {
      setPhase("minted");
      if (err instanceof CorlinmanApiError) {
        if (err.status === 401) {
          setError(t("auth.resetInvalidToken"));
        } else if (err.status === 410) {
          setError(t("auth.resetTokenExpired"));
        } else if (err.status === 404) {
          setError(t("auth.resetNoToken"));
        } else if (err.status === 422) {
          setError(t("auth.weakPassword", { min: 8 }));
        } else {
          setError(err.message || t("auth.resetFailed"));
        }
      } else {
        setError(String(err));
      }
    }
  }

  return (
    <details
      className="text-xs text-tp-ink-3"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer select-none hover:text-tp-ink-2">
        {t("auth.forgotPassword")}
      </summary>

      {phase === "idle" && (
        <div className="mt-2 space-y-3 rounded border border-tp-glass-edge bg-tp-glass-inner p-3 leading-relaxed">
          <p>{t("auth.resetIntro")}</p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="w-full"
            disabled={minting}
            onClick={onMint}
          >
            {minting ? t("auth.submitting") : t("auth.resetMint")}
          </Button>
          {error ? (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          ) : null}
        </div>
      )}

      {(phase === "minted" || phase === "submitting") && (
        <form
          onSubmit={onSubmit}
          className="mt-2 space-y-3 rounded border border-tp-glass-edge bg-tp-glass-inner p-3 leading-relaxed"
        >
          <div className="space-y-1.5">
            <p className="font-medium text-tp-ink-2">
              {t("auth.resetStep1Title")}
            </p>
            <p>{t("auth.resetStep1Body")}</p>
            <pre className="overflow-x-auto rounded bg-black/40 p-2 font-mono text-[11px] text-tp-ink-2">
              cat {tokenPath}
            </pre>
            <p className="text-tp-ink-3">
              {t("auth.resetCountdown", {
                m: Math.floor(secondsLeft / 60),
                s: String(secondsLeft % 60).padStart(2, "0"),
              })}
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="reset-token" className="text-tp-ink-2">
              {t("auth.resetTokenLabel")}
            </Label>
            <Input
              id="reset-token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="paste here"
              required
              disabled={phase === "submitting" || secondsLeft <= 0}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="reset-new" className="text-tp-ink-2">
              {t("account.security.newPassword")}
            </Label>
            <Input
              id="reset-new"
              type="password"
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              required
              minLength={8}
              disabled={phase === "submitting"}
              autoComplete="new-password"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="reset-confirm" className="text-tp-ink-2">
              {t("account.security.confirmNewPassword")}
            </Label>
            <Input
              id="reset-confirm"
              type="password"
              value={confirmPw}
              onChange={(e) => setConfirmPw(e.target.value)}
              required
              minLength={8}
              disabled={phase === "submitting"}
              autoComplete="new-password"
            />
          </div>

          {error ? (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          ) : null}

          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setPhase("idle")}
              disabled={phase === "submitting"}
            >
              {t("auth.resetCancel")}
            </Button>
            <Button
              type="submit"
              size="sm"
              className="flex-1"
              disabled={phase === "submitting" || secondsLeft <= 0}
            >
              {phase === "submitting"
                ? t("auth.submitting")
                : t("auth.resetSubmit")}
            </Button>
          </div>
          {secondsLeft <= 0 && (
            <p className="text-amber-500">{t("auth.resetTokenExpired")}</p>
          )}
        </form>
      )}

      {phase === "done" && (
        <div
          role="status"
          className="mt-2 space-y-2 rounded border border-emerald-500/40 bg-emerald-500/10 p-3 leading-relaxed"
        >
          <p className="font-medium text-emerald-400">
            {t("auth.resetSuccessTitle")}
          </p>
          <p>{t("auth.resetSuccessBody")}</p>
        </div>
      )}
    </details>
  );
}
