"use client";

/**
 * Editable per-channel config card. Renders the channel's editable fields —
 * grouped into secrets / endpoints / id-lists / keyword filter / flags from
 * `CHANNEL_CONFIG_SPEC` — and PUTs the STRUCTURED body the backend expects
 * (`{ secrets, urls, ids, filters, flags }`). Only changed fields are sent;
 * blank secret inputs are omitted (= keep the current on-disk token).
 *
 * Seeds from the status route's non-secret `config_keys`; secrets always
 * start blank since the backend never echoes them.
 */

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useTranslation } from "react-i18next";

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
import { Switch } from "@/components/ui/switch";
import {
  CHANNEL_CONFIG_SPEC,
  buildChannelConfigBody,
  isEmptyConfigBody,
  putChannelConfig,
  seedDraft,
  type ChannelConfigDraft,
  type ConfigEditableChannel,
} from "@/lib/api/channel-config";
import type { ChannelConfigKeys } from "@/lib/api/full-inbox-channel";

export interface ChannelConfigEditorProps {
  channel: ConfigEditableChannel;
  configKeys: ChannelConfigKeys;
  /** Called after a successful save so the parent can refetch status. */
  onSaved?: () => void;
}

export function ChannelConfigEditor({
  channel,
  configKeys,
  onSaved,
}: ChannelConfigEditorProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const spec = CHANNEL_CONFIG_SPEC[channel];

  // Initial = seeded from the server's current non-secret keys; draft is the
  // editable copy. Re-seed when the upstream config_keys change (after a save
  // / refetch) so the form reflects what's actually persisted.
  const initial = React.useMemo(
    () => seedDraft(channel, configKeys),
    [channel, configKeys],
  );
  const [draft, setDraft] = React.useState<ChannelConfigDraft>(initial);
  React.useEffect(() => setDraft(initial), [initial]);

  const body = buildChannelConfigBody(channel, draft, initial);
  const dirty = !isEmptyConfigBody(body);

  const mutation = useMutation({
    mutationFn: () => putChannelConfig(channel, body),
    onSuccess: async (out) => {
      toast.success(
        t("channelConfig.saved", { count: out.wrote.length }),
      );
      await queryClient.invalidateQueries({
        queryKey: ["admin", "channels", channel],
      });
      onSaved?.();
    },
    onError: (err) => {
      toast.error(
        t("channelConfig.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const setField = (
    group: keyof ChannelConfigDraft,
    key: string,
    value: string | boolean,
  ) =>
    setDraft((d) => ({
      ...d,
      [group]: { ...d[group], [key]: value },
    }));

  const fieldLabel = (key: string) =>
    t(`channelConfig.field.${key}`, { defaultValue: key });

  return (
    <Card data-testid={`channel-config-editor-${channel}`}>
      <CardHeader>
        <CardTitle className="text-base">{t("channelConfig.title")}</CardTitle>
        <CardDescription>{t("channelConfig.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {spec.secrets.length > 0 ? (
          <fieldset className="space-y-2">
            <legend className="text-xs uppercase tracking-wider text-tp-ink-3">
              {t("channelConfig.secretsLegend")}
            </legend>
            {spec.secrets.map((key) => (
              <div key={key} className="space-y-1">
                <Label htmlFor={`cc-${channel}-${key}`} className="text-sm">
                  {fieldLabel(key)}
                </Label>
                <Input
                  id={`cc-${channel}-${key}`}
                  type="password"
                  autoComplete="new-password"
                  placeholder={t("channelConfig.secretPlaceholder")}
                  value={draft.secrets[key] ?? ""}
                  onChange={(e) => setField("secrets", key, e.target.value)}
                  data-testid={`cc-secret-${key}`}
                />
              </div>
            ))}
          </fieldset>
        ) : null}

        {spec.urls.map((key) => (
          <div key={key} className="space-y-1">
            <Label htmlFor={`cc-${channel}-${key}`} className="text-sm">
              {fieldLabel(key)}
            </Label>
            <Input
              id={`cc-${channel}-${key}`}
              value={draft.urls[key] ?? ""}
              onChange={(e) => setField("urls", key, e.target.value)}
              data-testid={`cc-url-${key}`}
            />
          </div>
        ))}

        {[...spec.ids, ...spec.filters].map((key) => {
          const group = spec.ids.includes(key) ? "ids" : "filters";
          return (
            <div key={key} className="space-y-1">
              <Label htmlFor={`cc-${channel}-${key}`} className="text-sm">
                {fieldLabel(key)}
              </Label>
              <Input
                id={`cc-${channel}-${key}`}
                placeholder={t("channelConfig.listPlaceholder")}
                value={(group === "ids" ? draft.ids[key] : draft.filters[key]) ?? ""}
                onChange={(e) => setField(group, key, e.target.value)}
                data-testid={`cc-list-${key}`}
              />
              <p className="text-[11px] text-tp-ink-4">
                {t("channelConfig.listHint")}
              </p>
            </div>
          );
        })}

        {spec.flags.map((key) => (
          <div
            key={key}
            className="flex items-center justify-between gap-4 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2"
          >
            <Label htmlFor={`cc-${channel}-${key}`} className="text-sm">
              {fieldLabel(key)}
            </Label>
            <Switch
              id={`cc-${channel}-${key}`}
              checked={!!draft.flags[key]}
              onCheckedChange={(next) => setField("flags", key, next)}
              data-testid={`cc-flag-${key}`}
              aria-label={fieldLabel(key)}
            />
          </div>
        ))}

        <div className="flex items-center justify-end">
          <Button
            type="button"
            size="sm"
            onClick={() => mutation.mutate()}
            disabled={!dirty || mutation.isPending}
            data-testid={`channel-config-save-${channel}`}
          >
            {mutation.isPending ? t("channelConfig.saving") : t("channelConfig.save")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
