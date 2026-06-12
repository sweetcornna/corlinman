import { describe, expect, it } from "vitest";

import { transcriptToChatMessages } from "@/lib/chat/transcript";
import type { TranscriptMessage } from "@/lib/api/sessions";

const SESSION = "corlinman:test:abc123";

function row(
  role: TranscriptMessage["role"],
  content: string,
  overrides: Partial<TranscriptMessage> = {},
): TranscriptMessage {
  return {
    role,
    content,
    ts: "2026-06-11T08:00:00Z",
    ...overrides,
  };
}

function toolRow(name: string, content = ""): TranscriptMessage {
  return row("assistant", content, {
    tool_calls: [
      {
        id: `call_${name}`,
        type: "function",
        function: { name, arguments: "{}" },
        result: "ok",
      },
    ],
  });
}

describe("transcriptToChatMessages grouping", () => {
  it("merges consecutive assistant rows into one message (tool rounds + final text)", () => {
    // The journal writes one assistant row per reasoning round; live
    // streaming keeps the whole turn in one bubble. Replay must match live.
    const transcript: TranscriptMessage[] = [
      row("user", "do the thing"),
      toolRow("read_file"),
      toolRow("grep"),
      toolRow("write_file"),
      row("assistant", "done — wrote the file."),
    ];

    const out = transcriptToChatMessages(transcript, SESSION);

    expect(out).toHaveLength(2);
    expect(out[0].role).toBe("user");
    const turn = out[1];
    expect(turn.role).toBe("assistant");
    expect(turn.toolCalls?.map((tc) => tc.toolName)).toEqual([
      "read_file",
      "grep",
      "write_file",
    ]);
    expect(turn.content).toBe("done — wrote the file.");
    // id/createdAt anchor on the FIRST row of the run.
    expect(turn.id).toMatch(/_assistant$/);
  });

  it("joins multiple non-empty assistant contents with a blank line", () => {
    const out = transcriptToChatMessages(
      [
        row("assistant", "part one", { tool_calls: [] }),
        row("assistant", ""),
        row("assistant", "part two"),
      ],
      SESSION,
    );
    expect(out).toHaveLength(1);
    expect(out[0].content).toBe("part one\n\npart two");
  });

  it("does not merge assistant runs split by a user row", () => {
    const out = transcriptToChatMessages(
      [
        toolRow("read_file"),
        row("assistant", "first answer"),
        row("user", "follow-up"),
        toolRow("grep"),
        row("assistant", "second answer"),
      ],
      SESSION,
    );

    expect(out.map((m) => m.role)).toEqual(["assistant", "user", "assistant"]);
    expect(out[0].toolCalls).toHaveLength(1);
    expect(out[0].content).toBe("first answer");
    expect(out[2].toolCalls).toHaveLength(1);
    expect(out[2].content).toBe("second answer");
  });

  it("preserves per-row mapping: empty tool-call ids fall back per source row", () => {
    // Two rows whose tool calls lack ids must not collide after merging —
    // the fallback id embeds the source row's end-anchored index.
    const noId = (name: string): TranscriptMessage =>
      row("assistant", "", {
        tool_calls: [{ function: { name, arguments: "{}" } }],
      });
    const out = transcriptToChatMessages([noId("a"), noId("b")], SESSION);
    expect(out).toHaveLength(1);
    const ids = out[0].toolCalls?.map((tc) => tc.callId) ?? [];
    expect(new Set(ids).size).toBe(2);
  });

  it("keeps group ids stable when an earlier page is prepended", () => {
    // W5 "load earlier" PREPENDS older rows. Ids are end-anchored, so the
    // groups already on screen must keep their ids — otherwise React
    // remounts the whole list and scroll position is lost.
    const newerPage: TranscriptMessage[] = [
      row("user", "question"),
      toolRow("read_file"),
      toolRow("grep"),
      row("assistant", "answer"),
    ];
    const olderPage: TranscriptMessage[] = [
      row("user", "much earlier question"),
      row("assistant", "much earlier answer"),
    ];

    const before = transcriptToChatMessages(newerPage, SESSION);
    const after = transcriptToChatMessages(
      [...olderPage, ...newerPage],
      SESSION,
    );

    // The trailing messages (the ones that were already rendered) keep
    // both identity and shape; only new messages appear above them.
    expect(after).toHaveLength(before.length + 2);
    const tail = after.slice(-before.length);
    expect(tail.map((m) => m.id)).toEqual(before.map((m) => m.id));
    expect(tail.map((m) => m.toolCalls?.map((tc) => tc.callId))).toEqual(
      before.map((m) => m.toolCalls?.map((tc) => tc.callId)),
    );
  });

  it("maps transcript attachment sizes so file cards do not render as 0B", () => {
    const out = transcriptToChatMessages(
      [
        row("user", "see attached", {
          attachments: [
            {
              kind: "file",
              url: "/v1/files/abc123",
              name: "github_latest_projects.csv",
              mime: "text/csv",
              size: 12_345,
            },
          ],
        }),
      ],
      SESSION,
    );

    expect(out[0].attachments?.[0]).toMatchObject({
      kind: "document",
      name: "github_latest_projects.csv",
      mime: "text/csv",
      sizeBytes: 12_345,
      remoteUrl: "/v1/files/abc123",
    });
  });
});
