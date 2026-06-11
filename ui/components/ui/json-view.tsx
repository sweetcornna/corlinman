"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Minimal JSON syntax highlighter.
 *
 * Renders a <pre> block with per-token classes:
 *   - .sg-json-k  — object keys
 *   - .sg-json-s  — string values
 *   - .sg-json-n  — numbers
 *   - .sg-json-b  — booleans / null
 *   - .sg-json-c  — comments (only `// …` single-line, inserted via `comments` prop)
 *
 * Intentionally hand-rolled rather than vendoring react-syntax-highlighter
 * (which would add ~150KB). For our per-log-row payloads (< 2KB typical),
 * a small regex tokenizer is plenty.
 *
 * Usage:
 *   <JsonView value={{ status: 403, ok: false }} />
 *   <JsonView raw="{ \"status\": 403, \"ok\": false }" />
 *   <JsonView value={req} comments={{ before: 'request', after: 'response', afterValue: res }} />
 */

export interface JsonViewCommentBundle {
  before?: string;
  /** When provided, emits a second block below the first with this header. */
  after?: string;
  afterValue?: unknown;
}

export interface JsonViewProps extends React.HTMLAttributes<HTMLPreElement> {
  /** Serialise this with JSON.stringify(…, null, 2). */
  value?: unknown;
  /** Or pass pre-serialised raw JSON. Mutually exclusive with `value`. */
  raw?: string;
  /** Optional comment bundle for two-block request/response layouts. */
  comments?: JsonViewCommentBundle;
}

/** Highlights a single line — returns an array of React children. */
function highlight(line: string): React.ReactNode[] {
  // Key:   "…": (quoted, followed by colon)
  // String: "…"
  // Number: 123, -1.5, 1e9
  // Boolean/null: true, false, null
  // Comment: // … until eol
  const parts: React.ReactNode[] = [];
  let i = 0;
  let keyCounter = 0;
  const push = (node: React.ReactNode) => {
    parts.push(
      typeof node === "string" ? (
        <React.Fragment key={++keyCounter}>{node}</React.Fragment>
      ) : (
        <React.Fragment key={++keyCounter}>{node}</React.Fragment>
      ),
    );
  };

  while (i < line.length) {
    const rest = line.slice(i);

    // line comment `// …`
    const cm = /^(\/\/[^\n]*)/.exec(rest);
    if (cm) {
      push(<span className="sg-json-c">{cm[1]}</span>);
      i += cm[1].length;
      continue;
    }

    // key: "…":
    const km = /^("(?:[^"\\]|\\.)*")(\s*):/.exec(rest);
    if (km) {
      push(<span className="sg-json-k">{km[1]}</span>);
      push(km[2]);
      push(":");
      i += km[0].length;
      continue;
    }

    // string value: "…"
    const sm = /^("(?:[^"\\]|\\.)*")/.exec(rest);
    if (sm) {
      push(<span className="sg-json-s">{sm[1]}</span>);
      i += sm[1].length;
      continue;
    }

    // boolean / null
    const bm = /^(true|false|null)\b/.exec(rest);
    if (bm) {
      push(<span className="sg-json-b">{bm[1]}</span>);
      i += bm[1].length;
      continue;
    }

    // number
    const nm = /^(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(rest);
    if (nm) {
      push(<span className="sg-json-n">{nm[1]}</span>);
      i += nm[1].length;
      continue;
    }

    // plain char
    push(line[i]);
    i += 1;
  }
  return parts;
}

function toPretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function buildLines(
  value: unknown | undefined,
  raw: string | undefined,
  comments: JsonViewCommentBundle | undefined,
): string[] {
  const out: string[] = [];
  if (comments?.before) out.push(`// ${comments.before}`);
  if (raw !== undefined) out.push(...raw.split("\n"));
  else if (value !== undefined) out.push(...toPretty(value).split("\n"));
  if (comments?.after !== undefined || comments?.afterValue !== undefined) {
    out.push("");
    if (comments.after) out.push(`// ${comments.after}`);
    if (comments.afterValue !== undefined) {
      out.push(...toPretty(comments.afterValue).split("\n"));
    }
  }
  return out;
}

export const JsonView = React.forwardRef<HTMLPreElement, JsonViewProps>(
  function JsonView({ value, raw, comments, className, ...rest }, ref) {
    const lines = React.useMemo(
      () => buildLines(value, raw, comments),
      [value, raw, comments],
    );
    return (
      <pre
        ref={ref}
        className={cn(
          "sg-inset rounded-sg-md p-3 font-mono text-[11.5px] leading-[1.65]",
          "text-sg-ink-2",
          "whitespace-pre overflow-x-auto",
          className,
        )}
        {...rest}
      >
        {lines.map((ln, idx) => (
          <React.Fragment key={idx}>
            {highlight(ln)}
            {"\n"}
          </React.Fragment>
        ))}
      </pre>
    );
  },
);

export default JsonView;
