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
      className="flex w-[420px] shrink-0 flex-col border-l border-tp-glass-edge bg-tp-glass-inner/30"
      data-testid="artifact-panel"
      aria-label={t("chat.artifactPanelAriaLabel")}
    >
      <header className="flex items-center gap-1 border-b border-tp-glass-edge px-2 py-1.5">
        <FileCode className="h-3.5 w-3.5 text-tp-ink-3" aria-hidden="true" />
        <span className="text-[12px] font-medium text-tp-ink">
          {t("chat.artifactPanelTitle")}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded p-1 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
          aria-label={t("chat.artifactPanelClose")}
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </header>

      <nav
        className="flex items-center gap-1 overflow-x-auto border-b border-tp-glass-edge px-2 py-1"
        aria-label={t("chat.artifactTabsAriaLabel")}
      >
        {artifacts.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => onSelect(a.id)}
            className={cn(
              "group inline-flex max-w-[180px] items-center gap-1 truncate rounded px-1.5 py-0.5 text-[11px]",
              activeId === a.id
                ? "bg-tp-amber/20 text-tp-ink"
                : "text-tp-ink-2 hover:bg-tp-glass-inner hover:text-tp-ink",
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
              className="ml-0.5 hidden h-3 w-3 cursor-pointer items-center justify-center rounded text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-err group-hover:inline-flex"
              aria-label={t("chat.artifactRemove", { title: a.title })}
            >
              <X className="h-2.5 w-2.5" aria-hidden="true" />
            </span>
          </button>
        ))}
      </nav>

      {active ? (
        <>
          <div className="flex items-center gap-1 border-b border-tp-glass-edge px-2 py-1 text-[11px]">
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
                <span className="font-mono text-tp-ink-3">
                  v{active.versions.length}
                </span>
              ) : null}
              <button
                type="button"
                onClick={() => copy(active.source)}
                className="rounded p-1 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
                aria-label={t("chat.artifactCopySource")}
              >
                <Copy className="h-3 w-3" aria-hidden="true" />
              </button>
              <button
                type="button"
                onClick={() =>
                  download(active.source, suggestedFilename(active))
                }
                className="rounded p-1 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
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
              <iframe
                title={t("chat.artifactIframeTitle", { title: active.title })}
                className="h-full w-full border-0 bg-white"
                sandbox="allow-scripts allow-same-origin"
                srcDoc={active.source}
                data-testid="artifact-iframe-html"
              />
            ) : view === "preview" && active.kind === "svg" ? (
              <div
                className="flex h-full w-full items-center justify-center overflow-auto bg-tp-glass-inner/20 p-4"
                dangerouslySetInnerHTML={{ __html: active.source }}
                data-testid="artifact-svg"
              />
            ) : view === "preview" &&
              (active.kind === "mermaid" || active.kind === "markdown") ? (
              <div className="p-3 text-[12px] text-tp-ink-3">
                {t("chat.artifactPreviewDeferred", { kind: active.kind })}
              </div>
            ) : (
              <pre className="overflow-auto px-3 py-2 font-mono text-[11px] leading-relaxed text-tp-ink">
                {active.source}
              </pre>
            )}
          </div>

          <footer className="flex items-center justify-between border-t border-tp-glass-edge px-2 py-1 text-[10px] text-tp-ink-3">
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
        <div className="flex flex-1 items-center justify-center px-3 text-center text-[12px] text-tp-ink-3">
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
        "rounded px-1.5 py-0.5 text-[11px]",
        active ? "bg-tp-amber/20 text-tp-ink" : "text-tp-ink-3 hover:bg-tp-glass-inner",
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
