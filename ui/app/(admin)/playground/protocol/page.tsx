"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { useMotion } from "@/components/ui/motion-safe";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StreamPill, type StreamState } from "@/components/ui/stream-pill";
import { StatChip } from "@/components/ui/stat-chip";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { JsonView } from "@/components/ui/json-view";
import { AgentPicker } from "@/components/playground/agent-picker";
import { SplitPane } from "@/components/playground/split-pane";
import { TokenStream } from "@/components/playground/token-stream";
import { diffLineIndexes } from "@/components/playground/diff-highlight";
import {
  streamBlockProtocol,
  streamFunctionCall,
  type ProtocolVariant,
} from "@/lib/mocks/protocol-streams";
import { listInstalledSkills } from "@/lib/api";
import { ActiveProfileContext } from "@/lib/context/active-profile";

/**
 * W2.3 — when the active profile has fewer than this many skills mounted,
 * the playground renders a glass-card hint pointing the operator at
 * ``/admin/skills`` so they can pick up procedural knowledge bundles from
 * the openclaw skill hub. Threshold is intentionally low — once an
 * operator has a handful of skills the hint stops being useful signal.
 */
const SKILLS_HINT_THRESHOLD = 5;

const MODELS = ["gpt-4o", "claude-3.5-sonnet", "qwen2.5-72b"] as const;
type Model = (typeof MODELS)[number];

interface PaneState {
  tokens: string[];
  /** Per-token arrival timestamp (ms). Used for p50 tok/s readout. */
  arrivals: number[];
  done: boolean;
  running: boolean;
}

const EMPTY_PANE: PaneState = {
  tokens: [],
  arrivals: [],
  done: false,
  running: false,
};

type StreamFn = (
  prompt: string,
  opts?: { tokenDelayMs?: number; signal?: AbortSignal },
) => AsyncGenerator<string, void, unknown>;

const STREAM_FNS: Record<ProtocolVariant, StreamFn> = {
  block: streamBlockProtocol,
  "function-call": streamFunctionCall,
};

/** Keep the last N "frames" per side for the raw-frames JSON toggle. */
const RAW_FRAMES_MAX = 5;

/**
 * Baked sparkline paths (viewBox 0 0 300 36) — deterministic so SSR+CSR match.
 * Same geometry language as the Dashboard/Approvals stat chips.
 */
const SPARK_BLOCK =
  "M0 26 L30 22 L60 24 L90 18 L120 20 L150 14 L180 16 L210 10 L240 14 L270 8 L300 10 L300 36 L0 36 Z";
const SPARK_FN =
  "M0 24 L30 22 L60 18 L90 20 L120 16 L150 18 L180 12 L210 14 L240 10 L270 12 L300 8 L300 36 L0 36 Z";
const SPARK_TOTAL =
  "M0 30 L30 26 L60 24 L90 20 L120 22 L150 16 L180 18 L210 12 L240 14 L270 10 L300 8 L300 36 L0 36 Z";
const SPARK_DIV =
  "M0 10 L30 12 L60 16 L90 18 L120 20 L150 22 L180 24 L210 26 L240 28 L270 28 L300 30 L300 36 L0 36 Z";

/**
 * Protocol Playground · 协议对比 — Tidepool (Phase 5d).
 *
 * Warm-orange glass. Split-pane comparison of the block-style
 * `<<<[TOOL_REQUEST]>>>` + 「始」「末」 protocol (left) vs. OpenAI
 * function-call JSON (right). Shared prompt drives both panes; they stream
 * token-by-token in parallel and pulse-highlight divergent lines once both
 * finish.
 *
 * Mock streams live in `lib/mocks/protocol-streams.ts`. TODO(B3-BE1/BE2):
 * swap for the real gateway SSE endpoint.
 */
export default function ProtocolPlaygroundPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  // ─── state ────────────────────────────────────────────────────
  const [prompt, setPrompt] = React.useState(
    "Search the docs for the block protocol spec and summarise it.",
  );
  const [model, setModel] = React.useState<Model>(MODELS[0]);
  const [maxTokens, setMaxTokens] = React.useState(512);
  const [temperature, setTemperature] = React.useState(0.7);
  const [block, setBlock] = React.useState<PaneState>(EMPTY_PANE);
  const [fn, setFn] = React.useState<PaneState>(EMPTY_PANE);
  const [rawFramesOpen, setRawFramesOpen] = React.useState(false);
  // W2.3: explicit agent override for the chat request payload. ``null`` =
  // auto-route (existing message-peek heuristic in
  // ``_peek_agent_binding``). When set, the backend prefers this hint over
  // the heuristic.
  const [explicitAgent, setExplicitAgent] = React.useState<string | null>(null);
  const abortRef = React.useRef<AbortController | null>(null);
  const promptRef = React.useRef<HTMLTextAreaElement | null>(null);

  // Gateway connectivity — we treat the stream mock as always available
  // locally; when we move to real SSE, plumb the health query in here.
  // For now, the page cannot detect "offline" authoritatively, so we only
  // disable `Run` while a stream is in-flight. The copy + tooltip strings
  // for the offline case are prepared in locales for the real endpoint.
  const offline = false;

  const running = block.running || fn.running;
  const bothDone = block.done && fn.done;
  const hasRun = block.tokens.length > 0 || fn.tokens.length > 0;

  // ─── run both panes ───────────────────────────────────────────
  const runPane = React.useCallback(
    async (
      variant: ProtocolVariant,
      signal: AbortSignal,
      setter: React.Dispatch<React.SetStateAction<PaneState>>,
    ) => {
      setter({ tokens: [], arrivals: [], done: false, running: true });
      try {
        for await (const tok of STREAM_FNS[variant](prompt, { signal })) {
          if (signal.aborted) return;
          const now = Date.now();
          setter((prev) => ({
            ...prev,
            tokens: [...prev.tokens, tok],
            arrivals: [...prev.arrivals, now],
          }));
        }
        if (!signal.aborted) {
          setter((prev) => ({ ...prev, done: true, running: false }));
        }
      } catch {
        if (!signal.aborted) {
          setter((prev) => ({ ...prev, done: true, running: false }));
        }
      }
    },
    [prompt],
  );

  const runBoth = React.useCallback(() => {
    if (offline) return;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    const seed: PaneState = {
      tokens: [],
      arrivals: [],
      done: false,
      running: true,
    };
    setBlock(seed);
    setFn(seed);
    // W2.3: compose the chat request payload shape that the gateway
    // will receive when the mock streams are swapped for the real SSE
    // endpoint. ``agent_id`` is only present when the operator picked
    // an explicit agent — otherwise the backend's heuristic
    // (``_peek_agent_binding``) routes via message-peek as before.
    const _body = buildChatRequestBody({
      prompt,
      model,
      maxTokens,
      temperature,
      agentId: explicitAgent,
    });
    void _body; // referenced for typecheck; consumed by real SSE path
    void Promise.all([
      runPane("block", ac.signal, setBlock),
      runPane("function-call", ac.signal, setFn),
    ]);
  }, [runPane, offline, prompt, model, maxTokens, temperature, explicitAgent]);

  React.useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // ─── keyboard: ⌘↵ runs ────────────────────────────────────────
  React.useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const meta = ev.metaKey || ev.ctrlKey;
      if (meta && ev.key === "Enter") {
        ev.preventDefault();
        if (!running && !offline) runBoth();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [runBoth, running, offline]);

  // ─── derived ──────────────────────────────────────────────────
  const blockText = React.useMemo(() => block.tokens.join(""), [block.tokens]);
  const fnText = React.useMemo(() => fn.tokens.join(""), [fn.tokens]);

  const blockRate = React.useMemo(
    () => computeRate(block.arrivals),
    [block.arrivals],
  );
  const fnRate = React.useMemo(
    () => computeRate(fn.arrivals),
    [fn.arrivals],
  );

  const totalTokens = block.tokens.length + fn.tokens.length;

  // Divergent lines are calculated live (cheap enough for < 2KB buffers);
  // the diff *pulse* overlay only fires once both streams are done so the
  // user isn't chasing a moving target.
  const divergentLines = React.useMemo(() => {
    if (!hasRun) return new Set<number>();
    return diffLineIndexes(blockText, fnText);
  }, [blockText, fnText, hasRun]);

  const totalLines = React.useMemo(() => {
    const a = blockText.split("\n").length;
    const b = fnText.split("\n").length;
    return Math.max(a, b);
  }, [blockText, fnText]);

  const divergencePct = React.useMemo(() => {
    if (!hasRun || totalLines === 0) return 0;
    return (divergentLines.size / totalLines) * 100;
  }, [divergentLines, totalLines, hasRun]);

  // Raw frames — a windowed tail of "frames" per side. For the block pane
  // we chunk the output text between blank lines; for the function-call
  // pane we parse accumulated JSON and fall back to raw chunks.
  const blockFrames = React.useMemo(
    () => buildBlockFrames(blockText),
    [blockText],
  );
  const fnFrames = React.useMemo(
    () => buildFnFrames(fnText),
    [fnText],
  );

  // Pulse the divergence bar whenever the two streams' line-diff set grows
  // mid-run. Respected by reduced-motion.
  const [pulseKey, setPulseKey] = React.useState(0);
  const lastDivergedRef = React.useRef(0);
  React.useEffect(() => {
    if (reduced) return;
    if (!running) return;
    if (divergentLines.size > lastDivergedRef.current) {
      setPulseKey((k) => k + 1);
    }
    lastDivergedRef.current = divergentLines.size;
  }, [divergentLines, running, reduced]);
  // Reset the divergence high-water mark when a new run starts.
  React.useEffect(() => {
    if (running) lastDivergedRef.current = 0;
  }, [running]);

  const blockState: StreamState = block.running
    ? "live"
    : block.done
      ? "paused"
      : "paused";
  const fnState: StreamState = fn.running
    ? "live"
    : fn.done
      ? "paused"
      : "paused";

  const modelOptions: FilterChipOption[] = React.useMemo(
    () =>
      MODELS.map((m) => ({
        value: m,
        label: m,
      })),
    [],
  );

  // ─── render ───────────────────────────────────────────────────
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {/* ─── HERO ─────────────────────────────────────────── */}
      <GlassPanel
        as="header"
        variant="strong"
        className="relative overflow-hidden p-6 sm:p-7"
      >
        {/* aurora glow — amber bottom-right, ember top-left */}
        <div
          aria-hidden
          className="pointer-events-none absolute bottom-[-80px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-70 blur-3xl"
          style={{
            background:
              "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
          }}
        />
        <div
          aria-hidden
          className="pointer-events-none absolute top-[-60px] left-[-60px] h-[200px] w-[280px] rounded-full opacity-50 blur-[50px]"
          style={{
            background:
              "radial-gradient(closest-side, color-mix(in oklch, var(--tp-ember) 40%, transparent), transparent 70%)",
          }}
        />

        <div className="relative flex min-w-0 flex-col gap-3">
          <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-tp-glass-edge bg-tp-glass-inner-strong py-1 pl-2 pr-3 font-mono text-[11px] text-tp-ink-2">
            <span className="h-1.5 w-1.5 rounded-full bg-tp-amber tp-breathe-amber" />
            {t("playground.tp.heroLead")}
          </div>
          <h1 className="text-balance font-sans text-[30px] font-semibold leading-[1.12] tracking-[-0.025em] text-tp-ink sm:text-[34px]">
            {t("playground.tp.heroTitle")}
          </h1>
          <p className="max-w-[70ch] text-[14px] leading-[1.6] text-tp-ink-2">
            {offline
              ? t("playground.tp.heroSubOffline")
              : t("playground.tp.heroSub")}
          </p>
        </div>
      </GlassPanel>

      {/* ─── PROMPT + CONTROLS ────────────────────────────── */}
      <GlassPanel variant="soft" className="flex flex-col gap-3 p-4 sm:p-5">
        <label className="flex flex-col gap-1.5">
          <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
            {t("playground.tp.promptLabel")}
          </span>
          <textarea
            ref={promptRef}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={2}
            data-testid="prompt-input"
            className={cn(
              "w-full resize-none rounded-lg border px-3 py-2 font-mono text-[13px] text-tp-ink",
              "bg-tp-glass-inner border-tp-glass-edge placeholder:text-tp-ink-4",
              "outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 focus-visible:border-tp-amber/40",
            )}
            aria-label={t("playground.tp.promptLabel")}
            placeholder={t("playground.tp.promptPlaceholder")}
          />
        </label>

        <div className="flex flex-wrap items-end gap-3">
          {/* Model picker via FilterChipGroup */}
          <div className="flex flex-col gap-1.5">
            <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
              {t("playground.tp.controlsModel")}
            </span>
            <FilterChipGroup
              options={modelOptions}
              value={model}
              onChange={(next) => setModel(next as Model)}
              label={t("playground.tp.modelPickerLabel")}
              data-testid="model-picker"
            />
          </div>

          {/* W2.3: Agent picker — defaults to auto-route. When the
              operator picks an explicit agent we thread ``agent_id``
              into the chat request payload (see ``runBoth``); the
              backend prefers this hint over the message-peek heuristic
              in ``_peek_agent_binding``. */}
          <AgentPicker
            value={explicitAgent}
            onChange={setExplicitAgent}
          />

          <NumberControl
            label={t("playground.tp.controlsMaxTokens")}
            value={maxTokens}
            onChange={setMaxTokens}
            min={16}
            max={4096}
            step={16}
          />
          <NumberControl
            label={t("playground.tp.controlsTemperature")}
            value={temperature}
            onChange={setTemperature}
            min={0}
            max={2}
            step={0.1}
            format={(v) => v.toFixed(1)}
          />

          <div className="ml-auto flex items-center gap-2">
            <span className="hidden font-mono text-[10.5px] text-tp-ink-4 sm:inline">
              {t("playground.tp.runHint")}
            </span>
            <RunButton
              running={running}
              offline={offline}
              onClick={runBoth}
              runLabel={t("playground.tp.run")}
              runningLabel={t("playground.tp.running")}
              offlineTip={t("playground.tp.runOfflineTip")}
            />
          </div>
        </div>
      </GlassPanel>

      {/* ─── STAT ROW ─────────────────────────────────────── */}
      <section className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
        <StatChip
          label={t("playground.tp.statBlockThroughput")}
          value={blockRate === null ? "—" : blockRate.toFixed(1)}
          foot={t("playground.tp.statBlockFoot")}
          sparkPath={SPARK_BLOCK}
          sparkTone="amber"
          data-testid="stat-block"
        />
        <StatChip
          label={t("playground.tp.statFnThroughput")}
          value={fnRate === null ? "—" : fnRate.toFixed(1)}
          foot={t("playground.tp.statFnFoot")}
          sparkPath={SPARK_FN}
          sparkTone="ember"
          data-testid="stat-fn"
        />
        <StatChip
          label={t("playground.tp.statTotalTokens")}
          value={totalTokens === 0 ? "—" : totalTokens.toLocaleString()}
          foot={t("playground.tp.statTotalFoot")}
          sparkPath={SPARK_TOTAL}
          sparkTone="peach"
          data-testid="stat-total"
        />
        <StatChip
          label={t("playground.tp.statDivergence")}
          value={
            !hasRun ? "—" : `${divergencePct.toFixed(0)}%`
          }
          foot={t("playground.tp.statDivergenceFoot")}
          sparkPath={SPARK_DIV}
          sparkTone="ember"
          data-testid="stat-divergence"
        />
      </section>

      {/* ─── SPLIT PANE ───────────────────────────────────── */}
      <div className="relative min-h-0 flex-1">
        {/* Divergence pulse: a 1px amber line that flashes across the top
            of both panes whenever the diff set grows mid-run. Hidden under
            reduced motion. */}
        {!reduced && pulseKey > 0 ? (
          <span
            key={`pulse-${pulseKey}`}
            aria-label={t("playground.tp.divergencePulseAria")}
            className={cn(
              "pointer-events-none absolute inset-x-0 top-0 h-px animate-pulse-glow",
            )}
            style={{
              background:
                "linear-gradient(90deg, transparent 0%, var(--tp-amber) 50%, transparent 100%)",
            }}
          />
        ) : null}

        <GlassPanel
          variant="soft"
          className="playground-split flex min-h-[420px] flex-col overflow-hidden p-0"
        >
          <SplitPane
            ariaLabel={t("playground.tp.splitAria")}
            left={
              <PaneShell
                title={t("playground.tp.paneBlockTitle")}
                subtitle={t("playground.tp.paneBlockSubtitle")}
                state={blockState}
                rate={formatRate(blockRate, block, t)}
                tokens={block.tokens.length}
                testId="pane-block"
                accent="amber"
              >
                <TokenStream
                  tokens={block.tokens}
                  done={block.done || !block.running}
                  peerText={fnText}
                  diffReady={bothDone}
                  data-testid="stream-block"
                />
              </PaneShell>
            }
            right={
              <PaneShell
                title={t("playground.tp.paneFnTitle")}
                subtitle={t("playground.tp.paneFnSubtitle")}
                state={fnState}
                rate={formatRate(fnRate, fn, t)}
                tokens={fn.tokens.length}
                testId="pane-function-call"
                accent="ember"
                border="left"
              >
                <TokenStream
                  tokens={fn.tokens}
                  done={fn.done || !fn.running}
                  peerText={blockText}
                  diffReady={bothDone}
                  data-testid="stream-function-call"
                />
              </PaneShell>
            }
          />
        </GlassPanel>
      </div>

      {/* ─── RAW FRAMES TOGGLE ────────────────────────────── */}
      <section className="flex flex-col gap-2">
        <button
          type="button"
          onClick={() => setRawFramesOpen((v) => !v)}
          aria-expanded={rawFramesOpen}
          data-testid="raw-frames-toggle"
          className={cn(
            "inline-flex w-fit items-center gap-2 rounded-full border px-3 py-1",
            "bg-tp-glass-inner border-tp-glass-edge text-tp-ink-2",
            "font-mono text-[11px]",
            "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "inline-block h-1.5 w-1.5 rounded-full",
              rawFramesOpen ? "bg-tp-amber" : "bg-tp-ink-4",
            )}
          />
          {rawFramesOpen
            ? t("playground.tp.rawFramesHide")
            : t("playground.tp.rawFramesShow")}
        </button>

        {rawFramesOpen ? (
          <GlassPanel
            variant="soft"
            className="flex flex-col gap-3 p-4"
            data-testid="raw-frames-panel"
          >
            <h2 className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
              {t("playground.tp.rawFramesTitle")}
            </h2>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <FramesColumn
                label={t("playground.tp.rawFramesBlock")}
                frames={blockFrames}
                emptyLabel={t("playground.tp.rawFramesEmpty")}
              />
              <FramesColumn
                label={t("playground.tp.rawFramesFn")}
                frames={fnFrames}
                emptyLabel={t("playground.tp.rawFramesEmpty")}
              />
            </div>
          </GlassPanel>
        ) : null}
      </section>

      {/* ─── LOW-SKILL HINT (W2.3) ────────────────────────────
          When the active profile has < SKILLS_HINT_THRESHOLD skills
          mounted, point the operator at the hub so they don't sit on an
          empty skill library. The hint reads from the same
          ``/admin/skills`` row endpoint W2.1's Installed tab consumes;
          it stays hidden while the query is pending or errors so a
          gateway hiccup doesn't false-positive into "you have zero
          skills". */}
      <PlaygroundSkillsHint />

      {/* Scope the split-pane divider colours to the Tidepool amber palette
          without touching the shared SplitPane component. Uses a raw
          <style> block via dangerouslySetInnerHTML so we don't introduce a
          styled-jsx dependency just for this one-off override. The rules
          target the Tailwind-generated utility classes already present on
          the divider's inner spans. */}
      <style
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{
          __html: `
.playground-split [data-testid="split-pane-divider"] span[aria-hidden="true"] {
  background-color: var(--tp-glass-edge) !important;
  transition: background-color 160ms ease;
}
.playground-split [data-testid="split-pane-divider"]:hover span[aria-hidden="true"],
.playground-split [data-testid="split-pane-divider"]:focus-visible span[aria-hidden="true"] {
  background-color: var(--tp-amber) !important;
}
`,
        }}
      />
    </div>
  );
}

// ─── sub-components ─────────────────────────────────────────────────

interface PaneShellProps {
  title: string;
  subtitle: string;
  state: StreamState;
  rate: string;
  tokens: number;
  testId: string;
  accent: "amber" | "ember";
  border?: "left";
  children: React.ReactNode;
}

function PaneShell({
  title,
  subtitle,
  state,
  rate,
  tokens,
  testId,
  accent,
  border,
  children,
}: PaneShellProps) {
  const { t } = useTranslation();
  return (
    <GlassPanel
      as="section"
      variant="strong"
      rounded="rounded-none"
      data-testid={testId}
      className={cn(
        "flex h-full min-h-[360px] flex-col border-0",
        border === "left" && "border-l border-tp-glass-edge",
      )}
    >
      <header
        className={cn(
          "flex items-center justify-between gap-2 border-b border-tp-glass-edge px-4 py-2.5",
        )}
      >
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            aria-hidden
            className={cn(
              "h-1.5 w-1.5 shrink-0 rounded-full",
              accent === "amber" ? "bg-tp-amber" : "bg-tp-ember",
              state === "live" &&
                (accent === "amber" ? "tp-breathe-amber" : "tp-breathe"),
            )}
          />
          <h2 className="truncate text-[13.5px] font-semibold text-tp-ink">
            {title}
          </h2>
          <span className="truncate font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
            {subtitle}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10.5px] tabular-nums text-tp-ink-3">
            {t("playground.tp.paneMetricTokens", { n: tokens })}
          </span>
          <StreamPill state={state} rate={rate} />
        </div>
      </header>
      <div className="flex-1 overflow-auto p-3">{children}</div>
    </GlassPanel>
  );
}

interface NumberControlProps {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
}

function NumberControl({
  label,
  value,
  onChange,
  min,
  max,
  step,
  format,
}: NumberControlProps) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
        {label}
      </span>
      <input
        type="number"
        value={format ? format(value) : value}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (Number.isFinite(n)) onChange(n);
        }}
        min={min}
        max={max}
        step={step}
        className={cn(
          "w-[112px] rounded-lg border px-3 py-1.5 font-mono text-[12.5px] tabular-nums text-tp-ink",
          "bg-tp-glass-inner border-tp-glass-edge",
          "outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 focus-visible:border-tp-amber/40",
        )}
        aria-label={label}
      />
    </label>
  );
}

interface RunButtonProps {
  running: boolean;
  offline: boolean;
  onClick: () => void;
  runLabel: string;
  runningLabel: string;
  offlineTip: string;
}

function RunButton({
  running,
  offline,
  onClick,
  runLabel,
  runningLabel,
  offlineTip,
}: RunButtonProps) {
  const disabled = running || offline;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      data-testid="run-button"
      title={offline ? offlineTip : undefined}
      aria-label={offline ? offlineTip : runLabel}
      className={cn(
        "inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-[13px] font-medium",
        "border border-tp-amber/35 bg-tp-amber-soft text-tp-amber",
        "shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_6px_14px_-8px_color-mix(in_oklch,var(--tp-amber)_55%,transparent)]",
        "transition-transform duration-200 hover:-translate-y-px",
        "hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        "disabled:translate-y-0 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:translate-y-0",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          running ? "bg-tp-amber tp-breathe-amber" : "bg-tp-amber",
        )}
      />
      {running ? runningLabel : runLabel}
      <kbd className="ml-1 rounded bg-tp-amber/10 px-1.5 py-0.5 font-mono text-[10px] text-tp-amber/80">
        ⌘↵
      </kbd>
    </button>
  );
}

interface FramesColumnProps {
  label: string;
  frames: unknown[];
  emptyLabel: string;
}

function FramesColumn({ label, frames, emptyLabel }: FramesColumnProps) {
  return (
    <div className="flex flex-col gap-2">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
        {label}
      </div>
      {frames.length === 0 ? (
        <div className="rounded-lg border border-dashed border-tp-glass-edge bg-tp-glass-inner px-3 py-6 text-center text-[12px] text-tp-ink-4">
          {emptyLabel}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {frames.map((frame, i) => (
            <JsonView
              key={i}
              value={frame}
              className="max-h-[220px] overflow-y-auto"
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ─── helpers ────────────────────────────────────────────────────────

/**
 * W2.3: compose the chat-completion request body that will be POSTed to
 * the gateway when the mock streams are swapped for the real SSE
 * endpoint. ``agent_id`` is only included when the operator picked an
 * explicit agent — otherwise the backend's heuristic
 * (``_peek_agent_binding``) routes via the message-peek as before.
 *
 * Exported for unit-test coverage and for the eventual real SSE path
 * to consume.
 */
interface ChatRequestArgs {
  prompt: string;
  model: string;
  maxTokens: number;
  temperature: number;
  /** ``null`` = auto-route (default). */
  agentId: string | null;
}

export interface ChatRequestBody {
  messages: Array<{ role: "user"; content: string }>;
  model: string;
  max_tokens: number;
  temperature: number;
  agent_id?: string;
}

export function buildChatRequestBody({
  prompt,
  model,
  maxTokens,
  temperature,
  agentId,
}: ChatRequestArgs): ChatRequestBody {
  return {
    messages: [{ role: "user", content: prompt }],
    model,
    max_tokens: maxTokens,
    temperature,
    ...(agentId ? { agent_id: agentId } : {}),
  };
}

/**
 * Compute tokens-per-second over the rolling ≤2s tail. Returns `null` if
 * there are fewer than two arrivals (no meaningful rate yet).
 */
function computeRate(arrivals: number[]): number | null {
  if (arrivals.length < 2) return null;
  const last = arrivals[arrivals.length - 1]!;
  const windowMs = 2000;
  let firstIdx = 0;
  for (let i = arrivals.length - 1; i >= 0; i--) {
    if (last - (arrivals[i] ?? 0) > windowMs) {
      firstIdx = i + 1;
      break;
    }
  }
  const slice = arrivals.slice(firstIdx);
  if (slice.length < 2) return null;
  const spanMs = last - (slice[0] ?? last);
  if (spanMs <= 0) return null;
  return (slice.length / spanMs) * 1000;
}

function formatRate(
  rate: number | null,
  pane: PaneState,
  t: (k: string, opts?: Record<string, unknown>) => string,
): string {
  if (pane.tokens.length === 0 && !pane.running) {
    return t("playground.tp.paneMetricIdle");
  }
  if (rate === null) return t("playground.tp.paneMetricIdle");
  return t("playground.tp.paneMetricRate", { rate: rate.toFixed(1) });
}

/**
 * Split the block-protocol body into "frames" on blank lines, keep only
 * the last N. Each frame is presented as `{ kind, lines }` so JsonView
 * surfaces the 「始」「末」 delimiters cleanly.
 */
function buildBlockFrames(text: string): Array<Record<string, unknown>> {
  if (text.length === 0) return [];
  const chunks = text.split(/\n\s*\n/).filter((c) => c.trim().length > 0);
  const tail = chunks.slice(-RAW_FRAMES_MAX);
  return tail.map((chunk, i) => {
    const lines = chunk.split("\n");
    const kind = inferBlockKind(lines[0] ?? "");
    return {
      frame: i + 1,
      kind,
      lines,
    };
  });
}

function inferBlockKind(firstLine: string): string {
  if (firstLine.includes("<<<[TOOL_REQUEST]>>>")) return "tool_request";
  if (firstLine.includes("<<<[END_TOOL_REQUEST]>>>")) return "tool_end";
  return "text";
}

/**
 * Parse the function-call pane accumulation into up-to-N JSON frames.
 * Since the mock stream yields tokens of a single JSON blob, we try to
 * parse the final text first; if that succeeds, that's the only frame.
 * Otherwise we slice into equal chunks as a best-effort fallback.
 */
function buildFnFrames(text: string): unknown[] {
  if (text.length === 0) return [];
  const trimmed = text.trim();
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    return [parsed];
  } catch {
    // Fall back to line-based chunks — still better than a single raw blob.
    const lines = trimmed.split("\n").filter((l) => l.length > 0);
    const tail = lines.slice(-RAW_FRAMES_MAX);
    return tail.map((line, i) => ({ frame: i + 1, raw: line }));
  }
}

/**
 * W2.3 — low-skill-count CTA.
 *
 * Renders a single-line glass card that nudges the operator to browse the
 * skill hub when their active profile is under-tooled. Reads the same
 * ``GET /admin/skills`` row endpoint W2.1's Installed tab consumes so the
 * count stays in lock-step with what the user actually sees on the
 * Installed surface.
 *
 * Renders nothing when:
 *
 *   * the page is rendered outside :class:`ActiveProfileProvider` (e.g.
 *     standalone snapshot tests) — the context is ``null`` so we bail
 *     before issuing a query;
 *   * the query is still pending — avoids flashing "you have 0 skills"
 *     during the first paint;
 *   * the query errored — a gateway hiccup shouldn't false-positive into a
 *     hint;
 *   * the profile already has ``>= SKILLS_HINT_THRESHOLD`` skills.
 */
function PlaygroundSkillsHint(): React.ReactElement | null {
  const { t } = useTranslation();
  const ctx = React.useContext(ActiveProfileContext);
  const slug = ctx?.slug ?? null;

  const query = useQuery({
    queryKey: ["admin", "skills", "installed", slug ?? "__no_profile"],
    queryFn: () => listInstalledSkills(slug ?? "default"),
    enabled: slug !== null,
    retry: false,
    staleTime: 30_000,
  });

  if (!ctx || query.isPending || query.isError) return null;

  const rows = query.data?.rows ?? [];
  const count = rows.length;
  if (count >= SKILLS_HINT_THRESHOLD) return null;

  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:p-5"
      data-testid="playground-skills-hint"
    >
      <div className="flex min-w-0 flex-col gap-1">
        <h3 className="text-[14px] font-semibold text-tp-ink">
          {t("playground.skills.hint.title")}
        </h3>
        <p className="text-[13px] leading-[1.55] text-tp-ink-2">
          {t("playground.skills.hint.body", { count })}
        </p>
      </div>
      <Link
        href="/admin/skills"
        className={cn(
          "inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5",
          "border border-tp-amber/40 bg-tp-amber/10 text-[12px] font-mono text-tp-amber",
          "hover:bg-tp-amber/20 hover:text-tp-amber",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        )}
        data-testid="playground-skills-hint-cta"
      >
        {t("playground.skills.hint.cta")}
        <span aria-hidden>→</span>
      </Link>
    </GlassPanel>
  );
}
