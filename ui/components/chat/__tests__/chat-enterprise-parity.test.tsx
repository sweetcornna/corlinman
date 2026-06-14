/**
 * W6 ⑤ — chat enterprise-parity miscellany:
 *   1. conversation export → Markdown serialization (pure)
 *   2. message-edit failure keeps the editor open (covered in
 *      message-bubble.test.tsx)
 *   3. model-picker initial focus lands on the search input
 */
import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";

import { initI18n, i18next } from "@/lib/i18n";
import { exportTranscriptMarkdown } from "@/components/chat/chat-area";
import type { ChatMessage } from "@/lib/chat/types";

// zh-CN is the default test locale (see vitest.setup.ts); resolve `t` once.
const i18n = initI18n();
void i18next.changeLanguage("zh-CN");
const t = (key: string) => i18next.t(key) as string;

function msg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "user",
    content: "hello world",
    createdAt: Date.UTC(2026, 0, 2, 9, 15),
    ...overrides,
  };
}

describe("exportTranscriptMarkdown", () => {
  it("serializes role headings, content, and timestamps", () => {
    const md = exportTranscriptMarkdown(
      "My chat",
      [
        msg({ id: "u1", role: "user", content: "what is 2+2?" }),
        msg({ id: "a1", role: "assistant", content: "It is **4**." }),
      ],
      t,
    );
    expect(md).toContain("# My chat");
    // Role labels come from the zh-CN bundle.
    expect(md).toContain(`## ${t("chat.roleYou")}`);
    expect(md).toContain(`## ${t("chat.roleAssistant")}`);
    expect(md).toContain("what is 2+2?");
    expect(md).toContain("It is **4**.");
    // ISO timestamp from createdAt.
    expect(md).toContain("2026-01-02T09:15:00.000Z");
  });

  it("summarizes tool-call names for a turn", () => {
    const md = exportTranscriptMarkdown(
      "Tools",
      [
        msg({
          id: "a1",
          role: "assistant",
          content: "done",
          toolCalls: [
            {
              callId: "c1",
              toolName: "read_file",
              argsJson: "{}",
              status: "ok",
            },
            {
              callId: "c2",
              toolName: "run_shell",
              argsJson: "{}",
              status: "ok",
            },
          ],
        }),
      ],
      t,
    );
    expect(md).toContain(`${t("chat.exportToolCalls")}: read_file, run_shell`);
  });

  it("falls back to a generic title when none is given", () => {
    const md = exportTranscriptMarkdown("", [msg()], t);
    expect(md.startsWith("# Conversation")).toBe(true);
  });
});

// ── model-picker focus management ────────────────────────────────────────
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchModels: vi.fn(async () => ({ default: "", aliases: {} })),
    fetchModelsV2: vi.fn(async () => ({
      default: "",
      aliases: [],
      providers: [],
    })),
    fetchProviders: vi.fn(async () => []),
    getProviderModels: vi.fn(async () => ({ models: [] })),
  };
});

import { ChatModelPicker } from "@/components/chat/chat-model-picker";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
    </QueryClientProvider>
  );
}

describe("ChatModelPicker — focus management", () => {
  it("moves initial focus to the search input on open", async () => {
    render(
      wrap(
        <ChatModelPicker
          open
          onClose={() => {}}
          kind="llm"
          current="gpt-5"
          onPick={() => {}}
        />,
      ),
    );
    const search = screen.getByTestId("chat-model-picker-filter");
    await waitFor(() => {
      expect(document.activeElement).toBe(search);
    });
  });
});
