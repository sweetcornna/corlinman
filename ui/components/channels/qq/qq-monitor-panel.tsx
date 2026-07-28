"use client";

/**
 * `<QqMonitorPanel>` — group-message monitoring + scheduled digests,
 * mounted as a SECTION of the QQ channel page (/channels/qq), right
 * after the QZone panel.
 *
 * Operator surface for the per-instance monitor rules: watch a group
 * (optionally only certain members), then deliver an LLM digest of the
 * captured window to a group or a private chat, daily or every N
 * minutes.
 *
 * Write model — the backend has NO per-row PATCH, only a whole-list
 * `PUT …/monitors` guarded by `If-Match: <revision>`. So every row
 * action (toggle / edit / delete) mutates a LOCAL draft and the
 * header's Save button ships the whole array. A 409 means someone else
 * saved in between: we toast, drop the draft, and rebuild it from the
 * refetched server list. Consequently the monitors query has NO
 * `refetchInterval` (a background poll would clobber in-flight edits);
 * only the read-only status query polls (15s).
 *
 * Per the repo-wide convention (see ChannelConfigEditor), Save is never
 * truly disabled — clicking with no diff explains itself via
 * `toast.info(channels.noChanges)`.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  Clock,
  Eye,
  MessagesSquare,
  Pencil,
  Play,
  Plus,
  Send,
  Timer,
  Trash2,
  Users,
  X,
} from "@/components/icons";
import { cn } from "@/lib/utils";
import { CorlinmanApiError } from "@/lib/api";
import {
  QQ_MONITOR_ID_RE,
  getQqDefaultInstanceId,
  getQqMonitors,
  getQqMonitorsStatus,
  putQqMonitors,
  triggerQqMonitor,
  type QqMonitorSpec,
  type QqMonitorStatusEntry,
} from "@/lib/api/qq-monitors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { FieldHint } from "@/components/ui/field-hint";
import { FilterChipGroup } from "@/components/ui/filter-chip-group";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { formatRelativeAgo } from "@/components/channels/qq/qq-util";

/* ------------------------------------------------------------------ */
/*                        Draft helpers                               */
/* ------------------------------------------------------------------ */

/** Fixed-order projection so draft-vs-server compares don't depend on
 * JSON key order. */
function normalizeSpec(m: QqMonitorSpec): unknown[] {
  return [
    m.id,
    m.enabled,
    m.source_group,
    [...m.watch_user_ids],
    m.schedule_type,
    m.daily_time,
    m.interval_minutes,
    m.timezone,
    m.window_minutes,
    m.target_type,
    m.target_id,
    m.style_extra,
    m.send_when_empty,
  ];
}

function sameMonitors(a: QqMonitorSpec[], b: QqMonitorSpec[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (
      JSON.stringify(normalizeSpec(a[i]!)) !==
      JSON.stringify(normalizeSpec(b[i]!))
    ) {
      return false;
    }
  }
  return true;
}

/* ------------------------------------------------------------------ */
/*                        Form model                                  */
/* ------------------------------------------------------------------ */

interface MonitorFormState {
  id: string;
  sourceGroup: string;
  watchMode: "all" | "selected";
  watchUserIds: string[];
  scheduleType: "daily" | "interval";
  dailyTime: string;
  intervalMinutes: string;
  timezone: string;
  windowMinutes: string;
  targetType: "group" | "user";
  targetId: string;
  styleExtra: string;
  sendWhenEmpty: boolean;
}

function emptyForm(): MonitorFormState {
  return {
    id: "",
    sourceGroup: "",
    watchMode: "all",
    watchUserIds: [],
    scheduleType: "daily",
    dailyTime: "09:00",
    intervalMinutes: "60",
    timezone: "",
    windowMinutes: "0",
    targetType: "group",
    targetId: "",
    styleExtra: "",
    sendWhenEmpty: false,
  };
}

function specToForm(spec: QqMonitorSpec): MonitorFormState {
  return {
    id: spec.id,
    sourceGroup: spec.source_group,
    watchMode: spec.watch_user_ids.length === 0 ? "all" : "selected",
    watchUserIds: [...spec.watch_user_ids],
    scheduleType: spec.schedule_type,
    dailyTime: spec.daily_time ?? "09:00",
    intervalMinutes: String(spec.interval_minutes ?? 60),
    timezone: spec.timezone,
    windowMinutes: String(spec.window_minutes),
    targetType: spec.target_type,
    targetId: spec.target_id,
    styleExtra: spec.style_extra,
    sendWhenEmpty: spec.send_when_empty,
  };
}

/** `enabled` carries over from the row being edited (create → true) —
 * the form itself has no enabled control; that's the row Switch. */
function formToSpec(form: MonitorFormState, enabled: boolean): QqMonitorSpec {
  return {
    id: form.id.trim(),
    enabled,
    source_group: form.sourceGroup.trim(),
    watch_user_ids: form.watchMode === "all" ? [] : [...form.watchUserIds],
    schedule_type: form.scheduleType,
    daily_time: form.scheduleType === "daily" ? form.dailyTime : null,
    interval_minutes:
      form.scheduleType === "interval" ? Number(form.intervalMinutes) : null,
    timezone: form.timezone.trim(),
    window_minutes: Number(form.windowMinutes),
    target_type: form.targetType,
    target_id: form.targetId.trim(),
    style_extra: form.styleExtra,
    send_when_empty: form.sendWhenEmpty,
  };
}

/** Field → i18n error key. Empty record = valid. */
function validateForm(
  f: MonitorFormState,
  takenIds: string[],
): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!QQ_MONITOR_ID_RE.test(f.id.trim())) {
    errors.id = "qqMonitor.form.idInvalid";
  } else if (takenIds.includes(f.id.trim())) {
    errors.id = "qqMonitor.form.idTaken";
  }
  if (!/^\d+$/.test(f.sourceGroup.trim())) {
    errors.sourceGroup = "qqMonitor.form.groupInvalid";
  }
  if (f.scheduleType === "daily") {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(f.dailyTime)) {
      errors.dailyTime = "qqMonitor.form.dailyTimeInvalid";
    }
  } else {
    const n = Number(f.intervalMinutes);
    if (!/^\d+$/.test(f.intervalMinutes.trim()) || !Number.isInteger(n) || n < 5) {
      errors.intervalMinutes = "qqMonitor.form.intervalInvalid";
    }
  }
  const w = Number(f.windowMinutes);
  if (!/^\d+$/.test(f.windowMinutes.trim()) || !Number.isInteger(w) || w < 0) {
    errors.windowMinutes = "qqMonitor.form.windowInvalid";
  }
  if (!/^\d+$/.test(f.targetId.trim())) {
    errors.targetId = "qqMonitor.form.targetIdInvalid";
  }
  return errors;
}

/* ------------------------------------------------------------------ */
/*                        Panel                                       */
/* ------------------------------------------------------------------ */

export function QqMonitorPanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const instanceQuery = useQuery({
    queryKey: ["admin", "channels", "qq", "monitors", "instance"],
    queryFn: getQqDefaultInstanceId,
    staleTime: 60_000,
  });
  const instanceId = instanceQuery.data;

  const monitorsKey = React.useMemo(
    () => ["admin", "channels", "qq", "monitors", "list", instanceId],
    [instanceId],
  );
  const statusKey = React.useMemo(
    () => ["admin", "channels", "qq", "monitors", "status", instanceId],
    [instanceId],
  );

  // NO refetchInterval here — a background poll would clobber the draft.
  const monitorsQuery = useQuery({
    queryKey: monitorsKey,
    queryFn: () => getQqMonitors(instanceId!),
    enabled: instanceId !== undefined,
  });

  // Read-only run stats — safe to poll.
  const statusQuery = useQuery({
    queryKey: statusKey,
    queryFn: () => getQqMonitorsStatus(instanceId!),
    enabled: instanceId !== undefined,
    refetchInterval: 15_000,
  });

  // ─── local draft (whole-list write model) ──────────────────────────
  const [draft, setDraft] = React.useState<QqMonitorSpec[] | null>(null);
  const [revision, setRevision] = React.useState<string | null>(null);
  // 409 recovery: rebuild the draft ONLY once fresh data (a new
  // revision) lands — re-initing from the still-cached stale snapshot
  // would just 409 again on the next save.
  const [resync, setResync] = React.useState(false);
  React.useEffect(() => {
    if (!monitorsQuery.data) return;
    if (draft === null) {
      setDraft(monitorsQuery.data.monitors);
      setRevision(monitorsQuery.data.revision);
      return;
    }
    if (resync && monitorsQuery.data.revision !== revision) {
      setDraft(monitorsQuery.data.monitors);
      setRevision(monitorsQuery.data.revision);
      setResync(false);
    }
  }, [monitorsQuery.data, draft, resync, revision]);

  const dirty = React.useMemo(() => {
    if (draft === null || !monitorsQuery.data) return false;
    return !sameMonitors(draft, monitorsQuery.data.monitors);
  }, [draft, monitorsQuery.data]);

  // ─── editor (create vs in-place edit, keyed by editingId) ──────────
  const [editorOpen, setEditorOpen] = React.useState(false);
  const [editingId, setEditingId] = React.useState<string | null>(null);
  const [formInitial, setFormInitial] = React.useState<MonitorFormState>(
    emptyForm,
  );
  const formAnchorRef = React.useRef<HTMLDivElement | null>(null);

  const [pendingDelete, setPendingDelete] = React.useState<string | null>(null);

  const openCreate = () => {
    setEditingId(null);
    setFormInitial(emptyForm());
    setEditorOpen(true);
  };

  const openEdit = (spec: QqMonitorSpec) => {
    setEditingId(spec.id);
    setFormInitial(specToForm(spec));
    setEditorOpen(true);
    // `scrollIntoView` is absent under jsdom — guard for tests.
    requestAnimationFrame(() => {
      formAnchorRef.current?.scrollIntoView?.({
        behavior: "smooth",
        block: "start",
      });
    });
  };

  const closeEditor = () => {
    setEditorOpen(false);
    setEditingId(null);
  };

  const applyForm = (form: MonitorFormState) => {
    if (draft === null) return;
    if (editingId === null) {
      setDraft([...draft, formToSpec(form, true)]);
    } else {
      setDraft(
        draft.map((m) =>
          m.id === editingId ? formToSpec(form, m.enabled) : m,
        ),
      );
    }
    closeEditor();
  };

  const toggleEnabled = (id: string, next: boolean) => {
    if (draft === null) return;
    setDraft(draft.map((m) => (m.id === id ? { ...m, enabled: next } : m)));
  };

  const confirmDelete = () => {
    if (draft === null || pendingDelete === null) return;
    setDraft(draft.filter((m) => m.id !== pendingDelete));
    if (editingId === pendingDelete) closeEditor();
    setPendingDelete(null);
  };

  // ─── mutations ─────────────────────────────────────────────────────
  const saveMutation = useMutation({
    mutationFn: () => putQqMonitors(instanceId!, draft!, revision!),
    onSuccess: (res) => {
      toast.success(t("qqMonitor.saved"));
      // Adopt the server echo as the new baseline (normalization included).
      setDraft(res.monitors);
      setRevision(res.revision);
      void qc.invalidateQueries({ queryKey: monitorsKey });
      void qc.invalidateQueries({ queryKey: statusKey });
    },
    onError: (err) => {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        // Someone else saved first — refetch and rebuild the draft from
        // the fresh list once its (new) revision lands.
        toast.error(t("qqMonitor.conflict"));
        setResync(true);
        void qc.invalidateQueries({ queryKey: monitorsKey });
        return;
      }
      toast.error(
        t("qqMonitor.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const onSaveClick = () => {
    if (saveMutation.isPending) return;
    if (!dirty) {
      toast.info(t("channels.noChanges"));
      return;
    }
    if (instanceId === undefined || draft === null || revision === null) return;
    saveMutation.mutate();
  };

  const triggerMutation = useMutation({
    mutationFn: (monitorId: string) => triggerQqMonitor(instanceId!, monitorId),
    onSuccess: () => toast.success(t("qqMonitor.triggered")),
    onError: (err) => {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        toast.error(t("qqMonitor.triggerOffline"));
        return;
      }
      toast.error(
        t("qqMonitor.triggerFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  // ─── derived render state ──────────────────────────────────────────
  // Only fall to the error line when we have NEVER built a draft — a
  // failed background refetch must not unmount the list mid-edit.
  const loadError =
    (instanceQuery.isError || monitorsQuery.isError) && draft === null;
  const loading = !loadError && draft === null;
  const warnings = monitorsQuery.data?.warnings ?? [];
  const statuses = statusQuery.data?.statuses ?? {};
  const counts = statusQuery.data?.counts ?? {};
  const now = Date.now();

  const saveBlocked = !dirty || saveMutation.isPending;

  return (
    <div className="flex flex-col gap-4" data-testid="qq-monitor-panel">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="flex items-center gap-2 text-lg font-medium tracking-tight">
            <Eye className="h-4 w-4 text-sg-accent" aria-hidden />
            {t("qqMonitor.title")}
          </h2>
          <p className="max-w-2xl text-sm text-sg-ink-3">{t("qqMonitor.lede")}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            data-testid="qq-monitor-add"
            onClick={openCreate}
          >
            <Plus className="mr-1 h-3.5 w-3.5" aria-hidden />
            {t("qqMonitor.addRule")}
          </Button>
          {/* Save is never hard-disabled (repo convention) — a no-diff
              click explains itself via toast instead of dying silently. */}
          <Button
            size="sm"
            data-testid="qq-monitor-save"
            onClick={onSaveClick}
            aria-disabled={saveBlocked}
            aria-busy={saveMutation.isPending}
            className={cn(saveBlocked && "opacity-60")}
          >
            {t("qqMonitor.save")}
          </Button>
        </div>
      </header>

      {dirty ? (
        <p className="text-xs text-sg-warn" data-testid="qq-monitor-dirty-hint">
          {t("qqMonitor.unsaved")}
        </p>
      ) : null}

      {warnings.length > 0 ? (
        <div
          role="status"
          className="rounded-sg-md border border-sg-warn/30 bg-sg-warn-soft px-3 py-2 text-xs text-sg-warn"
        >
          {warnings.map((w) => (
            <p key={w}>{w}</p>
          ))}
        </div>
      ) : null}

      {loadError ? (
        <p className="text-sm text-sg-err" role="alert">
          {t("qqMonitor.loadFailed")}
        </p>
      ) : loading ? (
        <div className="flex flex-col gap-2" data-testid="qq-monitor-skeleton">
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-14 w-full" />
        </div>
      ) : draft !== null && draft.length === 0 && !editorOpen ? (
        <EmptyState
          icon={<Eye aria-hidden />}
          title={t("qqMonitor.noRules")}
          description={t("qqMonitor.noRulesHint")}
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {(draft ?? []).map((spec) => (
            <MonitorRow
              key={spec.id}
              spec={spec}
              status={statuses[spec.id]}
              count={counts[spec.id] ?? 0}
              now={now}
              triggering={
                triggerMutation.isPending &&
                triggerMutation.variables === spec.id
              }
              onToggle={(next) => toggleEnabled(spec.id, next)}
              onEdit={() => openEdit(spec)}
              onDelete={() => setPendingDelete(spec.id)}
              onTrigger={() => triggerMutation.mutate(spec.id)}
            />
          ))}
        </ul>
      )}

      {editorOpen ? (
        <div ref={formAnchorRef}>
          <MonitorForm
            key={editingId ?? "__create__"}
            mode={editingId === null ? "create" : "edit"}
            initial={formInitial}
            takenIds={
              editingId === null ? (draft ?? []).map((m) => m.id) : []
            }
            onApply={applyForm}
            onCancel={closeEditor}
          />
        </div>
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={t("qqMonitor.deleteTitle")}
        description={t("qqMonitor.deleteBody", { id: pendingDelete ?? "" })}
        confirmLabel={t("qqMonitor.row.delete")}
        cancelLabel={t("qqMonitor.form.cancel")}
        onConfirm={confirmDelete}
        testId="qq-monitor-delete-confirm"
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*                        Row                                         */
/* ------------------------------------------------------------------ */

function MonitorRow({
  spec,
  status,
  count,
  now,
  triggering,
  onToggle,
  onEdit,
  onDelete,
  onTrigger,
}: {
  spec: QqMonitorSpec;
  status: QqMonitorStatusEntry | undefined;
  count: number;
  now: number;
  triggering: boolean;
  onToggle: (next: boolean) => void;
  onEdit: () => void;
  onDelete: () => void;
  onTrigger: () => void;
}) {
  const { t } = useTranslation();

  const scheduleLabel =
    spec.schedule_type === "daily"
      ? t("qqMonitor.row.scheduleDaily", { time: spec.daily_time ?? "—" })
      : t("qqMonitor.row.scheduleInterval", {
          minutes: spec.interval_minutes ?? "—",
        });
  const targetLabel =
    spec.target_type === "group"
      ? t("qqMonitor.row.targetGroup", { id: spec.target_id })
      : t("qqMonitor.row.targetUser", { id: spec.target_id });
  const lastRunAgo =
    status?.last_run_ms !== null && status?.last_run_ms !== undefined
      ? formatRelativeAgo(String(status.last_run_ms), now)
      : null;

  return (
    <li
      data-testid={`qq-monitor-row-${spec.id}`}
      className={cn(
        "flex flex-wrap items-center gap-3 rounded-xl border border-sg-border bg-sg-inset px-3 py-2.5",
        !spec.enabled && "opacity-70",
      )}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <code className="rounded-md border border-sg-border bg-sg-inset-strong px-2 py-0.5 font-mono text-[11px] text-sg-ink-2">
            {spec.id}
          </code>
          <span className="inline-flex items-center gap-1 text-sg-ink-2">
            <MessagesSquare className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden />
            {spec.source_group}
          </span>
          <span className="inline-flex items-center gap-1 text-sg-ink-3">
            <Users className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden />
            {spec.watch_user_ids.length === 0
              ? t("qqMonitor.row.watchAll")
              : t("qqMonitor.row.watchSome", {
                  n: spec.watch_user_ids.length,
                })}
          </span>
          <span className="inline-flex items-center gap-1 text-sg-ink-3">
            {spec.schedule_type === "daily" ? (
              <Clock className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden />
            ) : (
              <Timer className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden />
            )}
            {scheduleLabel}
          </span>
          <span className="inline-flex items-center gap-1 text-sg-ink-3">
            <Send className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden />
            {targetLabel}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-sg-ink-4">
          <span>{t("qqMonitor.row.captured", { n: count })}</span>
          <span>
            {lastRunAgo !== null
              ? t("qqMonitor.row.lastRun", { ago: lastRunAgo })
              : t("qqMonitor.row.neverRun")}
          </span>
          {status?.last_ok === false && status.last_error ? (
            <span className="text-sg-err">{status.last_error}</span>
          ) : null}
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        <Switch
          checked={spec.enabled}
          onCheckedChange={onToggle}
          aria-label={t("qqMonitor.row.enableAria", { id: spec.id })}
          data-testid={`qq-monitor-toggle-${spec.id}`}
        />
        <RowIconButton
          label={t("qqMonitor.row.trigger")}
          onClick={onTrigger}
          disabled={triggering}
          testId={`qq-monitor-trigger-${spec.id}`}
        >
          <Play
            className={cn("h-3.5 w-3.5", triggering && "animate-pulse")}
            aria-hidden
          />
        </RowIconButton>
        <RowIconButton
          label={t("qqMonitor.row.edit")}
          onClick={onEdit}
          testId={`qq-monitor-edit-${spec.id}`}
        >
          <Pencil className="h-3.5 w-3.5" aria-hidden />
        </RowIconButton>
        <RowIconButton
          label={t("qqMonitor.row.delete")}
          onClick={onDelete}
          testId={`qq-monitor-delete-${spec.id}`}
        >
          <Trash2 className="h-3.5 w-3.5" aria-hidden />
        </RowIconButton>
      </div>
    </li>
  );
}

function RowIconButton({
  label,
  testId,
  disabled,
  onClick,
  children,
}: {
  label: string;
  testId?: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      data-testid={testId}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-md",
        "text-sg-ink-3 transition-colors",
        "hover:bg-sg-inset-hover hover:text-sg-ink",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        "disabled:pointer-events-none disabled:opacity-40",
      )}
    >
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/*                        Upsert form                                 */
/* ------------------------------------------------------------------ */

function MonitorForm({
  mode,
  initial,
  takenIds,
  onApply,
  onCancel,
}: {
  mode: "create" | "edit";
  initial: MonitorFormState;
  /** Ids already in the draft (create mode only — edit keeps its id). */
  takenIds: string[];
  onApply: (form: MonitorFormState) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = React.useState<MonitorFormState>(initial);
  const [attempted, setAttempted] = React.useState(false);
  const [watchDraft, setWatchDraft] = React.useState("");

  const errors = validateForm(form, mode === "create" ? takenIds : []);
  const set = <K extends keyof MonitorFormState>(
    key: K,
    value: MonitorFormState[K],
  ) => setForm((f) => ({ ...f, [key]: value }));

  /** Inline errors show after a submit attempt, or as soon as the field
   * holds a non-empty invalid value (live feedback while typing). */
  const showError = (field: string, value: string): string | null => {
    const err = errors[field];
    if (!err) return null;
    return attempted || value.trim() !== "" ? err : null;
  };

  const addWatchId = (raw: string) => {
    const id = raw.trim();
    if (!/^\d+$/.test(id)) return;
    if (form.watchUserIds.includes(id)) {
      setWatchDraft("");
      return;
    }
    set("watchUserIds", [...form.watchUserIds, id]);
    setWatchDraft("");
  };
  const removeWatchId = (id: string) =>
    set(
      "watchUserIds",
      form.watchUserIds.filter((x) => x !== id),
    );

  const apply = () => {
    setAttempted(true);
    if (Object.keys(errors).length > 0) return;
    onApply(form);
  };

  const errLine = (key: string | null, testId: string) =>
    key ? (
      <p role="alert" data-testid={testId} className="text-xs text-sg-err">
        {t(key)}
      </p>
    ) : null;

  return (
    <div
      data-testid="qq-monitor-form"
      className="flex flex-col gap-4 rounded-xl border border-sg-border bg-sg-inset px-4 py-4"
    >
      <h3 className="text-sm font-medium text-sg-ink">
        {mode === "create"
          ? t("qqMonitor.form.createTitle")
          : t("qqMonitor.form.editTitle", { id: initial.id })}
      </h3>

      <div className="grid gap-4 md:grid-cols-2">
        {/* id ---------------------------------------------------------- */}
        <div className="space-y-1.5">
          <Label htmlFor="qq-monitor-form-id">{t("qqMonitor.form.idLabel")}</Label>
          <Input
            id="qq-monitor-form-id"
            data-testid="qq-monitor-form-id"
            value={form.id}
            onChange={(e) => set("id", e.target.value)}
            readOnly={mode === "edit"}
            className={cn("font-mono", mode === "edit" && "opacity-60")}
            placeholder="daily-digest"
            spellCheck={false}
          />
          <FieldHint>{t("qqMonitor.form.idHint")}</FieldHint>
          {mode === "create"
            ? errLine(showError("id", form.id), "qq-monitor-err-id")
            : null}
        </div>

        {/* source group ------------------------------------------------- */}
        <div className="space-y-1.5">
          <Label htmlFor="qq-monitor-form-group">
            {t("qqMonitor.form.groupLabel")}
          </Label>
          <Input
            id="qq-monitor-form-group"
            data-testid="qq-monitor-form-group"
            value={form.sourceGroup}
            onChange={(e) => set("sourceGroup", e.target.value)}
            inputMode="numeric"
            className="font-mono"
            placeholder="123456789"
          />
          {errLine(
            showError("sourceGroup", form.sourceGroup),
            "qq-monitor-err-group",
          )}
        </div>

        {/* watch scope -------------------------------------------------- */}
        <div className="space-y-1.5 md:col-span-2">
          <span className="block text-[13px] font-medium leading-none text-sg-ink-2">
            {t("qqMonitor.form.watchLabel")}
          </span>
          <FilterChipGroup
            label={t("qqMonitor.form.watchLabel")}
            value={form.watchMode}
            onChange={(next) =>
              set("watchMode", next === "selected" ? "selected" : "all")
            }
            options={[
              { value: "all", label: t("qqMonitor.form.watchAll") },
              { value: "selected", label: t("qqMonitor.form.watchSome") },
            ]}
          />
          {form.watchMode === "selected" ? (
            <div className="flex min-h-[34px] flex-wrap items-center gap-1.5 rounded-md border border-sg-border bg-sg-inset px-2 py-1.5">
              {form.watchUserIds.map((id) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => removeWatchId(id)}
                  aria-label={t("qqMonitor.form.watchRemoveAria", { id })}
                  className={cn(
                    "group/chip inline-flex items-center gap-1 rounded-md border px-2 py-[2px] font-mono text-[10.5px]",
                    "border-sg-accent/30 bg-sg-accent-soft text-sg-accent",
                    "transition-colors",
                    "hover:bg-[color-mix(in_oklch,var(--sg-accent)_22%,transparent)]",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
                  )}
                >
                  {id}
                  <X
                    className="h-3 w-3 opacity-70 group-hover/chip:opacity-100"
                    aria-hidden
                  />
                </button>
              ))}
              <input
                value={watchDraft}
                data-testid="qq-monitor-form-watch-input"
                onChange={(e) => setWatchDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addWatchId(watchDraft);
                  } else if (
                    e.key === "Backspace" &&
                    !watchDraft &&
                    form.watchUserIds.length > 0
                  ) {
                    e.preventDefault();
                    removeWatchId(
                      form.watchUserIds[form.watchUserIds.length - 1]!,
                    );
                  }
                }}
                placeholder={t("qqMonitor.form.watchInputPlaceholder")}
                aria-label={t("qqMonitor.form.watchInputPlaceholder")}
                inputMode="numeric"
                className="h-7 min-w-[140px] flex-1 bg-transparent px-1 font-mono text-[11px] text-sg-ink placeholder:text-sg-ink-4 focus:outline-none"
              />
            </div>
          ) : null}
          <FieldHint>{t("qqMonitor.form.watchHint")}</FieldHint>
        </div>

        {/* schedule ----------------------------------------------------- */}
        <div className="space-y-1.5">
          <span className="block text-[13px] font-medium leading-none text-sg-ink-2">
            {t("qqMonitor.form.scheduleLabel")}
          </span>
          <FilterChipGroup
            label={t("qqMonitor.form.scheduleLabel")}
            value={form.scheduleType}
            onChange={(next) =>
              set("scheduleType", next === "interval" ? "interval" : "daily")
            }
            options={[
              { value: "daily", label: t("qqMonitor.form.scheduleDaily") },
              {
                value: "interval",
                label: t("qqMonitor.form.scheduleInterval"),
              },
            ]}
          />
          {form.scheduleType === "daily" ? (
            <>
              <Label htmlFor="qq-monitor-form-daily-time" className="sr-only">
                {t("qqMonitor.form.dailyTimeLabel")}
              </Label>
              <Input
                id="qq-monitor-form-daily-time"
                data-testid="qq-monitor-form-daily-time"
                type="time"
                value={form.dailyTime}
                onChange={(e) => set("dailyTime", e.target.value)}
                className="font-mono"
              />
              {errLine(
                showError("dailyTime", form.dailyTime),
                "qq-monitor-err-daily-time",
              )}
            </>
          ) : (
            <>
              <Label htmlFor="qq-monitor-form-interval" className="sr-only">
                {t("qqMonitor.form.intervalLabel")}
              </Label>
              <Input
                id="qq-monitor-form-interval"
                data-testid="qq-monitor-form-interval"
                type="number"
                inputMode="numeric"
                min={5}
                value={form.intervalMinutes}
                onChange={(e) => set("intervalMinutes", e.target.value)}
                className="font-mono"
              />
              <FieldHint>{t("qqMonitor.form.intervalHint")}</FieldHint>
              {errLine(
                showError("intervalMinutes", form.intervalMinutes),
                "qq-monitor-err-interval",
              )}
            </>
          )}
        </div>

        {/* window + timezone -------------------------------------------- */}
        <div className="space-y-1.5">
          <Label htmlFor="qq-monitor-form-window">
            {t("qqMonitor.form.windowLabel")}
          </Label>
          <Input
            id="qq-monitor-form-window"
            data-testid="qq-monitor-form-window"
            type="number"
            inputMode="numeric"
            min={0}
            value={form.windowMinutes}
            onChange={(e) => set("windowMinutes", e.target.value)}
            className="font-mono"
          />
          <FieldHint>{t("qqMonitor.form.windowHint")}</FieldHint>
          {errLine(
            showError("windowMinutes", form.windowMinutes),
            "qq-monitor-err-window",
          )}
          <Label htmlFor="qq-monitor-form-timezone">
            {t("qqMonitor.form.timezoneLabel")}
          </Label>
          <Input
            id="qq-monitor-form-timezone"
            data-testid="qq-monitor-form-timezone"
            value={form.timezone}
            onChange={(e) => set("timezone", e.target.value)}
            placeholder="Asia/Shanghai"
            spellCheck={false}
          />
          <FieldHint>{t("qqMonitor.form.timezoneHint")}</FieldHint>
        </div>

        {/* target ------------------------------------------------------- */}
        <div className="space-y-1.5 md:col-span-2">
          <span className="block text-[13px] font-medium leading-none text-sg-ink-2">
            {t("qqMonitor.form.targetLabel")}
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <FilterChipGroup
              label={t("qqMonitor.form.targetLabel")}
              value={form.targetType}
              onChange={(next) =>
                set("targetType", next === "user" ? "user" : "group")
              }
              options={[
                { value: "group", label: t("qqMonitor.form.targetGroup") },
                { value: "user", label: t("qqMonitor.form.targetUser") },
              ]}
            />
            <Input
              data-testid="qq-monitor-form-target-id"
              value={form.targetId}
              onChange={(e) => set("targetId", e.target.value)}
              inputMode="numeric"
              className="max-w-[220px] font-mono"
              placeholder={
                form.targetType === "group" ? "123456789" : "10001"
              }
              aria-label={t("qqMonitor.form.targetIdLabel")}
            />
          </div>
          {errLine(
            showError("targetId", form.targetId),
            "qq-monitor-err-target-id",
          )}
        </div>

        {/* style extra --------------------------------------------------- */}
        <div className="space-y-1.5 md:col-span-2">
          <Label htmlFor="qq-monitor-form-style">
            {t("qqMonitor.form.styleLabel")}
          </Label>
          <textarea
            id="qq-monitor-form-style"
            data-testid="qq-monitor-form-style"
            value={form.styleExtra}
            onChange={(e) => set("styleExtra", e.target.value)}
            placeholder={t("qqMonitor.form.stylePlaceholder")}
            spellCheck={false}
            className={cn(
              "flex min-h-[72px] w-full rounded-md border border-input bg-transparent",
              "px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            )}
          />
          <FieldHint>{t("qqMonitor.form.styleHint")}</FieldHint>
        </div>

        {/* send-when-empty + actions ------------------------------------ */}
        <div className="flex flex-wrap items-center justify-between gap-3 md:col-span-2">
          <div className="flex items-center gap-2">
            <Switch
              id="qq-monitor-form-send-empty"
              data-testid="qq-monitor-form-send-empty"
              checked={form.sendWhenEmpty}
              onCheckedChange={(v) => set("sendWhenEmpty", v)}
              aria-label={t("qqMonitor.form.sendWhenEmptyLabel")}
            />
            <Label
              htmlFor="qq-monitor-form-send-empty"
              className="cursor-pointer"
            >
              {t("qqMonitor.form.sendWhenEmptyLabel")}
            </Label>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={onCancel}
              data-testid="qq-monitor-form-cancel"
            >
              <X className="mr-1 h-3.5 w-3.5" aria-hidden />
              {t("qqMonitor.form.cancel")}
            </Button>
            <Button
              size="sm"
              onClick={apply}
              data-testid="qq-monitor-form-apply"
            >
              {mode === "create"
                ? t("qqMonitor.form.applyCreate")
                : t("qqMonitor.form.applyEdit")}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default QqMonitorPanel;
