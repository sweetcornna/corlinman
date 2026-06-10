"use client";

/**
 * ProviderGroupCard — collapsible card grouping every editable
 * credential field for one provider.
 *
 * Adapted from hermes-agent `web/src/pages/EnvPage.tsx:336-482`. The
 * grouping is simpler here because corlinman already stores credentials
 * under `[providers.<name>]` blocks, so each card maps 1:1 to a
 * provider — no env-var-prefix matching needed.
 *
 * The card is collapsed by default when nothing is configured for the
 * provider so the page stays scannable; once any field flips set or the
 * operator manually expands, it stays open until they collapse it again.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { EnvVarRow } from "@/components/credentials/env-var-row";
import type { CredentialProvider } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Per-provider display order. Anything not enumerated sorts after at
 * priority 99 (alphabetical fallback in the page-level reducer).
 */
export const PROVIDER_GROUPS: {
  provider: string;
  label: string;
  priority: number;
}[] = [
  { provider: "anthropic", label: "Anthropic", priority: 0 },
  { provider: "openai", label: "OpenAI", priority: 1 },
  { provider: "google", label: "Google", priority: 2 },
  { provider: "gemini", label: "Gemini", priority: 2 },
  { provider: "deepseek", label: "DeepSeek", priority: 3 },
  { provider: "openrouter", label: "OpenRouter", priority: 4 },
  { provider: "ollama", label: "Ollama", priority: 5 },
  { provider: "xai", label: "xAI", priority: 6 },
  { provider: "mock", label: "Mock", priority: 50 },
  { provider: "custom", label: "Custom", priority: 51 },
];

/** Display label for a provider — falls back to the raw name. */
export function getProviderLabel(name: string): string {
  return PROVIDER_GROUPS.find((g) => g.provider === name)?.label ?? name;
}

/** Lower priority sorts earlier. Unknown providers fall back to 99. */
export function getProviderPriority(name: string): number {
  return PROVIDER_GROUPS.find((g) => g.provider === name)?.priority ?? 99;
}

export interface ProviderGroupCardProps {
  provider: CredentialProvider;
  /** Map of label keys → translated strings keyed by raw field key. */
  fieldLabels?: Record<string, string>;
  /** Initial expanded state (caller may persist this in URL/localStorage). */
  defaultExpanded?: boolean;
  savingKey?: string | null;
  onSaveField: (key: string, value: string) => void | Promise<void>;
  onDeleteField: (key: string) => void;
  onToggleEnabled: (enabled: boolean) => void;
}

export function ProviderGroupCard({
  provider,
  fieldLabels,
  defaultExpanded,
  savingKey,
  onSaveField,
  onDeleteField,
  onToggleEnabled,
}: ProviderGroupCardProps) {
  const { t } = useTranslation();
  const configuredCount = provider.fields.filter((f) => f.set).length;
  const totalFields = provider.fields.length;
  const hasAnySet = configuredCount > 0;

  // Default-open when anything is set or the provider is enabled;
  // default-closed otherwise so empty rows don't dominate the page.
  // The caller can force an initial state via `defaultExpanded`.
  const [expanded, setExpanded] = React.useState<boolean>(
    defaultExpanded ?? (hasAnySet || provider.enabled),
  );

  // Re-open the card when the operator flips it from "all unset" to
  // "configured" via some other surface (e.g. OAuth tile).
  React.useEffect(() => {
    if (hasAnySet) setExpanded((prev) => prev || true);
  }, [hasAnySet]);

  const label = getProviderLabel(provider.name);

  // Partition the fields into the hermes-style "api keys → base URLs → other"
  // ordering so eye-of-the-page is always the API key. We rely on the
  // backend ordering inside `_ALLOWED_FIELDS` as the fallback for
  // anything that doesn't fit those patterns.
  const apiKeys = provider.fields.filter(
    (f) => f.key.endsWith("api_key") || f.key.endsWith("_token"),
  );
  const baseUrls = provider.fields.filter((f) => f.key.endsWith("base_url"));
  const seen = new Set([
    ...apiKeys.map((f) => f.key),
    ...baseUrls.map((f) => f.key),
  ]);
  const other = provider.fields.filter((f) => !seen.has(f.key));

  return (
    <div
      className="rounded-md border border-tp-glass-edge bg-tp-glass shadow-tp-panel backdrop-blur-glass backdrop-saturate-glass"
      data-testid={`credentials-provider-${provider.name}`}
    >
      <div
        className="flex w-full items-center justify-between gap-3 border-b border-tp-glass-edge px-3 py-2.5 hover:bg-tp-amber/5"
        data-testid={`credentials-provider-${provider.name}-header`}
      >
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          aria-expanded={expanded}
          onClick={() => setExpanded((e) => !e)}
          data-testid={`credentials-provider-${provider.name}-toggle-expand`}
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-tp-ink-3" aria-hidden />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-tp-ink-3" aria-hidden />
          )}
          <span className="truncate text-sm font-semibold capitalize tracking-tight">
            {label}
          </span>
          <Badge
            variant="secondary"
            className="font-mono text-[10px]"
            data-testid={`credentials-provider-${provider.name}-kind`}
          >
            {provider.kind}
          </Badge>
          {totalFields > 0 ? (
            hasAnySet ? (
              <Badge
                className={cn(
                  "border-transparent text-[10px]",
                  "bg-tp-amber/15 text-tp-amber",
                )}
                data-testid={`credentials-provider-${provider.name}-count`}
              >
                {t("credentials.group.configured", { count: configuredCount })}
              </Badge>
            ) : (
              <Badge
                variant="secondary"
                className="text-[10px]"
                data-testid={`credentials-provider-${provider.name}-count`}
              >
                {t("credentials.group.allUnset")}
              </Badge>
            )
          ) : null}
          {provider.enabled ? (
            <Badge className="border-transparent bg-sg-ok-soft text-sg-ok text-[10px]">
              {t("common.enabled")}
            </Badge>
          ) : null}
        </button>
        <div
          className="flex shrink-0 items-center gap-2"
          onClick={(e) => e.stopPropagation()}
        >
          <Switch
            checked={provider.enabled}
            onCheckedChange={onToggleEnabled}
            aria-label={
              provider.enabled
                ? t("credentials.providerDisabled", { provider: provider.name })
                : t("credentials.providerEnabled", { provider: provider.name })
            }
            data-testid={`credentials-provider-${provider.name}-enable`}
          />
        </div>
      </div>

      {expanded && (
        <div
          className="grid gap-1 px-3 py-3"
          data-testid={`credentials-provider-${provider.name}-body`}
        >
          {provider.fields.length === 0 ? (
            <p className="text-[11px] text-tp-ink-3">
              {t("credentials.fieldUnset")}
            </p>
          ) : (
            <>
              {apiKeys.map((f) => (
                <EnvVarRow
                  key={f.key}
                  provider={provider.name}
                  field={f}
                  compact
                  label={fieldLabels?.[f.key] ?? f.key}
                  saving={savingKey === `${provider.name}/${f.key}`}
                  onSave={(value) => onSaveField(f.key, value)}
                  onDelete={() => onDeleteField(f.key)}
                />
              ))}
              {baseUrls.map((f) => (
                <EnvVarRow
                  key={f.key}
                  provider={provider.name}
                  field={f}
                  compact
                  label={fieldLabels?.[f.key] ?? f.key}
                  saving={savingKey === `${provider.name}/${f.key}`}
                  onSave={(value) => onSaveField(f.key, value)}
                  onDelete={() => onDeleteField(f.key)}
                />
              ))}
              {other.map((f) => (
                <EnvVarRow
                  key={f.key}
                  provider={provider.name}
                  field={f}
                  compact
                  label={fieldLabels?.[f.key] ?? f.key}
                  saving={savingKey === `${provider.name}/${f.key}`}
                  onSave={(value) => onSaveField(f.key, value)}
                  onDelete={() => onDeleteField(f.key)}
                />
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default ProviderGroupCard;
