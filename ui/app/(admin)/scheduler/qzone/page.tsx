"use client";

/**
 * /admin/scheduler/qzone — W6 of PLAN_PERSONA_STUDIO.md, rewired in PR-F4.
 *
 * Operator surface for managing the `qzone.daily_publish` runtime
 * scheduler jobs that drive a persona's daily QQ-空间 说说 pipeline.
 *
 * Layout (mirrors the persona + scheduler admin pages):
 *   [ page header with title + one-click "Enable daily 说说" button
 *     (quick path — uses the form's persona selection) ]
 *   [ Upsert card — persona dropdown (job name derived as
 *     `${personaId}.daily_qzone`), prompt template, a friendly
 *     schedule picker (`<QzoneSchedulePicker>`), send-time jitter, a
 *     reference-image grid (`<QzoneRefImagePicker>`), and a
 *     "next fire at …" preview. Editing a row backfills this same card
 *     in place (no dialog) and switches Save → Update. ]
 *   [ Jobs table — one `<QzoneJobRow>` per job with the full action
 *     cluster: run now / edit / pause·resume / delete. ]
 *
 * Data flow:
 *   - `fetchSchedulerJobsTyped()` (15s poll) — every scheduler row;
 *     filtered client-side to `action_type === qzone.daily_publish`.
 *   - `fetchPersonas()` (no poll) — populates the persona dropdown.
 *   - `createSchedulerJob` / `patchSchedulerJob` — the write path
 *     (create vs. in-place edit, keyed by `editingName`).
 *   - `pauseSchedulerJob` / `resumeSchedulerJob` — the row pause/resume
 *     toggle (NOT an `{ enabled }` patch, so the backend re-validates
 *     before re-arming the tick loop).
 *   - `deleteSchedulerJob` — behind a page-level confirm dialog.
 *   - `triggerSchedulerJobTyped` — "run now" button.
 *
 * Style: minimal shadcn + Tailwind — does NOT pull the Tidepool
 * primitives the main scheduler page uses (those components are
 * tuned for the cron-tick countdown story, which isn't what an
 * operator inspecting a daily-说说 job needs).
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Pencil, Plus, RefreshCw, Sparkles, X } from "@/components/icons";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { FieldHint } from "@/components/ui/field-hint";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { QzoneJobRow } from "@/components/scheduler/qzone-job-row";
import { QzoneSchedulePicker } from "@/components/scheduler/qzone-schedule-picker";
import { QzoneRefImagePicker } from "@/components/scheduler/qzone-ref-image-picker";

import {
  createSchedulerJob,
  deleteSchedulerJob,
  fetchSchedulerJobsTyped,
  formatNextFire,
  isQzoneDailyJob,
  nextFireTime,
  patchSchedulerJob,
  QZONE_DAILY_ACTION_TYPE,
  triggerSchedulerJobTyped,
  type SchedulerJobRow,
} from "@/lib/api/scheduler";
import { pauseSchedulerJob, resumeSchedulerJob } from "@/lib/api";
import { composeCron, parseCron, type ScheduleState } from "@/lib/cron-schedule";
import { fetchPersonas, type Persona } from "@/lib/api/personas";

const JOBS_QUERY_KEY = ["admin", "scheduler", "qzone-jobs"] as const;
const PERSONAS_QUERY_KEY = ["admin", "personas"] as const;

/** Default cron string for new jobs — daily at 09:00 local. Matches the
 * bundled Grantley template so the form starts with a sensible value. */
const DEFAULT_CRON = "0 9 * * *";

/** Persona-neutral default prompt for one-click daily jobs — mirrors the
 * bundled template; the persona system prompt supplies the voice. */
const DEFAULT_DAILY_PROMPT =
  "用今日的视角写一条 200 字以内的 QQ 空间说说，配一张你最近状态的立绘图。" +
  "语气可以轻松随意，可以聊聊今天的心情、关注到的小事或正在做的事。" +
  "结尾必须调用 qzone_publish 工具发布（可以使用 generate 字段生成配图）。";

/** Send-time jitter is capped at two hours — beyond that the "daily at
 * 09:00" mental model breaks down and the operator should pick a window. */
const JITTER_MAX_MINUTES = 120;

interface FormState {
  personaId: string;
  promptTemplate: string;
  schedule: ScheduleState;
  enabled: boolean;
  imageRefLabels: string[];
  jitterMinutes: number;
}

/** Fresh default form — a factory (not a shared const) so a reset never
 * hands back a `schedule` object aliased with a previous edit. */
function makeDefaultForm(): FormState {
  return {
    personaId: "",
    promptTemplate: DEFAULT_DAILY_PROMPT,
    schedule: parseCron(DEFAULT_CRON),
    enabled: true,
    imageRefLabels: [],
    jitterMinutes: 0,
  };
}

/** Job name is mechanically derived from the persona — one daily job
 * per persona, so re-saving the same persona upserts in place. */
function deriveJobName(personaId: string): string {
  return `${personaId}.daily_qzone`;
}

/** IANA zone the operator's browser lives in. Sent with every job so the
 * backend evaluates the cron on this wall clock — which is exactly what
 * the client-side "next fire" preview shows. Without it the scheduler
 * fires in server/UTC time and the preview lies by the TZ offset. */
function browserTimeZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? null;
  } catch {
    return null;
  }
}

/** The forward-compat `image_ref_labels` / `jitter_minutes` fields ride
 * inside a runtime job's `metadata` today (the gateway hasn't surfaced
 * them top-level yet — PR-B5). Read whichever is present so an edit
 * round-trips them, falling back to the top-level fields once the wire
 * carries them. `SchedulerJobRow` has no `metadata` in its type, so reach
 * for it defensively. */
function readJobMeta(job: SchedulerJobRow): Record<string, unknown> {
  const meta = (job as { metadata?: unknown }).metadata;
  return meta && typeof meta === "object" ? (meta as Record<string, unknown>) : {};
}

function asStringArray(v: unknown): string[] | null {
  return Array.isArray(v) && v.every((x) => typeof x === "string")
    ? (v as string[])
    : null;
}

function asFiniteNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export default function QzoneSchedulerPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const [form, setForm] = React.useState<FormState>(makeDefaultForm);
  // `null` = creating a new job; a job name = editing that row in place.
  const [editingName, setEditingName] = React.useState<string | null>(null);
  // Name pending delete confirmation (drives the page-level ConfirmDialog).
  const [pendingDelete, setPendingDelete] = React.useState<string | null>(null);
  // Anchor the "scroll into view on edit" jump.
  const formAnchorRef = React.useRef<HTMLDivElement>(null);

  // 1-Hz tick so the "next fire at" preview stays roughly current
  // without a busy redraw. The preview is the only time-sensitive
  // surface on the page.
  const [now, setNow] = React.useState<Date>(() => new Date());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const jobsQuery = useQuery<SchedulerJobRow[]>({
    queryKey: JOBS_QUERY_KEY,
    queryFn: () => fetchSchedulerJobsTyped(),
    refetchInterval: 15_000,
  });
  const personasQuery = useQuery<Persona[]>({
    queryKey: PERSONAS_QUERY_KEY,
    queryFn: () => fetchPersonas(),
  });

  const qzoneJobs = React.useMemo(
    () => (jobsQuery.data ?? []).filter(isQzoneDailyJob),
    [jobsQuery.data],
  );
  const personas = personasQuery.data ?? [];

  const resetForm = React.useCallback(() => {
    setForm(makeDefaultForm());
    setEditingName(null);
  }, []);

  const composedCron = React.useMemo(
    () => composeCron(form.schedule),
    [form.schedule],
  );

  const nextFirePreview = React.useMemo(
    () => (composedCron ? nextFireTime(composedCron, now) : null),
    [composedCron, now],
  );

  const canSubmit =
    form.personaId.trim().length > 0 &&
    form.promptTemplate.trim().length > 0 &&
    composedCron !== null;

  // Create OR patch, keyed by `editingName`. The forward-compat
  // `image_ref_labels` / `jitter_minutes` ride top-level — the backend
  // ignores them until PR-B5 wires them in (harmless before then).
  const saveMutation = useMutation({
    mutationFn: () => {
      const cron = composeCron(form.schedule);
      if (cron === null) {
        return Promise.reject(new Error("invalid cron"));
      }
      const common = {
        cron,
        action_type: QZONE_DAILY_ACTION_TYPE,
        timezone: browserTimeZone(),
        persona_id: form.personaId,
        prompt_template: form.promptTemplate,
        image_ref_labels: form.imageRefLabels,
        jitter_minutes: form.jitterMinutes,
      };
      if (editingName) {
        // `enabled` is intentionally NOT patched here — pause/resume owns
        // that transition so the backend re-arms the tick loop.
        return patchSchedulerJob(editingName, common);
      }
      return createSchedulerJob({
        name: deriveJobName(form.personaId),
        enabled: form.enabled,
        ...common,
      });
    },
    onSuccess: (row) => {
      toast.success(
        t("schedulerQzone.created", {
          defaultValue: "Saved scheduler job {{name}}",
          name: row.name,
        }),
      );
      resetForm();
      qc.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(
        t("schedulerQzone.createFail", {
          defaultValue: "Failed to save scheduler job: {{msg}}",
          msg,
        }),
      );
    },
  });

  // One-click daily-说说 enable for ANY persona (not just the bundled
  // Grantley template): builds a sensible default job for the persona
  // picked in the form (or the only persona, when there is exactly one)
  // through the generic upsert endpoint.
  const enableDailyMutation = useMutation({
    mutationFn: (persona: Persona) =>
      createSchedulerJob({
        name: deriveJobName(persona.id),
        cron: DEFAULT_CRON,
        action_type: QZONE_DAILY_ACTION_TYPE,
        timezone: browserTimeZone(),
        persona_id: persona.id,
        prompt_template: DEFAULT_DAILY_PROMPT,
        enabled: true,
      }),
    onSuccess: (row, persona) => {
      toast.success(
        t("schedulerQzone.dailyEnabled", {
          defaultValue: "Daily QZone job enabled for {{persona}} ({{name}})",
          persona: persona.display_name || persona.id,
          name: row.name,
        }),
      );
      qc.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(
        t("schedulerQzone.dailyEnableFail", {
          defaultValue: "Failed to enable daily job: {{msg}}",
          msg,
        }),
      );
    },
  });

  const enableDailyForSelection = React.useCallback(() => {
    const list = personasQuery.data ?? [];
    const target =
      list.find((p) => p.id === form.personaId) ??
      (list.length === 1 ? list[0] : undefined);
    if (!target) {
      toast.info(
        t("schedulerQzone.needPersona", {
          defaultValue: "Pick a persona in the form below first.",
        }),
      );
      document.getElementById("qzone-job-persona")?.focus();
      return;
    }
    enableDailyMutation.mutate(target);
  }, [enableDailyMutation, form.personaId, personasQuery.data, t]);

  const triggerMutation = useMutation({
    mutationFn: (name: string) => triggerSchedulerJobTyped(name),
    onSuccess: (result, name) => {
      if (result.ok && result.result?.qzone_url) {
        toast.success(
          t("schedulerQzone.triggered", {
            defaultValue: "{{name}} published — {{url}}",
            name,
            url: result.result.qzone_url,
          }),
        );
      } else if (result.ok) {
        toast.success(
          t("schedulerQzone.triggeredNoUrl", {
            defaultValue: "{{name}} ran successfully",
            name,
          }),
        );
      } else {
        toast.warning(
          t("schedulerQzone.triggerFailed", {
            defaultValue: "{{name}} failed: {{err}}",
            name,
            err: result.result?.error ?? "unknown",
          }),
        );
      }
      qc.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
    },
    onError: (err, name) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(
        t("schedulerQzone.triggerError", {
          defaultValue: "Could not trigger {{name}}: {{msg}}",
          name,
          msg,
        }),
      );
    },
  });

  const toggleEnabledMutation = useMutation({
    mutationFn: (job: SchedulerJobRow) =>
      job.enabled ? pauseSchedulerJob(job.name) : resumeSchedulerJob(job.name),
    onSuccess: (_row, job) => {
      toast.success(
        job.enabled
          ? t("schedulerQzone.paused", {
              defaultValue: "Paused {{name}}",
              name: job.name,
            })
          : t("schedulerQzone.resumed", {
              defaultValue: "Resumed {{name}}",
              name: job.name,
            }),
      );
      qc.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(
        t("schedulerQzone.toggleFail", {
          defaultValue: "Pause/resume failed: {{msg}}",
          msg,
        }),
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteSchedulerJob(name),
    onSuccess: (res) => {
      toast.success(
        t("schedulerQzone.deleted", {
          defaultValue: "Deleted {{name}}",
          name: res.deleted,
        }),
      );
      // Bail out of the edit form if the row being edited was removed.
      if (editingName === res.deleted) resetForm();
      qc.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(
        t("schedulerQzone.deleteFail", {
          defaultValue: "Delete failed: {{msg}}",
          msg,
        }),
      );
    },
  });

  const startEdit = React.useCallback(
    (name: string) => {
      const job = qzoneJobs.find((j) => j.name === name);
      if (!job) return;
      const meta = readJobMeta(job);
      setForm({
        personaId: job.persona_id ?? "",
        promptTemplate: job.prompt_template ?? DEFAULT_DAILY_PROMPT,
        schedule: parseCron(job.cron),
        enabled: job.enabled ?? true,
        imageRefLabels:
          job.image_ref_labels ?? asStringArray(meta.image_ref_labels) ?? [],
        jitterMinutes:
          job.jitter_minutes ?? asFiniteNumber(meta.jitter_minutes) ?? 0,
      });
      setEditingName(job.name);
      // Scroll the upsert card into view. `scrollIntoView` is absent under
      // jsdom, so guard the call for the test environment.
      requestAnimationFrame(() => {
        formAnchorRef.current?.scrollIntoView?.({
          behavior: "smooth",
          block: "start",
        });
      });
    },
    [qzoneJobs],
  );

  const requestDelete = React.useCallback((name: string) => {
    setPendingDelete(name);
  }, []);

  const toggleEnabled = React.useCallback(
    (name: string) => {
      const job = qzoneJobs.find((j) => j.name === name);
      if (job) toggleEnabledMutation.mutate(job);
    },
    [qzoneJobs, toggleEnabledMutation],
  );

  const isEditing = editingName !== null;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Sparkles className="h-5 w-5 text-sg-accent" aria-hidden />
            {t("schedulerQzone.title", { defaultValue: "QZone daily publishing" })}
          </h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            {t("schedulerQzone.lede", {
              defaultValue: "让人格按计划每天自动发布一条 QQ 空间说说。",
            })}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => jobsQuery.refetch()}
            disabled={jobsQuery.isFetching}
          >
            <RefreshCw
              className={cn(
                "mr-1 h-3.5 w-3.5",
                jobsQuery.isFetching && "animate-spin",
              )}
              aria-hidden
            />
            {t("schedulerQzone.refresh", { defaultValue: "Refresh" })}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            data-testid="qzone-enable-daily"
            onClick={enableDailyForSelection}
            disabled={enableDailyMutation.isPending}
          >
            <Sparkles className="mr-1 h-3.5 w-3.5" aria-hidden />
            {t("schedulerQzone.enableDaily", {
              defaultValue: "Enable daily 说说",
            })}
          </Button>
        </div>
      </header>

      {/* Edit / upsert form -------------------------------------------------- */}
      <div ref={formAnchorRef}>
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {isEditing ? (
                <Pencil
                  className="mr-1 inline h-4 w-4 align-text-bottom"
                  aria-hidden
                />
              ) : (
                <Plus
                  className="mr-1 inline h-4 w-4 align-text-bottom"
                  aria-hidden
                />
              )}
              {isEditing
                ? t("schedulerQzone.editTitle", { defaultValue: "编辑说说任务" })
                : t("schedulerQzone.create", { defaultValue: "配置每日说说任务" })}
            </CardTitle>
            <CardDescription>
              {t("schedulerQzone.createHelp", {
                defaultValue: "选择人格并保存；同一人格重复保存会就地更新任务。",
              })}
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-5 md:grid-cols-2">
            {/* Persona ------------------------------------------------------- */}
            <div className="space-y-1.5">
              <Label htmlFor="qzone-job-persona">
                {t("schedulerQzone.fieldPersona", { defaultValue: "Persona" })}
              </Label>
              <select
                id="qzone-job-persona"
                value={form.personaId}
                disabled={isEditing}
                onChange={(e) =>
                  setForm((f) => ({ ...f, personaId: e.target.value }))
                }
                className={cn(
                  "flex h-9 w-full rounded-md border border-input bg-transparent",
                  "px-3 py-1 text-sm shadow-sm transition-colors",
                  "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                )}
              >
                <option value="">
                  {t("schedulerQzone.fieldPersonaPlaceholder", {
                    defaultValue: "— pick a persona —",
                  })}
                </option>
                {personas.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.display_name} ({p.id})
                  </option>
                ))}
              </select>
              {personasQuery.isPending ? (
                <FieldHint>
                  {t("schedulerQzone.loadingPersonas", {
                    defaultValue: "Loading personas…",
                  })}
                </FieldHint>
              ) : null}
              {form.personaId ? (
                <FieldHint id="qzone-derived-name">
                  {t("schedulerQzone.derivedName", {
                    defaultValue: "任务名：{{name}}",
                    name: deriveJobName(form.personaId),
                  })}
                </FieldHint>
              ) : null}
              {isEditing ? (
                <FieldHint>
                  {t("schedulerQzone.personaLockedHint", {
                    defaultValue:
                      "已有任务的人格不可更改。如需换人格，请删除此任务后新建。",
                  })}
                </FieldHint>
              ) : null}
            </div>

            {/* Send-time jitter --------------------------------------------- */}
            <div className="space-y-1.5">
              <Label htmlFor="qzone-job-jitter">
                {t("schedulerQzone.jitterLabel", {
                  defaultValue: "发送抖动（分钟）",
                })}
              </Label>
              <Input
                id="qzone-job-jitter"
                type="number"
                min={0}
                max={JITTER_MAX_MINUTES}
                value={String(form.jitterMinutes)}
                data-testid="qzone-job-jitter"
                onChange={(e) => {
                  const n = Number.parseInt(e.target.value, 10);
                  const clamped = Number.isFinite(n)
                    ? Math.min(JITTER_MAX_MINUTES, Math.max(0, n))
                    : 0;
                  setForm((f) => ({ ...f, jitterMinutes: clamped }));
                }}
                className="max-w-[160px]"
              />
              <FieldHint>
                {t("schedulerQzone.jitterHint", {
                  defaultValue:
                    "在触发时间上随机 ± 这么多分钟（0–120），让发布看起来更自然。",
                })}
              </FieldHint>
            </div>

            {/* Prompt -------------------------------------------------------- */}
            <div className="space-y-1.5 md:col-span-2">
              <Label htmlFor="qzone-job-prompt">
                {t("schedulerQzone.fieldPrompt", { defaultValue: "提示词" })}
              </Label>
              <textarea
                id="qzone-job-prompt"
                value={form.promptTemplate}
                onChange={(e) =>
                  setForm((f) => ({ ...f, promptTemplate: e.target.value }))
                }
                placeholder={t("schedulerQzone.fieldPromptPlaceholder", {
                  defaultValue:
                    "用今日的视角写一条 200 字以内的 QQ 空间说说，配一张你最近状态的立绘图。",
                })}
                spellCheck={false}
                className={cn(
                  "flex min-h-[120px] w-full rounded-md border border-input bg-transparent",
                  "px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground",
                  "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                )}
              />
              <FieldHint
                detail={t("schedulerQzone.fieldPromptDetail", {
                  defaultValue:
                    "内容会原样作为用户消息发送；人格设定与发布指令由系统自动附加。",
                })}
              >
                {t("schedulerQzone.fieldPromptHelp", {
                  defaultValue: "告诉人格每天写什么，可随时修改。",
                })}
              </FieldHint>
            </div>

            {/* Schedule picker + next-fire preview -------------------------- */}
            <div className="space-y-2 md:col-span-2">
              <QzoneSchedulePicker
                value={form.schedule}
                onChange={(schedule) => setForm((f) => ({ ...f, schedule }))}
              />
              <p
                className={cn(
                  "text-xs",
                  composedCron === null ? "text-sg-err" : "text-muted-foreground",
                )}
                data-testid="qzone-next-fire"
              >
                {nextFirePreview !== null
                  ? t("schedulerQzone.cronNext", {
                      defaultValue: "Next fire: {{when}}",
                      when: `${formatNextFire(nextFirePreview)} (${browserTimeZone() ?? "UTC"})`,
                    })
                  : t("schedulerQzone.cronInvalid", {
                      defaultValue: "Use 5-field cron (min hour dom mon dow).",
                    })}
              </p>
            </div>

            {/* Reference images (only once a persona is chosen) ------------- */}
            {form.personaId ? (
              <div className="md:col-span-2">
                <QzoneRefImagePicker
                  personaId={form.personaId}
                  selected={form.imageRefLabels}
                  onChange={(imageRefLabels) =>
                    setForm((f) => ({ ...f, imageRefLabels }))
                  }
                />
              </div>
            ) : null}

            {/* Enabled toggle (create) / pause hint (edit) + actions -------- */}
            <div className="flex flex-wrap items-center justify-between gap-3 md:col-span-2">
              {isEditing ? (
                <FieldHint>
                  {t("schedulerQzone.enabledEditHint", {
                    defaultValue:
                      "启用或暂停请使用任务列表中该行的暂停/恢复按钮。",
                  })}
                </FieldHint>
              ) : (
                <div className="flex items-center gap-2">
                  <Switch
                    id="qzone-job-enabled"
                    checked={form.enabled}
                    onCheckedChange={(v) =>
                      setForm((f) => ({ ...f, enabled: Boolean(v) }))
                    }
                  />
                  <Label htmlFor="qzone-job-enabled" className="cursor-pointer">
                    {form.enabled
                      ? t("schedulerQzone.toggleOn", { defaultValue: "Enabled" })
                      : t("schedulerQzone.toggleOff", { defaultValue: "Paused" })}
                  </Label>
                </div>
              )}
              <div className="flex items-center gap-2">
                {isEditing ? (
                  <Button
                    variant="ghost"
                    onClick={resetForm}
                    data-testid="qzone-cancel-edit"
                  >
                    <X className="mr-1 h-3.5 w-3.5" aria-hidden />
                    {t("schedulerQzone.cancelEdit", { defaultValue: "取消编辑" })}
                  </Button>
                ) : null}
                <Button
                  onClick={() => saveMutation.mutate()}
                  disabled={!canSubmit || saveMutation.isPending}
                  data-testid="qzone-job-save"
                >
                  {isEditing
                    ? t("schedulerQzone.update", { defaultValue: "更新任务" })
                    : t("schedulerQzone.save", { defaultValue: "Save job" })}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Existing jobs ------------------------------------------------------- */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {t("schedulerQzone.tableTitle", { defaultValue: "QZone daily jobs" })}
          </CardTitle>
          <CardDescription>
            {t("schedulerQzone.tableHelp", {
              defaultValue:
                "此处仅显示每日说说任务，其他定时任务请在「定时任务」页面管理。",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {jobsQuery.isPending ? (
            <div className="space-y-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : qzoneJobs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t("schedulerQzone.empty", {
                defaultValue: "暂无每日说说任务，在上方选择人格并保存即可创建。",
              })}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>
                    {t("schedulerQzone.col.name", { defaultValue: "Name" })}
                  </TableHead>
                  <TableHead>
                    {t("schedulerQzone.col.persona", { defaultValue: "Persona" })}
                  </TableHead>
                  <TableHead>
                    {t("schedulerQzone.col.cron", { defaultValue: "Cron" })}
                  </TableHead>
                  <TableHead>
                    {t("schedulerQzone.col.state", { defaultValue: "State" })}
                  </TableHead>
                  <TableHead>
                    {t("schedulerQzone.col.lastRun", { defaultValue: "Last run" })}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("schedulerQzone.col.actions", { defaultValue: "Actions" })}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {qzoneJobs.map((job) => (
                  <QzoneJobRow
                    key={job.name}
                    job={job}
                    onTrigger={(name) => triggerMutation.mutate(name)}
                    onEdit={startEdit}
                    onToggleEnabled={toggleEnabled}
                    onDelete={requestDelete}
                    triggering={
                      triggerMutation.isPending &&
                      triggerMutation.variables === job.name
                    }
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
        title={t("schedulerQzone.deleteTitle", {
          defaultValue: "删除该每日说说任务？",
        })}
        description={t("schedulerQzone.deleteBody", {
          defaultValue: "确定删除 {{name}}？此操作不可撤销。",
          name: pendingDelete ?? "",
        })}
        cancelLabel={t("common.cancel", { defaultValue: "Cancel" })}
        confirmLabel={t("common.delete", { defaultValue: "Delete" })}
        testId="qzone-job-delete-confirm"
        busy={deleteMutation.isPending}
        onConfirm={() => {
          const name = pendingDelete;
          setPendingDelete(null);
          if (name) deleteMutation.mutate(name);
        }}
      />
    </div>
  );
}
