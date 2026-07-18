"use client";

/**
 * /admin/scheduler/qzone — W6 of PLAN_PERSONA_STUDIO.md.
 *
 * Operator surface for managing the `qzone.daily_publish` runtime
 * scheduler jobs that drive a persona's daily QQ-空间 说说 pipeline.
 *
 * Layout (mirrors the persona + scheduler admin pages):
 *   [ page header with title + one-click "Enable daily 说说" button
 *     (quick path — uses the form's persona selection) ]
 *   [ Edit card — persona dropdown (job name derived as
 *     `${personaId}.daily_qzone`), prefilled prompt template, cron
 *     preset select with advanced raw-cron reveal, toggle + helper
 *     showing "next fire at …" ]
 *   [ Jobs table — name · cron · persona · enabled · last-run
 *     summary · actions (run now, link to last qzone url) ]
 *
 * Data flow:
 *   - `fetchSchedulerJobsTyped()` (15s poll) — every scheduler row;
 *     filtered client-side to `action_type === qzone.daily_publish`.
 *   - `fetchPersonas()` (no poll) — populates the persona dropdown.
 *   - `createSchedulerJob` — write path (also powers the one-click
 *     daily enable for any persona).
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
import { ExternalLink, Play, Plus, RefreshCw, Sparkles } from "lucide-react";

import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { FieldHint } from "@/components/ui/field-hint";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

import {
  createSchedulerJob,
  fetchSchedulerJobsTyped,
  formatNextFire,
  isQzoneDailyJob,
  nextFireTime,
  QZONE_DAILY_ACTION_TYPE,
  triggerSchedulerJobTyped,
  type SchedulerJobRow,
} from "@/lib/api/scheduler";
import { fetchPersonas, type Persona } from "@/lib/api/personas";
import { formatDateTime } from "@/lib/format";

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

/** Cron choices surfaced in the preset select. Anything else lives
 * behind the 自定义 advanced reveal (raw 5-field cron input). */
const CRON_PRESETS = [
  { cron: "0 9 * * *", key: "cronPreset09", fallback: "每天 09:00" },
  { cron: "0 12 * * *", key: "cronPreset12", fallback: "每天 12:00" },
  { cron: "0 21 * * *", key: "cronPreset21", fallback: "每天 21:00" },
] as const;

/** Sentinel select value that reveals the raw cron input. */
const CRON_CUSTOM = "__custom__";

interface FormState {
  personaId: string;
  promptTemplate: string;
  cron: string;
  enabled: boolean;
}

const DEFAULT_FORM: FormState = {
  personaId: "",
  promptTemplate: DEFAULT_DAILY_PROMPT,
  cron: DEFAULT_CRON,
  enabled: true,
};

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

export default function QzoneSchedulerPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const [form, setForm] = React.useState<FormState>(DEFAULT_FORM);
  // Whether the operator opened the advanced raw-cron input (自定义).
  const [cronCustom, setCronCustom] = React.useState(false);

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

  const createMutation = useMutation({
    mutationFn: () =>
      createSchedulerJob({
        name: deriveJobName(form.personaId),
        cron: form.cron.trim(),
        action_type: QZONE_DAILY_ACTION_TYPE,
        timezone: browserTimeZone(),
        persona_id: form.personaId,
        prompt_template: form.promptTemplate,
        enabled: form.enabled,
      }),
    onSuccess: (row) => {
      toast.success(
        t("schedulerQzone.created", {
          defaultValue: "Saved scheduler job {{name}}",
          name: row.name,
        }),
      );
      setForm(DEFAULT_FORM);
      setCronCustom(false);
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
    const personas = personasQuery.data ?? [];
    const target =
      personas.find((p) => p.id === form.personaId) ??
      (personas.length === 1 ? personas[0] : undefined);
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

  const qzoneJobs = React.useMemo(
    () => (jobsQuery.data ?? []).filter(isQzoneDailyJob),
    [jobsQuery.data],
  );
  const personas = personasQuery.data ?? [];

  const nextFirePreview = React.useMemo(() => {
    if (!form.cron.trim()) return null;
    return nextFireTime(form.cron.trim(), now);
  }, [form.cron, now]);

  const canSubmit =
    form.personaId.trim().length > 0 &&
    form.promptTemplate.trim().length > 0 &&
    nextFirePreview !== null;

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
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            <Plus className="mr-1 inline h-4 w-4 align-text-bottom" aria-hidden />
            {t("schedulerQzone.create", { defaultValue: "配置每日说说任务" })}
          </CardTitle>
          <CardDescription>
            {t("schedulerQzone.createHelp", {
              defaultValue: "选择人格并保存；同一人格重复保存会就地更新任务。",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1.5 md:col-span-1">
            <Label htmlFor="qzone-job-persona">
              {t("schedulerQzone.fieldPersona", { defaultValue: "Persona" })}
            </Label>
            <select
              id="qzone-job-persona"
              value={form.personaId}
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
          </div>
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
          <div className="space-y-1.5">
            <Label htmlFor="qzone-job-cron-preset">
              {t("schedulerQzone.fieldCron", { defaultValue: "发布时间" })}
            </Label>
            <select
              id="qzone-job-cron-preset"
              value={cronCustom ? CRON_CUSTOM : form.cron}
              onChange={(e) => {
                const v = e.target.value;
                if (v === CRON_CUSTOM) {
                  setCronCustom(true);
                } else {
                  setCronCustom(false);
                  setForm((f) => ({ ...f, cron: v }));
                }
              }}
              className={cn(
                "flex h-9 w-full rounded-md border border-input bg-transparent",
                "px-3 py-1 text-sm shadow-sm transition-colors",
                "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              )}
            >
              {CRON_PRESETS.map((preset) => (
                <option key={preset.cron} value={preset.cron}>
                  {t(`schedulerQzone.${preset.key}`, {
                    defaultValue: preset.fallback,
                  })}
                </option>
              ))}
              <option value={CRON_CUSTOM}>
                {t("schedulerQzone.cronPresetCustom", { defaultValue: "自定义…" })}
              </option>
            </select>
            {cronCustom ? (
              <Input
                id="qzone-job-cron"
                value={form.cron}
                onChange={(e) =>
                  setForm((f) => ({ ...f, cron: e.target.value }))
                }
                placeholder={DEFAULT_CRON}
                spellCheck={false}
                aria-invalid={form.cron.length > 0 && nextFirePreview === null}
              />
            ) : null}
            <p
              className={cn(
                "text-xs",
                nextFirePreview === null && form.cron.trim().length > 0
                  ? "text-sg-err"
                  : "text-muted-foreground",
              )}
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
          <div className="flex items-end gap-3">
            <div className="flex flex-1 items-center gap-2">
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
            <Button
              onClick={() => createMutation.mutate()}
              disabled={!canSubmit || createMutation.isPending}
            >
              {t("schedulerQzone.save", { defaultValue: "Save job" })}
            </Button>
          </div>
        </CardContent>
      </Card>

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
                    onTrigger={() => triggerMutation.mutate(job.name)}
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
    </div>
  );
}

interface QzoneJobRowProps {
  job: SchedulerJobRow;
  onTrigger: () => void;
  triggering: boolean;
}

function QzoneJobRow({ job, onTrigger, triggering }: QzoneJobRowProps) {
  const { t } = useTranslation();
  const lastRunDate =
    job.last_run_at_ms !== null && job.last_run_at_ms !== undefined
      ? new Date(job.last_run_at_ms)
      : null;

  return (
    <TableRow>
      <TableCell className="font-mono text-xs">{job.name}</TableCell>
      <TableCell>{job.persona_id ?? "—"}</TableCell>
      <TableCell className="font-mono text-xs">{job.cron}</TableCell>
      <TableCell>
        {job.enabled ? (
          <Badge variant="default">
            {t("schedulerQzone.row.enabled", { defaultValue: "enabled" })}
          </Badge>
        ) : (
          <Badge variant="secondary">
            {t("schedulerQzone.row.paused", { defaultValue: "paused" })}
          </Badge>
        )}
      </TableCell>
      <TableCell className="space-y-1 text-xs">
        {lastRunDate === null ? (
          <span className="text-muted-foreground">
            {t("schedulerQzone.row.neverRun", { defaultValue: "never run" })}
          </span>
        ) : (
          <>
            <div className="flex items-center gap-1.5">
              {job.last_run_ok ? (
                <Badge variant="default">
                  {t("schedulerQzone.row.ok", { defaultValue: "ok" })}
                </Badge>
              ) : (
                <Badge variant="destructive">
                  {t("schedulerQzone.row.error", { defaultValue: "error" })}
                </Badge>
              )}
              <span className="text-muted-foreground">
                {formatDateTime(lastRunDate)}
              </span>
            </div>
            {job.last_run_ok && job.last_qzone_url ? (
              <a
                href={job.last_qzone_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sg-accent hover:underline"
              >
                <ExternalLink className="h-3 w-3" aria-hidden />
                {t("schedulerQzone.row.viewQzone", { defaultValue: "View on QZone" })}
              </a>
            ) : null}
            {!job.last_run_ok && job.last_error ? (
              <p className="text-sg-err">{job.last_error}</p>
            ) : null}
          </>
        )}
      </TableCell>
      <TableCell className="text-right">
        <Button
          variant="outline"
          size="sm"
          onClick={onTrigger}
          disabled={triggering}
        >
          <Play
            className={cn("mr-1 h-3 w-3", triggering && "animate-pulse")}
            aria-hidden
          />
          {t("schedulerQzone.row.runNow", { defaultValue: "Run now" })}
        </Button>
      </TableCell>
    </TableRow>
  );
}
