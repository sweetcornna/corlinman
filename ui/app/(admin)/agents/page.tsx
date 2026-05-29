"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { FileText, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { CreateAgentModal } from "@/components/agents/create-agent-modal";
import {
  apiFetch,
  deleteAgent,
  fetchModelsV2,
  listAgentBindings,
  setAgentModelBinding,
  type AgentBinding,
  type AgentBindingsResponse,
  type AgentSummary,
  type ModelsResponseV2,
} from "@/lib/api";

/** Lists `Agent/*.md` files. Click a row → Monaco editor at `/agents/detail?name=`.
 *
 * W-D2 extension: a "Model" column with an inline `<select>` whose options
 * come from `/admin/models` aliases (the same list the chat surface offers).
 * The provider column is read-only, and the action-trace column controls
 * whether chat exposes reasoning/tool/subagent trajectory for that agent. */

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

/** Inline select wired to PATCH `/admin/agents/{name}/binding`.
 *
 * Uses an uncontrolled local draft so the operator can pick a value
 * without immediately blocking on the network round-trip; we commit on
 * `onBlur` (and on `change` when the new value differs from the current
 * binding) so a quick keyboard-only adjustment still fires the write
 * without requiring an explicit save button. */
function ModelSelect({
  agentName,
  current,
  options,
  provider,
  showActionTrace,
}: {
  agentName: string;
  current: string | null;
  options: string[];
  provider: string | null;
  showActionTrace: boolean;
}) {
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  const [draft, setDraft] = useState<string>(current ?? "");
  const [saving, setSaving] = useState(false);

  // Re-sync when the underlying data refreshes (e.g. after a manual
  // refetch) so the dropdown reflects the on-disk truth.
  useEffect(() => {
    setDraft(current ?? "");
  }, [current]);

  const commit = async (next: string) => {
    const normalised = next.trim();
    const incoming: string | null = normalised === "" ? null : normalised;
    if (incoming === current) return;
    setSaving(true);
    try {
      await setAgentModelBinding(agentName, {
        model: incoming,
        provider, // preserve the existing provider pin
        show_action_trace: showActionTrace,
      });
      toast.success(t("agents.modelSaved", { defaultValue: "Model updated" }));
      await queryClient.invalidateQueries({
        queryKey: ["admin", "agent-bindings"],
      });
    } catch (err) {
      toast.error(
        `${t("agents.modelSaveFailed", { defaultValue: "Failed to update model" })}: ${(err as Error).message}`,
      );
      // Roll the draft back so the UI doesn't lie about persisted state.
      setDraft(current ?? "");
    } finally {
      setSaving(false);
    }
  };

  return (
    <select
      className="rounded-md border border-tp-glass-edge bg-tp-glass px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-tp-amber disabled:opacity-50"
      value={draft}
      disabled={saving}
      data-testid={`agent-model-select-${agentName}`}
      aria-label={t("agents.modelAriaLabel", {
        defaultValue: "Model for agent",
        agent: agentName,
      })}
      onChange={(ev) => {
        const next = ev.target.value;
        setDraft(next);
        // Native <select> already commits via the change event itself,
        // so we fire the write immediately rather than waiting for blur.
        void commit(next);
      }}
      onBlur={(ev) => {
        void commit(ev.target.value);
      }}
    >
      <option value="">
        {t("agents.modelInherit", { defaultValue: "(inherit default)" })}
      </option>
      {/* If the current binding isn't in the options list (e.g. a model
          that was removed from aliases), keep it as a separate sticky
          option so the operator doesn't get a silent drop-down reset. */}
      {current && !options.includes(current) ? (
        <option value={current}>{current} (unlisted)</option>
      ) : null}
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  );
}

function ActionTraceSwitch({
  agentName,
  model,
  provider,
  checked,
}: {
  agentName: string;
  model: string | null;
  provider: string | null;
  checked: boolean;
}) {
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  const [saving, setSaving] = useState(false);

  const commit = async (next: boolean) => {
    if (next === checked) return;
    setSaving(true);
    try {
      await setAgentModelBinding(agentName, {
        model,
        provider,
        show_action_trace: next,
      });
      toast.success(
        t("agents.actionTraceSaved", {
          defaultValue: "Action trace preference updated",
        }),
      );
      await queryClient.invalidateQueries({
        queryKey: ["admin", "agent-bindings"],
      });
    } catch (err) {
      toast.error(
        `${t("agents.actionTraceSaveFailed", { defaultValue: "Failed to update action trace" })}: ${(err as Error).message}`,
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Switch
      checked={checked}
      disabled={saving}
      onCheckedChange={(next) => {
        void commit(next);
      }}
      aria-label={t("agents.actionTraceAriaLabel", {
        defaultValue: "Show action trace for agent",
        agent: agentName,
      })}
      data-testid={`agent-action-trace-${agentName}`}
      className="h-6 w-11"
    />
  );
}

/**
 * Per-row source badge. Three tiers — `built-in` (gray, immutable),
 * `user` (amber, the operator-owned overlay), `project`
 * (blue, workspace-scoped). The colour choice keeps built-in cards
 * visually subordinate so the operator's overlays pop in a long list,
 * while project rows read as "shared, not local" against the amber
 * user tier.
 */
function SourceBadge({
  source,
}: {
  source: AgentSummary["source"];
}) {
  const { t } = useTranslation();
  const value: AgentSummary["source"] = source ?? "user";
  if (value === "built-in") {
    return (
      <Badge
        variant="outline"
        className="border-tp-glass-edge bg-tp-glass text-tp-ink-3"
      >
        {t("agents.source.builtIn")}
      </Badge>
    );
  }
  if (value === "project") {
    return (
      <Badge className="border-transparent bg-tp-ok/20 text-tp-ok hover:bg-tp-ok/25">
        {t("agents.source.project")}
      </Badge>
    );
  }
  return (
    <Badge className="border-transparent bg-tp-amber/20 text-tp-amber hover:bg-tp-amber/25">
      {t("agents.source.user")}
    </Badge>
  );
}

export default function AgentsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  // Two-state delete dialog: name + busy flag. `null` means closed.
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const query = useQuery<AgentSummary[]>({
    queryKey: ["admin", "agents"],
    queryFn: () => apiFetch<AgentSummary[]>("/admin/agents"),
  });

  async function handleDelete(name: string) {
    setDeleteBusy(true);
    try {
      await deleteAgent(name);
      toast.success(t("agents.deleteSuccess", { name }));
      await qc.invalidateQueries({ queryKey: ["admin", "agents"] });
      setPendingDelete(null);
    } catch (err) {
      toast.error(
        t("agents.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    } finally {
      setDeleteBusy(false);
    }
  }

  // Bindings + model list both feed the new column. Failures fall
  // through silently — the table still renders with "no binding" rows
  // so the operator can at least see the agent list when one of these
  // surfaces is misconfigured.
  const bindingsQuery = useQuery<AgentBindingsResponse>({
    queryKey: ["admin", "agent-bindings"],
    queryFn: () => listAgentBindings(),
  });

  const modelsQuery = useQuery<ModelsResponseV2>({
    queryKey: ["admin", "models"],
    queryFn: () => fetchModelsV2(),
  });

  const bindingByName = useMemo(() => {
    const m = new Map<string, AgentBinding>();
    for (const b of bindingsQuery.data?.agents ?? []) {
      m.set(b.name, b);
    }
    return m;
  }, [bindingsQuery.data]);

  // The model dropdown options come from the alias list (each alias
  // carries a `model` field that maps to the upstream id). De-dupe and
  // sort so adjacent rows show the same options in the same order.
  const modelOptions = useMemo(() => {
    const set = new Set<string>();
    for (const alias of modelsQuery.data?.aliases ?? []) {
      if (alias.name) set.add(alias.name);
      if (alias.model) set.add(alias.model);
    }
    return Array.from(set).sort();
  }, [modelsQuery.data]);

  return (
    <>
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("agents.title")}
          </h1>
          <p className="text-sm text-tp-ink-3">{t("agents.subtitle")}</p>
        </div>
        <Button
          type="button"
          size="sm"
          onClick={() => setCreateOpen(true)}
          data-testid="create-agent-open"
        >
          <Plus className="h-3.5 w-3.5" />
          {t("agents.create.button")}
        </Button>
      </header>

      <section className="overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
              <TableHead className="pl-4">{t("agents.colName")}</TableHead>
              <TableHead className="w-28">{t("agents.colSource")}</TableHead>
              <TableHead>{t("agents.colPath")}</TableHead>
              <TableHead className="w-48">
                {t("agents.colModel", { defaultValue: "Model" })}
              </TableHead>
              <TableHead className="w-32">
                {t("agents.colProvider", { defaultValue: "Provider" })}
              </TableHead>
              <TableHead className="w-28">
                {t("agents.colActionTrace", { defaultValue: "Trace" })}
              </TableHead>
              <TableHead className="w-32">{t("agents.colBytes")}</TableHead>
              <TableHead className="w-56">
                {t("agents.colLastModified")}
              </TableHead>
              <TableHead className="w-24">{t("agents.colActions")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isPending ? (
              Array.from({ length: 3 }).map((_, i) => (
                <TableRow key={`sk-${i}`} className="border-b border-tp-glass-edge">
                  {Array.from({ length: 9 }).map((_, j) => (
                    <TableCell key={j} className={j === 0 ? "pl-4" : undefined}>
                      <Skeleton className="h-4 w-24" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : query.isError ? (
              <TableRow>
                <TableCell
                  colSpan={9}
                  className="py-10 text-center text-sm text-destructive"
                >
                  {t("agents.loadFailed")}: {(query.error as Error).message}
                </TableCell>
              </TableRow>
            ) : !query.data || query.data.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={9}
                  className="py-10 text-center text-sm text-tp-ink-3"
                >
                  {t("agents.empty")}
                </TableCell>
              </TableRow>
            ) : (
              query.data.map((a) => {
                const binding = bindingByName.get(a.name) ?? null;
                const provider = binding?.provider ?? null;
                const showActionTrace = binding?.show_action_trace ?? true;
                const source: AgentSummary["source"] = a.source ?? "user";
                const isBuiltIn = source === "built-in";
                // Trim long descriptions to keep the row a single line —
                // operators get the full text via tooltip on hover.
                const desc = a.description ?? "";
                const descShort =
                  desc.length > 80 ? `${desc.slice(0, 80)}…` : desc;
                return (
                  <TableRow
                    key={a.name}
                    className="border-b border-tp-glass-edge transition-colors hover:bg-tp-glass-inner-hover"
                  >
                    <TableCell className="pl-4 font-medium">
                      <Link
                        href={{
                          pathname: "/agents/detail",
                          query: { name: a.name },
                        }}
                        className="inline-flex items-center gap-2 hover:text-tp-amber"
                        data-testid={`agent-link-${a.name}`}
                      >
                        <FileText className="h-3.5 w-3.5 text-tp-ink-3" />
                        {a.name}
                      </Link>
                      {descShort ? (
                        <div
                          className="mt-0.5 text-[11px] text-tp-ink-3"
                          title={desc}
                          data-testid={`agent-desc-${a.name}`}
                        >
                          {descShort}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell data-testid={`agent-source-${a.name}`}>
                      <SourceBadge source={source} />
                    </TableCell>
                    <TableCell className="font-mono text-xs text-tp-ink-3">
                      {a.file_path}
                    </TableCell>
                    <TableCell>
                      {/* Only render the select once both queries have settled
                          so we don't whip-saw the default option. */}
                      {bindingsQuery.isPending || modelsQuery.isPending ? (
                        <Skeleton className="h-6 w-32" />
                      ) : (
                        <ModelSelect
                          agentName={a.name}
                          current={binding?.model ?? null}
                          options={modelOptions}
                          provider={provider}
                          showActionTrace={showActionTrace}
                        />
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-tp-ink-3">
                      {provider ?? (
                        <span className="italic text-tp-ink-3">
                          {t("agents.providerAuto", { defaultValue: "(auto)" })}
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      {bindingsQuery.isPending ? (
                        <Skeleton className="h-6 w-11 rounded-full" />
                      ) : (
                        <ActionTraceSwitch
                          agentName={a.name}
                          model={binding?.model ?? null}
                          provider={provider}
                          checked={showActionTrace}
                        />
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {formatBytes(a.bytes)}
                    </TableCell>
                    <TableCell className="text-xs text-tp-ink-3">
                      {formatTime(a.last_modified)}
                    </TableCell>
                    <TableCell>
                      {isBuiltIn ? (
                        <span
                          className="inline-flex items-center text-[11px] text-tp-ink-3"
                          title={t("agents.create.builtinReadonly")}
                          data-testid={`agent-delete-disabled-${a.name}`}
                        >
                          —
                        </span>
                      ) : (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-8 px-2 text-destructive hover:bg-destructive/10 hover:text-destructive"
                          onClick={() => setPendingDelete(a.name)}
                          data-testid={`agent-delete-${a.name}`}
                          aria-label={t("agents.delete")}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </section>

      <CreateAgentModal
        open={createOpen}
        onOpenChange={setCreateOpen}
        initialAgents={query.data}
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
        title={t("agents.deleteConfirmTitle", {
          name: pendingDelete ?? "",
        })}
        description={t("agents.deleteConfirmBody")}
        confirmLabel={t("agents.deleteConfirmAction")}
        cancelLabel={t("common.cancel")}
        destructive
        busy={deleteBusy}
        onConfirm={async () => {
          if (pendingDelete) await handleDelete(pendingDelete);
        }}
        testId="agent-delete-confirm"
      />
    </>
  );
}
