"use client";

/**
 * GettingStartedCard — dashboard "zero → usable chat model" checklist
 * (PR5 provider-setup flow).
 *
 * Mounted near the top of the dashboard while `useSetupStatus()` says the
 * deployment isn't chat-ready. Three live check items (provider → models
 * → default), a primary CTA that opens the guided ProviderSetupFlow in a
 * dialog, and a secondary link to /models. Dismissable via localStorage
 * (`corlinman_getting_started_dismissed`); auto-hides once configured.
 */

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { ArrowUpRight, Check, Rocket, X } from "@/components/icons";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ProviderSetupFlow } from "@/components/model-hub/provider-setup-flow";
import { useSetupStatus } from "@/lib/hooks/use-setup-status";
import { cn } from "@/lib/utils";

export const GETTING_STARTED_DISMISS_KEY = "corlinman_getting_started_dismissed";

function readDismissed(): boolean {
  try {
    return window.localStorage.getItem(GETTING_STARTED_DISMISS_KEY) === "1";
  } catch {
    return false;
  }
}

export function GettingStartedCard() {
  const { t } = useTranslation();
  const status = useSetupStatus();
  const [dismissed, setDismissed] = React.useState<boolean>(() =>
    typeof window === "undefined" ? true : readDismissed(),
  );
  const [flowOpen, setFlowOpen] = React.useState(false);

  // Also hide on `errored`: when the config surface is unreachable/503,
  // useSetupStatus reports configured=false with empty data, which would
  // otherwise show a permanently-uncompletable checklist whose CTA opens
  // a flow that can only fail at probe (self-review P3).
  if (dismissed || status.loading || status.errored || status.configured)
    return null;

  const items: Array<{ key: string; label: string; done: boolean }> = [
    {
      key: "provider",
      label: t("gettingStarted.itemProvider"),
      done: status.hasProvider,
    },
    {
      key: "models",
      label: t("gettingStarted.itemModels"),
      done: status.hasAliases,
    },
    {
      key: "default",
      label: t("gettingStarted.itemDefault"),
      done: status.hasDefault,
    },
  ];

  return (
    <section
      className="relative flex flex-col gap-3 rounded-sg-lg border border-sg-accent/25 bg-sg-card p-4 shadow-sg-2"
      data-testid="getting-started-card"
    >
      <button
        type="button"
        onClick={() => {
          try {
            window.localStorage.setItem(GETTING_STARTED_DISMISS_KEY, "1");
          } catch {
            // localStorage unavailable — dismiss for this session only.
          }
          setDismissed(true);
        }}
        className="absolute right-3 top-3 inline-flex h-6 w-6 items-center justify-center rounded-md text-sg-ink-4 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
        aria-label={t("gettingStarted.dismiss")}
        data-testid="getting-started-dismiss"
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </button>

      <div className="flex items-center gap-2">
        <Rocket className="h-4 w-4 text-sg-accent" aria-hidden />
        <h2 className="text-sm font-semibold">{t("gettingStarted.title")}</h2>
      </div>
      <p className="text-xs text-sg-ink-3">{t("gettingStarted.body")}</p>

      <ol className="flex flex-col gap-1.5">
        {items.map((item, i) => (
          <li
            key={item.key}
            className="flex items-center gap-2 text-xs"
            data-testid={`getting-started-item-${item.key}`}
            data-done={item.done}
          >
            <span
              className={cn(
                "inline-flex h-5 w-5 items-center justify-center rounded-full border text-[10px] font-semibold",
                item.done
                  ? "border-transparent bg-sg-ok-soft text-sg-ok"
                  : "border-sg-border bg-sg-inset text-sg-ink-4",
              )}
            >
              {item.done ? (
                <Check className="h-3 w-3" aria-hidden />
              ) : (
                i + 1
              )}
            </span>
            <span
              className={item.done ? "text-sg-ink-4 line-through" : "text-sg-ink-2"}
            >
              {item.label}
            </span>
          </li>
        ))}
      </ol>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          onClick={() => setFlowOpen(true)}
          data-testid="getting-started-cta"
        >
          {t("setupFlow.quickSetup")}
        </Button>
        <Link
          href={"/models" as never}
          className="inline-flex items-center gap-1 text-xs text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline"
          data-testid="getting-started-link"
        >
          {t("gettingStarted.goToModels")}
          <ArrowUpRight className="h-3 w-3" aria-hidden />
        </Link>
      </div>

      <Dialog open={flowOpen} onOpenChange={setFlowOpen}>
        <DialogContent className="max-w-md" data-testid="getting-started-dialog">
          <DialogHeader>
            <DialogTitle>{t("setupFlow.dialogTitle")}</DialogTitle>
            <DialogDescription>{t("setupFlow.dialogDesc")}</DialogDescription>
          </DialogHeader>
          <ProviderSetupFlow
            variant="dialog"
            onComplete={() => setFlowOpen(false)}
          />
        </DialogContent>
      </Dialog>
    </section>
  );
}
