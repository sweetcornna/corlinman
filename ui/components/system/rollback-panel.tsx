"use client";

/**
 * `<RollbackPanel>` — pick one of the last few releases and roll back
 * (sub2api's rollback list). Lives on the `/system` page below the
 * upgrade surfaces.
 *
 *   - `GET /admin/system/rollback-versions` → up to 3 releases strictly
 *     older than the running version. `instant: true` marks the kept
 *     previous version (docker keeps the swapped-out container) —
 *     restoring it needs no download.
 *   - Confirm dialog (one-click, no typed friction) → `POST
 *     /admin/system/rollback {tag}` → the caller routes to
 *     `?upgrade=<id>` where `<UpgradeProgress>` follows it exactly like
 *     an upgrade (a rollback IS an upgrade with `allow_downgrade`).
 *   - 503 (upgrader unwired / socket not mounted) hides the panel
 *     entirely — the manual copy-paste commands are the path there.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { History, Zap } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  CorlinmanApiError,
  fetchRollbackVersions,
  startRollback,
  type RollbackVersionsResponse,
  type UpgradeStartResponse,
} from "@/lib/api";

export interface RollbackPanelProps {
  /** Fires with the 202 response — the host routes to `?upgrade=<id>`. */
  onRollbackStarted: (res: UpgradeStartResponse) => void;
}

export function RollbackPanel({ onRollbackStarted }: RollbackPanelProps) {
  const { t } = useTranslation();
  const [confirmTag, setConfirmTag] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  const q = useQuery<RollbackVersionsResponse>({
    queryKey: ["admin", "system", "rollback-versions"],
    queryFn: fetchRollbackVersions,
    staleTime: 60_000,
    retry: false,
  });

  // Unwired backend (503/404 on older gateways) or nothing to roll back
  // to → render nothing; the manual commands section covers those boxes.
  if (q.isError || !q.data || q.data.versions.length === 0) return null;
  const { current, versions } = q.data;

  const handleConfirm = async () => {
    if (!confirmTag || submitting) return;
    setSubmitting(true);
    try {
      const res = await startRollback(confirmTag);
      setConfirmTag(null);
      onRollbackStarted(res);
    } catch (err) {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        toast.error(t("system.rollback.alreadyRunning"));
      } else {
        toast.error(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section
      data-testid="rollback-panel"
      className="space-y-3 rounded-lg border border-sg-border bg-sg-card p-4 sm:p-6"
    >
      <header className="space-y-0.5">
        <h2 className="flex items-center gap-2 text-base font-semibold tracking-tight">
          <History className="h-4 w-4 text-sg-ink-3" aria-hidden />
          {t("system.rollback.title")}
        </h2>
        <p className="text-xs text-sg-ink-3">
          {t("system.rollback.subtitle", { current })}
        </p>
      </header>

      <ul className="divide-y divide-sg-border rounded-md border border-sg-border">
        {versions.map((v) => (
          <li
            key={v.tag}
            className="flex items-center justify-between gap-3 px-3 py-2"
            data-testid={`rollback-row-${v.tag}`}
          >
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm text-sg-ink">{v.tag}</span>
              {v.instant ? (
                <span
                  data-testid="rollback-instant-chip"
                  title={t("system.rollback.instantHint")}
                  className="inline-flex items-center gap-1 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400"
                >
                  <Zap className="h-2.5 w-2.5" aria-hidden />
                  {t("system.rollback.instant")}
                </span>
              ) : null}
              {v.published_at ? (
                <span className="text-[11px] text-sg-ink-3">
                  {new Date(v.published_at).toLocaleDateString()}
                </span>
              ) : null}
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              data-testid={`rollback-button-${v.tag}`}
              onClick={() => setConfirmTag(v.tag)}
            >
              {t("system.rollback.action")}
            </Button>
          </li>
        ))}
      </ul>

      <ConfirmDialog
        open={confirmTag !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmTag(null);
        }}
        title={t("system.rollback.confirmTitle", { tag: confirmTag ?? "" })}
        description={t("system.rollback.confirmBody", {
          current,
          tag: confirmTag ?? "",
        })}
        confirmLabel={t("system.rollback.confirm")}
        cancelLabel={t("system.rollback.cancel")}
        onConfirm={handleConfirm}
        busy={submitting}
        testId="rollback-confirm"
      />
    </section>
  );
}
