"use client";

/**
 * W-B2 — Add custom provider modal.
 *
 * Posts to `/admin/providers/custom` (handled by `createCustomProvider`)
 * with the operator-supplied slug + kind + base_url + api_key + params.
 *
 * Validation:
 *   - slug: live-validated against the backend regex
 *     `^[a-z0-9][a-z0-9_-]{0,31}$`. Invalid input shows an inline red error
 *     and disables the Submit button. Built-in slot collisions (anthropic,
 *     openai, ...) are reported by the backend as 409 — we surface that
 *     into the same inline error so the operator doesn't have to re-read.
 *   - kind: dropdown is populated from `GET /admin/providers/kinds`. We
 *     never hard-code the list — whatever the server enumerates is what
 *     the user can pick from. While the kinds query is loading, the
 *     dropdown is disabled and shows a placeholder.
 *   - base_url: required when the kind is `openai_compatible`
 *     (cannot resolve a default endpoint and would otherwise fail at
 *     first call). Optional for everything else.
 *   - api_key: free-form text with an eye-toggle reveal borrowed from the
 *     credentials EnvVarRow pattern.
 *   - params: ad-hoc key/value list — adds the row to the POST body as a
 *     `Record<string, string>`. Empty key rows are dropped.
 */

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Eye, EyeOff, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  CorlinmanApiError,
  CUSTOM_PROVIDER_SLUG_RE,
  createCustomProvider,
  listProviderKindDescriptors,
  type CustomProviderCreateBody,
  type ProviderKindDescriptor,
} from "@/lib/api";
import { cn } from "@/lib/utils";

export interface AddCustomProviderModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fired after a successful create (200/201). The parent should
   * refetch its custom-provider list. */
  onCreated?: () => void;
}

interface ParamRow {
  /** Stable React key independent of `key` so the inputs stay focused
   * while the operator types. */
  id: string;
  key: string;
  value: string;
}

/** Kinds whose backend providers don't ship a default endpoint and so
 * require a `base_url`. Mirrors the same rule from the existing built-in
 * provider editor for `openai_compatible`. */
const KINDS_REQUIRING_BASE_URL = new Set(["openai_compatible"]);

function freshParamRow(): ParamRow {
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    key: "",
    value: "",
  };
}

function presetParamRow(key: string, value: string): ParamRow {
  return {
    ...freshParamRow(),
    key,
    value,
  };
}

export function AddCustomProviderModal({
  open,
  onOpenChange,
  onCreated,
}: AddCustomProviderModalProps) {
  const [slug, setSlug] = React.useState("");
  const [slugTouched, setSlugTouched] = React.useState(false);
  const [kind, setKind] = React.useState<string>("");
  const [baseUrl, setBaseUrl] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [revealKey, setRevealKey] = React.useState(false);
  const [params, setParams] = React.useState<ParamRow[]>([freshParamRow()]);
  const [submitError, setSubmitError] = React.useState<string | null>(null);

  const kindsQuery = useQuery<ProviderKindDescriptor[]>({
    queryKey: ["admin", "providers", "kinds"],
    queryFn: listProviderKindDescriptors,
    enabled: open,
    staleTime: 5 * 60_000,
  });

  // Reset form whenever the modal re-opens. We don't reset on close so
  // re-opening within the same render cycle (e.g. backend rejected with
  // 409 and operator re-opens) preserves what they typed.
  React.useEffect(() => {
    if (!open) return;
    setSlug("");
    setSlugTouched(false);
    setKind("");
    setBaseUrl("");
    setApiKey("");
    setRevealKey(false);
    setParams([freshParamRow()]);
    setSubmitError(null);
  }, [open]);

  // Default-pick the first kind once the dropdown loads so the operator
  // doesn't have to manually pick before the form is submittable.
  React.useEffect(() => {
    if (kind || !kindsQuery.data || kindsQuery.data.length === 0) return;
    setKind(kindsQuery.data[0]!.kind);
  }, [kind, kindsQuery.data]);

  // Look up the descriptor for the currently-picked kind so we can render
  // its description as helper text under the dropdown.
  const selectedKindDescriptor = React.useMemo<
    ProviderKindDescriptor | undefined
  >(
    () => (kindsQuery.data ?? []).find((k) => k.kind === kind),
    [kind, kindsQuery.data],
  );

  const slugValid = CUSTOM_PROVIDER_SLUG_RE.test(slug);
  const baseUrlRequired = KINDS_REQUIRING_BASE_URL.has(kind);
  const baseUrlOk = !baseUrlRequired || baseUrl.trim().length > 0;
  const kindOk = !!kind;
  const formValid = slugValid && kindOk && baseUrlOk;

  const createMutation = useMutation({
    mutationFn: (body: CustomProviderCreateBody) => createCustomProvider(body),
    onSuccess: () => {
      toast.success(`Custom provider "${slug}" added`);
      onCreated?.();
      onOpenChange(false);
    },
    onError: (err) => {
      const message =
        err instanceof CorlinmanApiError
          ? err.status === 409
            ? `Slug "${slug}" collides with a built-in or existing provider.`
            : err.message
          : err instanceof Error
            ? err.message
            : String(err);
      setSubmitError(message);
      toast.error(`Add custom provider failed: ${message}`);
    },
  });

  function handleSubmit() {
    if (!formValid || createMutation.isPending) return;
    setSubmitError(null);

    const paramsMap: Record<string, unknown> = {};
    for (const row of params) {
      const k = row.key.trim();
      if (!k) continue;
      paramsMap[k] = row.value;
    }
    // Mark this provider as custom so the backend (and any later UI
    // filter) can tell it apart from the built-in section without a
    // separate column.
    paramsMap.custom = true;

    const body: CustomProviderCreateBody = {
      slug: slug.trim(),
      kind,
      base_url: baseUrl.trim() ? baseUrl.trim() : null,
      api_key: apiKey ? { value: apiKey } : null,
      params: paramsMap,
    };
    createMutation.mutate(body);
  }

  function applyFishAudioPreset() {
    setSlug("fish_audio");
    setSlugTouched(true);
    setKind("openai_compatible");
    setBaseUrl("https://api.fish.audio");
    setParams([
      presetParamRow("tts_backend", "fish"),
      presetParamRow("reference_id", ""),
      presetParamRow("format", "mp3"),
    ]);
    setSubmitError(null);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Add custom provider</DialogTitle>
          <DialogDescription>
            Register a non-built-in provider against one of the supported
            transport kinds. Saves are written to <code>config.toml</code>{" "}
            and hot-reloaded.
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] space-y-4 overflow-y-auto pr-1">
          <div className="space-y-2 rounded-md border border-tp-glass-edge p-3">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold">Presets</h3>
                <p className="text-[11px] text-tp-ink-3">
                  Pre-fill common provider wiring, then adjust secrets and
                  voice-specific fields.
                </p>
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                data-testid="custom-provider-preset-fish-audio"
                onClick={applyFishAudioPreset}
              >
                Fish Audio TTS
              </Button>
            </div>
            <p className="text-[11px] text-tp-ink-3">
              Creates an OpenAI-compatible custom provider with Fish TTS
              params. Fill <code>reference_id</code> with the trained or
              public voice id.
            </p>
          </div>

          {/* slug + kind row */}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="custom-provider-slug" className="text-xs">
                Slug
              </Label>
              <Input
                id="custom-provider-slug"
                data-testid="custom-provider-slug-input"
                value={slug}
                onChange={(e) => {
                  setSlug(e.target.value);
                  setSlugTouched(true);
                  setSubmitError(null);
                }}
                onBlur={() => setSlugTouched(true)}
                placeholder="my-vllm"
                className={cn(
                  "font-mono text-xs",
                  slugTouched && !slugValid && slug.length > 0
                    ? "border-destructive focus-visible:ring-destructive"
                    : "",
                )}
                aria-invalid={slugTouched && !slugValid && slug.length > 0}
                aria-describedby="custom-provider-slug-help"
              />
              <p
                id="custom-provider-slug-help"
                className={cn(
                  "text-[11px]",
                  slugTouched && !slugValid && slug.length > 0
                    ? "text-destructive"
                    : "text-tp-ink-3",
                )}
              >
                {slugTouched && !slugValid && slug.length > 0
                  ? "Must match ^[a-z0-9][a-z0-9_-]{0,31}$ (start with letter/digit; lowercase only)."
                  : "Lowercase letters, digits, hyphens, underscores. Up to 32 chars."}
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="custom-provider-kind" className="text-xs">
                Kind
              </Label>
              <select
                id="custom-provider-kind"
                data-testid="custom-provider-kind-select"
                value={kind}
                onChange={(e) => setKind(e.target.value)}
                disabled={kindsQuery.isPending || kindsQuery.isError}
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                {kindsQuery.isPending ? (
                  <option value="">Loading…</option>
                ) : kindsQuery.isError ? (
                  <option value="">Failed to load kinds</option>
                ) : (kindsQuery.data ?? []).length === 0 ? (
                  <option value="">(no kinds advertised)</option>
                ) : (
                  (kindsQuery.data ?? []).map((k) => (
                    <option key={k.kind} value={k.kind}>
                      {k.label ?? k.kind}
                    </option>
                  ))
                )}
              </select>
              {kindsQuery.isError ? (
                <p className="text-[11px] text-destructive">
                  Could not fetch /admin/providers/kinds.
                </p>
              ) : selectedKindDescriptor?.description ? (
                <p className="text-[11px] text-tp-ink-3">
                  {selectedKindDescriptor.description}
                </p>
              ) : null}
            </div>
          </div>

          {/* base_url */}
          <div className="space-y-1.5">
            <Label htmlFor="custom-provider-base-url" className="text-xs">
              Base URL{" "}
              {baseUrlRequired ? (
                <span className="text-destructive">*</span>
              ) : (
                <span className="text-tp-ink-3">(optional)</span>
              )}
            </Label>
            <Input
              id="custom-provider-base-url"
              data-testid="custom-provider-base-url-input"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={
                baseUrlRequired
                  ? "https://vllm.internal/v1"
                  : "(use SDK default)"
              }
              className="font-mono text-xs"
            />
            <p className="text-[11px] text-tp-ink-3">
              {baseUrlRequired
                ? `Required for "${kind}" — there is no built-in default endpoint.`
                : "Leave blank to use the kind's default endpoint."}
            </p>
          </div>

          {/* api_key */}
          <div className="space-y-1.5">
            <Label htmlFor="custom-provider-api-key" className="text-xs">
              API key{" "}
              <span className="text-tp-ink-3">(stored as literal)</span>
            </Label>
            <div className="flex items-center gap-2">
              <Input
                id="custom-provider-api-key"
                data-testid="custom-provider-api-key-input"
                type={revealKey ? "text" : "password"}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-…"
                autoComplete="off"
                spellCheck={false}
                className="font-mono text-xs"
              />
              <Button
                type="button"
                size="sm"
                variant="ghost"
                data-testid="custom-provider-api-key-reveal"
                onClick={() => setRevealKey((r) => !r)}
                aria-label={revealKey ? "Hide API key" : "Reveal API key"}
                aria-pressed={revealKey}
              >
                {revealKey ? (
                  <EyeOff className="h-3.5 w-3.5" />
                ) : (
                  <Eye className="h-3.5 w-3.5" />
                )}
              </Button>
            </div>
            <p className="text-[11px] text-tp-ink-3">
              Leave blank if the provider needs no auth or you&apos;ll supply
              it via env later.
            </p>
          </div>

          {/* params */}
          <div className="space-y-2 rounded-md border border-tp-glass-edge p-3">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-sm font-semibold">Params</h3>
                <p className="text-[11px] text-tp-ink-3">
                  Free-form key/value pairs written to{" "}
                  <code>params = {"{ … }"}</code> in config.toml.
                </p>
              </div>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                data-testid="custom-provider-params-add"
                onClick={() =>
                  setParams((rows) => [...rows, freshParamRow()])
                }
              >
                <Plus className="h-3 w-3" /> Add row
              </Button>
            </div>
            <div className="space-y-2">
              {params.map((row, idx) => (
                <div
                  key={row.id}
                  className="flex items-center gap-2"
                  data-testid={`custom-provider-params-row-${idx}`}
                >
                  <Input
                    value={row.key}
                    onChange={(e) =>
                      setParams((rows) =>
                        rows.map((r) =>
                          r.id === row.id
                            ? { ...r, key: e.target.value }
                            : r,
                        ),
                      )
                    }
                    placeholder="key"
                    className="h-8 flex-1 font-mono text-xs"
                  />
                  <Input
                    value={row.value}
                    onChange={(e) =>
                      setParams((rows) =>
                        rows.map((r) =>
                          r.id === row.id
                            ? { ...r, value: e.target.value }
                            : r,
                        ),
                      )
                    }
                    placeholder="value"
                    className="h-8 flex-1 font-mono text-xs"
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    aria-label="Remove row"
                    onClick={() =>
                      setParams((rows) =>
                        rows.length <= 1
                          ? [freshParamRow()]
                          : rows.filter((r) => r.id !== row.id),
                      )
                    }
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
            </div>
          </div>

          {submitError ? (
            <div
              className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-destructive"
              data-testid="custom-provider-submit-error"
              role="alert"
            >
              {submitError}
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={createMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!formValid || createMutation.isPending}
            data-testid="custom-provider-submit"
          >
            {createMutation.isPending ? "Saving…" : "Add provider"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default AddCustomProviderModal;
