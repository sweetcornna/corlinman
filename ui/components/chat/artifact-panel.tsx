"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Copy, Download, FileCode, GitFork, X } from "lucide-react";

import { cn } from "@/lib/utils";
import type { Artifact } from "@/lib/chat/artifacts";

interface ArtifactPanelProps {
  artifacts: Artifact[];
  activeId: string | null;
  open: boolean;
  onClose: () => void;
  onSelect: (id: string) => void;
  onRemove: (id: string) => void;
}

type ViewMode = "preview" | "source";

export function ArtifactPanel({
  artifacts,
  activeId,
  open,
  onClose,
  onSelect,
  onRemove,
}: ArtifactPanelProps) {
  const { t } = useTranslation();
  const active = React.useMemo(
    () => artifacts.find((a) => a.id === activeId) ?? null,
    [artifacts, activeId],
  );
  const [view, setView] = React.useState<ViewMode>("preview");

  React.useEffect(() => {
    if (!active) return;
    if (active.kind === "html" || active.kind === "svg") {
      setView("preview");
    } else {
      setView("source");
    }
  }, [active?.id, active?.kind]);

  if (!open || artifacts.length === 0) return null;

  return (
    <aside
      className="flex w-[420px] shrink-0 flex-col overflow-hidden rounded-sg-lg sg-card"
      data-testid="artifact-panel"
      aria-label={t("chat.artifactPanelAriaLabel")}
    >
      <header className="flex items-center gap-1 border-b border-sg-border px-2 py-1.5">
        <FileCode className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
        <span className="text-[12px] font-medium text-sg-ink">
          {t("chat.artifactPanelTitle")}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded-sg-sm p-1 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
          aria-label={t("chat.artifactPanelClose")}
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </header>

      <nav
        className="flex items-center gap-1 overflow-x-auto border-b border-sg-border px-2 py-1.5"
        aria-label={t("chat.artifactTabsAriaLabel")}
      >
        {artifacts.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => onSelect(a.id)}
            className={cn(
              "group inline-flex max-w-[180px] items-center gap-1 truncate rounded-full px-2 py-0.5 text-[11px]",
              activeId === a.id
                ? "bg-sg-accent-soft text-sg-ink"
                : "text-sg-ink-3 hover:bg-sg-inset hover:text-sg-ink",
            )}
            data-testid="artifact-tab"
            data-active={activeId === a.id ? "true" : undefined}
            title={a.title}
          >
            <span className="truncate font-mono">{a.language || "text"}</span>
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                onRemove(a.id);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  e.stopPropagation();
                  onRemove(a.id);
                }
              }}
              className="ml-0.5 hidden h-3 w-3 cursor-pointer items-center justify-center rounded-full text-sg-ink-4 hover:bg-sg-inset hover:text-sg-err group-hover:inline-flex"
              aria-label={t("chat.artifactRemove", { title: a.title })}
            >
              <X className="h-2.5 w-2.5" aria-hidden="true" />
            </span>
          </button>
        ))}
      </nav>

      {active ? (
        <>
          <div className="flex items-center gap-1 border-b border-sg-border px-2 py-1 text-[11px]">
            {active.kind === "html" || active.kind === "svg" ? (
              <>
                <ViewToggle
                  label={t("chat.artifactPreview")}
                  active={view === "preview"}
                  onClick={() => setView("preview")}
                  testId="artifact-view-preview"
                />
                <ViewToggle
                  label={t("chat.artifactSource")}
                  active={view === "source"}
                  onClick={() => setView("source")}
                  testId="artifact-view-source"
                />
              </>
            ) : null}
            <div className="ml-auto flex items-center gap-1">
              {active.versions && active.versions.length > 1 ? (
                <span className="font-mono text-sg-ink-4">
                  v{active.versions.length}
                </span>
              ) : null}
              <button
                type="button"
                onClick={() => copy(active.source)}
                className="rounded-sg-sm p-1 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
                aria-label={t("chat.artifactCopySource")}
              >
                <Copy className="h-3 w-3" aria-hidden="true" />
              </button>
              <button
                type="button"
                onClick={() =>
                  download(active.source, suggestedFilename(active))
                }
                className="rounded-sg-sm p-1 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
                aria-label={t("chat.artifactDownloadSource")}
              >
                <Download className="h-3 w-3" aria-hidden="true" />
              </button>
            </div>
          </div>

          <div
            className="flex-1 overflow-auto"
            data-testid="artifact-body"
            data-view={view}
          >
            {view === "preview" && active.kind === "html" ? (
              // Model output is untrusted. `allow-scripts` enables demo
              // interactivity, but we MUST NOT also grant
              // `allow-same-origin` — together they neutralise the
              // sandbox vs. the parent's origin (cookies, fetch to /api,
              // etc.). Without `allow-same-origin`, the iframe's
              // document.origin is the opaque "null" origin and the
              // operator's `corlinman_session` cookie stays out of reach.
              <iframe
                title={t("chat.artifactIframeTitle", { title: active.title })}
                className="h-full w-full border-0 bg-white"
                sandbox="allow-scripts"
                srcDoc={active.source}
                data-testid="artifact-iframe-html"
              />
            ) : view === "preview" && active.kind === "svg" ? (
              // SVG accepts <script> and event-handler attributes. Render
              // it inside a fully locked-down iframe (no sandbox flags at
              // all) instead of dangerouslySetInnerHTML, so any embedded
              // script can neither execute in this origin nor reach the
              // parent DOM.
              <iframe
                title={t("chat.artifactIframeTitle", { title: active.title })}
                className="h-full w-full border-0 bg-sg-inset"
                sandbox=""
                srcDoc={active.source}
                data-testid="artifact-iframe-svg"
              />
            ) : view === "preview" &&
              (active.kind === "mermaid" || active.kind === "markdown") ? (
              <div className="p-3 text-[12px] text-sg-ink-4">
                {t("chat.artifactPreviewDeferred", { kind: active.kind })}
              </div>
            ) : (
              <pre className="overflow-auto px-3 py-2 font-mono text-[11px] leading-relaxed text-sg-ink">
                {active.source}
              </pre>
            )}
          </div>

          <footer className="flex items-center justify-between border-t border-sg-border px-2 py-1 text-[10px] text-sg-ink-4">
            <span className="font-mono">
              {t("chat.artifactCharCount", { n: active.source.length })}
            </span>
            <span className="inline-flex items-center gap-1">
              <GitFork className="h-3 w-3" aria-hidden="true" />
              {t("chat.artifactFromMessage", { short: active.messageId.slice(0, 6) })}
            </span>
          </footer>
        </>
      ) : (
        <div className="flex flex-1 items-center justify-center px-3 text-center text-[12px] text-sg-ink-4">
          {t("chat.artifactEmptyMain")}
        </div>
      )}
    </aside>
  );
}

function ViewToggle({
  label,
  active,
  onClick,
  testId,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  testId: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full px-2 py-0.5 text-[11px]",
        active ? "bg-sg-accent-soft text-sg-ink" : "text-sg-ink-4 hover:bg-sg-inset",
      )}
      data-testid={testId}
      data-active={active ? "true" : undefined}
    >
      {label}
    </button>
  );
}

function suggestedFilename(a: { language: string; title: string }): string {
  const ext = a.language || "txt";
  const slug = a.title.replace(/[^a-z0-9-]+/gi, "-").slice(0, 30) || "artifact";
  return `${slug}.${ext}`;
}

function copy(text: string): void {
  void navigator.clipboard?.writeText(text);
}

function download(text: string, filename: string): void {
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
