/**
 * Tool renderer dispatcher. Looks up a specialized renderer by (lowercased)
 * tool name; falls back to the generic JSON view.
 */
import * as React from "react";

import { BashToolRenderer } from "./bash";
import { GenericToolRenderer, type ToolRendererProps } from "./generic";
import { GrepToolRenderer } from "./grep";
import { ReadFileToolRenderer } from "./read-file";
import { WebFetchToolRenderer } from "./webfetch";
import { WriteFileToolRenderer } from "./write-file";

export type { ToolRendererProps };
export { BashToolRenderer, GenericToolRenderer, GrepToolRenderer, ReadFileToolRenderer, WebFetchToolRenderer, WriteFileToolRenderer };

type RendererComponent = React.ComponentType<ToolRendererProps>;

const RENDERERS: Record<string, RendererComponent> = {
  bash: BashToolRenderer,
  shell: BashToolRenderer,
  read: ReadFileToolRenderer,
  read_file: ReadFileToolRenderer,
  readfile: ReadFileToolRenderer,
  view: ReadFileToolRenderer,
  write: WriteFileToolRenderer,
  write_file: WriteFileToolRenderer,
  edit: WriteFileToolRenderer,
  str_replace: WriteFileToolRenderer,
  webfetch: WebFetchToolRenderer,
  web_fetch: WebFetchToolRenderer,
  fetch: WebFetchToolRenderer,
  grep: GrepToolRenderer,
  search: GrepToolRenderer,
};

export function rendererForTool(toolName: string): RendererComponent {
  const key = toolName.toLowerCase().replace(/[\s-]+/g, "_");
  return RENDERERS[key] ?? GenericToolRenderer;
}
