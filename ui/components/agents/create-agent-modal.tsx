"use client";

/**
 * Create-agent modal (W2.1).
 *
 * Renders a form (name / format / save-to / clone-from / body / force)
 * inside a shadcn `Dialog`. Submission flow:
 *
 *   1. Local validation: name regex `^[a-z][a-z0-9_-]*$` is enforced on
 *      every keystroke so the submit button stays disabled until the
 *      shape is valid.
 *   2. POST /admin/agents via `createAgent`.
 *   3. On 201: invalidate the `["admin", "agents"]` query, toast, close.
 *   4. On 400 ``agent_exists``: render the conflict error inline on the
 *      name field.
 *   5. On 409 ``shadows_builtin``: reveal the Force checkbox so the
 *      operator can opt into shadowing the built-in.
 *
 * The clone-from dropdown copies the existing card's body via
 * `fetchAgent` when the operator picks one — so they can fork from a
 * working card without having to remember the YAML/MD schema.
 *
 * Project-overlay save-to is gated behind a flag the parent passes
 * (defaults to off — the gateway doesn't currently expose a probe for
 * `.corlinman/agents/`; we surface the option but disable the radio so
 * users see what's coming without picking a broken target).
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
import {
  CorlinmanApiError,
  createAgent,
  fetchAgent,
  listAgents,
  type AgentSummary,
} from "@/lib/api";

/** Shared regex with the gateway's `_validate_create_name`. */
export const AGENT_NAME_RE = /^[a-z][a-z0-9_-]*$/;

export interface CreateAgentModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Optional override for the agents list query (tests stub this). */
  initialAgents?: AgentSummary[];
  /** Fires after a 201 — parent uses this to toast / refresh. */
  onCreated?: (name: string) => void;
  /** When false, the Project overlay radio is disabled with a tooltip
   * (the gateway treats project overlays as opt-in workspace state). */
  projectOverlayAvailable?: boolean;
}

type AgentFormat = "md" | "yaml";
type AgentSource = "user" | "project";

interface FormState {
  name: string;
  format: AgentFormat;
  saveTo: AgentSource;
  cloneFrom: string;
  body: string;
  force: boolean;
}

const DEFAULT_MD_TEMPLATE = `---
description: One-line summary that the router uses to dispatch this agent.
model: opus
# tools:
#   - read
#   - write
---

# Agent body

Describe how this agent should behave when invoked. Markdown is fine —
the dispatcher renders this as the system prompt.
`;

const DEFAULT_YAML_TEMPLATE = `name: my-agent
description: One-line summary that the router uses to dispatch this agent.
model: opus
tools:
  - read
  - write
prompt: |
  You are a helpful assistant. Replace this with your agent's system
  prompt — every line will be passed through to the dispatcher.
`;

function blankFor(format: AgentFormat): FormState {
  return {
    name: "",
    format,
    saveTo: "user",
    cloneFrom: "",
    body: format === "md" ? DEFAULT_MD_TEMPLATE : DEFAULT_YAML_TEMPLATE,
    force: false,
  };
}

interface FormErrors {
  name?: string;
  /** Generic top-of-form error from the server (500 / network). */
  form?: string;
}

export function CreateAgentModal({
  open,
  onOpenChange,
  initialAgents,
  onCreated,
  projectOverlayAvailable = false,
}: CreateAgentModalProps): React.ReactElement {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [form, setForm] = React.useState<FormState>(blankFor("md"));
  const [errors, setErrors] = React.useState<FormErrors>({});
  const [bodyTouched, setBodyTouched] = React.useState(false);

  // Reset state every time the dialog opens. Leaving stale form state
  // behind after a successful create or a cancel is confusing.
  React.useEffect(() => {
    if (open) {
      setForm(blankFor("md"));
      setErrors({});
      setBodyTouched(false);
    }
  }, [open]);

  // Clone-from dropdown options. The query is suspended when
  // `initialAgents` is supplied (test convenience).
  const agentsQuery = useQuery<AgentSummary[]>({
    queryKey: ["admin", "agents"],
    queryFn: () => listAgents(),
    enabled: open && initialAgents === undefined,
    initialData: initialAgents,
  });
  // Wrap the fallback chain in useMemo so it's a stable reference for
  // the dependents below — otherwise the React Hooks plugin warns that
  // the array literal recomputes on every render.
  const agentList: AgentSummary[] = React.useMemo(
    () => agentsQuery.data ?? initialAgents ?? [],
    [agentsQuery.data, initialAgents],
  );

  // Built-in shadow detection: looking up the current name in the
  // agents list lets us decide whether the Force checkbox needs to be
  // visible (before submission) — vs only revealing it after a 409.
  const nameCollidesWithBuiltin = React.useMemo(() => {
    if (!form.name) return false;
    return agentList.some(
      (a) => a.name === form.name && a.source === "built-in",
    );
  }, [agentList, form.name]);

  const nameValid = AGENT_NAME_RE.test(form.name);
  // The submit button stays disabled until the name regex matches —
  // saves a server round-trip and matches the rest of the codebase's
  // "disable until valid" convention (see CreateTenantDialog).
  const canSubmit = nameValid && !errors.form;

  // ----- field handlers ----------------------------------------------------

  function setName(name: string) {
    setForm((s) => ({ ...s, name }));
    // Clear any prior conflict the moment the operator edits the name —
    // they're acknowledging the error by changing the input.
    setErrors((e) => (e.name ? { ...e, name: undefined } : e));
  }

  function setFormat(format: AgentFormat) {
    setForm((s) => {
      // Only re-template the body when the operator hasn't manually
      // edited it; otherwise we'd nuke their work on a format flip.
      const nextBody = bodyTouched
        ? s.body
        : format === "md"
          ? DEFAULT_MD_TEMPLATE
          : DEFAULT_YAML_TEMPLATE;
      return { ...s, format, body: nextBody };
    });
  }

  function setSaveTo(saveTo: AgentSource) {
    setForm((s) => ({ ...s, saveTo }));
  }

  function setForce(force: boolean) {
    setForm((s) => ({ ...s, force }));
  }

  async function setCloneFrom(name: string) {
    setForm((s) => ({ ...s, cloneFrom: name }));
    if (!name) {
      // Revert to the per-format template if the operator picks ``None``.
      if (!bodyTouched) {
        setForm((s) => ({
          ...s,
          body: s.format === "md" ? DEFAULT_MD_TEMPLATE : DEFAULT_YAML_TEMPLATE,
        }));
      }
      return;
    }
    try {
      const content = await fetchAgent(name);
      setForm((s) => ({ ...s, body: content.content }));
      setBodyTouched(false);
    } catch (err) {
      toast.error(
        t("agents.create.errorGeneric", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    }
  }

  // ----- submit ------------------------------------------------------------

  const mutation = useMutation({
    mutationFn: (body: FormState) =>
      createAgent({
        name: body.name,
        format: body.format,
        body: body.body,
        force: body.force,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["admin", "agents"] });
      toast.success(t("agents.create.success", { name: res.name }));
      onCreated?.(res.name);
      onOpenChange(false);
    },
    onError: (err) => {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 400) {
          // Name conflict (existing user/project overlay file).
          setErrors({ name: t("agents.create.nameConflict") });
          return;
        }
        if (err.status === 409) {
          // Built-in shadow without force. Reveal the Force checkbox so
          // the operator can opt in without leaving the dialog.
          setErrors({ name: t("agents.create.builtinReadonly") });
          return;
        }
      }
      setErrors({
        form: t("agents.create.errorGeneric", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      });
    },
  });

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!nameValid) {
      setErrors((s) => ({ ...s, name: t("agents.create.nameInvalid") }));
      return;
    }
    mutation.mutate(form);
  }

  // The Force checkbox is visible whenever (a) the name collides with a
  // built-in card we already know about, or (b) the server bounced us
  // back with a 409 (errors.name === builtinReadonly) — in both cases
  // the operator needs the opt-in.
  const showForceCheckbox =
    nameCollidesWithBuiltin ||
    errors.name === t("agents.create.builtinReadonly");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-xl"
        data-testid="create-agent-modal"
      >
        <DialogHeader>
          <DialogTitle>{t("agents.create.dialogTitle")}</DialogTitle>
          <DialogDescription>
            {t("agents.create.dialogDesc")}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={onSubmit}
          className="space-y-3"
          data-testid="create-agent-form"
          noValidate
        >
          {errors.form ? (
            <Alert variant="danger" data-testid="create-agent-form-error">
              {errors.form}
            </Alert>
          ) : null}

          {/* Name ---------------------------------------------------- */}
          <div className="space-y-1">
            <Label htmlFor="agent-name">{t("agents.create.nameLabel")}</Label>
            <Input
              id="agent-name"
              data-testid="agent-name"
              autoFocus
              autoComplete="off"
              spellCheck={false}
              placeholder={t("agents.create.namePlaceholder")}
              value={form.name}
              aria-invalid={errors.name ? true : undefined}
              aria-describedby="agent-name-help"
              onChange={(e) => setName(e.target.value)}
              className="font-mono"
            />
            <p
              id="agent-name-help"
              className="text-[11px] text-tp-ink-3"
            >
              {t("agents.create.nameHelp")}
            </p>
            {!nameValid && form.name.length > 0 ? (
              <p
                role="alert"
                className="text-[11px] text-sg-err"
                data-testid="agent-name-error"
              >
                {t("agents.create.nameInvalid")}
              </p>
            ) : null}
            {errors.name ? (
              <p
                role="alert"
                className="text-[11px] text-sg-err"
                data-testid="agent-name-server-error"
              >
                {errors.name}
              </p>
            ) : null}
          </div>

          {/* Format -------------------------------------------------- */}
          <fieldset className="space-y-1">
            <legend className="text-sm font-medium">
              {t("agents.create.formatLabel")}
            </legend>
            <div className="flex gap-4">
              <label className="inline-flex items-center gap-2 text-xs">
                <input
                  type="radio"
                  name="agent-format"
                  value="md"
                  checked={form.format === "md"}
                  onChange={() => setFormat("md")}
                  data-testid="agent-format-md"
                />
                {t("agents.create.formatMd")}
              </label>
              <label className="inline-flex items-center gap-2 text-xs">
                <input
                  type="radio"
                  name="agent-format"
                  value="yaml"
                  checked={form.format === "yaml"}
                  onChange={() => setFormat("yaml")}
                  data-testid="agent-format-yaml"
                />
                {t("agents.create.formatYaml")}
              </label>
            </div>
          </fieldset>

          {/* Save-to ------------------------------------------------- */}
          <fieldset className="space-y-1">
            <legend className="text-sm font-medium">
              {t("agents.create.sourceLabel")}
            </legend>
            <div className="flex flex-col gap-2">
              <label className="inline-flex items-center gap-2 text-xs">
                <input
                  type="radio"
                  name="agent-save-to"
                  value="user"
                  checked={form.saveTo === "user"}
                  onChange={() => setSaveTo("user")}
                  data-testid="agent-save-to-user"
                />
                <span className="font-mono">
                  {t("agents.create.sourceUser")}
                </span>
              </label>
              <label
                className={`inline-flex items-center gap-2 text-xs ${
                  !projectOverlayAvailable ? "opacity-50" : ""
                }`}
                title={
                  !projectOverlayAvailable
                    ? "Project overlay directory not detected"
                    : undefined
                }
              >
                <input
                  type="radio"
                  name="agent-save-to"
                  value="project"
                  checked={form.saveTo === "project"}
                  disabled={!projectOverlayAvailable}
                  onChange={() => setSaveTo("project")}
                  data-testid="agent-save-to-project"
                />
                <span className="font-mono">
                  {t("agents.create.sourceProject")}
                </span>
              </label>
            </div>
          </fieldset>

          {/* Clone-from ---------------------------------------------- */}
          <div className="space-y-1">
            <Label htmlFor="agent-clone-from">
              {t("agents.create.cloneLabel")}
            </Label>
            <select
              id="agent-clone-from"
              data-testid="agent-clone-from"
              className="sg-inset flex h-10 w-full rounded-sg-md px-3 py-1 text-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              value={form.cloneFrom}
              onChange={(e) => void setCloneFrom(e.target.value)}
            >
              <option value="">{t("agents.create.cloneNone")}</option>
              {agentList.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                  {a.source ? ` (${a.source})` : ""}
                </option>
              ))}
            </select>
          </div>

          {/* Body ---------------------------------------------------- */}
          <div className="space-y-1">
            <Label htmlFor="agent-body">{t("agents.create.bodyLabel")}</Label>
            <textarea
              id="agent-body"
              data-testid="agent-body"
              className="sg-inset min-h-[200px] w-full rounded-sg-md px-3 py-2 font-mono text-xs leading-relaxed focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder={t("agents.create.bodyPlaceholder")}
              spellCheck={false}
              value={form.body}
              onChange={(e) => {
                setForm((s) => ({ ...s, body: e.target.value }));
                setBodyTouched(true);
              }}
            />
          </div>

          {/* Force --------------------------------------------------- */}
          {showForceCheckbox ? (
            <div className="space-y-1 rounded-md border border-tp-amber/30 bg-tp-amber/5 p-3">
              <label className="inline-flex items-start gap-2 text-xs">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={form.force}
                  onChange={(e) => setForce(e.target.checked)}
                  data-testid="agent-force"
                />
                <span>
                  <span className="font-medium">
                    {t("agents.create.forceLabel")}
                  </span>
                  <span className="block text-tp-ink-3">
                    {t("agents.create.forceHint")}
                  </span>
                </span>
              </label>
            </div>
          ) : null}

          <DialogFooter className="pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
              data-testid="create-agent-cancel"
            >
              {t("agents.create.cancel")}
            </Button>
            <Button
              type="submit"
              disabled={!canSubmit || mutation.isPending}
              data-testid="create-agent-submit"
            >
              {mutation.isPending
                ? t("agents.create.submitting")
                : t("agents.create.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
