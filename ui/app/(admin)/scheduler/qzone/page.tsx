"use client";

/**
 * /admin/scheduler/qzone — W6 of PLAN_PERSONA_STUDIO.md.
 *
 * Operator surface for managing the `qzone.daily_publish` runtime
 * scheduler jobs that drive a persona's daily QQ-空间 说说 pipeline.
 *
 * Layout (mirrors the persona + scheduler admin pages):
 *   [ page header with title + "Enable Grantley template" button ]
 *   [ Create-job card — persona dropdown, prompt template, cron,
 *     toggle + helper showing "next fire at …" ]
 *   [ Jobs table — name · cron · persona · enabled · last-run
 *     summary · actions (run now, link to last qzone url) ]
 *
 * Data flow:
 *   - `fetchSchedulerJobsTyped()` (15s poll) — every scheduler row;
 *     filtered client-side to `action_type === qzone.daily_publish`.
 *   - `fetchPersonas()` (no poll) — populates the persona dropdown.
 *   - `createSchedulerJob` / `enableQzoneTemplate` — write paths.
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
  enableQzoneTemplate,
  fetchSchedulerJobsTyped,
  formatNextFire,
  isQzoneDailyJob,
  nextFireTime,
  QZONE_DAILY_ACTION_TYPE,
  triggerSchedulerJobTyped,
  type SchedulerJobRow,
} from "@/lib/api/scheduler";
import { fetchPersonas, type Persona } from "@/lib/api/personas";

const JOBS_QUERY_KEY = ["admin", "scheduler", "qzone-jobs"] as const;
const PERSONAS_QUERY_KEY = ["admin", "personas"] as const;

/** Default cron string for new jobs — daily at 09:00 local. Matches the
 * bundled Grantley template so the form starts with a sensible value. */
const DEFAULT_CRON = "0 9 * * *";

/** Slug regex mirroring the backend `_JOB_NAME_RE`. Client-side gate
 * so a typo doesn't survive the round-trip. */
const JOB_NAME_RE = /^[a-z0-9_.-]{1,128}$/;

interface FormState {
  name: string;
  personaId: string;
  promptTemplate: string;
  cron: string;
  enabled: boolean;
}

const DEFAULT_FORM: FormState = {
  name: "",
  personaId: "",
  promptTemplate: "",
  cron: DEFAULT_CRON,
  enabled: true,
};

export default function QzoneSchedulerPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const [form, setForm] = React.useState<FormState>(DEFAULT_FORM);

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
        name: form.name.trim(),
        cron: form.cron.trim(),
        action_type: QZONE_DAILY_ACTION_TYPE,
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

  const enableGrantleyMutation = useMutation({
    mutationFn: () => enableQzoneTemplate("grantley"),
    onSuccess: (row) => {
      toast.success(
        t("schedulerQzone.grantleyEnabled", {
          defaultValue: "Grantley daily QZone job enabled ({{name}})",
          name: row.name,
        }),
      );
      qc.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(
        t("schedulerQzone.grantleyFail", {
          defaultValue: "Failed to enable Grantley template: {{msg}}",
          msg,
        }),
      );
    },
  });

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

  const nameValid = JOB_NAME_RE.test(form.name.trim());
  const canSubmit =
    nameValid &&
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
              defaultValue:
                "Drive a persona's daily QQ-空间 说说 pipeline on a cron schedule. " +
                "Each job runs one agent turn under the persona's voice and asserts " +
                "it ends with a qzone_publish tool call.",
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
            onClick={() => enableGrantleyMutation.mutate()}
            disabled={enableGrantleyMutation.isPending}
          >
            <Sparkles className="mr-1 h-3.5 w-3.5" aria-hidden />
            {t("schedulerQzone.enableGrantley", {
              defaultValue: "Enable Grantley daily 说说",
            })}
          </Button>
        </div>
      </header>

      {/* Create / upsert form ------------------------------------------------ */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            <Plus className="mr-1 inline h-4 w-4 align-text-bottom" aria-hidden />
            {t("schedulerQzone.create", { defaultValue: "Create a daily QZone job" })}
          </CardTitle>
          <CardDescription>
            {t("schedulerQzone.createHelp", {
              defaultValue:
                "Re-submitting the same name updates the existing job in place — useful " +
                "for editing the cron or prompt without churning the registry.",
            })}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1.5 md:col-span-1">
            <Label htmlFor="qzone-job-name">
              {t("schedulerQzone.fieldName", { defaultValue: "Job name" })}
            </Label>
            <Input
              id="qzone-job-name"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="grantley.daily_qzone"
              spellCheck={false}
              aria-invalid={form.name.length > 0 && !nameValid}
            />
            <p className="text-xs text-muted-foreground">
              {t("schedulerQzone.fieldNameHelp", {
                defaultValue: "[a-z0-9_.-]{1,128} — used as the unique key in the registry.",
              })}
            </p>
          </div>
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
              <p className="text-xs text-muted-foreground">
                {t("schedulerQzone.loadingPersonas", {
                  defaultValue: "Loading personas…",
                })}
              </p>
            ) : null}
          </div>
          <div className="space-y-1.5 md:col-span-2">
            <Label htmlFor="qzone-job-prompt">
              {t("schedulerQzone.fieldPrompt", {
                defaultValue: "Prompt template (user turn)",
              })}
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
            <p className="text-xs text-muted-foreground">
              {t("schedulerQzone.fieldPromptHelp", {
                defaultValue:
                  "Sent verbatim as the user turn. The persona system prompt + a " +
                  "tail instructing the agent to end with qzone_publish are appended " +
                  "automatically.",
              })}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="qzone-job-cron">
              {t("schedulerQzone.fieldCron", { defaultValue: "Cron expression" })}
            </Label>
            <Input
              id="qzone-job-cron"
              value={form.cron}
              onChange={(e) => setForm((f) => ({ ...f, cron: e.target.value }))}
              placeholder={DEFAULT_CRON}
              spellCheck={false}
              aria-invalid={form.cron.length > 0 && nextFirePreview === null}
            />
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
                    when: formatNextFire(nextFirePreview),
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
                'Only jobs with action_type="qzone.daily_publish" are shown here. ' +
                'Manage non-qzone jobs from the main /admin/scheduler page.',
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
                defaultValue:
                  "No QZone daily jobs yet. Use the form above or enable the Grantley template.",
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
                {lastRunDate.toLocaleString()}
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
