"use client";

/**
 * First-run onboarding wizard — 6-step reshape (2026-05-28).
 *
 * Step order (locked — see docs/PLAN_FIRST_RUN_WIZARD.md):
 *   1. API config        (skippable, handoff to /admin/credentials)
 *   2. Change username   (required)
 *   3. Change password   (required, gated — once past, can't go back to step 2)
 *   4. Persona           (default / custom / skip)
 *   5. Image provider    (reuse / separate / skip)
 *   6. Done              (handoff to /admin)
 *
 * Gating rules:
 *   - The step indicator only allows clicking *back* to a completed step,
 *     never forward to one that hasn't been reached.
 *   - After step 3 completes, the indicator visually locks steps 1 + 2 so
 *     the operator can't try to rewind the username change after the
 *     password rotation (atomicity story for the "先改账号 再改密码" rule).
 *   - A persona "custom" choice records a deferred `/persona` redirect that
 *     fires at the end of the wizard so the operator still configures the
 *     image provider first.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import {
  ArrowRight,
  Check,
  Image as ImageIcon,
  KeyRound,
  Lock,
  Plug,
  Sparkles,
  User,
} from "lucide-react";

import { getSession } from "@/lib/auth";
import {
  CorlinmanApiError,
  finalizeOnboardAccount,
  finalizeOnboardImageProvider,
  finalizeOnboardPassword,
  finalizeOnboardPersona,
  type OnboardImageProviderSpec,
  type OnboardPersonaChoice,
} from "@/lib/api";
import { BrandMark } from "@/components/layout/brand-mark";
import { Mascot } from "@/components/ui/mascot";
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
import { cn } from "@/lib/utils";
import { springs, useMotionVariants } from "@/lib/motion";

const MIN_PASSWORD_LEN = 8;

// ---------------------------------------------------------------------------
// Step machine
// ---------------------------------------------------------------------------

type StepId = 1 | 2 | 3 | 4 | 5 | 6;

interface StepDef {
  id: StepId;
  /** i18n key for the indicator label. */
  i18nKey: string;
  /** Fallback used when the locale entry isn't ready yet. */
  fallback: string;
  icon: React.ComponentType<{ className?: string }>;
}

const STEPS: readonly StepDef[] = [
  { id: 1, i18nKey: "onboard.step.api.title", fallback: "配置 API", icon: Plug },
  {
    id: 2,
    i18nKey: "onboard.step.username.title",
    fallback: "修改默认账号",
    icon: User,
  },
  {
    id: 3,
    i18nKey: "onboard.step.password.title",
    fallback: "修改默认密码",
    icon: Lock,
  },
  {
    id: 4,
    i18nKey: "onboard.step.persona.title",
    fallback: "助手个性化",
    icon: Sparkles,
  },
  {
    id: 5,
    i18nKey: "onboard.step.image.title",
    fallback: "图片生成 API",
    icon: ImageIcon,
  },
  {
    id: 6,
    i18nKey: "auth.onboardFinish",
    fallback: "完成",
    icon: Check,
  },
] as const;

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function OnboardPage() {
  return (
    <div className="relative grid min-h-dvh grid-cols-1 md:grid-cols-[40%_60%]">
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
  const variants = useMotionVariants();
  return (
    <aside className="relative hidden overflow-hidden border-r border-sg-border md:flex md:flex-col md:justify-between md:p-10">
      {/* Deep-space showcase — nebula glows + grain over the <html> gradient. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 sg-drift lg-hue-drift"
        style={{
          backgroundImage:
            "radial-gradient(760px 520px at 18% 12%, var(--sg-nebula-1), transparent 60%), " +
            "radial-gradient(620px 480px at 88% 30%, var(--sg-nebula-2), transparent 62%), " +
            "radial-gradient(560px 420px at 40% 104%, var(--sg-nebula-3), transparent 64%)",
        }}
      />
      {/* Twinkling starfield (dark theme only — hidden in daylight via CSS). */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 lg-stars"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 sg-noise opacity-[0.03]"
      />

      <div className="relative z-10 flex items-center gap-2">
        <BrandMark />
      </div>
      <motion.div
        className="relative z-10 space-y-5"
        variants={variants.liquidRise}
        initial="hidden"
        animate="visible"
      >
        <Mascot size={148} className="-ml-2" />
        <h2 className="sg-grad-text text-3xl font-semibold tracking-tight">
          {t("auth.onboardHeroTitle")}
        </h2>
        <p className="max-w-xs text-sm leading-relaxed text-sg-ink-3">
          {t("auth.onboardHeroBody")}
        </p>
      </motion.div>
      <div className="relative z-10 flex items-center gap-2 text-xs text-sg-ink-5">
        <span className="font-mono">v0.6.0</span>
        <span>·</span>
        <span>{t("auth.onboardBuildLabel")}</span>
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Wizard shell — owns step state + transitions
// ---------------------------------------------------------------------------

function OnboardWizard() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const router = useRouter();

  const [step, setStep] = useState<StepId>(1);
  const [completed, setCompleted] = useState<Set<StepId>>(() => new Set());
  /**
   * Once true, steps 1 + 2 are no longer reachable via the indicator (the
   * password rotation has happened and rewinding the username flow would be
   * confusing — the system would happily change it again but operators
   * read this as a fresh password challenge).
   */
  const [lockedPastPassword, setLockedPastPassword] = useState(false);
  /** Deferred redirect from persona step 4 — fires after step 6. */
  const [pendingRedirect, setPendingRedirect] = useState<string | null>(null);

  // One-shot session probe — same approach as the legacy onboard page. Lets
  // us short-circuit when the gateway already has a customized admin.
  const probedRef = useRef(false);
  const [meChecked, setMeChecked] = useState(false);
  useEffect(() => {
    if (probedRef.current) return;
    probedRef.current = true;
    (async () => {
      try {
        await getSession();
      } catch {
        // Network hiccup — proceed; the first finalize call will surface it.
      } finally {
        setMeChecked(true);
      }
    })();
  }, []);

  const markCompleted = (id: StepId) => {
    setCompleted((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  };

  const goTo = (id: StepId) => {
    // Only allow backward moves to a completed step, never forward beyond
    // what's been done.
    if (id === step) return;
    if (id > step) return;
    if (lockedPastPassword && id <= 2) return;
    if (!completed.has(id) && id < step) {
      // Skipped step (e.g. step 1 if the user clicked "skip API"): treat as
      // re-entry only if everything *after* it remains incomplete.
      setStep(id);
      return;
    }
    setStep(id);
  };

  const advance = (from: StepId) => {
    markCompleted(from);
    const nextId = (from + 1) as StepId;
    setStep(nextId);
  };

  function handleFinish() {
    markCompleted(6);
    if (pendingRedirect) {
      router.push(pendingRedirect as never);
      return;
    }
    router.push("/admin");
  }

  return (
    <div className="w-full max-w-md space-y-6">
      <div className="space-y-1.5 md:hidden">
        <BrandMark />
      </div>
      <StepIndicator
        current={step}
        completed={completed}
        lockedPastPassword={lockedPastPassword}
        onGoTo={goTo}
      />

      {/* Step content cascades in on each swap — keyed by step so the spring
          re-fires as the operator advances. No exit choreography (a held exit
          would gate the next step's mount + the step-machine flow). */}
      <motion.div
        key={step}
        className="space-y-6"
        variants={variants.liquidRise}
        initial="hidden"
        animate="visible"
      >
        {step === 1 && (
          <ApiConfigStep
            onSkip={() => advance(1)}
            onContinue={() => advance(1)}
          />
        )}
        {step === 2 && <UsernameStep onDone={() => advance(2)} />}
        {step === 3 && (
          <PasswordStep
            onDone={() => {
              setLockedPastPassword(true);
              advance(3);
            }}
          />
        )}
        {step === 4 && (
          <PersonaStep
            onDone={(redirect) => {
              if (redirect) setPendingRedirect(redirect);
              advance(4);
            }}
          />
        )}
        {step === 5 && <ImageProviderStep onDone={() => advance(5)} />}
        {step === 6 && <DoneStep onFinish={handleFinish} />}
      </motion.div>

      <p className="text-center text-xs text-sg-ink-5">
        {t("auth.onboardHint")}
      </p>
      <span hidden data-testid="onboard-me-checked" data-checked={meChecked} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step indicator — 6 numbered dots; backward-clickable when completed.
// ---------------------------------------------------------------------------

function StepIndicator({
  current,
  completed,
  lockedPastPassword,
  onGoTo,
}: {
  current: StepId;
  completed: Set<StepId>;
  lockedPastPassword: boolean;
  onGoTo: (id: StepId) => void;
}) {
  const { t } = useTranslation();
  return (
    <ol
      className="flex items-center justify-between gap-1"
      data-testid="onboard-stepper"
    >
      {STEPS.map((s, i) => {
        const isCurrent = s.id === current;
        const isDone = completed.has(s.id);
        const isReachable =
          isDone &&
          s.id < current &&
          !(lockedPastPassword && s.id <= 2);
        const Icon = s.icon;
        return (
          <li key={s.id} className="flex flex-1 items-center">
            <button
              type="button"
              onClick={() => (isReachable ? onGoTo(s.id) : undefined)}
              disabled={!isReachable}
              title={t(s.i18nKey, { defaultValue: s.fallback })}
              aria-current={isCurrent ? "step" : undefined}
              aria-label={t(s.i18nKey, { defaultValue: s.fallback })}
              data-testid={`onboard-step-${s.id}`}
              data-state={
                isCurrent ? "current" : isDone ? "done" : "pending"
              }
              className={cn(
                "group flex items-center gap-2 rounded-sg-sm px-1 py-1 transition-colors",
                isReachable
                  ? "cursor-pointer hover:bg-sg-inset-hover"
                  : "cursor-default",
              )}
            >
              <span
                className={cn(
                  "relative inline-flex h-7 w-7 items-center justify-center rounded-full border text-xs font-semibold transition-all",
                  isCurrent &&
                    "border-sg-accent bg-sg-accent text-white shadow-sg-glow",
                  !isCurrent &&
                    isDone &&
                    "border-transparent bg-sg-accent text-white",
                  !isCurrent &&
                    !isDone &&
                    "border-sg-border bg-sg-inset text-sg-ink-4",
                )}
              >
                {/* Shared-layout active ring — springs between steps. */}
                {isCurrent ? (
                  <motion.span
                    aria-hidden
                    layoutId="onboard-step-active"
                    transition={springs.snappy}
                    className="pointer-events-none absolute -inset-1 rounded-full ring-2 ring-sg-accent ring-offset-1 ring-offset-transparent"
                  />
                ) : null}
                {isDone && !isCurrent ? (
                  <Check className="h-3.5 w-3.5" aria-hidden />
                ) : (
                  <Icon className="relative h-3.5 w-3.5" aria-hidden />
                )}
              </span>
              <span
                className={cn(
                  "hidden text-xs lg:inline",
                  isCurrent ? "font-medium text-sg-ink" : "text-sg-ink-4",
                )}
              >
                {t(s.i18nKey, { defaultValue: s.fallback })}
              </span>
            </button>
            {i < STEPS.length - 1 ? (
              <span
                aria-hidden
                className={cn(
                  "mx-1 h-px flex-1 transition-colors",
                  completed.has(s.id) ? "bg-sg-accent/40" : "bg-sg-border",
                )}
              />
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — API config (skippable). Reuses the existing /admin/credentials
// + /admin/providers + OAuth surfaces. No backend call from this page; the
// user comes back here when done (we just need them to acknowledge or skip).
// ---------------------------------------------------------------------------

interface HandoffCardDef {
  href: string;
  testid: string;
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  body: string;
}

function ApiConfigStep({
  onContinue,
  onSkip,
}: {
  onContinue: () => void;
  onSkip: () => void;
}) {
  const { t } = useTranslation();
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
  ];

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
          {t("onboard.step.api.title", { defaultValue: "配置 API" })}
        </h1>
        <p className="text-sm text-sg-ink-3">
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
                  <Icon className="h-4 w-4 text-sg-accent" aria-hidden />
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
                  <Link
                    href={c.href as never}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t("auth.onboardHandoffGo")}
                  </Link>
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>
      <div className="flex gap-2">
        <Button
          type="button"
          variant="outline"
          onClick={onSkip}
          data-testid="onboard-api-skip"
          className="flex-1"
        >
          {t("auth.onboardSkipLlm", { defaultValue: "暂时跳过" })}
        </Button>
        <Button
          type="button"
          onClick={onContinue}
          data-testid="onboard-api-continue"
          className="flex-1"
        >
          {t("auth.onboardNext")}
          <ArrowRight className="ml-1 h-4 w-4" aria-hidden />
        </Button>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — Change username (required).
// ---------------------------------------------------------------------------

function UsernameStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const [newUsername, setNewUsername] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (!newUsername.trim()) {
      setError(t("account.security.invalidUsername", { defaultValue: "用户名不可为空" }));
      return;
    }
    setSubmitting(true);
    try {
      await finalizeOnboardAccount(newUsername.trim());
      onDone();
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 409) {
          setError(
            t("onboard.step.username.unchanged", {
              defaultValue: "新用户名不能与当前用户名相同",
            }),
          );
        } else if (err.status === 422) {
          setError(t("auth.invalidUsername", { defaultValue: "用户名格式不合法" }));
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
        <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
          {t("onboard.step.username.title", { defaultValue: "修改默认账号" })}
        </h1>
        <p className="text-sm text-sg-ink-3">
          {t("onboard.step.username.subtitle", {
            defaultValue: "默认账号是 admin，请改成你自己的用户名。",
          })}
        </p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="new-username">
            {t("account.security.newUsername", { defaultValue: "新用户名" })}
          </Label>
          <Input
            id="new-username"
            name="new-username"
            autoComplete="username"
            required
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            disabled={submitting}
            data-testid="onboard-username-input"
          />
          <p className="text-xs text-sg-ink-4">
            {t("account.security.usernameRule", {
              defaultValue: "小写字母、数字、_ 或 -；最多 64 字符",
            })}
          </p>
        </div>
        {error ? (
          <p
            role="alert"
            className="text-sm text-sg-err"
            data-testid="onboard-error"
          >
            {error}
          </p>
        ) : null}
        <Button
          type="submit"
          className="w-full"
          disabled={submitting}
          data-testid="onboard-username-submit"
        >
          {submitting ? t("auth.submitting") : t("auth.onboardNext")}
        </Button>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 3 — Change password (required, gated). Once this completes the
// indicator locks step 1 + 2.
// ---------------------------------------------------------------------------

function PasswordStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (newPassword.length < MIN_PASSWORD_LEN) {
      setError(t("auth.onboardWeakPassword", { min: MIN_PASSWORD_LEN }));
      return;
    }
    if (newPassword !== confirm) {
      setError(t("auth.onboardPasswordMismatch"));
      return;
    }
    setSubmitting(true);
    try {
      await finalizeOnboardPassword(oldPassword, newPassword);
      onDone();
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 401) {
          setError(t("auth.invalidOldPassword", { defaultValue: "当前密码不正确" }));
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
        <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
          {t("onboard.step.password.title", { defaultValue: "修改默认密码" })}
        </h1>
        <p className="text-sm text-sg-ink-3">
          {t("onboard.step.password.subtitle", {
            defaultValue: "默认密码是 root。请设置一个安全的新密码。",
          })}
        </p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="old-password">
            {t("account.security.currentPassword", { defaultValue: "当前密码" })}
          </Label>
          <Input
            id="old-password"
            name="old-password"
            type="password"
            autoComplete="current-password"
            required
            value={oldPassword}
            onChange={(e) => setOldPassword(e.target.value)}
            disabled={submitting}
            data-testid="onboard-old-password"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="new-password">
            {t("account.security.newPassword", { defaultValue: "新密码" })}
          </Label>
          <Input
            id="new-password"
            name="new-password"
            type="password"
            autoComplete="new-password"
            required
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            disabled={submitting}
            data-testid="onboard-new-password"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="confirm-password">
            {t("auth.onboardConfirmPassword")}
          </Label>
          <Input
            id="confirm-password"
            name="confirm-password"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={submitting}
            data-testid="onboard-confirm-password"
          />
        </div>
        {error ? (
          <p
            role="alert"
            className="text-sm text-sg-err"
            data-testid="onboard-error"
          >
            {error}
          </p>
        ) : null}
        <Button
          type="submit"
          className="w-full"
          disabled={submitting}
          data-testid="onboard-password-submit"
        >
          {submitting ? t("auth.submitting") : t("auth.onboardNext")}
        </Button>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 4 — Persona choice (3 cards).
// ---------------------------------------------------------------------------

interface PersonaCardSpec {
  choice: OnboardPersonaChoice;
  testid: string;
  i18nKey: string;
  fallback: string;
  body: string;
  icon: React.ComponentType<{ className?: string }>;
}

function PersonaStep({
  onDone,
}: {
  onDone: (redirect: string | null) => void;
}) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = useState<OnboardPersonaChoice | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const cards: PersonaCardSpec[] = useMemo(
    () => [
      {
        choice: "default",
        testid: "onboard-persona-default",
        i18nKey: "onboard.persona.choice.default",
        fallback: "使用默认助手 grantley",
        body: t("onboard.persona.choice.defaultBody", {
          defaultValue: "使用内置助手 grantley，无需任何额外配置。",
        }),
        icon: Sparkles,
      },
      {
        choice: "custom",
        testid: "onboard-persona-custom",
        i18nKey: "onboard.persona.choice.custom",
        fallback: "创建自定义人格",
        body: t("onboard.persona.choice.customBody", {
          defaultValue: "通过 /persona 引导式问答创建专属人格（稍后跳转）。",
        }),
        icon: User,
      },
      {
        choice: "skip",
        testid: "onboard-persona-skip",
        i18nKey: "onboard.persona.choice.skip",
        fallback: "暂时跳过",
        body: t("onboard.persona.choice.skipBody", {
          defaultValue: "稍后再在管理面板中配置人格。",
        }),
        icon: ArrowRight,
      },
    ],
    [t],
  );

  async function pick(choice: OnboardPersonaChoice) {
    setError(null);
    setSubmitting(choice);
    try {
      const res = await finalizeOnboardPersona(choice);
      onDone(res.redirect ?? null);
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        setError(err.message);
      } else {
        setError(String(err));
      }
      setSubmitting(null);
    }
  }

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
          {t("onboard.step.persona.title", { defaultValue: "助手个性化" })}
        </h1>
        <p className="text-sm text-sg-ink-3">
          {t("onboard.step.persona.subtitle", {
            defaultValue: "选择如何初始化助手人格。",
          })}
        </p>
      </div>
      <div className="space-y-3">
        {cards.map((c) => {
          const Icon = c.icon;
          const isLoading = submitting === c.choice;
          return (
            <button
              key={c.choice}
              type="button"
              onClick={() => pick(c.choice)}
              disabled={submitting !== null}
              data-testid={c.testid}
              className={cn(
                "lg-edge lg-sheen lg-gel sg-card relative block w-full overflow-hidden rounded-sg-lg p-4 text-left shadow-sg-2 transition-all duration-200",
                submitting === null &&
                  "hover:-translate-y-px hover:border-sg-accent/30 hover:shadow-sg-3",
                submitting !== null && submitting !== c.choice && "opacity-50",
                "disabled:cursor-wait",
              )}
            >
              <div className="flex items-start gap-3">
                <span className="sg-inset mt-0.5 inline-flex h-8 w-8 items-center justify-center rounded-sg-sm text-sg-accent">
                  <Icon className="h-4 w-4" aria-hidden />
                </span>
                <div className="flex-1 space-y-0.5">
                  <div className="text-sm font-medium text-sg-ink">
                    {t(c.i18nKey, { defaultValue: c.fallback })}
                  </div>
                  <div className="text-xs text-sg-ink-3">{c.body}</div>
                </div>
                {isLoading ? (
                  <span className="text-xs text-sg-ink-4">
                    {t("auth.submitting")}
                  </span>
                ) : null}
              </div>
            </button>
          );
        })}
      </div>
      {error ? (
        <p
          role="alert"
          className="text-sm text-sg-err"
          data-testid="onboard-error"
        >
          {error}
        </p>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 5 — Image provider choice (reuse / separate / skip).
// "Reuse" can 409 with `image_not_supported`; we swap to a fallback card.
// ---------------------------------------------------------------------------

type ImageSubview = "choices" | "reuseUnsupported" | "separateForm";

function ImageProviderStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [subview, setSubview] = useState<ImageSubview>("choices");

  async function pickSkip() {
    setError(null);
    setSubmitting("skip");
    try {
      await finalizeOnboardImageProvider({ choice: "skip" });
      onDone();
    } catch (err) {
      setError(
        err instanceof CorlinmanApiError ? err.message : String(err),
      );
      setSubmitting(null);
    }
  }

  async function pickReuse() {
    setError(null);
    setSubmitting("reuse");
    try {
      // The wizard does not know which provider was just configured in step 1
      // (the user may have skipped). The backend resolves "current" itself:
      // we pass an empty string so the server falls back to the active
      // chat-default provider.
      await finalizeOnboardImageProvider({
        choice: "reuse",
        provider_name: "",
      });
      onDone();
    } catch (err) {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        setSubview("reuseUnsupported");
      } else {
        setError(
          err instanceof CorlinmanApiError ? err.message : String(err),
        );
      }
      setSubmitting(null);
    }
  }

  async function pickSeparate(spec: OnboardImageProviderSpec) {
    setError(null);
    setSubmitting("separate");
    try {
      await finalizeOnboardImageProvider({ choice: "separate", spec });
      onDone();
    } catch (err) {
      setError(
        err instanceof CorlinmanApiError ? err.message : String(err),
      );
      setSubmitting(null);
    }
  }

  if (subview === "separateForm") {
    return (
      <SeparateImageProviderForm
        submitting={submitting === "separate"}
        error={error}
        onCancel={() => {
          setError(null);
          setSubview("choices");
        }}
        onSubmit={pickSeparate}
      />
    );
  }

  if (subview === "reuseUnsupported") {
    return (
      <>
        <div className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
            {t("onboard.step.image.title", { defaultValue: "图片生成 API" })}
          </h1>
        </div>
        <Card data-testid="onboard-image-unsupported">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base text-sg-err">
              <span aria-hidden>❌</span>
              {t("onboard.image.notSupported", {
                defaultValue: "当前 API 不支持图片生成",
              })}
            </CardTitle>
            <CardDescription>
              {t("onboard.image.notSupportedHint", {
                defaultValue:
                  "你当前的 API 端点未发现图片模型。你可以单独配置一个图片 Provider，或者跳过这一步。",
              })}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex gap-2 pt-0">
            <Button
              type="button"
              variant="outline"
              onClick={pickSkip}
              disabled={submitting !== null}
              data-testid="onboard-image-unsupported-skip"
              className="flex-1"
            >
              {t("onboard.image.choice.skip", { defaultValue: "跳过" })}
            </Button>
            <Button
              type="button"
              onClick={() => setSubview("separateForm")}
              disabled={submitting !== null}
              data-testid="onboard-image-unsupported-separate"
              className="flex-1"
            >
              {t("onboard.image.choice.separate", {
                defaultValue: "单独配置",
              })}
            </Button>
          </CardContent>
        </Card>
      </>
    );
  }

  const cards: {
    key: string;
    testid: string;
    i18nKey: string;
    fallback: string;
    body: string;
    icon: React.ComponentType<{ className?: string }>;
    onClick: () => void;
  }[] = [
    {
      key: "reuse",
      testid: "onboard-image-reuse",
      i18nKey: "onboard.image.choice.reuse",
      fallback: "复用当前 API",
      body: t("onboard.image.choice.reuseBody", {
        defaultValue: "尝试用刚刚配置的 LLM Provider 来生成图片。",
      }),
      icon: Plug,
      onClick: pickReuse,
    },
    {
      key: "separate",
      testid: "onboard-image-separate",
      i18nKey: "onboard.image.choice.separate",
      fallback: "单独配置",
      body: t("onboard.image.choice.separateBody", {
        defaultValue: "为图片生成另起一个 Provider（例如 OpenAI gpt-image-1）。",
      }),
      icon: ImageIcon,
      onClick: () => setSubview("separateForm"),
    },
    {
      key: "skip",
      testid: "onboard-image-skip",
      i18nKey: "onboard.image.choice.skip",
      fallback: "跳过",
      body: t("onboard.image.choice.skipBody", {
        defaultValue: "稍后再配置图片生成。",
      }),
      icon: ArrowRight,
      onClick: pickSkip,
    },
  ];

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
          {t("onboard.step.image.title", { defaultValue: "图片生成 API" })}
        </h1>
        <p className="text-sm text-sg-ink-3">
          {t("onboard.step.image.subtitle", {
            defaultValue: "选择如何处理图片生成请求。",
          })}
        </p>
      </div>
      <div className="space-y-3">
        {cards.map((c) => {
          const Icon = c.icon;
          const isLoading = submitting === c.key;
          return (
            <button
              key={c.key}
              type="button"
              onClick={c.onClick}
              disabled={submitting !== null}
              data-testid={c.testid}
              className={cn(
                "lg-edge lg-sheen lg-gel sg-card relative block w-full overflow-hidden rounded-sg-lg p-4 text-left shadow-sg-2 transition-all duration-200",
                submitting === null &&
                  "hover:-translate-y-px hover:border-sg-accent/30 hover:shadow-sg-3",
                submitting !== null && submitting !== c.key && "opacity-50",
                "disabled:cursor-wait",
              )}
            >
              <div className="flex items-start gap-3">
                <span className="sg-inset mt-0.5 inline-flex h-8 w-8 items-center justify-center rounded-sg-sm text-sg-accent">
                  <Icon className="h-4 w-4" aria-hidden />
                </span>
                <div className="flex-1 space-y-0.5">
                  <div className="text-sm font-medium text-sg-ink">
                    {t(c.i18nKey, { defaultValue: c.fallback })}
                  </div>
                  <div className="text-xs text-sg-ink-3">{c.body}</div>
                </div>
                {isLoading ? (
                  <span className="text-xs text-sg-ink-4">
                    {t("auth.submitting")}
                  </span>
                ) : null}
              </div>
            </button>
          );
        })}
      </div>
      {error ? (
        <p
          role="alert"
          className="text-sm text-sg-err"
          data-testid="onboard-error"
        >
          {error}
        </p>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 5b — Separate-provider mini form (name + base_url + api_key + model)
// ---------------------------------------------------------------------------

function SeparateImageProviderForm({
  submitting,
  error,
  onCancel,
  onSubmit,
}: {
  submitting: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (spec: OnboardImageProviderSpec) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("image");
  const [baseUrl, setBaseUrl] = useState("https://api.openai.com");
  const [apiKey, setApiKey] = useState("");
  const [imageModel, setImageModel] = useState("gpt-image-1");
  const [localError, setLocalError] = useState<string | null>(null);

  function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setLocalError(null);
    if (!name.trim() || !baseUrl.trim() || !apiKey.trim()) {
      setLocalError(
        t("onboard.image.separate.required", {
          defaultValue: "名称、Base URL、API Key 都不能为空。",
        }),
      );
      return;
    }
    onSubmit({
      name: name.trim(),
      base_url: baseUrl.trim(),
      api_key: apiKey,
      image_model: imageModel.trim() || undefined,
      image_capable: true,
      kind: "openai_compatible",
    });
  }

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
          {t("onboard.image.choice.separate", { defaultValue: "单独配置" })}
        </h1>
        <p className="text-sm text-sg-ink-3">
          {t("onboard.image.separate.subtitle", {
            defaultValue: "为图片生成单独注册一个 OpenAI-compatible Provider。",
          })}
        </p>
      </div>
      <form onSubmit={submit} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="image-provider-name">
            {t("onboard.image.separate.nameLabel", { defaultValue: "名称" })}
          </Label>
          <Input
            id="image-provider-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={submitting}
            required
            data-testid="onboard-image-separate-name"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="image-provider-base-url">
            {t("onboard.image.separate.baseUrlLabel", {
              defaultValue: "Base URL",
            })}
          </Label>
          <Input
            id="image-provider-base-url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            disabled={submitting}
            required
            data-testid="onboard-image-separate-baseurl"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="image-provider-api-key">
            {t("onboard.image.separate.apiKeyLabel", {
              defaultValue: "API Key",
            })}
          </Label>
          <Input
            id="image-provider-api-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={submitting}
            required
            data-testid="onboard-image-separate-apikey"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="image-provider-model">
            {t("onboard.image.separate.modelLabel", {
              defaultValue: "图片模型 ID",
            })}
          </Label>
          <Input
            id="image-provider-model"
            value={imageModel}
            onChange={(e) => setImageModel(e.target.value)}
            disabled={submitting}
            placeholder="gpt-image-1"
            data-testid="onboard-image-separate-model"
          />
        </div>
        {(localError || error) ? (
          <p
            role="alert"
            className="text-sm text-sg-err"
            data-testid="onboard-error"
          >
            {localError || error}
          </p>
        ) : null}
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={onCancel}
            disabled={submitting}
            className="flex-1"
            data-testid="onboard-image-separate-cancel"
          >
            {t("auth.onboardBack")}
          </Button>
          <Button
            type="submit"
            disabled={submitting}
            className="flex-1"
            data-testid="onboard-image-separate-submit"
          >
            {submitting ? t("auth.submitting") : t("auth.onboardNext")}
          </Button>
        </div>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 6 — Done card.
// ---------------------------------------------------------------------------

function DoneStep({ onFinish }: { onFinish: () => void }) {
  const { t } = useTranslation();
  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-sg-ink">
          {t("auth.onboardHandoffTitle", { defaultValue: "初始化完成" })}
        </h1>
        <p className="text-sm text-sg-ink-3">
          {t("onboard.done.subtitle", {
            defaultValue: "首次配置已完成。进入管理面板继续探索功能。",
          })}
        </p>
      </div>
      <Card data-testid="onboard-done-card">
        <CardHeader className="pb-2">
          <div className="flex items-center gap-2">
            <span
              className="sg-breathe inline-flex h-8 w-8 items-center justify-center rounded-full bg-sg-ok-soft text-sg-ok"
              aria-hidden
            >
              <Check className="h-4 w-4" />
            </span>
            <CardTitle className="text-base text-sg-ink">
              {t("onboard.done.title", { defaultValue: "你已完成所有步骤" })}
            </CardTitle>
          </div>
          <CardDescription>
            {t("onboard.done.body", {
              defaultValue:
                "之后可在 账户与安全 / 凭证 / 人格 / 系统 中进一步定制。",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          <Button
            type="button"
            onClick={onFinish}
            className="w-full"
            data-testid="onboard-finish"
          >
            {t("onboard.done.cta", { defaultValue: "进入管理面板" })}
            <ArrowRight className="ml-1 h-4 w-4" aria-hidden />
          </Button>
        </CardContent>
      </Card>
    </>
  );
}
