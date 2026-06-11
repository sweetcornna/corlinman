"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { Drawer } from "@/components/ui/drawer";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { CATEGORY_META, categorize } from "./skill-card";
import { parseOrigin } from "./installed-list";
import type { InstalledSkillRow, SkillUpdateBody } from "@/lib/api";

/**
 * Right-side modal drawer that renders + EDITS one installed skill.
 *
 * W2.4 wire-up: this used to be dead code keyed on the prototype mock
 * `Skill` type — it now operates on the live {@link InstalledSkillRow}
 * returned by `GET /admin/skills` and writes edits back through the
 * `PUT /admin/skills/{name}` route. The parent page owns the mutation;
 * this component owns the form state + the changed-field diff it hands
 * to {@link SkillDrawerProps.onSave}.
 *
 * The overlay + slide animation + focus-trap + Esc-to-close all come from
 * the shared `<Drawer>` primitive (Radix Dialog). The five editable fields
 * mirror the gateway's `SkillUpdateBody`:
 *
 *   - description                  (runtime-consumed summary)
 *   - when_to_use                  (model-selection hint)
 *   - allowed_tools                (tool allowlist, one per line)
 *   - disable_model_invocation     (manual-only toggle)
 *   - body_markdown                (prose injected verbatim)
 */

export interface SkillDrawerProps {
  /** The row to view/edit, or `null` when the drawer is closed. */
  skill: InstalledSkillRow | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Persist a partial patch. The parent owns the API mutation; this
   * component only emits the subset of fields the operator actually
   * changed. Resolves on success (drawer closes); rejects so the
   * parent can surface a toast and keep the drawer open.
   */
  onSave?: (name: string, patch: SkillUpdateBody) => Promise<void>;
  /** Save in-flight — disables the form + footer actions. */
  saving?: boolean;
}

/** Parse the textarea allowlist (one tool per line) into a clean list. */
function parseToolLines(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

/** Stable comparison so we only patch fields the operator actually moved. */
function listsEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((v, i) => v === b[i]);
}

export function SkillDrawer({
  skill,
  open,
  onOpenChange,
  onSave,
  saving,
}: SkillDrawerProps) {
  const { t } = useTranslation();

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      width="lg"
      title={skill?.name ?? ""}
      description={skill?.description}
      // Block dismiss-while-saving so a mid-write outside-click can't
      // orphan the mutation; the footer Cancel still closes when idle.
      dismissable={!saving}
    >
      {skill ? (
        <SkillDrawerBody
          // Remount the form when the target row changes so the seeded
          // state always reflects the freshly-opened skill.
          key={skill.name}
          skill={skill}
          onSave={onSave}
          onClose={() => onOpenChange(false)}
          saving={Boolean(saving)}
          t={t}
        />
      ) : null}
    </Drawer>
  );
}

function SkillDrawerBody({
  skill,
  onSave,
  onClose,
  saving,
  t,
}: {
  skill: InstalledSkillRow;
  onSave?: (name: string, patch: SkillUpdateBody) => Promise<void>;
  onClose: () => void;
  saving: boolean;
  t: ReturnType<typeof useTranslation>["t"];
}) {
  // Seed editable form state from the row. `key={skill.name}` on the
  // parent guarantees a fresh mount per row, so a plain `useState`
  // initialiser is the correct seed point.
  const [description, setDescription] = React.useState(skill.description);
  const [whenToUse, setWhenToUse] = React.useState(skill.when_to_use ?? "");
  const [toolsText, setToolsText] = React.useState(
    (skill.allowed_tools ?? []).join("\n"),
  );
  const [disableInvocation, setDisableInvocation] = React.useState(
    Boolean(skill.disable_model_invocation),
  );
  const [body, setBody] = React.useState(skill.body_markdown ?? "");

  const badge = parseOrigin(skill.origin);
  const category = categorize(skill.name, skill.allowed_tools ?? []);
  const meta = CATEGORY_META[category];
  const CategoryIcon = meta.icon;

  // Build the changed-field patch. Only keys the operator moved are
  // included so a save never blanks an untouched field server-side.
  const patch = React.useMemo<SkillUpdateBody>(() => {
    const next: SkillUpdateBody = {};
    if (description !== skill.description) next.description = description;
    const tools = parseToolLines(toolsText);
    if (!listsEqual(tools, skill.allowed_tools ?? [])) {
      next.allowed_tools = tools;
    }
    const nextWtu = whenToUse.trim();
    const prevWtu = (skill.when_to_use ?? "").trim();
    if (nextWtu !== prevWtu) next.when_to_use = nextWtu;
    if (disableInvocation !== Boolean(skill.disable_model_invocation)) {
      next.disable_model_invocation = disableInvocation;
    }
    if (body !== (skill.body_markdown ?? "")) next.body_markdown = body;
    return next;
  }, [
    description,
    toolsText,
    whenToUse,
    disableInvocation,
    body,
    skill,
  ]);

  const dirty = Object.keys(patch).length > 0;

  const handleSave = React.useCallback(async () => {
    if (!onSave || !dirty || saving) return;
    await onSave(skill.name, patch);
  }, [onSave, dirty, saving, skill.name, patch]);

  const fieldCls = cn(
    "w-full rounded-lg border border-sg-border bg-sg-inset",
    "px-3 py-2 text-[13px] text-sg-ink placeholder:text-sg-ink-4",
    "transition-colors hover:bg-sg-inset-hover",
    "focus:outline-none focus:ring-2 focus:ring-sg-accent/40",
    "disabled:opacity-60",
  );

  return (
    <div className="flex h-full flex-col">
      <form
        data-testid="skill-edit-form"
        onSubmit={(e) => {
          e.preventDefault();
          void handleSave();
        }}
        className="flex flex-1 flex-col gap-5 px-5 py-5 text-sm"
      >
        {/* Meta row — glyph + name + origin badge */}
        <div className="flex flex-wrap items-center gap-3">
          <div
            className={cn(
              "flex h-11 w-11 shrink-0 items-center justify-center rounded-full",
              "border border-sg-accent/25 bg-sg-accent-soft",
            )}
            aria-hidden
          >
            <CategoryIcon className="h-5 w-5 text-sg-accent" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-[18px] font-medium leading-tight tracking-[-0.01em] text-sg-ink">
              {skill.name}
            </h2>
            <div className="mt-0.5 flex flex-wrap items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
              <span className="inline-flex items-center gap-1">
                <CategoryIcon className="h-3 w-3" aria-hidden />
                {meta.label}
              </span>
              <span aria-hidden>·</span>
              <span className="normal-case tracking-normal">
                v{skill.version}
              </span>
              <span aria-hidden>·</span>
              <span className="normal-case tracking-normal">
                {badge.label}
                {badge.version ? `@${badge.version}` : ""}
              </span>
            </div>
          </div>
        </div>

        {/* Description */}
        <Field
          label={t("skills.drawer.descriptionLabel")}
          htmlFor="skill-edit-description"
        >
          <textarea
            id="skill-edit-description"
            data-testid="skill-edit-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={saving}
            rows={2}
            placeholder={t("skills.drawer.descriptionPlaceholder")}
            className={cn(fieldCls, "resize-y leading-[1.55]")}
          />
        </Field>

        {/* When to use */}
        <Field
          label={t("skills.drawer.whenToUseLabel")}
          htmlFor="skill-edit-when-to-use"
          hint={t("skills.drawer.whenToUseHint")}
        >
          <textarea
            id="skill-edit-when-to-use"
            data-testid="skill-edit-when-to-use"
            value={whenToUse}
            onChange={(e) => setWhenToUse(e.target.value)}
            disabled={saving}
            rows={2}
            placeholder={t("skills.drawer.whenToUsePlaceholder")}
            className={cn(fieldCls, "resize-y leading-[1.55]")}
          />
        </Field>

        {/* Allowed tools — one per line */}
        <Field
          label={t("skills.drawer.allowedToolsLabel")}
          htmlFor="skill-edit-allowed-tools"
          hint={t("skills.drawer.allowedToolsHint")}
        >
          <textarea
            id="skill-edit-allowed-tools"
            data-testid="skill-edit-allowed-tools"
            value={toolsText}
            onChange={(e) => setToolsText(e.target.value)}
            disabled={saving}
            rows={3}
            spellCheck={false}
            placeholder={t("skills.drawer.allowedToolsPlaceholder")}
            className={cn(fieldCls, "resize-y font-mono text-[12px] leading-[1.6]")}
          />
        </Field>

        {/* Disable model invocation toggle */}
        <label
          htmlFor="skill-edit-disable-invocation"
          className={cn(
            "flex items-start gap-3 rounded-lg border border-sg-border",
            "bg-sg-inset px-3 py-2.5",
            saving && "opacity-60",
          )}
        >
          <input
            id="skill-edit-disable-invocation"
            data-testid="skill-edit-disable-invocation"
            type="checkbox"
            checked={disableInvocation}
            onChange={(e) => setDisableInvocation(e.target.checked)}
            disabled={saving}
            className="mt-0.5 h-4 w-4 shrink-0 accent-sg-accent"
          />
          <span className="flex flex-col gap-0.5">
            <span className="text-[13px] font-medium text-sg-ink">
              {t("skills.drawer.disableInvocationLabel")}
            </span>
            <span className="text-[11.5px] leading-[1.5] text-sg-ink-3">
              {t("skills.drawer.disableInvocationHint")}
            </span>
          </span>
        </label>

        {/* Body markdown */}
        <Field
          label={t("skills.drawer.bodyLabel")}
          htmlFor="skill-edit-body"
          hint={t("skills.drawer.bodyHint")}
        >
          <textarea
            id="skill-edit-body"
            data-testid="skill-edit-body"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            disabled={saving}
            rows={10}
            spellCheck={false}
            placeholder={t("skills.drawer.bodyPlaceholder")}
            className={cn(
              fieldCls,
              "resize-y font-mono text-[12.5px] leading-[1.6]",
            )}
          />
        </Field>
      </form>

      {/* Sticky footer actions */}
      <div className="sticky bottom-0 flex items-center justify-between gap-2 border-t border-sg-border bg-sg-overlay px-5 py-3">
        <span
          className="text-[11.5px] text-sg-ink-4"
          data-testid="skill-edit-dirty"
          aria-live="polite"
        >
          {dirty ? t("skills.drawer.unsaved") : t("skills.drawer.noChanges")}
        </span>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onClose}
            disabled={saving}
            data-testid="skill-edit-cancel"
          >
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={() => void handleSave()}
            disabled={!dirty || saving || !onSave}
            data-testid="skill-edit-save"
          >
            {saving ? t("skills.drawer.saving") : t("skills.drawer.save")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={htmlFor}
        className="font-mono text-[10px] uppercase tracking-[0.12em] text-sg-ink-4"
      >
        {label}
      </label>
      {children}
      {hint ? (
        <p className="text-[11px] leading-[1.5] text-sg-ink-4">{hint}</p>
      ) : null}
    </div>
  );
}

export default SkillDrawer;
