"use client";

/**
 * `<ReleaseNotes>` — sanitized GitHub-release markdown renderer (W2.1).
 *
 * GitHub release bodies are arbitrary user input — they can contain raw
 * HTML, script tags, event handlers, javascript: URLs, etc. We render via
 * `react-markdown` with the `rehype-sanitize` plugin which strips every
 * dangerous construct using its conservative default schema (no `<script>`,
 * no inline event handlers, no `javascript:` URLs, no raw HTML elements
 * outside the safe markdown subset).
 *
 * Styling: the project doesn't ship `@tailwindcss/typography` (the `prose`
 * plugin), so we apply per-element classes locally via the `components`
 * prop. The classes are tuned to the Tidepool token palette — `text-sg-ink`
 * for body, `text-sg-accent` for links, glass-edge dividers — matching the
 * rest of the admin surface.
 */

import * as React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";

import { cn } from "@/lib/utils";

const MARKDOWN_COMPONENTS: Components = {
  h1: ({ className, ...rest }) => (
    <h1
      className={cn(
        "mt-4 text-base font-semibold tracking-tight text-sg-ink first:mt-0",
        className,
      )}
      {...rest}
    />
  ),
  h2: ({ className, ...rest }) => (
    <h2
      className={cn(
        "mt-4 text-sm font-semibold tracking-tight text-sg-ink first:mt-0",
        className,
      )}
      {...rest}
    />
  ),
  h3: ({ className, ...rest }) => (
    <h3
      className={cn(
        "mt-3 text-sm font-medium text-sg-ink first:mt-0",
        className,
      )}
      {...rest}
    />
  ),
  p: ({ className, ...rest }) => (
    <p
      className={cn("my-2 text-sm leading-relaxed text-sg-ink-2", className)}
      {...rest}
    />
  ),
  ul: ({ className, ...rest }) => (
    <ul
      className={cn(
        "my-2 ml-5 list-disc space-y-1 text-sm text-sg-ink-2",
        className,
      )}
      {...rest}
    />
  ),
  ol: ({ className, ...rest }) => (
    <ol
      className={cn(
        "my-2 ml-5 list-decimal space-y-1 text-sm text-sg-ink-2",
        className,
      )}
      {...rest}
    />
  ),
  li: ({ className, ...rest }) => (
    <li className={cn("leading-relaxed", className)} {...rest} />
  ),
  a: ({ className, ...rest }) => (
    <a
      // GitHub release links open in a new tab so the user doesn't lose
      // their /admin/system context. rehype-sanitize already strips
      // javascript: URLs from `href`, so this is safe.
      target="_blank"
      rel="noreferrer noopener"
      className={cn(
        "font-medium text-sg-accent underline decoration-sg-accent/40 underline-offset-2 transition-colors hover:text-sg-accent-2 hover:decoration-sg-accent",
        className,
      )}
      {...rest}
    />
  ),
  code: ({ className, ...rest }) => (
    <code
      className={cn(
        "rounded bg-sg-inset px-1.5 py-0.5 font-mono text-[12px] text-sg-ink",
        className,
      )}
      {...rest}
    />
  ),
  pre: ({ className, ...rest }) => (
    <pre
      className={cn(
        "my-2 overflow-x-auto rounded-md border border-sg-border bg-sg-inset p-3 font-mono text-[12px] text-sg-ink",
        className,
      )}
      {...rest}
    />
  ),
  blockquote: ({ className, ...rest }) => (
    <blockquote
      className={cn(
        "my-2 border-l-2 border-sg-accent/50 pl-3 text-sm italic text-sg-ink-3",
        className,
      )}
      {...rest}
    />
  ),
  hr: ({ className, ...rest }) => (
    <hr
      className={cn("my-3 border-t border-sg-border", className)}
      {...rest}
    />
  ),
  strong: ({ className, ...rest }) => (
    <strong
      className={cn("font-semibold text-sg-ink", className)}
      {...rest}
    />
  ),
};

export interface ReleaseNotesProps {
  /** Raw markdown sourced from a GitHub release `body`. */
  markdown: string;
  className?: string;
}

export function ReleaseNotes({ markdown, className }: ReleaseNotesProps) {
  return (
    <div
      data-testid="release-notes"
      className={cn("max-w-none text-sm text-sg-ink-2", className)}
    >
      <ReactMarkdown
        // `rehype-sanitize` with no options uses its default schema, which
        // already blocks `<script>`, inline event handlers (`onclick=…`),
        // `javascript:` URLs, `<iframe>`, `<object>`, etc.
        rehypePlugins={[rehypeSanitize]}
        components={MARKDOWN_COMPONENTS}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
