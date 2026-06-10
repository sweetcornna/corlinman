"use client";

// Prism-light syntax highlighter with a curated language set, loaded via
// next/dynamic from markdown-message so the grammar bundle stays out of the
// main chunk. Colors come from CSS classes (.chat-md .token.*) defined in
// globals.css — token-driven, theme-aware, no inline hex.
import { PrismLight } from "react-syntax-highlighter";

import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import diff from "react-syntax-highlighter/dist/esm/languages/prism/diff";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import toml from "react-syntax-highlighter/dist/esm/languages/prism/toml";

const LANGS: Record<string, unknown> = {
  tsx,
  typescript,
  javascript,
  jsx,
  python,
  bash,
  json,
  yaml,
  markdown,
  css,
  sql,
  rust,
  go,
  diff,
  markup,
  toml,
};
for (const [name, grammar] of Object.entries(LANGS)) {
  PrismLight.registerLanguage(name, grammar);
}

const ALIASES: Record<string, string> = {
  ts: "typescript",
  js: "javascript",
  py: "python",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  yml: "yaml",
  md: "markdown",
  html: "markup",
  xml: "markup",
  svg: "markup",
  golang: "go",
};

export function normalizeLang(lang: string): string {
  const lower = lang.toLowerCase();
  return ALIASES[lower] ?? lower;
}

export default function CodeHighlighter({
  language,
  code,
}: {
  language: string;
  code: string;
}) {
  const lang = normalizeLang(language);
  const supported = lang in LANGS;
  return (
    <PrismLight
      language={supported ? lang : "markdown"}
      useInlineStyles={false}
      // PreTag/CodeTag keep our own classes; colors via .chat-md .token.*
      PreTag={(props) => (
        <pre
          {...props}
          className="overflow-x-auto px-3.5 py-3 font-mono text-[12.5px] leading-relaxed text-sg-ink"
        />
      )}
    >
      {code}
    </PrismLight>
  );
}
