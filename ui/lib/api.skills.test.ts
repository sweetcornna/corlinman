/**
 * `updateInstalledSkill` API-client tests (W2.4 wire-up).
 *
 * The editable SkillDrawer round-trips a partial patch through
 * `PUT /admin/skills/{name}`. These tests lock the wire contract the
 * deployed `gateway/routes_admin_b/skills.py::update_skill` handler
 * expects:
 *
 *   - HTTP method is PUT
 *   - the skill name is path-encoded
 *   - `profile` rides as a query-string param (defaults to "default")
 *   - the body is JSON-serialised verbatim (the gateway honours
 *     `exclude_unset`, so only the keys we send are written back)
 *   - the parsed `InstalledSkillRow` JSON is returned
 *
 * `apiFetch` reads `GATEWAY_BASE_URL` from `process.env` at module-load,
 * so we don't stub the env (empty base = same-origin) and assert against
 * the raw relative path.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  updateInstalledSkill,
  type InstalledSkillRow,
  type SkillUpdateBody,
} from "./api";

const ROW: InstalledSkillRow = {
  name: "scratchpad",
  description: "Edited summary.",
  version: "1.0.0",
  state: "active",
  origin: "user",
  pinned: false,
  use_count: 0,
  last_used_at: null,
  created_at: null,
  body_markdown: "# scratchpad\nedited body\n",
  when_to_use: "when you need a scratch buffer",
  allowed_tools: ["web_search.query"],
  disable_model_invocation: true,
};

function mockFetchOnce(row: InstalledSkillRow) {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify(row), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("updateInstalledSkill", () => {
  it("PUTs the patch to the name-encoded path with the profile query", async () => {
    const fetchMock = mockFetchOnce(ROW);
    const patch: SkillUpdateBody = {
      description: "Edited summary.",
      disable_model_invocation: true,
    };

    const result = await updateInstalledSkill("scratchpad", patch, "default");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/admin/skills/scratchpad?profile=default");
    expect(init.method).toBe("PUT");
    expect(init.credentials).toBe("include");
    // Body is JSON-serialised verbatim — only the keys we sent.
    expect(JSON.parse(init.body as string)).toEqual({
      description: "Edited summary.",
      disable_model_invocation: true,
    });
    // The parsed row is returned through apiFetch.
    expect(result).toEqual(ROW);
  });

  it("path-encodes the skill name and threads a non-default profile", async () => {
    const fetchMock = mockFetchOnce(ROW);

    await updateInstalledSkill(
      "my skill/name",
      { when_to_use: "x" },
      "team-alpha",
    );

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(
      "/admin/skills/my%20skill%2Fname?profile=team-alpha",
    );
  });

  it("defaults the profile to `default` when omitted", async () => {
    const fetchMock = mockFetchOnce(ROW);

    await updateInstalledSkill("scratchpad", { body_markdown: "hi" });

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/admin/skills/scratchpad?profile=default");
  });
});
