"use client";

/**
 * Create-tenant dialog (Phase 4 W1 4-1B).
 *
 * Renders a four-field form (slug, admin username, admin password, plus an
 * optional display name behind a collapsed "advanced" disclosure — the
 * backend defaults it to the slug) inside a shadcn `Dialog`. Submission flow:
 *
 *   1. Local validation: catch empty slug + uppercase slug for typing-time
 *      polish. Everything else (length > 63, illegal chars in the middle)
 *      is left to the server's 400 response.
 *   2. POST /admin/tenants via `createTenant`.
 *   3. On 201: invalidate the `["admin", "tenants"]` query and call
 *      `onCreated(slug)` so the parent page can toast / refresh.
 *   4. On 400 invalid_tenant_slug: render `errorInvalidSlug` inline.
 *   5. On 409 tenant_exists: render `errorTenantExists` inline.
 *
 * The form is uncontrolled at the input level (refs via state) so the
 * tests can drive it with `fireEvent.change` / `fireEvent.submit` without
 * requiring a `react-hook-form` setup that the rest of the codebase
 * doesn't lean on for this kind of small modal.
 */

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

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
  // Re-exported for the page to import alongside; used here directly.
} from "@/lib/api";
import { createTenant, isValidSlug } from "@/lib/api/tenants";

export interface CreateTenantDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fired after a 201 response. The page uses the slug to display a toast. */
  onCreated?: (slug: string) => void;
}

interface FormErrors {
  slug?: string;
  admin_username?: string;
  admin_password?: string;
  /** Generic top-of-form error from the server (400/409/500). */
  form?: string;
}

interface FormState {
  slug: string;
  display_name: string;
  admin_username: string;
  admin_password: string;
}

const BLANK: FormState = {
  slug: "",
  display_name: "",
  admin_username: "",
  admin_password: "",
};

export function CreateTenantDialog({
  open,
  onOpenChange,
  onCreated,
}: CreateTenantDialogProps): React.ReactElement {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [form, setForm] = React.useState<FormState>(BLANK);
  const [errors, setErrors] = React.useState<FormErrors>({});
  // display_name is optional (the backend defaults it to the slug), so it
  // lives behind a collapsed "advanced" disclosure.
  const [showAdvanced, setShowAdvanced] = React.useState(false);

  // Reset whenever the dialog re-opens — staying open with stale state
  // is confusing after a successful create or a cancel.
  React.useEffect(() => {
    if (open) {
      setForm(BLANK);
      setErrors({});
      setShowAdvanced(false);
    }
  }, [open]);

  const mutation = useMutation({
    mutationFn: (body: FormState) =>
      createTenant({
        slug: body.slug,
        display_name: body.display_name || undefined,
        admin_username: body.admin_username,
        admin_password: body.admin_password,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["admin", "tenants"] });
      toast.success(t("tenants.createSuccess", { slug: res.tenant_id }));
      onCreated?.(res.tenant_id);
      onOpenChange(false);
    },
    onError: (err) => {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 409) {
          setErrors({ form: t("tenants.errorTenantExists") });
          return;
        }
        if (err.status === 400) {
          // The server returns `{ error, reason }` JSON; surface the
          // reason if we can pluck it out, otherwise fall back to the
          // raw message.
          const reason = extractReason(err.message);
          setErrors({
            slug: reason
              ? t("tenants.errorInvalidSlug", { reason })
              : t("tenants.errorSlugFormat"),
          });
          return;
        }
      }
      setErrors({
        form: t("tenants.errorGeneric", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      });
    },
  });

  function validate(state: FormState): FormErrors {
    const next: FormErrors = {};
    if (!state.slug.trim()) {
      next.slug = t("tenants.errorSlugRequired");
    } else if (state.slug !== state.slug.toLowerCase()) {
      next.slug = t("tenants.errorSlugUppercase");
    } else if (!isValidSlug(state.slug)) {
      // Last-line client-side check so the user gets feedback before a
      // round-trip; the server is still authoritative.
      next.slug = t("tenants.errorSlugFormat");
    }
    if (!state.admin_username.trim()) {
      next.admin_username = t("tenants.errorAdminUsernameRequired");
    }
    if (!state.admin_password) {
      next.admin_password = t("tenants.errorAdminPasswordRequired");
    }
    return next;
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const v = validate(form);
    setErrors(v);
    if (Object.keys(v).length > 0) return;
    mutation.mutate(form);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("tenants.modalTitle")}</DialogTitle>
          <DialogDescription>{t("tenants.modalDesc")}</DialogDescription>
        </DialogHeader>

        <form
          onSubmit={onSubmit}
          className="space-y-3"
          data-testid="create-tenant-form"
          noValidate
        >
          {errors.form ? (
            <p
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
              data-testid="create-tenant-form-error"
            >
              {errors.form}
            </p>
          ) : null}

          <div className="space-y-1">
            <Label htmlFor="tenant-slug">{t("tenants.fieldSlug")}</Label>
            <Input
              id="tenant-slug"
              data-testid="tenant-slug"
              autoFocus
              autoComplete="off"
              spellCheck={false}
              placeholder={t("tenants.fieldSlugPlaceholder")}
              value={form.slug}
              aria-invalid={errors.slug ? true : undefined}
              aria-describedby="tenant-slug-hint"
              onChange={(e) =>
                setForm((s) => ({ ...s, slug: e.target.value }))
              }
              className="font-mono"
            />
            <p
              id="tenant-slug-hint"
              className="text-[11px] text-sg-ink-3"
            >
              {t("tenants.fieldSlugHint")}
            </p>
            {errors.slug ? (
              <p
                role="alert"
                className="text-[11px] text-destructive"
                data-testid="tenant-slug-error"
              >
                {errors.slug}
              </p>
            ) : null}
          </div>

          <div className="space-y-1">
            <Label htmlFor="tenant-admin-username">
              {t("tenants.fieldAdminUsername")}
            </Label>
            <Input
              id="tenant-admin-username"
              data-testid="tenant-admin-username"
              autoComplete="off"
              placeholder={t("tenants.fieldAdminUsernamePlaceholder")}
              value={form.admin_username}
              aria-invalid={errors.admin_username ? true : undefined}
              onChange={(e) =>
                setForm((s) => ({ ...s, admin_username: e.target.value }))
              }
            />
            {errors.admin_username ? (
              <p
                role="alert"
                className="text-[11px] text-destructive"
                data-testid="tenant-admin-username-error"
              >
                {errors.admin_username}
              </p>
            ) : null}
          </div>

          <div className="space-y-1">
            <Label htmlFor="tenant-admin-password">
              {t("tenants.fieldAdminPassword")}
            </Label>
            <Input
              id="tenant-admin-password"
              data-testid="tenant-admin-password"
              type="password"
              autoComplete="new-password"
              placeholder={t("tenants.fieldAdminPasswordPlaceholder")}
              value={form.admin_password}
              aria-invalid={errors.admin_password ? true : undefined}
              onChange={(e) =>
                setForm((s) => ({ ...s, admin_password: e.target.value }))
              }
            />
            {errors.admin_password ? (
              <p
                role="alert"
                className="text-[11px] text-destructive"
                data-testid="tenant-admin-password-error"
              >
                {errors.admin_password}
              </p>
            ) : null}
          </div>

          <div>
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              data-testid="tenant-toggle-advanced"
              className="text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline focus-visible:outline-none focus-visible:underline"
            >
              {showAdvanced ? "—" : "+"}{" "}
              {t("common.advancedOptions", { defaultValue: "高级选项" })}
            </button>
          </div>

          {showAdvanced ? (
            <div className="space-y-1">
              <Label htmlFor="tenant-display-name">
                {t("tenants.fieldDisplayName")}
              </Label>
              <Input
                id="tenant-display-name"
                data-testid="tenant-display-name"
                autoComplete="off"
                placeholder={t("tenants.fieldDisplayNamePlaceholder")}
                value={form.display_name}
                onChange={(e) =>
                  setForm((s) => ({ ...s, display_name: e.target.value }))
                }
              />
              <p className="text-[11px] text-sg-ink-3">
                {t("tenants.fieldDisplayNameHint")}
              </p>
            </div>
          ) : null}

          <DialogFooter className="pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              {t("tenants.cancel")}
            </Button>
            <Button
              type="submit"
              disabled={mutation.isPending}
              data-testid="create-tenant-submit"
            >
              {mutation.isPending
                ? t("tenants.creating")
                : t("tenants.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Best-effort `reason` extraction from the server's 400 body. The body is
 * JSON like `{ "error": "invalid_tenant_slug", "reason": "..." }` but
 * `apiFetch` collapses non-2xx responses into `CorlinmanApiError.message`
 * carrying the raw text. A simple JSON.parse handles both shapes.
 */
function extractReason(msg: string): string | null {
  try {
    const parsed = JSON.parse(msg) as { reason?: unknown };
    if (typeof parsed.reason === "string") return parsed.reason;
  } catch {
    /* not JSON — fall through */
  }
  return null;
}
