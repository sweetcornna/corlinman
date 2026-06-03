"use client";

/**
 * Evolution settings editor — the operator surface for the three config
 * tunables that previously had no UI:
 *
 *   - `meta_approver_users` — the allow-list that gates meta-kind
 *     approvals. Empty by default, which 403s EVERY meta approval out of
 *     the box (`engine_config` / `engine_prompt` / `observer_filter` /
 *     `cluster_threshold`), so this is the load-bearing fix.
 *   - `budget` — weekly proposal quota (enabled + total + per-kind caps).
 *   - `auto_rollback` — grace window + metrics-breach thresholds.
 *
 * Reads/writes via `@/lib/api/evolution` against `/admin/evolution/settings`.
 * Rendered as a dialog opened from the curator-section header so it doesn't
 * disturb the existing proposal-queue / curator layout.
 */

import * as React from "react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import {
  fetchEvolutionSettings,
  saveEvolutionSettings,
  normalizeEvolutionSettings,
  type EvolutionSettings,
} from "@/lib/api/evolution";

/** The four meta kinds whose budgets the per-kind editor pre-seeds rows for
 *  (mirrors `META_KINDS` on the Rust side / `components/evolution/types`). */
const BUDGET_KINDS = [
  "engine_config",
  "engine_prompt",
  "observer_filter",
  "cluster_threshold",
] as const;

export interface EvolutionSettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function EvolutionSettingsDialog({
  open,
  onOpenChange,
}: EvolutionSettingsDialogProps) {
  const { t } = useTranslation();

  const [draft, setDraft] = useState<EvolutionSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [approverInput, setApproverInput] = useState("");

  // Load on open; reset transient state when closed.
  useEffect(() => {
    if (!open) {
      setError(null);
      setApproverInput("");
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDisabled(false);
    void fetchEvolutionSettings().then((res) => {
      if (cancelled) return;
      if (res.kind === "ok") {
        setDraft(res.settings);
      } else if (res.kind === "disabled") {
        setDraft(normalizeEvolutionSettings(null));
        setDisabled(true);
      } else {
        setError(res.message);
        setDraft(normalizeEvolutionSettings(null));
      }
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const approvers = draft?.meta_approver_users ?? [];

  const addApprover = () => {
    const id = approverInput.trim();
    if (!id || !draft) return;
    if (approvers.includes(id)) {
      setApproverInput("");
      return;
    }
    setDraft({ ...draft, meta_approver_users: [...approvers, id] });
    setApproverInput("");
  };

  const removeApprover = (id: string) => {
    if (!draft) return;
    setDraft({
      ...draft,
      meta_approver_users: approvers.filter((u) => u !== id),
    });
  };

  const setBudget = (patch: Partial<EvolutionSettings["budget"]>) => {
    if (!draft) return;
    setDraft({ ...draft, budget: { ...draft.budget, ...patch } });
  };

  const setPerKind = (kind: string, value: number) => {
    if (!draft) return;
    const next = { ...draft.budget.per_kind };
    if (Number.isNaN(value)) return;
    next[kind] = value;
    setDraft({ ...draft, budget: { ...draft.budget, per_kind: next } });
  };

  const setAutoRollback = (
    patch: Partial<EvolutionSettings["auto_rollback"]>,
  ) => {
    if (!draft) return;
    setDraft({
      ...draft,
      auto_rollback: { ...draft.auto_rollback, ...patch },
    });
  };

  const setThreshold = (
    key: keyof EvolutionSettings["auto_rollback"]["thresholds"],
    value: number,
  ) => {
    if (!draft || Number.isNaN(value)) return;
    setDraft({
      ...draft,
      auto_rollback: {
        ...draft.auto_rollback,
        thresholds: { ...draft.auto_rollback.thresholds, [key]: value },
      },
    });
  };

  const handleSave = async () => {
    if (!draft) return;
    setSaving(true);
    setError(null);
    try {
      const res = await saveEvolutionSettings(draft);
      setDraft(res.settings);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const perKindRows = useMemo(() => {
    if (!draft) return [];
    const seen = new Set<string>();
    const rows: { kind: string; value: number }[] = [];
    for (const k of BUDGET_KINDS) {
      seen.add(k);
      rows.push({ kind: k, value: draft.budget.per_kind[k] ?? 0 });
    }
    // Surface any extra kinds the operator set out-of-band so editing
    // them here doesn't silently drop them on save.
    for (const [k, v] of Object.entries(draft.budget.per_kind)) {
      if (!seen.has(k)) rows.push({ kind: k, value: v });
    }
    return rows;
  }, [draft]);

  const meta = draft?.auto_rollback;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-[560px]"
        data-testid="evolution-settings-dialog"
      >
        <DialogHeader>
          <DialogTitle>{t("evolution.settings.title")}</DialogTitle>
          <DialogDescription>
            {t("evolution.settings.subtitle")}
          </DialogDescription>
        </DialogHeader>

        {loading || !draft ? (
          <div className="py-8 text-center text-[12.5px] text-tp-ink-3">
            {t("evolution.settings.loading")}
          </div>
        ) : (
          <div className="flex flex-col gap-6 py-1">
            {disabled ? (
              <div className="rounded-lg border border-tp-warn/30 bg-tp-warn-soft px-3 py-2 text-[12px] text-tp-warn">
                {t("evolution.settings.configUnset")}
              </div>
            ) : null}

            {/* ── Meta approvers ── */}
            <section className="flex flex-col gap-2" data-testid="meta-approvers">
              <div className="flex flex-col gap-0.5">
                <h3 className="font-medium text-[13px] text-tp-ink-1">
                  {t("evolution.settings.metaApprovers")}
                </h3>
                <p className="text-[11.5px] text-tp-ink-3">
                  {t("evolution.settings.metaApproversHint")}
                </p>
              </div>
              {approvers.length === 0 ? (
                <p
                  className="rounded-lg border border-tp-err/30 bg-tp-err-soft px-3 py-2 text-[11.5px] text-tp-err"
                  data-testid="meta-approvers-empty"
                >
                  {t("evolution.settings.metaApproversEmpty")}
                </p>
              ) : (
                <ul className="flex flex-wrap gap-1.5">
                  {approvers.map((id) => (
                    <li
                      key={id}
                      className="inline-flex items-center gap-1.5 rounded-full border border-tp-glass-edge bg-tp-glass-inner px-2.5 py-1 text-[12px] text-tp-ink-1"
                    >
                      <span className="font-mono">{id}</span>
                      <button
                        type="button"
                        onClick={() => removeApprover(id)}
                        aria-label={t("evolution.settings.removeApprover", { id })}
                        className="rounded-full px-1 text-tp-ink-3 hover:text-tp-err focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
                      >
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div className="flex items-center gap-2">
                <Input
                  value={approverInput}
                  onChange={(e) => setApproverInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addApprover();
                    }
                  }}
                  placeholder={t("evolution.settings.approverPlaceholder")}
                  aria-label={t("evolution.settings.approverPlaceholder")}
                  data-testid="approver-input"
                  className="h-8 flex-1 text-[12.5px]"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={addApprover}
                  disabled={!approverInput.trim()}
                  data-testid="approver-add"
                >
                  {t("evolution.settings.addApprover")}
                </Button>
              </div>
            </section>

            {/* ── Budget ── */}
            <section className="flex flex-col gap-2" data-testid="budget-settings">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-medium text-[13px] text-tp-ink-1">
                  {t("evolution.settings.budget")}
                </h3>
                <Switch
                  checked={draft.budget.enabled}
                  onCheckedChange={(v) => setBudget({ enabled: v })}
                  aria-label={t("evolution.settings.budgetEnabled")}
                  data-testid="budget-enabled"
                />
              </div>
              <Field
                label={t("evolution.settings.weeklyTotal")}
                hint={t("evolution.settings.weeklyTotalHint")}
              >
                <NumberInput
                  value={draft.budget.weekly_total}
                  min={0}
                  onChange={(n) => setBudget({ weekly_total: n })}
                  testid="budget-weekly-total"
                />
              </Field>
              <div className="flex flex-col gap-1.5">
                <span className="text-[11.5px] text-tp-ink-3">
                  {t("evolution.settings.perKind")}
                </span>
                {perKindRows.map((row) => (
                  <div
                    key={row.kind}
                    className="flex items-center justify-between gap-2"
                  >
                    <Label className="font-mono text-[12px] text-tp-ink-2">
                      {row.kind}
                    </Label>
                    <NumberInput
                      value={row.value}
                      min={0}
                      onChange={(n) => setPerKind(row.kind, n)}
                      testid={`budget-perkind-${row.kind}`}
                      className="w-24"
                    />
                  </div>
                ))}
              </div>
            </section>

            {/* ── Auto-rollback ── */}
            {meta ? (
              <section
                className="flex flex-col gap-2"
                data-testid="auto-rollback-settings"
              >
                <div className="flex items-center justify-between gap-3">
                  <h3 className="font-medium text-[13px] text-tp-ink-1">
                    {t("evolution.settings.autoRollback")}
                  </h3>
                  <Switch
                    checked={meta.enabled}
                    onCheckedChange={(v) => setAutoRollback({ enabled: v })}
                    aria-label={t("evolution.settings.autoRollbackEnabled")}
                    data-testid="auto-rollback-enabled"
                  />
                </div>
                <Field
                  label={t("evolution.settings.graceWindowHours")}
                  hint={t("evolution.settings.graceWindowHint")}
                >
                  <NumberInput
                    value={meta.grace_window_hours}
                    min={0}
                    onChange={(n) => setAutoRollback({ grace_window_hours: n })}
                    testid="grace-window-hours"
                  />
                </Field>
                <Field label={t("evolution.settings.errRateDelta")}>
                  <NumberInput
                    value={meta.thresholds.default_err_rate_delta_pct}
                    step={0.1}
                    onChange={(n) =>
                      setThreshold("default_err_rate_delta_pct", n)
                    }
                    testid="threshold-err-rate"
                  />
                </Field>
                <Field label={t("evolution.settings.p95Delta")}>
                  <NumberInput
                    value={meta.thresholds.default_p95_latency_delta_pct}
                    step={0.1}
                    onChange={(n) =>
                      setThreshold("default_p95_latency_delta_pct", n)
                    }
                    testid="threshold-p95"
                  />
                </Field>
                <Field label={t("evolution.settings.signalWindowSecs")}>
                  <NumberInput
                    value={meta.thresholds.signal_window_secs}
                    min={0}
                    onChange={(n) => setThreshold("signal_window_secs", n)}
                    testid="threshold-signal-window"
                  />
                </Field>
                <Field label={t("evolution.settings.minBaselineSignals")}>
                  <NumberInput
                    value={meta.thresholds.min_baseline_signals}
                    min={0}
                    onChange={(n) => setThreshold("min_baseline_signals", n)}
                    testid="threshold-min-baseline"
                  />
                </Field>
              </section>
            ) : null}

            {error ? (
              <p
                role="alert"
                className="rounded-lg border border-tp-err/40 bg-tp-err-soft px-3 py-2 text-[12px] text-tp-err"
              >
                {t("evolution.settings.saveFailed", { msg: error })}
              </p>
            ) : null}
          </div>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            {t("common.close")}
          </Button>
          <Button
            onClick={() => void handleSave()}
            disabled={saving || loading || !draft || disabled}
            data-testid="evolution-settings-save"
          >
            {saving
              ? t("evolution.settings.saving")
              : t("evolution.settings.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="flex flex-col">
        <Label className="text-[12.5px] text-tp-ink-1">{label}</Label>
        {hint ? <span className="text-[11px] text-tp-ink-3">{hint}</span> : null}
      </div>
      {children}
    </div>
  );
}

function NumberInput({
  value,
  onChange,
  min,
  step,
  testid,
  className,
}: {
  value: number;
  onChange: (n: number) => void;
  min?: number;
  step?: number;
  testid?: string;
  className?: string;
}) {
  return (
    <Input
      type="number"
      inputMode="decimal"
      value={Number.isFinite(value) ? value : 0}
      min={min}
      step={step}
      onChange={(e) => onChange(Number(e.target.value))}
      data-testid={testid}
      className={cn("h-8 w-28 text-right text-[12.5px]", className)}
    />
  );
}
