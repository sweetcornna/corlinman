"use client";

/**
 * `<MarkdownMessage>` — chat-tuned markdown renderer.
 *
 * Differs from `<ReleaseNotes>` (the release-notes renderer) in two ways:
 *
 *   1. Code blocks get a header bar with the language label + a copy button.
 *      We don't ship a syntax highlighter in this MVP (shiki is sizable);
 *      Wave 2 will swap the `<pre>` body for `<Shiki language={lang}>` when
 *      we wire it in.
 *   2. Inline content has tighter vertical rhythm to feel like chat, not
 *      release notes.
 *
 * Sanitised with `rehype-sanitize`'s default schema. Raw HTML, script tags,
 * inline handlers, javascript: URLs all stripped.
 */

import * as React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import { Check, Copy, ExternalLink } from "lucide-react";

import { cn } from "@/lib/utils";

interface MarkdownMessageProps {
  content: string;
  /** Add a thin cursor caret at the end of the content (streaming UX). */
  streaming?: boolean;
  className?: string;
  /** Invoked when the user clicks "Open in panel" on a code block. The
   *  parent decides whether to mount an artifact for it. */
  onOpenArtifact?: (lang: string, source: string) => void;
}

export function MarkdownMessage({
  content,
  streaming,
  className,
  onOpenArtifact,
}: MarkdownMessageProps) {
  const components: Components = React.useMemo(
    () => ({
      p: ({ className: c, ...rest }) => (
        <p
          className={cn("my-1.5 text-[13px] leading-relaxed text-tp-ink", c)}
          {...rest}
        />
      ),
      h1: ({ className: c, ...rest }) => (
        <h1
          className={cn(
            "mt-3 text-base font-semibold text-tp-ink first:mt-0",
            c,
          )}
          {...rest}
        />
      ),
      h2: ({ className: c, ...rest }) => (
        <h2
          className={cn(
            "mt-3 text-sm font-semibold text-tp-ink first:mt-0",
            c,
          )}
          {...rest}
        />
      ),
      h3: ({ className: c, ...rest }) => (
        <h3
          className={cn(
            "mt-2 text-sm font-medium text-tp-ink first:mt-0",
            c,
          )}
          {...rest}
        />
      ),
      ul: ({ className: c, ...rest }) => (
        <ul
          className={cn(
            "my-1.5 list-disc space-y-0.5 pl-5 text-[13px] text-tp-ink",
            c,
          )}
          {...rest}
        />
      ),
      ol: ({ className: c, ...rest }) => (
        <ol
          className={cn(
            "my-1.5 list-decimal space-y-0.5 pl-5 text-[13px] text-tp-ink",
            c,
          )}
          {...rest}
        />
      ),
      li: ({ className: c, ...rest }) => (
        <li className={cn("leading-relaxed", c)} {...rest} />
      ),
      a: ({ className: c, ...rest }) => (
        <a
          className={cn(
            "text-tp-amber underline-offset-2 hover:underline",
            c,
          )}
          target="_blank"
          rel="noreferrer"
          {...rest}
        />
      ),
      blockquote: ({ className: c, ...rest }) => (
        <blockquote
          className={cn(
            "my-2 border-l-2 border-tp-glass-edge pl-3 text-tp-ink-2 italic",
            c,
          )}
          {...rest}
        />
      ),
      table: ({ className: c, ...rest }) => (
        <div className="my-2 w-full overflow-x-auto">
          <table
            className={cn(
              "w-full border-collapse text-[12px] text-tp-ink",
              c,
            )}
            {...rest}
          />
        </div>
      ),
      th: ({ className: c, ...rest }) => (
        <th
          className={cn(
            "border border-tp-glass-edge bg-tp-glass-inner px-2 py-1 text-left font-medium",
            c,
          )}
          {...rest}
        />
      ),
      td: ({ className: c, ...rest }) => (
        <td
          className={cn("border border-tp-glass-edge px-2 py-1", c)}
          {...rest}
        />
      ),
      code: ({ className: c, children, ...rest }) => {
        // Inline code (no language → no className from remark)
        const isInline = !c?.startsWith("language-");
        if (isInline) {
          return (
            <code
              className="rounded bg-tp-glass-inner px-1 py-0.5 font-mono text-[12px] text-tp-ink"
              {...rest}
            >
              {children}
            </code>
          );
        }
        const lang = c?.slice("language-".length) || "";
        return (
          <CodeBlock
            language={lang}
            onOpenArtifact={onOpenArtifact}
          >
            {String(children).replace(/\n$/, "")}
          </CodeBlock>
        );
      },
      pre: ({ children }) => <>{children}</>,
    }),
    [],
  );

  return (
    <div className={cn("chat-md", className)}>
      <ReactMarkdown
        rehypePlugins={[rehypeSanitize]}
        components={components}
      >
        {content}
      </ReactMarkdown>
      {streaming ? (
        <span
          aria-hidden="true"
          className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-tp-ink align-middle"
          data-testid="md-cursor"
        />
      ) : null}
    </div>
  );
}

interface CodeBlockProps {
  language: string;
  children: string;
  onOpenArtifact?: (lang: string, source: string) => void;
}

const ARTIFACT_AUTO_LANGS = new Set(["html", "svg", "mermaid"]);
const ARTIFACT_LINE_THRESHOLD = 25;

function CodeBlock({ language, children, onOpenArtifact }: CodeBlockProps) {
  const [copied, setCopied] = React.useState(false);
  const copy = React.useCallback(() => {
    void navigator.clipboard?.writeText(children).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }, [children]);

  const lineCount = React.useMemo(
    () => children.split("\n").length,
    [children],
  );
  const showArtifactCta =
    Boolean(onOpenArtifact) &&
    (ARTIFACT_AUTO_LANGS.has(language.toLowerCase()) ||
      lineCount >= ARTIFACT_LINE_THRESHOLD);

  return (
    <div
      className="my-2 overflow-hidden rounded-md border border-tp-glass-edge bg-tp-glass-inner"
      data-testid="md-codeblock"
    >
      <div className="flex items-center justify-between border-b border-tp-glass-edge bg-tp-glass-inner/60 px-2 py-1 text-[11px] text-tp-ink-3">
        <span className="font-mono">{language || "text"}</span>
        <div className="flex items-center gap-1">
          {showArtifactCta ? (
            <button
              type="button"
              onClick={() => onOpenArtifact?.(language, children)}
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-tp-ink-3 transition hover:bg-tp-glass-inner hover:text-tp-ink"
              aria-label="Open in side panel"
              data-testid="md-codeblock-open-artifact"
            >
              <ExternalLink className="h-3 w-3" aria-hidden="true" />
              Open
            </button>
          ) : null}
          <button
            type="button"
            onClick={copy}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-tp-ink-3 transition hover:bg-tp-glass-inner hover:text-tp-ink"
            aria-label={copied ? "Copied" : "Copy code"}
          >
            {copied ? (
              <Check className="h-3 w-3" aria-hidden="true" />
            ) : (
              <Copy className="h-3 w-3" aria-hidden="true" />
            )}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>
      <pre className="overflow-x-auto px-3 py-2 font-mono text-[12px] leading-relaxed text-tp-ink">
        <code>{children}</code>
      </pre>
    </div>
  );
}
