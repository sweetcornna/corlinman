"use client";

/**
 * EnvVarRow — a single editable credential field inside a
 * `[providers.<name>]` block.
 *
 * Two visual modes, modelled after hermes-agent `web/src/pages/EnvPage.tsx`
 * (lines 99-330):
 *
 *   - **Compact** (props.compact && !isSet && !editing): a low-density
 *     inline row used inside :class:`ProviderGroupCard` so an unset
 *     provider doesn't take more than one line per field. Hover lifts
 *     opacity from 50% → 100%.
 *   - **Full** (everything else): the wide layout with label badge,
 *     description, monospace preview box, eye-icon reveal, replace +
 *     clear buttons. During edit the preview collapses to a paste-only
 *     password input with Save + Cancel.
 *
 * Reveal flow (eye icon):
 *   1. First click → fetch `/admin/credentials/{provider}/{key}/reveal`,
 *      store the cleartext in local component state.
 *   2. Subsequent toggles within the same mount just flip a boolean —
 *      no re-fetch.
 *   3. Any `field` prop change (e.g. parent refetch after save) clears
 *      the cached value so we never echo a stale literal.
 *
 * The endpoint is auth-gated by the same admin middleware as every other
 * `/admin/credentials/*` route; the value is never logged server-side.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  Check,
  Eye,
  EyeOff,
  Pencil,
  Save,
  Trash2,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { revealCredential, type CredentialField } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface EnvVarRowProps {
  provider: string;
  field: CredentialField;
  /** Pretty label for the field — defaults to the raw key. */
  label?: string;
  /** Optional description text rendered next to the label. */
  description?: string;
  /** Render in the dense single-line variant. */
  compact?: boolean;
  saving?: boolean;
  onSave: (value: string) => void | Promise<void>;
  onDelete: () => void | Promise<void>;
  /** Optional override id prefix for nested data-testid attributes. */
  testIdPrefix?: string;
}

export function EnvVarRow({
  provider,
  field,
  label,
  description,
  compact = false,
  saving = false,
  onSave,
  onDelete,
  testIdPrefix,
}: EnvVarRowProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = React.useState(false);
  const [value, setValue] = React.useState("");
  const [revealed, setRevealed] = React.useState(false);
  /** Cached cleartext fetched from /reveal. `null` = not yet fetched. */
  const [cleartext, setCleartext] = React.useState<string | null>(null);
  const [revealLoading, setRevealLoading] = React.useState(false);
  const [editRevealed, setEditRevealed] = React.useState(false);
  const [typeWarned, setTypeWarned] = React.useState(false);

  const prefix = testIdPrefix ?? `cred-${provider}-${field.key}`;
  const displayLabel = label ?? field.key;

  // Reset edit buffer + reveal state whenever the field flips between
  // set/unset (e.g. external refetch after save). Without this, the
  // input would retain a stale value across mounts of the same row.
  React.useEffect(() => {
    if (!editing) setValue("");
    setRevealed(false);
    setCleartext(null);
    setEditRevealed(false);
  }, [editing, field.set, field.preview]);

  async function handleSave() {
    if (!value) return;
    await onSave(value);
    setEditing(false);
    setValue("");
    setTypeWarned(false);
  }

  function handleCancel() {
    setEditing(false);
    setValue("");
    setTypeWarned(false);
    setEditRevealed(false);
  }

  async function handleRevealToggle() {
    // Toggle off — keep the cached cleartext around so the next reveal
    // is instant. Re-mounts or field-prop changes clear it via the
    // effect above.
    if (revealed) {
      setRevealed(false);
      return;
    }
    if (cleartext === null) {
      setRevealLoading(true);
      try {
        const v = await revealCredential(provider, field.key);
        setCleartext(v);
        setRevealed(true);
      } catch (err) {
        toast.error(
          err instanceof Error ? err.message : t("credentials.envRow.revealFailed"),
        );
      } finally {
        setRevealLoading(false);
      }
      return;
    }
    setRevealed(true);
  }

  // -- editing --
  if (editing) {
    return (
      <div
        className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border border-tp-amber/40 bg-tp-glass-inner/40 px-3 py-2 shadow-tp-amber-soft"
        data-testid={`${prefix}-row`}
      >
        <Label
          htmlFor={`${prefix}-input`}
          className="w-full shrink-0 font-mono text-[11px] text-tp-ink-2 sm:w-32"
        >
          {displayLabel}
        </Label>
        <Input
          id={`${prefix}-input`}
          data-testid={`${prefix}-input`}
          type={editRevealed ? "text" : "password"}
          autoFocus
          autoComplete="off"
          spellCheck={false}
          placeholder={t("credentials.envRow.placeholder")}
          value={value}
          onPaste={(e) => {
            // Pasting is the intended path; we still let onChange fire
            // so the Save button activates without an extra render.
            const pasted = e.clipboardData.getData("text");
            if (pasted) {
              e.preventDefault();
              setValue(pasted.trim());
            }
          }}
          onChange={(e) => {
            const next = e.target.value;
            // First non-paste keystroke surfaces a soft nudge so the
            // operator notices the paste-only pattern. We don't block —
            // some keyboards (and some passwords) really do need typing.
            if (!typeWarned && next.length === 1 && !value) {
              toast.message(t("credentials.pasteHint"));
              setTypeWarned(true);
            }
            setValue(next);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void handleSave();
            } else if (e.key === "Escape") {
              e.preventDefault();
              handleCancel();
            }
          }}
          className="h-9 min-w-0 flex-1 font-mono text-xs"
          disabled={saving}
        />
        <Button
          size="sm"
          variant="ghost"
          data-testid={`${prefix}-edit-reveal`}
          aria-label={
            editRevealed
              ? t("credentials.envRow.hide")
              : t("credentials.envRow.reveal")
          }
          aria-pressed={editRevealed}
          onClick={() => setEditRevealed((r) => !r)}
          disabled={saving}
        >
          {editRevealed ? (
            <EyeOff className="h-3.5 w-3.5" />
          ) : (
            <Eye className="h-3.5 w-3.5" />
          )}
        </Button>
        <Button
          size="sm"
          data-testid={`${prefix}-save`}
          disabled={saving || !value}
          onClick={() => void handleSave()}
          aria-label={t("common.save")}
        >
          <Save className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">{t("common.save")}</span>
        </Button>
        <Button
          size="sm"
          variant="ghost"
          data-testid={`${prefix}-cancel`}
          disabled={saving}
          onClick={handleCancel}
          aria-label={t("common.cancel")}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    );
  }

  // -- compact unset (rendered inside ProviderGroupCard) --
  if (compact && !field.set) {
    return (
      <div
        className="flex min-w-0 items-center justify-between gap-3 px-1 py-1.5 opacity-50 transition-opacity hover:opacity-100"
        data-testid={`${prefix}-row`}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="font-mono text-[11px] text-tp-ink-3">
            {displayLabel}
          </span>
          {(description ?? field.env_ref) && (
            <span className="hidden truncate text-[10px] text-tp-ink-3/70 sm:inline">
              {description ??
                t("credentials.envHint", { env: field.env_ref })}
            </span>
          )}
        </div>
        <Button
          size="sm"
          variant="outline"
          data-testid={`${prefix}-add`}
          onClick={() => {
            setEditing(true);
            setValue("");
          }}
        >
          <Pencil className="h-3 w-3" />
          {t("credentials.envRow.set")}
        </Button>
      </div>
    );
  }

  // -- non-compact unset --
  if (!field.set) {
    return (
      <div
        className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border border-dashed border-tp-glass-edge px-3 py-2 opacity-75 transition-opacity hover:opacity-100"
        data-testid={`${prefix}-row`}
      >
        <Label className="w-full shrink-0 font-mono text-[11px] text-tp-ink-3 sm:w-32">
          {displayLabel}
        </Label>
        <div className="min-w-0 flex-1 truncate text-[11px] text-tp-ink-3">
          {field.env_ref ? (
            <span className="font-mono">
              {t("credentials.envHint", { env: field.env_ref })}
            </span>
          ) : (
            <span>{t("credentials.fieldUnset")}</span>
          )}
        </div>
        <Button
          size="sm"
          variant="outline"
          data-testid={`${prefix}-add`}
          onClick={() => setEditing(true)}
        >
          <Pencil className="h-3 w-3" />
          {t("credentials.envRow.set")}
        </Button>
      </div>
    );
  }

  // -- set --
  const showCleartext = revealed && cleartext !== null;
  return (
    <div
      className={cn(
        "flex min-w-0 flex-wrap items-center gap-2 rounded-md border px-3 py-2",
        compact
          ? "border-transparent px-1 py-1.5"
          : "border-tp-glass-edge",
      )}
      data-testid={`${prefix}-row`}
    >
      <Label
        className={cn(
          "w-full shrink-0 font-mono text-tp-ink-2 sm:w-32",
          compact ? "text-[11px]" : "text-[11px]",
        )}
      >
        {displayLabel}
      </Label>
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <div
          data-testid={`${prefix}-preview`}
          className={cn(
            "min-w-0 flex-1 truncate rounded border border-tp-glass-edge bg-tp-glass-inner/40 px-2 py-1 font-mono text-[11px]",
            showCleartext ? "text-tp-ink select-all" : "text-tp-ink-3",
          )}
        >
          {showCleartext ? (
            <span data-testid={`${prefix}-preview-cleartext`}>{cleartext}</span>
          ) : field.preview ? (
            <span aria-hidden>{"•".repeat(8)}</span>
          ) : field.env_ref ? (
            <span className="text-tp-ink-3">env: {field.env_ref}</span>
          ) : (
            <span className="text-tp-ink-3">{t("credentials.fieldSet")}</span>
          )}
        </div>
        {field.preview ? (
          <Button
            size="sm"
            variant="ghost"
            data-testid={`${prefix}-reveal`}
            disabled={revealLoading}
            aria-label={
              revealed
                ? t("credentials.envRow.hide")
                : t("credentials.envRow.reveal")
            }
            aria-pressed={revealed}
            onClick={() => void handleRevealToggle()}
          >
            {revealed ? (
              <EyeOff className="h-3.5 w-3.5" />
            ) : (
              <Eye className="h-3.5 w-3.5" />
            )}
          </Button>
        ) : null}
      </div>
      <Button
        size="sm"
        variant="outline"
        data-testid={`${prefix}-replace`}
        onClick={() => setEditing(true)}
      >
        <Pencil className="h-3 w-3" />
        {t("credentials.envRow.replace")}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        data-testid={`${prefix}-delete`}
        aria-label={t("credentials.envRow.clear")}
        disabled={saving}
        onClick={() => void onDelete()}
        className="text-destructive hover:text-destructive"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

export default EnvVarRow;
