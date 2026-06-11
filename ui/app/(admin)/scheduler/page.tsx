"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { toast } from "sonner";
import { Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import {
  fetchSchedulerHistory,
  fetchSchedulerJobs,
  pauseSchedulerJob,
  resumeSchedulerJob,
  triggerSchedulerJob,
  type SchedulerHistory,
  type SchedulerJob,
} from "@/lib/api";
import { SchedulerHeader } from "@/components/scheduler/scheduler-header";
import { SchedulerStatsRow } from "@/components/scheduler/scheduler-stats-row";
import { SchedulerRow } from "@/components/scheduler/scheduler-row";
import { SchedulerHistoryDrawer } from "@/components/scheduler/scheduler-history-drawer";
import {
  SchedulerEmptyBlock,
  SchedulerListSkeleton,
  SchedulerOfflineBlock,
} from "@/components/scheduler/scheduler-list-states";
import {
  deriveStatus,
  formatRelative,
  pickNextUpcoming,
  type SchedulerStatus,
} from "@/components/scheduler/scheduler-util";

/**
 * Scheduler — Phase 5d Tidepool cutover.
 *
 * Layout:
 *   [ SchedulerHeader (glass strong, prose + ⌘K CTA) ]
 *   [ SchedulerStatsRow — Total · Enabled · Paused · Errored ]
 *   [ search input + FilterChipGroup ]
 *   [ job rows (stacked)         │ SchedulerHistoryDrawer (when selected) ]
 *
 * Data flow is unchanged from the pre-cutover page:
 *   - `/admin/scheduler/jobs`     (60s poll) — the cron table
 *   - `/admin/scheduler/history`  (15s poll) — the recent-attempts ring
 *   - `triggerSchedulerJob(name)` — POST trigger, toast on result
 *
 * Selection: click a row to open the history drawer; click it again to
 * close. Esc also closes. Mirrors the Approvals / Hooks pattern.
 *
 * Tidepool primitives in use: `GlassPanel`, `StatChip`, `FilterChipGroup`,
 * `CountdownRing`, `DetailDrawer` (via SchedulerHistoryDrawer).
 */

type FilterValue = "all" | "enabled" | "paused" | "errored";

// Consider a failed history entry "recent" if it's within this window.
const RECENT_ERROR_WINDOW_MS = 60 * 60 * 1000; // 1h
const EMPTY_SCHEDULER_JOBS: SchedulerJob[] = [];

export default function SchedulerPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const qc = useQueryClient();

  const [search, setSearch] = React.useState("");
  const [filter, setFilter] = React.useState<FilterValue>("all");
  const [selectedName, setSelectedName] = React.useState<string | null>(null);

  // 1-Hz tick — the row countdowns and the hero "next run in X" prose
  // both read from this.
  const [now, setNow] = React.useState<number>(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  const jobsQuery = useQuery<SchedulerJob[]>({
    queryKey: ["admin", "scheduler", "jobs"],
    queryFn: fetchSchedulerJobs,
    refetchInterval: 60_000,
    retry: false,
  });

  const historyQuery = useQuery<SchedulerHistory[]>({
    queryKey: ["admin", "scheduler", "history"],
    queryFn: fetchSchedulerHistory,
    refetchInterval: 15_000,
    retry: false,
  });

  const triggerMutation = useMutation({
    mutationFn: (name: string) => triggerSchedulerJob(name),
    onSuccess: (_, name) => {
      toast.success(t("scheduler.triggered", { name }));
      qc.invalidateQueries({ queryKey: ["admin", "scheduler", "history"] });
    },
    onError: (err, name) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(t("scheduler.triggerFail", { name, msg }));
      qc.invalidateQueries({ queryKey: ["admin", "scheduler", "history"] });
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ name, paused }: { name: string; paused: boolean }) =>
      paused ? resumeSchedulerJob(name) : pauseSchedulerJob(name),
    onSuccess: (_, { name, paused }) => {
      toast.success(
        paused
          ? t("scheduler.resumed", { name })
          : t("scheduler.paused", { name }),
      );
      qc.invalidateQueries({ queryKey: ["admin", "scheduler", "jobs"] });
    },
    onError: (err, { name }) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(t("scheduler.toggleFail", { name, msg }));
      qc.invalidateQueries({ queryKey: ["admin", "scheduler", "jobs"] });
    },
  });

  const jobs = jobsQuery.data ?? EMPTY_SCHEDULER_JOBS;
  const offline = jobsQuery.isError;

  // ─── derived ─────────────────────────────────────────────────────────
  const statusByName = React.useMemo(() => {
    const m = new Map<string, SchedulerStatus>();
    for (const j of jobs) m.set(j.name, deriveStatus(j));
    return m;
  }, [jobs]);

  const counts = React.useMemo(() => {
    let enabled = 0;
    let paused = 0;
    let errored = 0;
    for (const j of jobs) {
      switch (statusByName.get(j.name)) {
        case "enabled":
          enabled += 1;
          break;
        case "paused":
          paused += 1;
          break;
        case "errored":
          errored += 1;
          break;
      }
    }
    return { total: jobs.length, enabled, paused, errored };
  }, [jobs, statusByName]);

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return jobs.filter((j) => {
      const status = statusByName.get(j.name);
      if (filter !== "all" && status !== filter) return false;
      if (!q) return true;
      return (
        j.name.toLowerCase().includes(q) ||
        j.cron.toLowerCase().includes(q) ||
        (j.timezone ?? "").toLowerCase().includes(q)
      );
    });
  }, [jobs, search, filter, statusByName]);

  const nextUpcoming = React.useMemo(
    () => pickNextUpcoming(jobs, now),
    [jobs, now],
  );

  const recentlyErrored = React.useMemo(() => {
    const history = historyQuery.data;
    if (!history) return 0;
    let n = 0;
    for (const h of history) {
      const s = h.status?.toLowerCase() ?? "";
      if (!(s.includes("err") || s.includes("fail"))) continue;
      const then = new Date(h.at).getTime();
      if (!Number.isFinite(then)) continue;
      if (now - then <= RECENT_ERROR_WINDOW_MS) n += 1;
    }
    return n;
  }, [historyQuery.data, now]);

  const updatedLabel = React.useMemo(() => {
    const ts = jobsQuery.dataUpdatedAt;
    if (!ts) return undefined;
    return formatRelative(new Date(ts).toISOString(), t);
  }, [jobsQuery.dataUpdatedAt, t]);

  const selectedJob = React.useMemo(
    () => (selectedName ? jobs.find((j) => j.name === selectedName) ?? null : null),
    [jobs, selectedName],
  );

  // Drop the selection if the job disappears from the current filter.
  React.useEffect(() => {
    if (!selectedName) return;
    if (!filtered.some((j) => j.name === selectedName)) {
      setSelectedName(null);
    }
  }, [filtered, selectedName]);

  const scopedHistory = React.useMemo(() => {
    if (!selectedName || !historyQuery.data) return [];
    return historyQuery.data.filter((h) => h.job === selectedName);
  }, [historyQuery.data, selectedName]);

  // ─── keyboard: Esc closes the drawer ─────────────────────────────────
  React.useEffect(() => {
    if (!selectedName) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() ?? "";
      if (tag === "input" || tag === "textarea" || target?.isContentEditable) {
        return;
      }
      e.preventDefault();
      setSelectedName(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedName]);

  const filterOptions: FilterChipOption[] = [
    {
      value: "all",
      label: t("scheduler.tp.filterAll"),
      count: counts.total,
    },
    {
      value: "enabled",
      label: t("scheduler.tp.filterEnabled"),
      count: counts.enabled,
      tone: "ok",
    },
    {
      value: "paused",
      label: t("scheduler.tp.filterPaused"),
      count: counts.paused,
      tone: "info",
    },
    {
      value: "errored",
      label: t("scheduler.tp.filterErrored"),
      count: counts.errored,
      tone: "err",
    },
  ];

  const selectedStatus = selectedJob
    ? statusByName.get(selectedJob.name) ?? deriveStatus(selectedJob)
    : null;

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <SchedulerHeader
        counts={offline ? undefined : counts}
        updatedLabel={updatedLabel}
        nextUp={
          offline || !nextUpcoming
            ? null
            : { name: nextUpcoming.job.name, deltaMs: nextUpcoming.deltaMs }
        }
        recentlyErrored={offline ? 0 : recentlyErrored}
        offline={offline}
        fetching={jobsQuery.isFetching}
        onRefresh={() => {
          jobsQuery.refetch();
          historyQuery.refetch();
        }}
      />

      <SchedulerStatsRow
        total={counts.total}
        enabled={counts.enabled}
        paused={counts.paused}
        errored={counts.errored}
        live={!offline}
      />

      {/* Offline info banner — parallel to Approvals. */}
      {offline ? (
        <div
          role="alert"
          className={cn(
            "flex items-center gap-3 rounded-xl border px-3 py-2 text-[12.5px]",
            "border-sg-border bg-sg-inset text-sg-ink-3",
          )}
        >
          <span>{t("scheduler.tp.endpointOfflineBanner")}</span>
        </div>
      ) : null}

      {/* Search + filter chips */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <label className="relative flex min-w-[220px] flex-1 items-center sm:max-w-[360px]">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-sg-ink-4"
            aria-hidden
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("scheduler.tp.searchPlaceholder")}
            aria-label={t("scheduler.tp.searchPlaceholder")}
            className="h-9 w-full rounded-lg border border-sg-border bg-sg-inset pl-8 pr-3 text-[13px] text-sg-ink placeholder:text-sg-ink-4 transition-colors hover:bg-sg-inset-hover focus:outline-none focus:ring-2 focus:ring-sg-accent/40"
          />
        </label>
        <FilterChipGroup
          options={filterOptions}
          value={filter}
          onChange={(next) => setFilter(next as FilterValue)}
          label={t("scheduler.tp.filterLabel")}
        />
      </section>

      {/* List + drawer */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-2">
          {jobsQuery.isPending ? (
            <SchedulerListSkeleton />
          ) : offline ? (
            <SchedulerOfflineBlock
              message={(jobsQuery.error as Error | undefined)?.message}
            />
          ) : filtered.length === 0 ? (
            <SchedulerEmptyBlock hasAnyJobs={jobs.length > 0} />
          ) : (
            filtered.map((job) => {
              const status = statusByName.get(job.name) ?? deriveStatus(job);
              return (
                <SchedulerRow
                  key={job.name}
                  job={job}
                  status={status}
                  now={now}
                  selected={selectedName === job.name}
                  triggering={
                    triggerMutation.isPending &&
                    triggerMutation.variables === job.name
                  }
                  pausing={
                    toggleMutation.isPending &&
                    toggleMutation.variables?.name === job.name
                  }
                  onSelect={(name) =>
                    setSelectedName((prev) => (prev === name ? null : name))
                  }
                  onTrigger={(name) => triggerMutation.mutate(name)}
                  onPause={(name) =>
                    toggleMutation.mutate({ name, paused: false })
                  }
                  onResume={(name) =>
                    toggleMutation.mutate({ name, paused: true })
                  }
                />
              );
            })
          )}
        </div>
        <aside className="lg:sticky lg:top-4 lg:self-start">
          {selectedJob && selectedStatus ? (
            <SchedulerHistoryDrawer
              job={selectedJob}
              status={selectedStatus}
              history={scopedHistory}
            />
          ) : null}
        </aside>
      </section>
    </motion.div>
  );
}
