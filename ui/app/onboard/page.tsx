"use client";

/**
 * First-run onboarding page — 2-step wizard (2026-05 reshape).
 *
 * Step 1 (account)  — admin/root force-rotate flow. If the gateway already
 *                     seeded an admin (/admin/me returns 200 with
 *                     must_change_password=true) we skip Step 1 and land
 *                     on Step 2 with a "Using default admin/root" hint
 *                     + a "Customize admin account" escape hatch.
 *
 * Step 2 (handoff)  — a welcome card that hands the operator off to the
 *                     generic provider-setup surfaces:
 *                       - /admin/credentials → API keys
 *                       - /admin/providers   → custom providers + per-agent models
 *                       - /admin/credentials#oauth → subscription OAuth login (anchor)
 *                     Also exposes a "Use mock provider for now" button
 *                     that POSTs /admin/onboard/finalize-skip and pushes
 *                     the operator at /admin.
 *
 * The newapi-specific wizard steps (probe / channel-pick / atomic finalize)
 * were removed alongside their backend routes — provider setup now lives
 * post-onboard on the three admin pages above.
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { KeyRound, LogIn, Plug } from "lucide-react";

import { getSession, onboard } from "@/lib/auth";
import { CorlinmanApiError, finalizeSkipOnboard } from "@/lib/api";
import { BrandMark } from "@/components/layout/brand-mark";
import { LanguageToggle } from "@/components/layout/language-toggle";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

const MIN_PASSWORD_LEN = 8;

type Step = "account" | "handoff";

export default function OnboardPage() {
  return (
    <div className="relative grid min-h-dvh grid-cols-1 bg-background md:grid-cols-[40%_60%]">
      <div className="absolute right-4 top-4 z-10 flex items-center gap-2">
        <LanguageToggle />
        <ThemeToggle />
      </div>
      <HeroColumn />
      <div className="flex items-center justify-center p-8">
        <OnboardWizard />
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
          {t("auth.onboardHeroTitle")}
        </h2>
        <p className="max-w-xs text-sm text-tp-ink-3">
          {t("auth.onboardHeroBody")}
        </p>
      </div>
      <div className="flex items-center gap-2 text-xs text-tp-ink-3">
        <span className="font-mono">v0.6.0</span>
        <span>·</span>
        <span>first-run</span>
      </div>
      <div
        className="pointer-events-none absolute inset-0 dot-grid opacity-60"
        aria-hidden
      />
      <div
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(600px_300px_at_20%_20%,hsl(var(--primary)/0.15),transparent_60%)]"
        aria-hidden
      />
    </aside>
  );
}

function OnboardWizard() {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>("account");

  /**
   * `seededAdmin` = true → /admin/me returned 200 + must_change_password=true.
   * `seededAdmin` = false → admin is configured but already customized.
   * `seededAdmin` = null → /admin/me returned 401 (no admin yet); start at
   *                       Step 1.
   */
  const [seededAdmin, setSeededAdmin] = useState<boolean | null>(null);
  const [meChecked, setMeChecked] = useState(false);

  // One-shot admin detection. We never re-fetch — the wizard is a single
  // session and the gateway either has admin or it doesn't.
  const probedRef = useRef(false);
  useEffect(() => {
    if (probedRef.current) return;
    probedRef.current = true;
    (async () => {
      try {
        const me = await getSession();
        if (me) {
          const seeded = me.must_change_password === true;
          setSeededAdmin(seeded);
          // Skip Step 1 whenever an admin already exists — only difference
          // is whether we surface the "using default" hint.
          setStep("handoff");
        } else {
          setSeededAdmin(null);
        }
      } catch {
        // /admin/me hiccup — fall through to the classic flow.
        setSeededAdmin(null);
      } finally {
        setMeChecked(true);
      }
    })();
  }, []);

  return (
    <div className="w-full max-w-md space-y-6">
      <div className="space-y-1.5 md:hidden">
        <BrandMark />
      </div>
      {seededAdmin === true && step !== "account" ? (
        <div
          className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-xs text-tp-ink-3"
          data-testid="onboard-default-admin-hint"
        >
          {t("auth.onboardUsingDefaultAdmin")}
        </div>
      ) : null}
      <StepIndicator current={step} />
      {seededAdmin !== null && step !== "account" ? (
        <button
          type="button"
          onClick={() => setStep("account")}
          className="text-xs text-tp-ink-3 underline-offset-4 hover:underline"
          data-testid="onboard-customize-admin"
        >
          {t("auth.onboardCustomizeAdmin")}
        </button>
      ) : null}
      {step === "account" && (
        <AccountStep
          // If admin was already seeded the wizard treats Step 1 as
          // optional — let the user back out into Step 2.
          onSkip={
            seededAdmin !== null ? () => setStep("handoff") : undefined
          }
          onDone={() => setStep("handoff")}
        />
      )}
      {step === "handoff" && <HandoffStep />}
      <p className="text-center text-xs text-tp-ink-3">
        {t("auth.onboardHint")}
      </p>
      {/* `meChecked` is observed by tests via data attribute; a no-op DOM hook
          keeps it side-effect-free in production. */}
      <span hidden data-testid="onboard-me-checked" data-checked={meChecked} />
    </div>
  );
}

function StepIndicator({ current }: { current: Step }) {
  const { t } = useTranslation();
  const steps: { key: Step; label: string }[] = [
    { key: "account", label: t("auth.onboardStepAccount") },
    { key: "handoff", label: t("auth.onboardStepHandoff") },
  ];
  const idx = steps.findIndex((s) => s.key === current);
  return (
    <ol className="flex items-center gap-2 text-xs">
      {steps.map((s, i) => (
        <li key={s.key} className="flex items-center gap-2">
          <span
            className={`inline-flex h-6 w-6 items-center justify-center rounded-full border ${
              i <= idx
                ? "border-primary bg-primary text-primary-foreground"
                : "border-tp-glass-edge text-tp-ink-3"
            }`}
          >
            {i + 1}
          </span>
          <span
            className={
              i === idx ? "font-medium" : "text-tp-ink-3 hidden md:inline"
            }
          >
            {s.label}
          </span>
          {i < steps.length - 1 && (
            <span aria-hidden className="mx-1 text-tp-ink-3">
              →
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Step 1: Account
// ---------------------------------------------------------------------------

function AccountStep({
  onDone,
  onSkip,
}: {
  onDone: () => void;
  /** When admin is already seeded, the wizard exposes a back-out link. */
  onSkip?: () => void;
}) {
  const { t } = useTranslation();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (password.length < MIN_PASSWORD_LEN) {
      setError(t("auth.onboardWeakPassword", { min: MIN_PASSWORD_LEN }));
      return;
    }
    if (password !== confirm) {
      setError(t("auth.onboardPasswordMismatch"));
      return;
    }
    setSubmitting(true);
    try {
      await onboard({ username, password });
      onDone();
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 409) {
          setError(t("auth.onboardAlreadyConfigured"));
          setTimeout(() => router.replace("/login"), 1500);
        } else if (err.status === 422) {
          setError(t("auth.onboardWeakPassword", { min: MIN_PASSWORD_LEN }));
        } else {
          setError(err.message);
        }
      } else {
        setError(String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.onboardTitle")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("auth.onboardSubtitle")}</p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
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
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="confirm">{t("auth.onboardConfirmPassword")}</Label>
          <Input
            id="confirm"
            name="confirm"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={submitting}
          />
        </div>
        {error ? (
          <p
            role="alert"
            className="text-sm text-destructive"
            data-testid="onboard-error"
          >
            {error}
          </p>
        ) : null}
        <div className="flex gap-2">
          {onSkip ? (
            <Button
              type="button"
              variant="outline"
              onClick={onSkip}
              data-testid="account-skip"
            >
              {t("auth.onboardBack")}
            </Button>
          ) : null}
          <Button
            type="submit"
            className="flex-1"
            disabled={submitting}
          >
            {submitting ? t("auth.submitting") : t("auth.onboardSubmit")}
          </Button>
        </div>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 2: Handoff — three provider-setup cards + "use mock provider" escape
// ---------------------------------------------------------------------------

interface HandoffCardDef {
  href: string;
  testid: string;
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  body: string;
}

function HandoffStep() {
  const { t } = useTranslation();
  const router = useRouter();
  const [skipping, setSkipping] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSkip() {
    setError(null);
    setSkipping(true);
    try {
      await finalizeSkipOnboard();
      router.push("/admin");
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        setError(t("auth.onboardFinalizeError", { detail: err.message }));
      } else {
        setError(String(err));
      }
      setSkipping(false);
    }
  }

  const cards: HandoffCardDef[] = [
    {
      href: "/admin/credentials",
      testid: "onboard-handoff-credentials",
      icon: KeyRound,
      title: t("auth.onboardHandoffCredentialsTitle"),
      body: t("auth.onboardHandoffCredentialsBody"),
    },
    {
      href: "/admin/providers",
      testid: "onboard-handoff-providers",
      icon: Plug,
      title: t("auth.onboardHandoffProvidersTitle"),
      body: t("auth.onboardHandoffProvidersBody"),
    },
    {
      href: "/admin/credentials#oauth",
      testid: "onboard-handoff-oauth",
      icon: LogIn,
      title: t("auth.onboardHandoffOAuthTitle"),
      body: t("auth.onboardHandoffOAuthBody"),
    },
  ];

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.onboardHandoffTitle")}
        </h1>
        <p className="text-sm text-tp-ink-3">
          {t("auth.onboardHandoffSubtitle")}
        </p>
      </div>
      <div className="space-y-3" data-testid="onboard-handoff-cards">
        {cards.map((c) => {
          const Icon = c.icon;
          return (
            <Card key={c.href} data-testid={c.testid}>
              <CardHeader className="pb-2">
                <div className="flex items-center gap-2">
                  <Icon className="h-4 w-4 text-tp-ink-3" aria-hidden />
                  <CardTitle className="text-base">{c.title}</CardTitle>
                </div>
                <CardDescription>{c.body}</CardDescription>
              </CardHeader>
              <CardContent className="pt-0">
                <Button
                  asChild
                  variant="outline"
                  size="sm"
                  data-testid={`${c.testid}-go`}
                >
                  <Link href={c.href as never}>
                    {t("auth.onboardHandoffGo")}
                  </Link>
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>
      {error ? (
        <p
          role="alert"
          className="text-sm text-destructive"
          data-testid="onboard-error"
        >
          {error}
        </p>
      ) : null}
      <div className="rounded-md border border-tp-glass-edge bg-tp-glass-inner p-3">
        <Button
          type="button"
          variant="secondary"
          className="w-full font-semibold"
          onClick={onSkip}
          disabled={skipping}
          data-testid="onboard-skip-mock"
        >
          {skipping ? t("auth.submitting") : t("auth.onboardSkipLlm")}
        </Button>
        <p className="mt-2 text-xs text-tp-ink-3">
          {t("auth.onboardSkipHint")}
        </p>
      </div>
    </>
  );
}
