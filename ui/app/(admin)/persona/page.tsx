"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Pencil, Plus, Sparkles, Trash2 } from "lucide-react";

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
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
  ASSET_ALLOWED_MIMES,
  ASSET_LABEL_RE,
  ASSET_MAX_BYTES,
  AssetUploadError,
  PERSONA_TOTAL_BYTES_CAP,
  REFERENCE_VISIBLE_CAP,
  SUPPORTED_HUMANLIKE_CHANNELS,
  createPersona,
  deleteAsset,
  deletePersona,
  fetchHumanlike,
  fetchPersonas,
  listAssets,
  setHumanlike,
  updatePersona,
  uploadAsset,
  type AssetKind,
  type AssetRecord,
  type AssetUploadErrorCode,
  type HumanlikeChannel,
  type HumanlikeState,
  type NewPersona,
  type PartialPersona,
  type Persona,
} from "@/lib/api/personas";

/**
 * `/admin/persona` — operator-facing persona management.
 *
 * Three concerns on one page:
 *   1. **QQ humanlike toggle** — `Switch` + persona `Select`. When the
 *      toggle is on the agent replies as the chosen persona across QQ DMs
 *      and groups; flipping it off restarts the channel without any
 *      persona binding.
 *   2. **Personas list** — `Table` of every persona. Built-ins carry the
 *      `built-in` badge and have their Delete button disabled (the
 *      backend returns 404 if you try anyway — we collapse that into a
 *      friendly toast). Custom ones can be edited or removed.
 *   3. **Editor modal** — `Dialog`-based editor for both create and
 *      update flows. `id` is read-only when editing (the URL is the
 *      source of truth; rename is a future endpoint). The system-prompt
 *      `<textarea>` is monospace with a 400px minimum height so 5–10k
 *      char markdown bodies are comfortable to scan.
 *
 * The "test box" (single-input preview) and "reset to default" button
 * are intentionally rendered disabled — the backend has no preview /
 * reset endpoints yet, so showing them avoids a surprising "the
 * button disappeared in the next release" rather than a surprising
 * "this button does nothing" UX.
 */

const PERSONAS_QUERY_KEY = ["admin", "personas"] as const;
const humanlikeQueryKey = (channel: HumanlikeChannel) =>
  ["admin", "channels", channel, "humanlike"] as const;

/** Lowercase a–z / 0–9 / hyphens, leading char alpha-or-digit. Same
 * shape as the backend slug validator for custom providers — keep it
 * narrow so a stray space or capital letter is caught client-side. */
const SLUG_RE = /^[a-z0-9][a-z0-9-]*$/;

export default function PersonaPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const personasQuery = useQuery<Persona[]>({
    queryKey: PERSONAS_QUERY_KEY,
    queryFn: () => fetchPersonas(),
  });

  const personas = personasQuery.data ?? [];

  // Editor state — `editing === null` is the new-persona flow. When
  // editing an existing row we stash the snapshot so the modal can hint
  // the resetTo-default button (which is presentational-only today).
  const [editorOpen, setEditorOpen] = React.useState(false);
  const [editing, setEditing] = React.useState<Persona | null>(null);
  const [pendingDelete, setPendingDelete] = React.useState<Persona | null>(null);

  function openCreate() {
    setEditing(null);
    setEditorOpen(true);
  }
  function openEdit(p: Persona) {
    setEditing(p);
    setEditorOpen(true);
  }

  /* ----------------------- Delete mutation ----------------------- */

  const deleteMutation = useMutation({
    mutationFn: async (p: Persona) => {
      const result = await deletePersona(p.id);
      return { persona: p, result };
    },
    onSuccess: async ({ persona, result }) => {
      if (result === "builtin_protected") {
        toast.error(t("persona.deleteBuiltinBlocked"));
        // No optimistic mutation — refetch so list stays in sync.
        await queryClient.invalidateQueries({ queryKey: PERSONAS_QUERY_KEY });
        return;
      }
      toast.success(
        t("persona.deleteSucceeded", { name: persona.display_name }),
      );
      // Optimistically prune the row, then refetch.
      queryClient.setQueryData<Persona[]>(PERSONAS_QUERY_KEY, (prev) =>
        (prev ?? []).filter((x) => x.id !== persona.id),
      );
      await queryClient.invalidateQueries({ queryKey: PERSONAS_QUERY_KEY });
    },
    onError: (err) => {
      toast.error(
        t("persona.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  /* ------------------- Editor save mutation -------------------- */

  const saveMutation = useMutation({
    mutationFn: async (payload: NewPersona | { id: string; patch: PartialPersona }) => {
      if ("patch" in payload) {
        const updated = await updatePersona(payload.id, payload.patch);
        return { mode: "update" as const, persona: updated };
      }
      const created = await createPersona(payload);
      return { mode: "create" as const, persona: created };
    },
    onSuccess: async ({ mode, persona }) => {
      if (mode === "create") {
        toast.success(
          t("persona.createSucceeded", { name: persona.display_name }),
        );
      } else {
        toast.success(
          t("persona.updateSucceeded", { name: persona.display_name }),
        );
      }
      setEditorOpen(false);
      setEditing(null);
      await queryClient.invalidateQueries({ queryKey: PERSONAS_QUERY_KEY });
    },
    onError: (err, vars) => {
      const isUpdate = "patch" in vars;
      toast.error(
        t(isUpdate ? "persona.updateFailed" : "persona.createFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  return (
    <>
      <header className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Sparkles className="h-5 w-5 text-tp-amber" aria-hidden="true" />
          {t("persona.title")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("persona.subtitle")}</p>
      </header>

      <HumanlikeCard personas={personas} />

      <section
        className="space-y-3"
        aria-labelledby="personas-list-heading"
      >
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-1">
            <h2
              id="personas-list-heading"
              className="text-lg font-medium tracking-tight"
            >
              {t("persona.listTitle")}
            </h2>
            <p className="text-xs text-tp-ink-3">{t("persona.listSubtitle")}</p>
          </div>
          <Button
            type="button"
            size="sm"
            onClick={openCreate}
            data-testid="persona-new"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {t("persona.newPersona")}
          </Button>
        </div>

        <div className="overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass">
          <Table>
            <TableHeader>
              <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
                <TableHead className="pl-4">{t("persona.colName")}</TableHead>
                <TableHead>{t("persona.colSummary")}</TableHead>
                <TableHead className="w-24">{t("persona.colBuiltin")}</TableHead>
                <TableHead className="w-48">{t("persona.colUpdated")}</TableHead>
                <TableHead className="w-40 pr-4 text-right">
                  {t("persona.colActions")}
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {personasQuery.isPending ? (
                <PersonasTableSkeleton />
              ) : personasQuery.isError ? (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-10 text-center text-sm text-destructive"
                    data-testid="personas-load-failed"
                  >
                    {t("persona.loadFailed")}:{" "}
                    {(personasQuery.error as Error).message}
                  </TableCell>
                </TableRow>
              ) : personas.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-10 text-center text-sm text-tp-ink-3"
                    data-testid="personas-empty"
                  >
                    {t("persona.empty")}
                  </TableCell>
                </TableRow>
              ) : (
                personas.map((p) => (
                  <PersonaRow
                    key={p.id}
                    persona={p}
                    onEdit={openEdit}
                    onDelete={setPendingDelete}
                  />
                ))
              )}
            </TableBody>
          </Table>
        </div>
      </section>

      <PersonaEditorDialog
        open={editorOpen}
        onOpenChange={(open) => {
          if (!open) {
            setEditorOpen(false);
            setEditing(null);
          }
        }}
        existing={editing}
        existingIds={personas.map((p) => p.id)}
        saving={saveMutation.isPending}
        onSubmit={(payload) => saveMutation.mutate(payload)}
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={t("persona.deleteConfirmTitle")}
        description={t("persona.deleteConfirmBody")}
        cancelLabel={t("persona.cancel")}
        confirmLabel={t("persona.deleteConfirmAction")}
        testId="persona-delete-confirm"
        onConfirm={async () => {
          const target = pendingDelete;
          setPendingDelete(null);
          if (target) await deleteMutation.mutateAsync(target);
        }}
      />
    </>
  );
}

/* ----------------------------------------------------------------- */
/*                     QQ humanlike toggle card                      */
/* ----------------------------------------------------------------- */

/**
 * Top card on the page — toggles humanlike mode on the QQ OneBot channel
 * and lets the operator pick which persona to bind. We treat the toggle
 * + select + save as a single editor: changes are local until the
 * operator hits Save, mirroring the explicit-save pattern in the rest
 * of the admin (Models, Providers).
 */
function HumanlikeCard({ personas }: { personas: Persona[] }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Which channel's humanlike block we're editing. The backend supports
  // qq/telegram/discord/slack/feishu uniformly; the live resolver re-reads
  // the persisted block per inbound message, so a save takes effect without
  // a channel restart.
  const [channel, setChannel] = React.useState<HumanlikeChannel>("qq");

  const humanlikeQuery = useQuery<HumanlikeState>({
    queryKey: humanlikeQueryKey(channel),
    queryFn: () => fetchHumanlike(channel),
  });
  const state = humanlikeQuery.data ?? null;
  const isLoading = humanlikeQuery.isPending;
  const loadError = humanlikeQuery.error ?? null;

  // Local edit state, re-seeded from the persisted state of the *current*
  // channel. Keyed on channel so switching channels reflects that channel's
  // saved values rather than carrying over the previous edit.
  const [enabled, setEnabled] = React.useState(false);
  const [personaId, setPersonaId] = React.useState<string | null>(null);

  React.useEffect(() => {
    setEnabled(state?.enabled ?? false);
    setPersonaId(state?.persona_id ?? null);
  }, [channel, state?.enabled, state?.persona_id]);

  const mutation = useMutation({
    mutationFn: async (next: HumanlikeState) => setHumanlike(channel, next),
    onSuccess: async (next) => {
      toast.success(t("persona.saveSucceeded"));
      queryClient.setQueryData<HumanlikeState>(humanlikeQueryKey(channel), next);
      await queryClient.invalidateQueries({
        queryKey: humanlikeQueryKey(channel),
      });
    },
    onError: (err) => {
      toast.error(
        t("persona.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  function onSave() {
    mutation.mutate({ enabled, persona_id: enabled ? personaId : null });
  }

  // Status line uses the persisted (server-confirmed) state, not the
  // local-edit state — gives the operator a clear "what's live" signal
  // distinct from the unsaved-edit affordance.
  const livePersonaName = React.useMemo(() => {
    if (!state?.persona_id) return null;
    return personas.find((p) => p.id === state.persona_id)?.display_name ?? state.persona_id;
  }, [personas, state]);

  let statusLine: string;
  if (!state || !state.enabled) {
    statusLine = t("persona.statusOff");
  } else if (livePersonaName) {
    statusLine = t("persona.statusOn", { name: livePersonaName });
  } else {
    statusLine = t("persona.statusOnNoPersona");
  }

  const isDirty =
    state !== null &&
    (state.enabled !== enabled ||
      (state.persona_id ?? null) !== (enabled ? personaId : null));

  return (
    <Card data-testid="qq-humanlike-card">
      <CardHeader>
        <CardTitle className="text-base">{t("persona.toggleTitle")}</CardTitle>
        <CardDescription>{t("persona.toggleDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loadError ? (
          <div
            role="alert"
            data-testid="qq-humanlike-load-error"
            className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
          >
            {t("persona.loadHumanlikeFailed", { msg: loadError.message })}
          </div>
        ) : null}

        <div className="space-y-2">
          <Label
            htmlFor="humanlike-channel"
            className="text-xs uppercase tracking-wider text-tp-ink-3"
          >
            {t("persona.channelSelectLabel")}
          </Label>
          <select
            id="humanlike-channel"
            value={channel}
            onChange={(e) => setChannel(e.target.value as HumanlikeChannel)}
            data-testid="humanlike-channel-select"
            className={cn(
              "emboss-inset flex h-10 w-full rounded-md px-3 py-1 text-sm transition-colors",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              "appearance-none bg-tp-glass-inner",
            )}
          >
            {SUPPORTED_HUMANLIKE_CHANNELS.map((c) => (
              <option key={c} value={c}>
                {t(`persona.channelName.${c}`)}
              </option>
            ))}
          </select>
        </div>

        <div className="flex items-center justify-between gap-4 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2">
          <Label htmlFor="qq-humanlike-toggle" className="text-sm">
            {t("persona.toggleLabel")}
          </Label>
          <Switch
            id="qq-humanlike-toggle"
            checked={enabled}
            onCheckedChange={(next) => setEnabled(next)}
            disabled={isLoading}
            data-testid="qq-humanlike-toggle"
            aria-label={t("persona.toggleLabel")}
          />
        </div>

        {enabled ? (
          <div className="space-y-2">
            <Label htmlFor="qq-humanlike-persona" className="text-xs uppercase tracking-wider text-tp-ink-3">
              {t("persona.personaSelectLabel")}
            </Label>
            <PersonaSelect
              id="qq-humanlike-persona"
              personas={personas}
              value={personaId}
              onChange={setPersonaId}
              placeholder={t("persona.personaSelectPlaceholder")}
              disabled={isLoading || personas.length === 0}
            />
          </div>
        ) : null}

        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-tp-ink-3" data-testid="qq-humanlike-status">
            {statusLine}
          </p>
          <Button
            type="button"
            size="sm"
            onClick={onSave}
            disabled={
              isLoading ||
              mutation.isPending ||
              !isDirty ||
              (enabled && !personaId)
            }
            data-testid="qq-humanlike-save"
          >
            {mutation.isPending ? t("persona.saving") : t("persona.save")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * Native `<select>` wrapped in our glass styling. We don't have a
 * shadcn `Select` component in this repo and the spec is firm on
 * "use existing shadcn/ui components — no new deps", so we stick to
 * the platform primitive (which is also fully keyboard-accessible
 * and screen-reader-friendly without extra ARIA wiring).
 */
function PersonaSelect({
  id,
  personas,
  value,
  onChange,
  placeholder,
  disabled,
}: {
  id?: string;
  personas: Persona[];
  value: string | null;
  onChange: (next: string | null) => void;
  placeholder: string;
  disabled?: boolean;
}) {
  return (
    <select
      id={id}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      disabled={disabled}
      data-testid="qq-humanlike-persona-select"
      className={cn(
        "emboss-inset flex h-10 w-full rounded-md px-3 py-1 text-sm transition-colors",
        "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "appearance-none bg-tp-glass-inner",
      )}
    >
      <option value="">{placeholder}</option>
      {personas.map((p) => (
        <option key={p.id} value={p.id}>
          {p.display_name} ({p.id})
        </option>
      ))}
    </select>
  );
}

/* ----------------------------------------------------------------- */
/*                          List + row                               */
/* ----------------------------------------------------------------- */

function PersonaRow({
  persona,
  onEdit,
  onDelete,
}: {
  persona: Persona;
  onEdit: (p: Persona) => void;
  onDelete: (p: Persona) => void;
}) {
  const { t } = useTranslation();
  const updated = React.useMemo(
    () => new Date(persona.updated_at_ms).toLocaleString(),
    [persona.updated_at_ms],
  );
  return (
    <TableRow
      data-testid={`persona-row-${persona.id}`}
      className="border-b border-tp-glass-edge"
    >
      <TableCell className="pl-4 font-medium">
        <div className="flex flex-col gap-0.5">
          <span>{persona.display_name}</span>
          <span className="font-mono text-[11px] text-tp-ink-3">
            {persona.id}
          </span>
        </div>
      </TableCell>
      <TableCell className="text-sm text-tp-ink-2">
        <span className="line-clamp-2">{persona.short_summary}</span>
      </TableCell>
      <TableCell>
        {persona.is_builtin ? (
          <Badge variant="secondary" data-testid={`persona-builtin-${persona.id}`}>
            {t("persona.builtinBadge")}
          </Badge>
        ) : null}
      </TableCell>
      <TableCell className="text-xs text-tp-ink-3">{updated}</TableCell>
      <TableCell className="pr-4 text-right">
        <div className="inline-flex gap-1">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onEdit(persona)}
            data-testid={`persona-edit-${persona.id}`}
          >
            <Pencil className="h-3 w-3" aria-hidden="true" />
            {t("persona.edit")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onDelete(persona)}
            disabled={persona.is_builtin}
            title={
              persona.is_builtin
                ? t("persona.builtinDeleteTooltip")
                : undefined
            }
            aria-label={t("persona.deleteAriaLabel", {
              name: persona.display_name,
            })}
            data-testid={`persona-delete-${persona.id}`}
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
          >
            <Trash2 className="h-3 w-3" aria-hidden="true" />
            {t("persona.delete")}
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

function PersonasTableSkeleton() {
  return (
    <>
      {Array.from({ length: 2 }).map((_, i) => (
        <TableRow
          key={`persona-sk-${i}`}
          className="border-b border-tp-glass-edge"
        >
          <TableCell className="pl-4">
            <Skeleton className="h-4 w-32" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-64" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-12" />
          </TableCell>
          <TableCell>
            <Skeleton className="h-4 w-32" />
          </TableCell>
          <TableCell className="pr-4 text-right">
            <Skeleton className="ml-auto h-7 w-28" />
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}

/* ----------------------------------------------------------------- */
/*                          Editor dialog                            */
/* ----------------------------------------------------------------- */

interface EditorDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` → create flow. */
  existing: Persona | null;
  /** Used to validate "new slug must not collide". */
  existingIds: string[];
  saving: boolean;
  onSubmit: (payload: NewPersona | { id: string; patch: PartialPersona }) => void;
}

function PersonaEditorDialog({
  open,
  onOpenChange,
  existing,
  existingIds,
  saving,
  onSubmit,
}: EditorDialogProps) {
  const { t } = useTranslation();

  const [id, setId] = React.useState("");
  const [displayName, setDisplayName] = React.useState("");
  const [shortSummary, setShortSummary] = React.useState("");
  const [systemPrompt, setSystemPrompt] = React.useState("");
  const [errors, setErrors] = React.useState<Record<string, string>>({});

  // Re-seed fields whenever the dialog opens with a fresh target.
  React.useEffect(() => {
    if (!open) return;
    if (existing) {
      setId(existing.id);
      setDisplayName(existing.display_name);
      setShortSummary(existing.short_summary);
      setSystemPrompt(existing.system_prompt);
    } else {
      setId("");
      setDisplayName("");
      setShortSummary("");
      setSystemPrompt("");
    }
    setErrors({});
  }, [open, existing]);

  function validate(): boolean {
    const next: Record<string, string> = {};
    if (!existing) {
      const trimmedId = id.trim();
      if (!trimmedId) next.id = t("persona.errIdRequired");
      else if (!SLUG_RE.test(trimmedId))
        next.id = t("persona.errIdInvalid");
      else if (existingIds.includes(trimmedId))
        next.id = t("persona.errIdInvalid");
    }
    if (!displayName.trim()) next.display_name = t("persona.errDisplayNameRequired");
    if (!shortSummary.trim()) next.short_summary = t("persona.errSummaryRequired");
    if (!systemPrompt.trim()) next.system_prompt = t("persona.errPromptRequired");
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  function onSave() {
    if (!validate()) return;
    if (existing) {
      // Build a *partial* patch — only include fields that actually
      // changed. The backend accepts the full body too, but a minimal
      // PATCH is cheaper to log and clearly signals intent.
      const patch: PartialPersona = {};
      if (displayName !== existing.display_name) patch.display_name = displayName;
      if (shortSummary !== existing.short_summary)
        patch.short_summary = shortSummary;
      if (systemPrompt !== existing.system_prompt)
        patch.system_prompt = systemPrompt;
      onSubmit({ id: existing.id, patch });
    } else {
      onSubmit({
        id: id.trim(),
        display_name: displayName.trim(),
        short_summary: shortSummary.trim(),
        system_prompt: systemPrompt,
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl" data-testid="persona-editor">
        <DialogHeader>
          <DialogTitle>
            {existing
              ? t("persona.editorEditTitle", { name: existing.display_name })
              : t("persona.editorNewTitle")}
          </DialogTitle>
          <DialogDescription>
            {t("persona.editorDescription")}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3">
          <div className="space-y-1">
            <Label htmlFor="persona-id" className="text-xs uppercase tracking-wider text-tp-ink-3">
              {t("persona.fieldId")}
            </Label>
            <Input
              id="persona-id"
              value={id}
              onChange={(e) => setId(e.target.value)}
              disabled={existing !== null}
              data-testid="persona-id-input"
              placeholder="grantley"
              className="font-mono"
            />
            <p className="text-[11px] text-tp-ink-3">{t("persona.fieldIdHint")}</p>
            {errors.id ? (
              <p className="text-xs text-destructive" data-testid="persona-id-error">
                {errors.id}
              </p>
            ) : null}
          </div>

          <div className="space-y-1">
            <Label htmlFor="persona-display-name" className="text-xs uppercase tracking-wider text-tp-ink-3">
              {t("persona.fieldDisplayName")}
            </Label>
            <Input
              id="persona-display-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              data-testid="persona-display-name-input"
              placeholder="格兰特利·贝尔"
            />
            {errors.display_name ? (
              <p className="text-xs text-destructive">{errors.display_name}</p>
            ) : null}
          </div>

          <div className="space-y-1">
            <Label htmlFor="persona-short-summary" className="text-xs uppercase tracking-wider text-tp-ink-3">
              {t("persona.fieldShortSummary")}
            </Label>
            <Input
              id="persona-short-summary"
              value={shortSummary}
              onChange={(e) => setShortSummary(e.target.value)}
              data-testid="persona-short-summary-input"
              placeholder="..."
            />
            <p className="text-[11px] text-tp-ink-3">{t("persona.fieldShortSummaryHint")}</p>
            {errors.short_summary ? (
              <p className="text-xs text-destructive">{errors.short_summary}</p>
            ) : null}
          </div>

          <div className="space-y-1">
            <Label htmlFor="persona-system-prompt" className="text-xs uppercase tracking-wider text-tp-ink-3">
              {t("persona.fieldSystemPrompt")}
            </Label>
            <textarea
              id="persona-system-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              data-testid="persona-system-prompt-textarea"
              placeholder="# Persona name\n…"
              spellCheck={false}
              className={cn(
                "flex min-h-[400px] w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm shadow-sm",
                "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              )}
            />
            <p className="text-[11px] text-tp-ink-3">{t("persona.fieldSystemPromptHint")}</p>
            {errors.system_prompt ? (
              <p className="text-xs text-destructive">{errors.system_prompt}</p>
            ) : null}
          </div>

          {/* Test box — server has no preview endpoint yet, so the input
              + button are disabled with a tooltip pointing at the real-
              world test path (DM @QQbot). Keeps the affordance visible
              so the operator knows the surface exists; flips on once
              the backend lands. */}
          <fieldset
            className="space-y-1 rounded-md border border-dashed border-tp-glass-edge px-3 py-2"
            data-testid="persona-test-box"
          >
            <Label className="text-xs uppercase tracking-wider text-tp-ink-3">
              {t("persona.testBoxTitle")}
            </Label>
            <div className="flex items-center gap-2">
              <Input
                disabled
                placeholder={t("persona.testBoxPlaceholder")}
                title={t("persona.testBoxTooltip")}
                data-testid="persona-test-input"
              />
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled
                title={t("persona.testBoxTooltip")}
                data-testid="persona-test-button"
              >
                {t("persona.testBoxButton")}
              </Button>
            </div>
            <p className="text-[11px] text-tp-ink-3">{t("persona.testBoxTooltip")}</p>
          </fieldset>

          {/* Asset sections — only meaningful once the persona has a
              real id (the URL the upload route hangs off). Until then
              we show a hint pointing the operator at the Save button. */}
          {existing ? (
            <PersonaAssetsPanel personaId={existing.id} />
          ) : (
            <div
              className="rounded-md border border-dashed border-tp-glass-edge px-3 py-2 text-xs text-tp-ink-3"
              data-testid="persona-assets-pending-save"
            >
              {t("persona.assetsSaveFirstHint")}
            </div>
          )}
        </div>

        <DialogFooter className="gap-2">
          {existing?.is_builtin ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled
              title={t("persona.resetToDefaultTooltip")}
              data-testid="persona-reset-default"
              className="mr-auto"
            >
              {t("persona.resetToDefault")}
            </Button>
          ) : null}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
            data-testid="persona-editor-cancel"
            disabled={saving}
          >
            {t("persona.cancel")}
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={onSave}
            disabled={saving}
            data-testid="persona-editor-save"
          >
            {saving
              ? t("persona.saving")
              : existing
                ? t("persona.saveUpdate")
                : t("persona.saveCreate")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ----------------------------------------------------------------- */
/*                       Asset panel (W2)                            */
/* ----------------------------------------------------------------- */

/** Local in-flight row — rendered as a placeholder cell with a spinner
 * while the multipart POST is in flight. We tag it with a ULID-ish key
 * so React's reconciliation never confuses it with the real
 * `AssetRecord` that lands once the upload resolves. */
interface PendingAsset {
  /** Stable client-side id; never collides with backend ulids. */
  client_id: string;
  kind: AssetKind;
  label: string;
  /** `URL.createObjectURL(file)` — revoked when the row settles. */
  preview_url: string;
}

/** Strip the extension + lowercase + collapse whitespace into hyphens.
 * Used as the default label when the operator drops a file with a
 * messy name (e.g. `"Happy Face.PNG" → "happy-face"`). Anything still
 * outside the backend slug rule falls through the editor's per-cell
 * validation. */
function slugifyFilename(name: string): string {
  const stem = name.replace(/\.[^.]+$/, "");
  return stem
    .toLowerCase()
    .replace(/\s+/g, "-")
    // Drop any character that isn't already in the slug alphabet so we
    // don't ship a label the server will immediately reject.
    .replace(/[^a-z0-9_-]/g, "-")
    // Trim leading/trailing hyphens; collapse runs.
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

/** Pretty-print a byte count in MiB / KiB. Mirrors the convention the
 * admin uses elsewhere (e.g. /admin/agents bytes column). */
function formatBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${n} B`;
}

/** Map an `AssetUploadError.code` to a localized toast string. Falls
 * back to a generic "upload failed" message when the server emits an
 * unknown code so the user still gets actionable feedback. */
function uploadErrorMessage(
  t: ReturnType<typeof useTranslation>["t"],
  code: AssetUploadErrorCode,
  raw: string,
): string {
  switch (code) {
    case "payload_too_large":
      return t("persona.assetsErrPayloadTooLarge");
    case "persona_quota_exceeded":
      return t("persona.assetsErrQuotaExceeded");
    case "unsupported_media_type":
      return t("persona.assetsErrUnsupportedMime");
    case "invalid_label":
      return t("persona.assetsErrInvalidLabel");
    case "duplicate_label":
      return t("persona.assetsErrDuplicateLabel");
    default:
      return t("persona.assetsErrUploadFailed", { msg: raw });
  }
}

/** Container component for both emoji + reference sections. Splits the
 * combined asset list (one HTTP call) into two filtered slices so
 * we can show the storage banner once and avoid double-fetching. */
function PersonaAssetsPanel({ personaId }: { personaId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const assetsKey = React.useMemo(
    () => ["admin", "personas", personaId, "assets"] as const,
    [personaId],
  );

  const assetsQuery = useQuery<AssetRecord[]>({
    queryKey: assetsKey,
    queryFn: () => listAssets(personaId),
  });

  // Memoise the empty-array fallback so the downstream useMemo deps
  // don't see a fresh `[]` reference on every render (which the
  // react-hooks/exhaustive-deps lint rule flags otherwise).
  const assets = React.useMemo<AssetRecord[]>(
    () => assetsQuery.data ?? [],
    [assetsQuery.data],
  );
  const emojis = React.useMemo(
    () => assets.filter((a) => a.kind === "emoji"),
    [assets],
  );
  const refs = React.useMemo(
    () => assets.filter((a) => a.kind === "reference"),
    [assets],
  );

  const totalBytes = React.useMemo(
    () => assets.reduce((acc, a) => acc + a.size_bytes, 0),
    [assets],
  );

  // Pending uploads keyed by kind. Optimistic cells render alongside
  // real rows; rollback drops the entry by `client_id` and triggers a
  // toast in the calling handler.
  const [pending, setPending] = React.useState<PendingAsset[]>([]);

  const refresh = React.useCallback(() => {
    return queryClient.invalidateQueries({ queryKey: assetsKey });
  }, [assetsKey, queryClient]);

  /** Shared upload pipeline — validates client-side, optimistically
   * inserts the placeholder cell, fires the multipart POST, then
   * either refreshes the list or rolls back + toasts. */
  const beginUpload = React.useCallback(
    async (kind: AssetKind, label: string, file: File) => {
      // Client-side gates. We mirror the backend's MIME + size rules
      // so the user sees an immediate red-toast without burning a
      // round-trip.
      if (!ASSET_ALLOWED_MIMES.includes(file.type)) {
        toast.error(t("persona.assetsUnsupportedMime"));
        return;
      }
      if (file.size > ASSET_MAX_BYTES) {
        toast.error(t("persona.assetsTooLarge"));
        return;
      }
      if (!ASSET_LABEL_RE.test(label)) {
        toast.error(t("persona.assetsLabelInvalid"));
        return;
      }

      const client_id = `pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const preview_url = URL.createObjectURL(file);
      const entry: PendingAsset = { client_id, kind, label, preview_url };
      setPending((prev) => [...prev, entry]);

      try {
        await uploadAsset(personaId, kind, label, file);
        toast.success(t("persona.assetsUploadSucceeded", { label }));
        await refresh();
      } catch (err) {
        if (err instanceof AssetUploadError) {
          toast.error(uploadErrorMessage(t, err.code, err.message));
        } else {
          const msg = err instanceof Error ? err.message : String(err);
          toast.error(t("persona.assetsErrUploadFailed", { msg }));
        }
      } finally {
        setPending((prev) => prev.filter((p) => p.client_id !== client_id));
        URL.revokeObjectURL(preview_url);
      }
    },
    [personaId, refresh, t],
  );

  /** Delete one asset and refetch on success. */
  const handleDelete = React.useCallback(
    async (asset: AssetRecord) => {
      try {
        await deleteAsset(personaId, asset.id);
        toast.success(
          t("persona.assetsDeleteSucceeded", { label: asset.label }),
        );
        await refresh();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        toast.error(t("persona.assetsErrUploadFailed", { msg }));
      }
    },
    [personaId, refresh, t],
  );

  const pendingEmoji = React.useMemo(
    () => pending.filter((p) => p.kind === "emoji"),
    [pending],
  );
  const pendingRefs = React.useMemo(
    () => pending.filter((p) => p.kind === "reference"),
    [pending],
  );

  return (
    <div className="space-y-3" data-testid="persona-assets-panel">
      <AssetSection
        kind="emoji"
        title={t("persona.assetsEmojiTitle")}
        description={t("persona.assetsEmojiDescription")}
        addLabel={t("persona.assetsAddEmoji")}
        assets={emojis}
        pending={pendingEmoji}
        loading={assetsQuery.isPending}
        loadError={assetsQuery.error ?? null}
        onUpload={(file, label) => beginUpload("emoji", label, file)}
        onDelete={handleDelete}
      />

      <AssetSection
        kind="reference"
        title={t("persona.assetsRefsTitle")}
        description={t("persona.assetsRefsDescription", {
          cap: REFERENCE_VISIBLE_CAP,
        })}
        addLabel={t("persona.assetsAddReference")}
        assets={refs}
        pending={pendingRefs}
        loading={assetsQuery.isPending}
        loadError={assetsQuery.error ?? null}
        onUpload={(file, label) => beginUpload("reference", label, file)}
        onDelete={handleDelete}
        overCap={refs.length > REFERENCE_VISIBLE_CAP}
        overCapHint={t("persona.assetsRefsOverCapHint", {
          cap: REFERENCE_VISIBLE_CAP,
        })}
      />

      <p
        className="text-right text-[11px] text-tp-ink-3"
        data-testid="persona-assets-total"
      >
        {t("persona.assetsTotalUsed", {
          used: formatBytes(totalBytes),
          cap: formatBytes(PERSONA_TOTAL_BYTES_CAP),
        })}
      </p>
    </div>
  );
}

/** One collapsible section (emoji OR reference). Owns the drop-zone +
 * grid + add-button. Stateless w.r.t. the asset list — the parent
 * owns the query + optimistic-pending bookkeeping. */
function AssetSection({
  kind,
  title,
  description,
  addLabel,
  assets,
  pending,
  loading,
  loadError,
  onUpload,
  onDelete,
  overCap = false,
  overCapHint,
}: {
  kind: AssetKind;
  title: string;
  description: string;
  addLabel: string;
  assets: AssetRecord[];
  pending: PendingAsset[];
  loading: boolean;
  loadError: Error | null;
  onUpload: (file: File, label: string) => void;
  onDelete: (asset: AssetRecord) => void;
  overCap?: boolean;
  overCapHint?: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(true);
  const [dragOver, setDragOver] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const [pendingDelete, setPendingDelete] =
    React.useState<AssetRecord | null>(null);

  const sectionTestId = `persona-assets-section-${kind}`;

  // Filter the system-clipboard / OS drag payload down to image files
  // the backend will accept. We dispatch one upload per file with a
  // slugified default label.
  const handleFiles = React.useCallback(
    (files: FileList | File[]) => {
      const list = Array.from(files);
      for (const file of list) {
        const label = slugifyFilename(file.name) || "asset";
        onUpload(file, label);
      }
    },
    [onUpload],
  );

  function onPick() {
    fileInputRef.current?.click();
  }

  function onFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files && e.target.files.length > 0) {
      handleFiles(e.target.files);
    }
    // Reset the input so picking the same filename twice re-fires.
    e.target.value = "";
  }

  function onDragEnter(e: React.DragEvent<HTMLDivElement>) {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      setDragOver(true);
    }
  }
  function onDragOver(e: React.DragEvent<HTMLDivElement>) {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  }
  function onDragLeave(e: React.DragEvent<HTMLDivElement>) {
    // Skip phantom leaves caused by hovering child elements.
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    setDragOver(false);
  }
  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) {
      handleFiles(files);
    }
  }

  return (
    <section
      className="space-y-2 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2"
      data-testid={sectionTestId}
    >
      <header className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => setOpen((p) => !p)}
          className="flex items-center gap-2 text-left"
          aria-expanded={open}
          data-testid={`${sectionTestId}-toggle`}
        >
          <span className="text-sm font-medium">{title}</span>
          <span className="rounded-full bg-tp-glass px-1.5 py-0.5 font-mono text-[10px] text-tp-ink-3">
            {assets.length}
          </span>
        </button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onPick}
          data-testid={`${sectionTestId}-add`}
        >
          {addLabel}
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept={ASSET_ALLOWED_MIMES.join(",")}
          multiple
          className="hidden"
          onChange={onFileInputChange}
          data-testid={`${sectionTestId}-file`}
        />
      </header>

      {open ? (
        <>
          <p className="text-[11px] text-tp-ink-3">{description}</p>

          {overCap && overCapHint ? (
            <p
              className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200"
              data-testid={`${sectionTestId}-overcap`}
            >
              {overCapHint}
            </p>
          ) : null}

          {loadError ? (
            <p
              className="text-xs text-destructive"
              data-testid={`${sectionTestId}-load-error`}
            >
              {t("persona.assetsLoadFailed", {
                msg: loadError.message,
              })}
            </p>
          ) : null}

          <div
            onDragEnter={onDragEnter}
            onDragOver={onDragOver}
            onDragLeave={onDragLeave}
            onDrop={onDrop}
            className={cn(
              "rounded-md border border-dashed px-3 py-3 transition-colors",
              dragOver
                ? "border-tp-amber bg-tp-amber/10"
                : "border-tp-glass-edge",
            )}
            data-testid={`${sectionTestId}-dropzone`}
            aria-label={t("persona.assetsDropHere", { kind: title })}
          >
            {loading ? (
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                {Array.from({ length: 2 }).map((_, i) => (
                  <Skeleton key={i} className="h-24 w-full rounded-md" />
                ))}
              </div>
            ) : assets.length === 0 && pending.length === 0 ? (
              <p className="py-4 text-center text-xs text-tp-ink-3">
                {t("persona.assetsEmpty")}
              </p>
            ) : (
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                {assets.map((asset) => (
                  <AssetCell
                    key={asset.id}
                    asset={asset}
                    onDelete={() => setPendingDelete(asset)}
                    testId={`${sectionTestId}-cell-${asset.label}`}
                  />
                ))}
                {pending.map((p) => (
                  <PendingAssetCell
                    key={p.client_id}
                    pending={p}
                    testId={`${sectionTestId}-pending-${p.client_id}`}
                  />
                ))}
              </div>
            )}
          </div>
        </>
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
        title={t("persona.assetsDeleteConfirmTitle")}
        description={t("persona.assetsDeleteConfirmBody")}
        cancelLabel={t("persona.cancel")}
        confirmLabel={t("persona.assetsDelete")}
        testId={`${sectionTestId}-delete-confirm`}
        onConfirm={async () => {
          const target = pendingDelete;
          setPendingDelete(null);
          if (target) await onDelete(target);
        }}
      />
    </section>
  );
}

function AssetCell({
  asset,
  onDelete,
  testId,
}: {
  asset: AssetRecord;
  onDelete: () => void;
  testId: string;
}) {
  const { t } = useTranslation();
  // Backend doesn't have a rename endpoint yet, so the label input is
  // read-only with a tooltip pointing at the delete-then-reupload
  // workaround. Validation still runs client-side as a teaching aid.
  const labelOk = ASSET_LABEL_RE.test(asset.label);
  return (
    <div
      className="group relative flex flex-col gap-1 rounded-md border border-tp-glass-edge bg-tp-glass p-1.5"
      data-testid={testId}
    >
      {/* Square preview thumbnail, capped at 96px. `object-contain`
          preserves aspect ratio so non-square stickers don't get
          smashed into a square. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={asset.url}
        alt={asset.label}
        className="mx-auto h-20 w-20 rounded-sm object-contain"
        loading="lazy"
      />
      <Input
        value={asset.label}
        readOnly
        title={t("persona.assetsLabelChangeHintNyi")}
        aria-invalid={!labelOk}
        className="h-7 px-1.5 text-[11px]"
        data-testid={`${testId}-label`}
      />
      <div className="flex items-center justify-between text-[10px] text-tp-ink-3">
        <span>{formatBytes(asset.size_bytes)}</span>
        <button
          type="button"
          onClick={onDelete}
          className="text-destructive hover:underline"
          data-testid={`${testId}-delete`}
        >
          {t("persona.assetsDelete")}
        </button>
      </div>
    </div>
  );
}

function PendingAssetCell({
  pending,
  testId,
}: {
  pending: PendingAsset;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      className="relative flex flex-col gap-1 rounded-md border border-dashed border-tp-glass-edge bg-tp-glass p-1.5 opacity-70"
      data-testid={testId}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={pending.preview_url}
        alt={pending.label}
        className="mx-auto h-20 w-20 rounded-sm object-contain"
      />
      <p className="truncate px-1 text-[11px]">{pending.label}</p>
      <p className="px-1 text-[10px] text-tp-ink-3">
        {t("persona.assetsUploading")}
      </p>
    </div>
  );
}
