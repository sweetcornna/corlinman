"use client";

import * as React from "react";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { GlassPanel } from "@/components/ui/glass-panel";
import { ShieldCheck, Trash2 } from "@/components/icons";
import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/format";
import {
  fetchAuthzPolicy,
  listAlwaysGrants,
  revokeAlwaysGrant,
  saveAuthzPolicy,
  type AlwaysGrant,
  type AuthzPolicy,
  type AuthzRule,
} from "@/lib/api";

/**
 * /authz — the unified-authorization admin page (W3-4). Two panels:
 *
 *  1. Policy editor — a form over the `[permissions]` config section.
 *     Saving goes through `PUT /admin/authz/policy`, which rewrites
 *     config.toml AND re-renders the py-config sidecar, so the agent
 *     picks the new rules up on its next turn (no restart).
 *  2. Grant records — the durable "always" grants from the GrantStore
 *     SQLite. Revoking one takes effect at the agent's next permission
 *     check (mtime-based cross-process invalidation).
 *
 * Approval queue lives on its own page (/approvals) — these panels are
 * operator POLICY, kept deliberately separate from per-call decisions.
 */

const ACTIONS = ["allow", "deny", "ask", "log"] as const;
const MEMORIES = ["", "once", "session", "always"] as const;
const MODES = ["", "default", "acceptEdits", "plan", "bypass"] as const;
const TRISTATE = ["", "true", "false"] as const;

type EditableRule = {
  tool: string;
  action: string;
  note: string;
  memory: string;
  surface: string;
  user: string;
  session: string;
  model: string;
  tenant: string;
};

type EditablePolicy = {
  mode: string;
  strict: string;
  default_action: string;
  last_match_wins: string;
  external_tools_enforced: string;
  rules: EditableRule[];
};

function toEditable(policy: AuthzPolicy): EditablePolicy {
  return {
    mode: policy.mode ?? "",
    strict: policy.strict === null ? "" : String(policy.strict),
    default_action: policy.default_action ?? "",
    last_match_wins:
      policy.last_match_wins === null ? "" : String(policy.last_match_wins),
    external_tools_enforced:
      policy.external_tools_enforced === null
        ? ""
        : String(policy.external_tools_enforced),
    rules: policy.rules.map((r) => ({
      tool: r.tool,
      action: r.action,
      note: r.note ?? "",
      memory: r.memory ?? "",
      surface: r.scope?.surface ?? "",
      user: r.scope?.user ?? "",
      session: r.scope?.session ?? "",
      model: r.scope?.model ?? "",
      tenant: r.scope?.tenant ?? "",
    })),
  };
}

function toWire(edit: EditablePolicy): AuthzPolicy {
  const tri = (v: string): boolean | null => (v === "" ? null : v === "true");
  const rules: AuthzRule[] = edit.rules.map((r) => {
    const scope: Record<string, string> = {};
    for (const key of ["tenant", "surface", "user", "session", "model"] as const) {
      if (r[key].trim()) scope[key] = r[key].trim();
    }
    return {
      tool: r.tool.trim(),
      action: r.action,
      note: r.note.trim() ? r.note.trim() : null,
      memory: r.memory ? r.memory : null,
      scope: Object.keys(scope).length > 0 ? scope : null,
    };
  });
  return {
    mode: edit.mode || null,
    strict: tri(edit.strict),
    default_action: edit.default_action || null,
    last_match_wins: tri(edit.last_match_wins),
    external_tools_enforced: tri(edit.external_tools_enforced),
    rules,
  };
}

const EMPTY_RULE: EditableRule = {
  tool: "",
  action: "ask",
  note: "",
  memory: "",
  surface: "",
  user: "",
  session: "",
  model: "",
  tenant: "",
};

export default function AuthzPage() {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-5">
      <header className="flex items-start gap-3">
        <span
          aria-hidden
          className={cn(
            "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center",
            "rounded-xl border border-sg-border bg-sg-inset",
          )}
        >
          <ShieldCheck className="h-4.5 w-4.5 text-sg-accent" />
        </span>
        <div>
          <h1 className="text-[17px] font-medium text-sg-ink">
            {t("authz.title")}
          </h1>
          <p className="mt-0.5 text-[12.5px] text-sg-ink-3">
            {t("authz.subtitle")}
          </p>
        </div>
      </header>

      <PolicyEditor />
      <GrantsPanel />
    </div>
  );
}

// ─── Panel 1 — policy editor ─────────────────────────────────────────────

function PolicyEditor() {
  const { t } = useTranslation();
  const query = useQuery<AuthzPolicy>({
    queryKey: ["admin", "authz", "policy"],
    queryFn: fetchAuthzPolicy,
    retry: false,
  });

  const [edit, setEdit] = useState<EditablePolicy | null>(null);
  const [banner, setBanner] = useState<
    { tone: "ok" | "err"; text: string } | null
  >(null);

  // Hydrate the form once per successful fetch (later refetches don't
  // clobber in-progress edits).
  useEffect(() => {
    if (query.data && edit === null) setEdit(toEditable(query.data));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data]);

  const save = useMutation({
    mutationFn: (policy: AuthzPolicy) => saveAuthzPolicy(policy),
    onSuccess: (saved) => {
      setEdit(toEditable(saved));
      setBanner({ tone: "ok", text: t("authz.policy.saved") });
    },
    onError: (err) => {
      setBanner({
        tone: "err",
        text: t("authz.policy.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      });
    },
  });

  const patchRule = (index: number, patch: Partial<EditableRule>) => {
    setEdit((prev) => {
      if (!prev) return prev;
      const rules = prev.rules.map((r, i) =>
        i === index ? { ...r, ...patch } : r,
      );
      return { ...prev, rules };
    });
  };

  const invalidRules =
    edit?.rules.some((r) => !r.tool.trim()) ?? false;

  return (
    <GlassPanel as="section" variant="soft" className="p-5">
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <h2 className="text-[14px] font-medium text-sg-ink">
            {t("authz.policy.title")}
          </h2>
          <p className="mt-0.5 text-[12px] text-sg-ink-3">
            {t("authz.policy.hint")}
          </p>
        </div>
        <button
          type="button"
          disabled={!edit || save.isPending || invalidRules}
          onClick={() => {
            if (!edit) return;
            setBanner(null);
            save.mutate(toWire(edit));
          }}
          className={cn(
            "shrink-0 rounded-full px-4 py-1.5 text-[12px] font-medium",
            "bg-sg-accent text-primary-foreground shadow-sg-primary",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/55",
            "disabled:pointer-events-none disabled:opacity-50",
          )}
        >
          {save.isPending ? t("authz.policy.saving") : t("authz.policy.save")}
        </button>
      </div>

      {banner ? (
        <p
          role="status"
          className={cn(
            "mt-3 rounded-xl border px-3 py-2 text-[12px]",
            banner.tone === "ok"
              ? "border-sg-ok/35 bg-sg-ok-soft text-sg-ok"
              : "border-sg-err/40 bg-sg-err-soft text-sg-err",
          )}
        >
          {banner.text}
        </p>
      ) : null}

      {query.isError ? (
        <p className="mt-4 text-[12.5px] text-sg-err">
          {t("authz.policy.loadFailed")}
        </p>
      ) : !edit ? (
        <p className="mt-4 text-[12.5px] text-sg-ink-3">{t("common.loading")}</p>
      ) : (
        <>
          {/* Global knobs */}
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <LabeledSelect
              label={t("authz.policy.mode")}
              value={edit.mode}
              options={MODES.map((m) => ({
                value: m,
                label: m === "" ? t("authz.policy.unset") : m,
              }))}
              onChange={(mode) => setEdit({ ...edit, mode })}
            />
            <TriSelect
              label={t("authz.policy.strict")}
              value={edit.strict}
              onChange={(strict) => setEdit({ ...edit, strict })}
            />
            <LabeledSelect
              label={t("authz.policy.defaultAction")}
              value={edit.default_action}
              options={["", ...ACTIONS].map((a) => ({
                value: a,
                label: a === "" ? t("authz.policy.unset") : a,
              }))}
              onChange={(default_action) => setEdit({ ...edit, default_action })}
            />
            <TriSelect
              label={t("authz.policy.lastMatchWins")}
              value={edit.last_match_wins}
              onChange={(last_match_wins) =>
                setEdit({ ...edit, last_match_wins })
              }
            />
            <TriSelect
              label={t("authz.policy.externalTools")}
              value={edit.external_tools_enforced}
              onChange={(external_tools_enforced) =>
                setEdit({ ...edit, external_tools_enforced })
              }
            />
          </div>

          {/* Rules */}
          <div className="mt-5 flex items-baseline justify-between">
            <h3 className="text-[12.5px] font-medium uppercase tracking-[0.08em] text-sg-ink-3">
              {t("authz.policy.rules")}
            </h3>
            <button
              type="button"
              onClick={() =>
                setEdit({ ...edit, rules: [...edit.rules, { ...EMPTY_RULE }] })
              }
              className={cn(
                "rounded-full border border-sg-border px-3 py-1 text-[11.5px]",
                "text-sg-ink-2 hover:bg-sg-inset-hover",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
              )}
            >
              {t("authz.policy.addRule")}
            </button>
          </div>

          {edit.rules.length === 0 ? (
            <p className="mt-3 text-[12.5px] text-sg-ink-3">
              {t("authz.policy.noRules")}
            </p>
          ) : (
            <ul className="mt-3 flex flex-col gap-2.5">
              {edit.rules.map((rule, i) => (
                <li
                  key={i}
                  className="rounded-xl border border-sg-border bg-sg-inset p-3"
                >
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-[minmax(0,2fr)_110px_110px_minmax(0,2fr)_32px]">
                    <LabeledInput
                      label={t("authz.policy.tool")}
                      value={rule.tool}
                      mono
                      placeholder="run_shell(rm:*)"
                      invalid={!rule.tool.trim()}
                      onChange={(tool) => patchRule(i, { tool })}
                    />
                    <LabeledSelect
                      label={t("authz.policy.action")}
                      value={rule.action}
                      options={ACTIONS.map((a) => ({ value: a, label: a }))}
                      onChange={(action) => patchRule(i, { action })}
                    />
                    <LabeledSelect
                      label={t("authz.policy.memory")}
                      value={rule.memory}
                      options={MEMORIES.map((m) => ({
                        value: m,
                        label: m === "" ? t("authz.policy.unset") : m,
                      }))}
                      onChange={(memory) => patchRule(i, { memory })}
                    />
                    <LabeledInput
                      label={t("authz.policy.note")}
                      value={rule.note}
                      placeholder={t("authz.policy.notePlaceholder")}
                      onChange={(note) => patchRule(i, { note })}
                    />
                    <button
                      type="button"
                      aria-label={t("authz.policy.removeRuleAria")}
                      onClick={() =>
                        setEdit({
                          ...edit,
                          rules: edit.rules.filter((_r, j) => j !== i),
                        })
                      }
                      className={cn(
                        "self-end rounded-lg border border-sg-border p-1.5",
                        "text-sg-ink-3 hover:bg-sg-err-soft hover:text-sg-err",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-err/40",
                      )}
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden />
                    </button>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
                    <LabeledInput
                      label={t("authz.policy.scopeSurface")}
                      value={rule.surface}
                      mono
                      placeholder="qq|telegram"
                      onChange={(surface) => patchRule(i, { surface })}
                    />
                    <LabeledInput
                      label={t("authz.policy.scopeUser")}
                      value={rule.user}
                      mono
                      placeholder="admin*"
                      onChange={(user) => patchRule(i, { user })}
                    />
                    <LabeledInput
                      label={t("authz.policy.scopeSession")}
                      value={rule.session}
                      mono
                      placeholder="acme::*"
                      onChange={(session) => patchRule(i, { session })}
                    />
                    <LabeledInput
                      label={t("authz.policy.scopeModel")}
                      value={rule.model}
                      mono
                      placeholder="claude-*"
                      onChange={(model) => patchRule(i, { model })}
                    />
                    <LabeledInput
                      label={t("authz.policy.scopeTenant")}
                      value={rule.tenant}
                      mono
                      placeholder="acme"
                      onChange={(tenant) => patchRule(i, { tenant })}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-[11.5px] text-sg-ink-4">
            {t("authz.policy.effectHint")}
          </p>
        </>
      )}
    </GlassPanel>
  );
}

// ─── Panel 2 — durable grants ────────────────────────────────────────────

function GrantsPanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const query = useQuery<AlwaysGrant[]>({
    queryKey: ["admin", "authz", "grants"],
    queryFn: listAlwaysGrants,
    retry: false,
  });
  const [error, setError] = useState<string | null>(null);

  const revoke = useMutation({
    mutationFn: (grant: AlwaysGrant) =>
      revokeAlwaysGrant({
        tenant: grant.tenant,
        surface: grant.surface,
        user_id: grant.user_id,
        tool: grant.tool,
        arg_digest: grant.arg_digest,
      }),
    onSuccess: () => setError(null),
    onError: (err) =>
      setError(
        t("authz.grants.revokeFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: ["admin", "authz", "grants"] }),
  });

  const rows = query.data ?? [];

  return (
    <GlassPanel as="section" variant="soft" className="p-5">
      <h2 className="text-[14px] font-medium text-sg-ink">
        {t("authz.grants.title")}
      </h2>
      <p className="mt-0.5 text-[12px] text-sg-ink-3">
        {t("authz.grants.hint")}
      </p>

      {error ? (
        <p
          role="alert"
          className="mt-3 rounded-xl border border-sg-err/40 bg-sg-err-soft px-3 py-2 text-[12px] text-sg-err"
        >
          {error}
        </p>
      ) : null}

      {query.isError ? (
        <p className="mt-4 text-[12.5px] text-sg-err">
          {t("authz.grants.loadFailed")}
        </p>
      ) : query.isPending ? (
        <p className="mt-4 text-[12.5px] text-sg-ink-3">{t("common.loading")}</p>
      ) : rows.length === 0 ? (
        <p className="mt-4 text-[12.5px] text-sg-ink-3">
          {t("authz.grants.empty")}
        </p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[640px] border-collapse text-left">
            <thead>
              <tr className="border-b border-sg-border text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
                <th className="pb-2 pr-3 font-medium">
                  {t("authz.grants.colTool")}
                </th>
                <th className="pb-2 pr-3 font-medium">
                  {t("authz.grants.colTenant")}
                </th>
                <th className="pb-2 pr-3 font-medium">
                  {t("authz.grants.colSurface")}
                </th>
                <th className="pb-2 pr-3 font-medium">
                  {t("authz.grants.colUser")}
                </th>
                <th className="pb-2 pr-3 font-medium">
                  {t("authz.grants.colDigest")}
                </th>
                <th className="pb-2 pr-3 font-medium">
                  {t("authz.grants.colCreated")}
                </th>
                <th className="pb-2 font-medium" />
              </tr>
            </thead>
            <tbody className="font-mono text-[12px] text-sg-ink-2">
              {rows.map((g) => (
                <tr
                  key={`${g.tenant}|${g.surface}|${g.user_id}|${g.tool}|${g.arg_digest}`}
                  className="border-b border-sg-border/60"
                >
                  <td className="py-2 pr-3 text-sg-ink">{g.tool}</td>
                  <td className="py-2 pr-3">{g.tenant}</td>
                  <td className="py-2 pr-3">
                    {g.surface || t("authz.grants.anyValue")}
                  </td>
                  <td className="py-2 pr-3">
                    {g.user_id || t("authz.grants.anyValue")}
                  </td>
                  <td className="py-2 pr-3 text-sg-ink-4">
                    {g.arg_digest.slice(0, 12)}…
                  </td>
                  <td className="py-2 pr-3">
                    {g.created_at !== null
                      ? formatDateTime(new Date(g.created_at * 1000))
                      : t("authz.grants.unsynced")}
                  </td>
                  <td className="py-2 text-right">
                    <button
                      type="button"
                      disabled={revoke.isPending}
                      onClick={() => {
                        if (
                          window.confirm(
                            t("authz.grants.revokeConfirm", { tool: g.tool }),
                          )
                        ) {
                          revoke.mutate(g);
                        }
                      }}
                      className={cn(
                        "rounded-full border border-sg-err/40 px-3 py-1 text-[11px]",
                        "font-sans text-sg-err hover:bg-sg-err-soft",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-err/40",
                        "disabled:pointer-events-none disabled:opacity-50",
                      )}
                    >
                      {t("authz.grants.revoke")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-[11.5px] text-sg-ink-4">
        {t("authz.grants.effectHint")}
      </p>
    </GlassPanel>
  );
}

// ─── form atoms ──────────────────────────────────────────────────────────

function LabeledSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
        {label}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          "h-8 rounded-lg border border-sg-border bg-sg-inset px-2 text-[12px]",
          "text-sg-ink-2",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        )}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function TriSelect({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const labels: Record<(typeof TRISTATE)[number], string> = {
    "": t("authz.policy.unset"),
    true: t("authz.policy.on"),
    false: t("authz.policy.off"),
  };
  return (
    <LabeledSelect
      label={label}
      value={value}
      options={TRISTATE.map((v) => ({ value: v, label: labels[v] }))}
      onChange={onChange}
    />
  );
}

function LabeledInput({
  label,
  value,
  onChange,
  placeholder,
  mono = false,
  invalid = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  mono?: boolean;
  invalid?: boolean;
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
        {label}
      </span>
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={invalid || undefined}
        className={cn(
          "h-8 rounded-lg border bg-sg-inset px-2 text-[12px] text-sg-ink-2",
          mono && "font-mono",
          invalid ? "border-sg-err/50" : "border-sg-border",
          "placeholder:text-sg-ink-4",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        )}
      />
    </label>
  );
}
