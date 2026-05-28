/**
 * Artifact extraction + state for the right-side preview pane.
 *
 * MVP rules for what becomes an artifact:
 *
 *   - Any fenced code block with language ∈ {html, svg, mermaid}
 *     → renders as both `source` and `preview` tabs.
 *   - Any fenced code block ≥ 25 lines → opened on demand (user clicks
 *     the "Open in panel" button in the code-block header).
 *
 * We don't auto-extract every code block to avoid noise. The user can
 * always pop a small block into the panel manually.
 */

import * as React from "react";

export type ArtifactKind =
  | "code"
  | "html"
  | "svg"
  | "mermaid"
  | "markdown";

export interface Artifact {
  /** Stable id derived from (messageId, codeBlockIndex). */
  id: string;
  kind: ArtifactKind;
  /** Display title — derived from language or first non-empty line. */
  title: string;
  language: string;
  source: string;
  /** Message id this artifact was extracted from. */
  messageId: string;
  /** Multiple versions if the assistant re-emits the artifact. */
  versions?: string[];
}

const PREVIEW_LANGS = new Set(["html", "svg", "mermaid", "markdown", "md"]);

export function isPreviewableLanguage(lang: string): boolean {
  return PREVIEW_LANGS.has(lang.toLowerCase());
}

export function deriveArtifactKind(lang: string): ArtifactKind {
  const l = lang.toLowerCase();
  if (l === "html") return "html";
  if (l === "svg") return "svg";
  if (l === "mermaid") return "mermaid";
  if (l === "markdown" || l === "md") return "markdown";
  return "code";
}

/** Trim source for the chip title. */
export function deriveArtifactTitle(language: string, source: string): string {
  const firstLine = source.trim().split("\n")[0] ?? "";
  const trimmed =
    firstLine.length > 48 ? `${firstLine.slice(0, 45)}…` : firstLine;
  return language ? `${language}: ${trimmed}` : trimmed || "(untitled)";
}

/** React state hook owning the artifact panel. */
export function useArtifacts() {
  const [artifacts, setArtifacts] = React.useState<Artifact[]>([]);
  const [activeId, setActiveId] = React.useState<string | null>(null);
  const [panelOpen, setPanelOpen] = React.useState(false);

  const open = React.useCallback((art: Artifact) => {
    setArtifacts((prev) => {
      const existing = prev.find((a) => a.id === art.id);
      if (existing) {
        // Same id → push as new version if source differs.
        if (existing.source !== art.source) {
          return prev.map((a) =>
            a.id === art.id
              ? {
                  ...a,
                  source: art.source,
                  versions: [...(a.versions ?? [a.source]), art.source],
                }
              : a,
          );
        }
        return prev;
      }
      return [...prev, art];
    });
    setActiveId(art.id);
    setPanelOpen(true);
  }, []);

  const close = React.useCallback(() => {
    setPanelOpen(false);
  }, []);

  const remove = React.useCallback((id: string) => {
    setArtifacts((prev) => prev.filter((a) => a.id !== id));
    setActiveId((cur) => (cur === id ? null : cur));
  }, []);

  const select = React.useCallback((id: string) => {
    setActiveId(id);
    setPanelOpen(true);
  }, []);

  return {
    artifacts,
    activeId,
    panelOpen,
    open,
    close,
    remove,
    select,
  };
}
