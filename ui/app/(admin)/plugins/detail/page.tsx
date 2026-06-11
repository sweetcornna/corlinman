"use client";

import * as React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";
import {
  fetchPluginDetail,
  invokePlugin,
  type PluginDetail,
  type PluginInvokeResponse,
} from "@/lib/api";
import { GlassPanel } from "@/components/ui/glass-panel";
import { JsonView } from "@/components/ui/json-view";
import { PluginDetailHeader } from "@/components/plugins/plugin-detail-header";

/**
 * Plugin detail — Tidepool cutover.
 *
 * Keeps the existing `?name=` URL param contract and the invoke form's
 * logic unchanged. Every section is now a GlassPanel; the doctor output
 * uses the `<JsonView>` primitive. Sandbox config renders only when the
 * manifest declares one.
 */

interface JsonSchemaLike {
  type?: string;
  properties?: Record<string, JsonSchemaLike>;
  required?: string[];
  description?: string;
  enum?: unknown[];
  default?: unknown;
}

interface ToolManifest {
  name: string;
  description?: string;
  input_schema?: JsonSchemaLike;
}

export default function PluginDetailPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const search = useSearchParams();
  const name = search?.get("name") ?? "";

  const detail = useQuery<PluginDetail>({
    queryKey: ["admin", "plugins", name],
    queryFn: () => fetchPluginDetail(name),
    enabled: !!name,
    retry: false,
  });

  const tools = extractTools(detail.data);
  const [selectedTool, setSelectedTool] = React.useState<string>("");
  React.useEffect(() => {
    if (tools.length > 0 && !selectedTool) {
      setSelectedTool(tools[0]!.name);
    }
  }, [tools, selectedTool]);

  if (!name) {
    return (
      <GlassPanel variant="soft" className="p-6 text-[13px] text-sg-ink-2">
        {t("plugins.missingName")}{" "}
        <Link href="/plugins" className="text-sg-accent underline-offset-2 hover:underline">
          {t("plugins.pluginListLink")}
        </Link>
      </GlassPanel>
    );
  }

  const summary = detail.data?.summary;
  const offline = detail.isError;
  const sandboxConfig = extractSandbox(detail.data);
  const diagnostics = detail.data?.diagnostics;
  const manifestMeta = extractManifestMeta(detail.data);

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <PluginDetailHeader
        name={name}
        version={summary?.version}
        status={summary?.status}
        description={summary?.description}
        errorMessage={
          offline
            ? `${t("plugins.loadFailed")}: ${(detail.error as Error | undefined)?.message ?? ""}`
            : summary?.error
        }
      />

      {detail.isPending ? (
        <GlassPanel variant="soft" className="h-40 animate-pulse p-6" aria-hidden />
      ) : offline ? null : detail.data ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,360px)_1fr]">
          {/* LEFT — manifest summary */}
          <GlassPanel variant="soft" className="flex flex-col gap-3 p-5">
            <SectionHeading>{t("plugins.tp.detailManifestLabel")}</SectionHeading>
            <dl className="flex flex-col gap-2 text-[13px]">
              <Field label={t("plugins.summaryType")} value={summary?.plugin_type} />
              <Field label={t("plugins.summaryOrigin")} value={summary?.origin} />
              <Field
                label={t("plugins.summaryManifest")}
                value={summary?.manifest_path}
                mono
              />
              {manifestMeta.entrypoint ? (
                <Field label="entrypoint" value={manifestMeta.entrypoint} mono />
              ) : null}
              {manifestMeta.protocols.length > 0 ? (
                <Field label="protocols" value={manifestMeta.protocols.join(", ")} mono />
              ) : null}
              {manifestMeta.hooks.length > 0 ? (
                <Field label="hooks" value={manifestMeta.hooks.join(", ")} mono />
              ) : null}
              {manifestMeta.skillRefs.length > 0 ? (
                <Field
                  label="skill refs"
                  value={manifestMeta.skillRefs.join(", ")}
                  mono
                />
              ) : null}
            </dl>
            {summary && summary.capabilities.length > 0 ? (
              <div className="mt-1 flex flex-wrap gap-1.5">
                {summary.capabilities.map((c) => (
                  <span
                    key={c}
                    className="rounded-full border border-sg-border bg-sg-inset px-2 py-[3px] font-mono text-[10.5px] tracking-wide text-sg-ink-3"
                  >
                    {c}
                  </span>
                ))}
              </div>
            ) : null}
          </GlassPanel>

          {/* RIGHT — tools · invoke · doctor · sandbox */}
          <div className="flex flex-col gap-4">
            <GlassPanel variant="soft" className="flex flex-col gap-3 p-5">
              <SectionHeading>{t("plugins.tp.detailToolsLabel")}</SectionHeading>
              {tools.length === 0 ? (
                <p className="text-[13px] text-sg-ink-3">
                  {t("plugins.tp.detailToolsEmpty")}
                </p>
              ) : (
                <ul className="flex flex-col divide-y divide-sg-border">
                  {tools.map((tool) => (
                    <li key={tool.name} className="flex flex-col gap-1 py-2 first:pt-0 last:pb-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="font-mono text-[12.5px] text-sg-ink">{tool.name}</code>
                        {tool.input_schema?.required?.length ? (
                          <span className="rounded-full border border-sg-border bg-sg-inset px-1.5 py-0 font-mono text-[9.5px] text-sg-ink-4">
                            {tool.input_schema.required.length} required
                          </span>
                        ) : null}
                      </div>
                      {tool.description ? (
                        <p className="text-[12.5px] leading-[1.5] text-sg-ink-2">
                          {tool.description}
                        </p>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </GlassPanel>

            <GlassPanel variant="soft" className="flex flex-col gap-3 p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <SectionHeading>{t("plugins.tp.detailInvokeLabel")}</SectionHeading>
                {tools.length > 1 ? (
                  <select
                    value={selectedTool}
                    onChange={(e) => setSelectedTool(e.target.value)}
                    className="h-8 rounded-md border border-sg-border bg-sg-inset px-2 font-mono text-[11px] text-sg-ink-2 outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
                  >
                    {tools.map((tool) => (
                      <option key={tool.name} value={tool.name}>
                        {tool.name}
                      </option>
                    ))}
                  </select>
                ) : tools.length === 1 ? (
                  <code className="font-mono text-[11px] text-sg-ink-3">{tools[0]!.name}</code>
                ) : null}
              </div>
              {tools.length === 0 ? (
                <p className="text-[13px] text-sg-ink-3">{t("plugins.noTools")}</p>
              ) : (
                <InvokeForm
                  pluginName={name}
                  tool={tools.find((tool) => tool.name === selectedTool) ?? tools[0]!}
                />
              )}
            </GlassPanel>

            <GlassPanel variant="soft" className="flex flex-col gap-3 p-5">
              <SectionHeading>{t("plugins.tp.detailDoctorLabel")}</SectionHeading>
              {!diagnostics || (Array.isArray(diagnostics) && diagnostics.length === 0) ? (
                <p className="text-[13px] text-sg-ink-3">
                  {t("plugins.tp.detailDoctorEmpty")}
                </p>
              ) : (
                <JsonView value={diagnostics} />
              )}
            </GlassPanel>

            <GlassPanel variant="soft" className="flex flex-col gap-3 p-5">
              <SectionHeading>{t("plugins.tp.detailSandboxLabel")}</SectionHeading>
              {sandboxConfig ? (
                <JsonView value={sandboxConfig} />
              ) : (
                <p className="text-[13px] text-sg-ink-3">
                  {t("plugins.tp.detailSandboxEmpty")}
                </p>
              )}
            </GlassPanel>
          </div>
        </div>
      ) : null}
    </motion.div>
  );
}

// ─── Sub-components ────────────────────────────────────────────────────

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-sg-ink-4">
      {children}
    </h2>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: string | undefined;
  mono?: boolean;
}) {
  if (!value) return null;
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
        {label}
      </dt>
      <dd
        className={cn(
          "min-w-0 flex-1 break-all text-right",
          mono ? "font-mono text-[11.5px] text-sg-ink-2" : "text-[12.5px] text-sg-ink",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function InvokeForm({
  pluginName,
  tool,
}: {
  pluginName: string;
  tool: ToolManifest;
}) {
  const { t } = useTranslation();
  const schema = tool.input_schema ?? {};
  const props = schema.properties ?? {};
  const simpleFields = Object.entries(props).filter(([, v]) =>
    ["string", "number", "integer", "boolean"].includes(v.type ?? ""),
  );
  const hasRichFields =
    Object.keys(props).length > 0 &&
    simpleFields.length < Object.keys(props).length;

  const [simpleValues, setSimpleValues] = React.useState<Record<string, unknown>>({});
  const [rawJson, setRawJson] = React.useState<string>("{}");
  const [useRaw, setUseRaw] = React.useState<boolean>(
    simpleFields.length === 0 || hasRichFields,
  );
  const [lastResponse, setLastResponse] = React.useState<PluginInvokeResponse | null>(null);

  React.useEffect(() => {
    setSimpleValues({});
    setRawJson("{}");
    setUseRaw(simpleFields.length === 0 || hasRichFields);
    setLastResponse(null);
  }, [tool.name, simpleFields.length, hasRichFields]);

  const invoke = useMutation({
    mutationFn: (args: unknown) => invokePlugin(pluginName, tool.name, args),
    onSuccess: (r) => setLastResponse(r),
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    let args: unknown;
    if (useRaw) {
      try {
        args = JSON.parse(rawJson);
      } catch {
        invoke.reset();
        setLastResponse(null);
        alert(t("common.invalidJson"));
        return;
      }
    } else {
      args = simpleValues;
    }
    invoke.mutate(args);
  };

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={handleSubmit}
      data-testid="plugin-invoke-form"
    >
      {tool.description ? (
        <p className="text-[12.5px] text-sg-ink-2">{tool.description}</p>
      ) : null}

      {!useRaw && simpleFields.length > 0 ? (
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {simpleFields.map(([key, fieldSchema]) => (
            <label key={key} className="flex flex-col gap-1 text-sm">
              <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-sg-ink-4">
                {key}
                {(schema.required ?? []).includes(key) ? " *" : ""}
                {fieldSchema.description ? (
                  <span className="ml-2 normal-case tracking-normal">
                    — {fieldSchema.description}
                  </span>
                ) : null}
              </span>
              <SimpleFieldInput
                schema={fieldSchema}
                value={simpleValues[key]}
                onChange={(v) => setSimpleValues({ ...simpleValues, [key]: v })}
              />
            </label>
          ))}
        </div>
      ) : (
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-sg-ink-4">
            {t("plugins.argumentsJson")}
          </span>
          <textarea
            value={rawJson}
            onChange={(e) => setRawJson(e.target.value)}
            rows={6}
            className="rounded-lg border border-sg-border bg-sg-inset p-2 font-mono text-[11.5px] text-sg-ink-2 outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
          />
        </label>
      )}

      {simpleFields.length > 0 && !hasRichFields ? (
        <label className="inline-flex items-center gap-2 text-[11.5px] text-sg-ink-3">
          <input
            type="checkbox"
            checked={useRaw}
            onChange={(e) => setUseRaw(e.target.checked)}
            className="h-3 w-3"
          />
          {t("plugins.editRawJson")}
        </label>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          disabled={invoke.isPending}
          data-testid="plugin-invoke-submit"
          className="inline-flex items-center gap-2 rounded-lg border border-sg-accent/35 bg-sg-accent-soft px-3 py-2 text-[13px] font-medium text-sg-accent transition-colors hover:bg-[color-mix(in_oklch,var(--sg-accent)_22%,transparent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50 disabled:cursor-not-allowed disabled:opacity-70"
        >
          {invoke.isPending ? t("plugins.invoking") : t("plugins.invoke")}
        </button>
        <span className="text-[11.5px] text-sg-ink-4">{t("plugins.tp.detailInvokeHint")}</span>
      </div>

      {invoke.isError ? (
        <p className="rounded-lg border border-sg-err/30 bg-sg-err-soft px-3 py-2 font-mono text-[11.5px] text-sg-err">
          {(invoke.error as Error).message}
        </p>
      ) : null}

      {lastResponse ? <ResponseBlock response={lastResponse} /> : null}
    </form>
  );
}

function SimpleFieldInput({
  schema,
  value,
  onChange,
}: {
  schema: JsonSchemaLike;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const type = schema.type;
  if (schema.enum && schema.enum.length > 0) {
    return (
      <select
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 rounded-lg border border-sg-border bg-sg-inset px-2 text-[13px] text-sg-ink outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
      >
        <option value="">(select)</option>
        {schema.enum.map((e) => (
          <option key={String(e)} value={String(e)}>
            {String(e)}
          </option>
        ))}
      </select>
    );
  }
  if (type === "boolean") {
    return (
      <input
        type="checkbox"
        checked={Boolean(value)}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  }
  if (type === "number" || type === "integer") {
    return (
      <input
        type="number"
        value={typeof value === "number" ? value : ""}
        onChange={(e) =>
          onChange(e.target.value === "" ? undefined : Number(e.target.value))
        }
        className="h-9 rounded-lg border border-sg-border bg-sg-inset px-2 text-[13px] text-sg-ink outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
      />
    );
  }
  return (
    <input
      type="text"
      value={typeof value === "string" ? value : ""}
      onChange={(e) => onChange(e.target.value)}
      className="h-9 rounded-lg border border-sg-border bg-sg-inset px-2 text-[13px] text-sg-ink outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
    />
  );
}

function ResponseBlock({ response }: { response: PluginInvokeResponse }) {
  const tone =
    response.status === "success"
      ? { ring: "border-sg-ok/30 bg-sg-ok-soft", text: "text-sg-ok" }
      : response.status === "accepted"
        ? { ring: "border-sg-border bg-sg-inset", text: "text-sg-ink-2" }
        : { ring: "border-sg-err/30 bg-sg-err-soft", text: "text-sg-err" };
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-sg-border bg-sg-inset p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "rounded-full border px-2 py-[2px] font-mono text-[10.5px] tracking-wide",
            tone.ring,
            tone.text,
          )}
        >
          {response.status}
        </span>
        <span className="font-mono text-[11px] text-sg-ink-3">
          {response.duration_ms} ms
        </span>
        {response.task_id ? (
          <code className="font-mono text-[11px] text-sg-ink-4">
            task_id: {response.task_id}
          </code>
        ) : null}
      </div>
      {response.message ? (
        <p className="font-mono text-[11.5px] text-sg-err">{response.message}</p>
      ) : null}
      {response.result !== undefined && response.result !== null ? (
        <JsonView value={response.result} className="max-h-64 overflow-auto" />
      ) : response.result_raw ? (
        <JsonView raw={response.result_raw} className="max-h-64 overflow-auto" />
      ) : null}
    </div>
  );
}

// ─── Manifest helpers ──────────────────────────────────────────────────

function extractTools(detail: PluginDetail | undefined): ToolManifest[] {
  if (!detail) return [];
  const manifest = detail.manifest as {
    capabilities?: { tools?: ToolManifest[] };
  };
  return manifest?.capabilities?.tools ?? [];
}

function extractSandbox(detail: PluginDetail | undefined): unknown {
  if (!detail) return undefined;
  const manifest = detail.manifest as {
    sandbox?: unknown;
    runtime?: { sandbox?: unknown };
  };
  return manifest?.sandbox ?? manifest?.runtime?.sandbox ?? undefined;
}

interface ManifestMeta {
  entrypoint: string | undefined;
  protocols: string[];
  hooks: string[];
  skillRefs: string[];
}

function extractManifestMeta(detail: PluginDetail | undefined): ManifestMeta {
  const empty: ManifestMeta = {
    entrypoint: undefined,
    protocols: [],
    hooks: [],
    skillRefs: [],
  };
  if (!detail) return empty;
  const m = detail.manifest as {
    entrypoint?: string;
    runtime?: { entrypoint?: string; protocol?: string | string[] };
    protocol?: string | string[];
    protocols?: string[];
    hooks?: string[] | Record<string, unknown>;
    capabilities?: { hooks?: string[] | Record<string, unknown> };
    skill_refs?: string[];
    skills?: string[];
  };
  const asArray = (v: unknown): string[] => {
    if (Array.isArray(v)) return v.filter((x): x is string => typeof x === "string");
    if (typeof v === "string") return [v];
    if (v && typeof v === "object") return Object.keys(v as Record<string, unknown>);
    return [];
  };
  return {
    entrypoint: m.entrypoint ?? m.runtime?.entrypoint,
    protocols: asArray(m.protocols ?? m.protocol ?? m.runtime?.protocol),
    hooks: asArray(m.hooks ?? m.capabilities?.hooks),
    skillRefs: asArray(m.skill_refs ?? m.skills),
  };
}
