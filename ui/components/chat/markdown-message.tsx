"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { useTranslation } from "react-i18next";
import { Check, Copy, ExternalLink, X } from "lucide-react";

import { cn } from "@/lib/utils";

// Grammar bundle stays out of the main chunk; while it loads (and while a
// message is still streaming) code renders as a plain <pre> with identical
// metrics, so there is no layout shift when highlighting lands.
const CodeHighlighter = dynamic(() => import("./code-highlighter"), {
  ssr: false,
  loading: () => null,
});

interface MarkdownMessageProps {
  content: string;
  streaming?: boolean;
  className?: string;
  onOpenArtifact?: (lang: string, source: string) => void;
}

export function MarkdownMessage({
  content,
  streaming,
  className,
  onOpenArtifact,
}: MarkdownMessageProps) {
  const [zoomSrc, setZoomSrc] = React.useState<string | null>(null);

  const components: Components = React.useMemo(
    () => ({
      p: ({ className: c, ...rest }) => (
        <p className={cn("my-2 text-[14px] leading-[1.7] text-sg-ink first:mt-0 last:mb-0", c)} {...rest} />
      ),
      h1: ({ className: c, ...rest }) => (
        <h1 className={cn("mt-5 mb-2 text-lg font-semibold tracking-tight text-sg-ink first:mt-0", c)} {...rest} />
      ),
      h2: ({ className: c, ...rest }) => (
        <h2 className={cn("mt-4 mb-1.5 text-base font-semibold tracking-tight text-sg-ink first:mt-0", c)} {...rest} />
      ),
      h3: ({ className: c, ...rest }) => (
        <h3 className={cn("mt-3 mb-1 text-sm font-semibold text-sg-ink first:mt-0", c)} {...rest} />
      ),
      ul: ({ className: c, ...rest }) => (
        <ul className={cn("my-2 list-disc space-y-1 pl-5 text-[14px] leading-[1.7] text-sg-ink marker:text-sg-ink-4", c)} {...rest} />
      ),
      ol: ({ className: c, ...rest }) => (
        <ol className={cn("my-2 list-decimal space-y-1 pl-5 text-[14px] leading-[1.7] text-sg-ink marker:text-sg-ink-4", c)} {...rest} />
      ),
      li: ({ className: c, ...rest }) => <li className={cn("leading-[1.7]", c)} {...rest} />,
      a: ({ className: c, ...rest }) => (
        <a
          className={cn("font-medium text-sg-accent decoration-sg-accent/40 underline-offset-2 hover:underline", c)}
          target="_blank"
          rel="noreferrer"
          {...rest}
        />
      ),
      strong: ({ className: c, ...rest }) => (
        <strong className={cn("font-semibold text-sg-ink", c)} {...rest} />
      ),
      blockquote: ({ className: c, ...rest }) => (
        <blockquote
          className={cn("my-3 border-l-2 border-sg-accent/40 bg-sg-accent-soft/40 py-1 pl-3 pr-2 text-sg-ink-2 [&>p]:my-1", c)}
          {...rest}
        />
      ),
      hr: ({ className: c, ...rest }) => (
        <hr className={cn("my-4 border-sg-border", c)} {...rest} />
      ),
      table: ({ className: c, ...rest }) => (
        <div className="my-3 w-full overflow-x-auto rounded-sg-md border border-sg-border">
          <table className={cn("w-full border-collapse text-[13px] text-sg-ink", c)} {...rest} />
        </div>
      ),
      th: ({ className: c, ...rest }) => (
        <th
          className={cn("border-b border-sg-border bg-sg-inset px-3 py-1.5 text-left text-[12px] font-semibold uppercase tracking-wide text-sg-ink-3", c)}
          {...rest}
        />
      ),
      td: ({ className: c, ...rest }) => (
        <td className={cn("border-b border-sg-border/60 px-3 py-1.5 last:border-b-0", c)} {...rest} />
      ),
      img: ({ className: c, src, alt, ...rest }) => (
        <button
          type="button"
          className="my-2 block cursor-zoom-in"
          onClick={() => src && setZoomSrc(String(src))}
          aria-label={alt || "image"}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={typeof src === "string" ? src : undefined}
            alt={alt ?? ""}
            loading="lazy"
            data-testid="md-image"
            className={cn("max-h-80 rounded-sg-md border border-sg-border object-contain shadow-sg-1", c)}
            {...rest}
          />
        </button>
      ),
      code: ({ className: c, children, ...rest }) => {
        const isInline = !c?.startsWith("language-");
        if (isInline) {
          return (
            <code
              className="rounded-md border border-sg-border/60 bg-sg-inset px-1.5 py-0.5 font-mono text-[12.5px] text-sg-accent-3"
              {...rest}
            >
              {children}
            </code>
          );
        }
        const lang = c?.slice("language-".length) || "";
        return (
          <CodeBlock language={lang} streaming={streaming} onOpenArtifact={onOpenArtifact}>
            {String(children).replace(/\n$/, "")}
          </CodeBlock>
        );
      },
      pre: ({ children }) => <>{children}</>,
    }),
    [onOpenArtifact, streaming],
  );

  return (
    <div className={cn("chat-md", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={components}>
        {content}
      </ReactMarkdown>
      {streaming ? (
        <span
          aria-hidden="true"
          className="ml-0.5 inline-block h-[15px] w-[7px] animate-pulse rounded-[2px] bg-sg-accent align-middle"
          data-testid="md-cursor"
        />
      ) : null}
      {zoomSrc ? <ImageLightbox src={zoomSrc} onClose={() => setZoomSrc(null)} /> : null}
    </div>
  );
}

/** Fullscreen image zoom — closes on click, Esc, or the corner button. */
function ImageLightbox({ src, onClose }: { src: string; onClose: () => void }) {
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      data-testid="md-image-lightbox"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6 backdrop-blur-sm"
      onClick={onClose}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label="close"
        className="absolute right-4 top-4 rounded-full bg-sg-inset p-2 text-sg-ink-2 hover:text-sg-ink"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={src} alt="" className="max-h-full max-w-full rounded-sg-lg object-contain shadow-sg-4" />
    </div>
  );
}

interface CodeBlockProps {
  language: string;
  children: string;
  streaming?: boolean;
  onOpenArtifact?: (lang: string, source: string) => void;
}

const ARTIFACT_AUTO_LANGS = new Set(["html", "svg", "mermaid"]);
const ARTIFACT_LINE_THRESHOLD = 25;

function CodeBlock({ language, children, streaming, onOpenArtifact }: CodeBlockProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = React.useState(false);
  const copy = React.useCallback(() => {
    void navigator.clipboard?.writeText(children).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }, [children]);

  const lineCount = React.useMemo(() => children.split("\n").length, [children]);
  const showArtifactCta =
    Boolean(onOpenArtifact) &&
    (ARTIFACT_AUTO_LANGS.has(language.toLowerCase()) ||
      lineCount >= ARTIFACT_LINE_THRESHOLD);

  return (
    <div
      className="group/code my-3 overflow-hidden rounded-sg-md border border-sg-border bg-sg-inset shadow-sg-1"
      data-testid="md-codeblock"
    >
      <div className="flex items-center justify-between border-b border-sg-border bg-sg-inset-strong px-3 py-1.5 text-[11px] text-sg-ink-4">
        <span className="font-mono lowercase tracking-wide">{language || "text"}</span>
        <div className="flex items-center gap-1">
          {showArtifactCta ? (
            <button
              type="button"
              onClick={() => onOpenArtifact?.(language, children)}
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-sg-ink-3 transition hover:bg-sg-inset-hover hover:text-sg-ink"
              aria-label={t("chat.mdCodeBlockOpenAriaLabel")}
              data-testid="md-codeblock-open-artifact"
            >
              <ExternalLink className="h-3 w-3" aria-hidden="true" />
              {t("chat.mdCodeBlockOpen")}
            </button>
          ) : null}
          <button
            type="button"
            onClick={copy}
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-sg-ink-3 transition hover:bg-sg-inset-hover hover:text-sg-ink"
            aria-label={copied ? t("chat.mdCodeBlockCopied") : t("chat.mdCodeBlockCopyAriaLabel")}
          >
            {copied ? (
              <Check className="h-3 w-3 text-sg-ok" aria-hidden="true" />
            ) : (
              <Copy className="h-3 w-3" aria-hidden="true" />
            )}
            {copied ? t("chat.mdCodeBlockCopied") : t("chat.mdCodeBlockCopy")}
          </button>
        </div>
      </div>
      {streaming ? (
        // Re-highlighting on every token delta janks long blocks; while the
        // message streams we render plain text with identical metrics.
        <pre className="overflow-x-auto px-3.5 py-3 font-mono text-[12.5px] leading-relaxed text-sg-ink">
          <code>{children}</code>
        </pre>
      ) : (
        <CodeHighlighter language={language} code={children} />
      )}
    </div>
  );
}
