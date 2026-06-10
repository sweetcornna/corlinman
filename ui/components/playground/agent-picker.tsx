"use client";

/**
 * <AgentPicker> — W2.3 Multi-agent.
 *
 * Compact dropdown that lets the playground operator override the
 * implicit "auto-route" agent selection (which uses the existing
 * message-peek heuristic in `_peek_agent_binding`). When the operator
 * picks an explicit agent the playground threads `agent_id` into the
 * chat request payload; the backend prefers that hint over the
 * heuristic when present.
 *
 * Default value is ``null`` → auto-route (no change in behavior from
 * before W2.3). Picking an item calls ``onChange(name)``.
 *
 * Source badges (`built-in` / `user` / `project`) are surfaced from the
 * W1.2-extended ``GET /admin/agents`` shape via the existing
 * ``listAgents`` API helper. Description is truncated to ~60 chars
 * inline.
 *
 * i18n: Strings live under ``playground.agentPicker.*`` and are owned
 * by W2.1 — the keys are referenced here; when missing react-i18next
 * returns the key string verbatim which is the documented W2.1
 * fallback behavior.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";

import { listAgents, type AgentSummary } from "@/lib/api";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export interface AgentPickerProps {
  /** Selected agent name; ``null`` = auto-route (default). */
  value: string | null;
  onChange: (agent_id: string | null) => void;
  className?: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DESCRIPTION_MAX_CHARS = 60;

// Stable ordering for the source-group sections. Built-in agents
// surface first because they're the registry-resolved defaults; user
// agents follow; project-scoped overrides anchor the tail.
const SOURCE_ORDER: ReadonlyArray<NonNullable<AgentSummary["source"]>> = [
  "built-in",
  "user",
  "project",
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AgentPicker({
  value,
  onChange,
  className,
}: AgentPickerProps): React.JSX.Element {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const rootRef = React.useRef<HTMLDivElement | null>(null);
  const searchRef = React.useRef<HTMLInputElement | null>(null);

  const agentsQuery = useQuery<AgentSummary[]>({
    queryKey: ["admin", "agents", "picker"],
    queryFn: () => listAgents(),
    staleTime: 30_000,
  });

  // Close on outside-click + Esc.
  React.useEffect(() => {
    if (!open) return;
    function onDocClick(ev: MouseEvent) {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(ev.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Focus the search input on open.
  React.useEffect(() => {
    if (open) {
      // Wait a frame so the popover is mounted.
      const id = requestAnimationFrame(() => searchRef.current?.focus());
      return () => cancelAnimationFrame(id);
    }
    return;
  }, [open]);

  const agents = agentsQuery.data ?? [];

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q.length === 0) return agents;
    return agents.filter((a) => {
      const name = a.name.toLowerCase();
      const desc = (a.description ?? "").toLowerCase();
      return name.includes(q) || desc.includes(q);
    });
  }, [agents, query]);

  // Group filtered list by source ("built-in" | "user" | "project"),
  // falling back to "user" when the source field is missing (older
  // gateways predate W1.2).
  const grouped = React.useMemo(() => {
    const buckets: Record<string, AgentSummary[]> = {
      "built-in": [],
      user: [],
      project: [],
    };
    for (const a of filtered) {
      const src = a.source ?? "user";
      const bucket = buckets[src] ?? buckets.user!;
      bucket.push(a);
    }
    return buckets;
  }, [filtered]);

  const triggerLabel =
    value === null
      ? t("playground.agentPicker.triggerAuto")
      : t("playground.agentPicker.triggerPicked", { name: value });

  return (
    <div
      ref={rootRef}
      className={cn("relative flex flex-col gap-1.5", className)}
    >
      <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
        {t("playground.agentPicker.label")}
      </span>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="agent-picker-trigger"
        className={cn(
          "inline-flex min-w-[180px] items-center justify-between gap-2 rounded-lg px-3 py-1.5",
          "border border-sg-border bg-sg-inset",
          "font-mono text-[12.5px] text-sg-ink",
          "outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40 focus-visible:border-sg-accent/40",
          "hover:bg-sg-inset-hover",
        )}
      >
        <span className="truncate">{triggerLabel}</span>
        <span
          aria-hidden
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            value === null ? "bg-sg-ink-4" : "bg-sg-accent",
          )}
        />
      </button>

      {open ? (
        <div
          role="listbox"
          aria-label={t("playground.agentPicker.label")}
          data-testid="agent-picker-popover"
          className={cn(
            "absolute left-0 top-full z-30 mt-1 flex w-[320px] flex-col gap-1.5 rounded-lg",
            "border border-sg-border bg-sg-overlay p-2 shadow-lg",
          )}
        >
          <input
            ref={searchRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("playground.agentPicker.searchPlaceholder")}
            aria-label={t("playground.agentPicker.searchPlaceholder")}
            data-testid="agent-picker-search"
            className={cn(
              "w-full rounded-md border px-2.5 py-1.5 font-mono text-[12px] text-sg-ink",
              "bg-sg-inset border-sg-border placeholder:text-sg-ink-4",
              "outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40 focus-visible:border-sg-accent/40",
            )}
          />

          {/* "Auto-route" — always visible and pinned to the top. */}
          <button
            type="button"
            role="option"
            aria-selected={value === null}
            data-testid="agent-picker-auto"
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            className={cn(
              "flex flex-col items-start gap-0.5 rounded-md px-2.5 py-1.5 text-left",
              "hover:bg-sg-inset-hover focus-visible:bg-sg-inset-hover",
              "outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
              value === null && "bg-sg-accent-soft",
            )}
          >
            <span className="font-mono text-[12.5px] text-sg-ink">
              {t("playground.agentPicker.autoLabel")}
            </span>
            <span className="text-[11px] text-sg-ink-4">
              {t("playground.agentPicker.autoHint")}
            </span>
          </button>

          {agentsQuery.isLoading ? (
            <div
              className="px-2.5 py-3 text-center text-[12px] text-sg-ink-4"
              data-testid="agent-picker-loading"
            >
              {t("playground.agentPicker.loading")}
            </div>
          ) : agents.length === 0 ? (
            <div
              className="px-2.5 py-3 text-center text-[12px] text-sg-ink-4"
              data-testid="agent-picker-empty"
            >
              {t("playground.agentPicker.empty")}
            </div>
          ) : filtered.length === 0 ? (
            <div
              className="px-2.5 py-3 text-center text-[12px] text-sg-ink-4"
              data-testid="agent-picker-no-match"
            >
              {t("playground.agentPicker.noMatch")}
            </div>
          ) : (
            <div
              className="flex max-h-[260px] flex-col gap-2 overflow-y-auto"
              data-testid="agent-picker-list"
            >
              {SOURCE_ORDER.map((src) => {
                const items = grouped[src] ?? [];
                if (items.length === 0) return null;
                return (
                  <section key={src} className="flex flex-col gap-0.5">
                    <header className="px-2.5 pt-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-sg-ink-4">
                      {t(`playground.agentPicker.source.${src}`)}
                    </header>
                    {items.map((agent) => (
                      <AgentRow
                        key={`${src}:${agent.name}`}
                        agent={agent}
                        selected={agent.name === value}
                        onPick={(name) => {
                          onChange(name);
                          setOpen(false);
                        }}
                      />
                    ))}
                  </section>
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row sub-component
// ---------------------------------------------------------------------------

interface AgentRowProps {
  agent: AgentSummary;
  selected: boolean;
  onPick: (name: string) => void;
}

function AgentRow({ agent, selected, onPick }: AgentRowProps): React.JSX.Element {
  const src = agent.source ?? "user";
  const description = truncate(agent.description ?? "", DESCRIPTION_MAX_CHARS);

  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      data-testid={`agent-picker-item-${agent.name}`}
      onClick={() => onPick(agent.name)}
      className={cn(
        "flex flex-col items-start gap-0.5 rounded-md px-2.5 py-1.5 text-left",
        "hover:bg-sg-inset-hover focus-visible:bg-sg-inset-hover",
        "outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        selected && "bg-sg-accent-soft",
      )}
    >
      <div className="flex w-full items-center justify-between gap-2">
        <span className="truncate font-mono text-[12.5px] text-sg-ink">
          {agent.name}
        </span>
        <SourceBadge source={src} />
      </div>
      {description.length > 0 ? (
        <span className="line-clamp-1 text-[11px] text-sg-ink-3">
          {description}
        </span>
      ) : null}
    </button>
  );
}

interface SourceBadgeProps {
  source: NonNullable<AgentSummary["source"]>;
}

function SourceBadge({ source }: SourceBadgeProps): React.JSX.Element {
  const { t } = useTranslation();
  return (
    <span
      className={cn(
        "shrink-0 rounded border px-1 py-px font-mono text-[9.5px] uppercase tracking-[0.05em]",
        source === "built-in"
          ? "border-sg-accent/35 text-sg-accent"
          : source === "project"
            ? "border-sg-accent-2/35 text-sg-accent-2"
            : "border-sg-border text-sg-ink-3",
      )}
    >
      {t(`playground.agentPicker.source.${source}`)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function truncate(text: string, max: number): string {
  const trimmed = text.trim().split("\n")[0] ?? "";
  if (trimmed.length <= max) return trimmed;
  return `${trimmed.slice(0, max - 1).trimEnd()}…`;
}
