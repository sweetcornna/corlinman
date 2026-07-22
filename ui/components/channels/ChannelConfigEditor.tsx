"use client";

/**
 * Editable per-channel config card. Renders the channel's editable fields —
 * grouped into secrets / strings / id-lists / keyword filter / flags /
 * numbers from `CHANNEL_CONFIG_SPEC` — and PUTs the STRUCTURED body the
 * backend expects (`{ secrets, urls, ids, filters, flags, numbers }`). Only
 * changed fields are sent; blank secret inputs are omitted (= keep the
 * current on-disk token).
 *
 * Fields marked `advanced: true` in the spec (endpoint-override URLs,
 * tuning numbers) fold behind a "+ advanced" disclosure — same pattern as
 * profiles/create-profile-modal — so the default view stays operator-sized.
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
import { FieldHint } from "@/components/ui/field-hint";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  CHANNEL_CONFIG_SPEC,
  buildChannelConfigBody,
  isEmptyConfigBody,
  putChannelConfig,
  seedDraft,
  specHasAdvanced,
  type ChannelConfigDraft,
  type ChannelFieldSpec,
  type ConfigEditableChannel,
} from "@/lib/api/channel-config";
import type { ChannelConfigKeys } from "@/lib/api/full-inbox-channel";

export interface ChannelConfigEditorProps {
  channel: ConfigEditableChannel;
  configKeys: ChannelConfigKeys;
  /** Runtime values for fields marked `managed`; these never enter saves. */
  managedValues?: Record<string, string>;
  /** Called after a successful save so the parent can refetch status. */
  onSaved?: () => void;
}

const basic = (fields: ChannelFieldSpec[]) => fields.filter((f) => !f.advanced);
const advanced = (fields: ChannelFieldSpec[]) => fields.filter((f) => f.advanced);

export function ChannelConfigEditor({
  channel,
  configKeys,
  managedValues = {},
  onSaved,
}: ChannelConfigEditorProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const spec = CHANNEL_CONFIG_SPEC[channel];
  const hasAdvanced = specHasAdvanced(spec);
  const [showAdvanced, setShowAdvanced] = React.useState(false);

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

  /** One short sentence under the control; absent key = no hint line. */
  const fieldHint = (key: string) => {
    const hint = t(`channelConfig.hint.${key}`, { defaultValue: "" });
    return hint ? <FieldHint>{hint}</FieldHint> : null;
  };

  const renderSecret = (f: ChannelFieldSpec) => (
    <div key={f.key} className="space-y-1">
      <Label htmlFor={`cc-${channel}-${f.key}`} className="text-sm">
        {fieldLabel(f.key)}
      </Label>
      <Input
        id={`cc-${channel}-${f.key}`}
        type="password"
        autoComplete="new-password"
        placeholder={t("channelConfig.secretPlaceholder")}
        value={draft.secrets[f.key] ?? ""}
        onChange={(e) => setField("secrets", f.key, e.target.value)}
        data-testid={`cc-secret-${f.key}`}
      />
      {fieldHint(f.key)}
    </div>
  );

  // Plain-string group: default = one-line input; `input: "select"` renders
  // a native select (option labels via channelConfig.option.<key>.<value>);
  // `input: "textarea"` renders a small textarea for prompt-sized text.
  const renderString = (f: ChannelFieldSpec) => (
    <div key={f.key} className="space-y-1">
      <Label htmlFor={`cc-${channel}-${f.key}`} className="text-sm">
        {fieldLabel(f.key)}
      </Label>
      {f.input === "select" ? (
        <select
          id={`cc-${channel}-${f.key}`}
          value={draft.urls[f.key] ?? ""}
          onChange={(e) => setField("urls", f.key, e.target.value)}
          data-testid={`cc-select-${f.key}`}
          className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {(f.options ?? []).map((opt) => (
            <option key={opt} value={opt}>
              {t(`channelConfig.option.${f.key}.${opt}`, { defaultValue: opt })}
            </option>
          ))}
        </select>
      ) : f.input === "textarea" ? (
        <textarea
          id={`cc-${channel}-${f.key}`}
          rows={3}
          value={draft.urls[f.key] ?? ""}
          onChange={(e) => setField("urls", f.key, e.target.value)}
          data-testid={`cc-text-${f.key}`}
          className="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
      ) : (
        <Input
          id={`cc-${channel}-${f.key}`}
          value={draft.urls[f.key] ?? ""}
          onChange={(e) => setField("urls", f.key, e.target.value)}
          data-testid={`cc-url-${f.key}`}
        />
      )}
      {fieldHint(f.key)}
    </div>
  );

  const renderList = (f: ChannelFieldSpec, group: "ids" | "filters") => {
    const managed = f.managed === true;
    const value = managed
      ? (managedValues[f.key] ?? "")
      : ((group === "ids" ? draft.ids[f.key] : draft.filters[f.key]) ?? "");
    return (
      <div key={f.key} className="space-y-1">
        <div className="flex items-center justify-between gap-2">
          <Label htmlFor={`cc-${channel}-${f.key}`} className="text-sm">
            {fieldLabel(f.key)}
          </Label>
          {managed ? (
            <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-sg-ink-4">
              {t("channelConfig.managed")}
            </span>
          ) : null}
        </div>
        <Input
          id={`cc-${channel}-${f.key}`}
          placeholder={
            managed
              ? t("channelConfig.managedPlaceholder")
              : t("channelConfig.listPlaceholder")
          }
          value={value}
          readOnly={managed}
          aria-readonly={managed}
          onChange={managed ? undefined : (e) => setField(group, f.key, e.target.value)}
          data-testid={`cc-list-${f.key}`}
        />
        {fieldHint(f.key) ?? (
          <FieldHint>{t("channelConfig.listHint")}</FieldHint>
        )}
      </div>
    );
  };

  const renderNumber = (f: ChannelFieldSpec) => (
    <div key={f.key} className="space-y-1">
      <Label htmlFor={`cc-${channel}-${f.key}`} className="text-sm">
        {fieldLabel(f.key)}
      </Label>
      <Input
        id={`cc-${channel}-${f.key}`}
        type="number"
        inputMode="decimal"
        placeholder={f.placeholder}
        value={draft.numbers[f.key] ?? ""}
        onChange={(e) => setField("numbers", f.key, e.target.value)}
        data-testid={`cc-num-${f.key}`}
      />
      {fieldHint(f.key)}
    </div>
  );

  const renderFlag = (f: ChannelFieldSpec) => (
    <div
      key={f.key}
      className="rounded-sg-md border border-sg-border bg-sg-inset px-3 py-2"
    >
      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={`cc-${channel}-${f.key}`} className="text-sm">
          {fieldLabel(f.key)}
        </Label>
        <Switch
          id={`cc-${channel}-${f.key}`}
          checked={!!draft.flags[f.key]}
          onCheckedChange={(next) => setField("flags", f.key, next)}
          data-testid={`cc-flag-${f.key}`}
          aria-label={fieldLabel(f.key)}
        />
      </div>
      {fieldHint(f.key)}
    </div>
  );

  const renderFields = (fields: {
    secrets: ChannelFieldSpec[];
    urls: ChannelFieldSpec[];
    ids: ChannelFieldSpec[];
    filters: ChannelFieldSpec[];
    flags: ChannelFieldSpec[];
    numbers: ChannelFieldSpec[];
  }) => (
    <>
      {fields.secrets.length > 0 ? (
        <fieldset className="space-y-2">
          <legend className="text-[11px] uppercase tracking-wider text-sg-ink-4">
            {t("channelConfig.secretsLegend")}
          </legend>
          {fields.secrets.map(renderSecret)}
        </fieldset>
      ) : null}
      {fields.urls.map(renderString)}
      {fields.ids.map((f) => renderList(f, "ids"))}
      {fields.filters.map((f) => renderList(f, "filters"))}
      {fields.flags.map(renderFlag)}
      {fields.numbers.map(renderNumber)}
    </>
  );

  return (
    <Card data-testid={`channel-config-editor-${channel}`}>
      <CardHeader>
        <CardTitle className="text-base">{t("channelConfig.title")}</CardTitle>
        <CardDescription>{t("channelConfig.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {renderFields({
          secrets: basic(spec.secrets),
          urls: basic(spec.urls),
          ids: basic(spec.ids),
          filters: basic(spec.filters),
          flags: basic(spec.flags),
          numbers: basic(spec.numbers),
        })}

        {hasAdvanced ? (
          <div>
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              data-testid={`cc-toggle-advanced-${channel}`}
              className="text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline focus-visible:outline-none focus-visible:underline"
            >
              {showAdvanced ? "—" : "+"} {t("channelConfig.advancedToggle")}
            </button>
          </div>
        ) : null}

        {hasAdvanced && showAdvanced ? (
          <div className="space-y-4" data-testid={`cc-advanced-${channel}`}>
            {renderFields({
              secrets: advanced(spec.secrets),
              urls: advanced(spec.urls),
              ids: advanced(spec.ids),
              filters: advanced(spec.filters),
              flags: advanced(spec.flags),
              numbers: advanced(spec.numbers),
            })}
          </div>
        ) : null}

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
