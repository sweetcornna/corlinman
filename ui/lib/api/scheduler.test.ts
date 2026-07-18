/**
 * Scheduler API client corpus (W6 extensions in `lib/api/scheduler.ts`).
 *
 * Covers the line-shape of the mutation calls the QZone scheduler page
 * drives:
 *   - `patchSchedulerJob` PATCH method + URL + partial-body passthrough
 *     (incl. the forward-compat `image_ref_labels` / `jitter_minutes`
 *     fields) + refreshed-row parse + 404 rethrow
 *   - `deleteSchedulerJob` DELETE method + URL + no body + `{ ok, deleted }`
 *     parse + 404 rethrow
 *
 * `nextFireTime`'s day-of-week semantics are already locked in
 * `cron-schedule.test.ts` (#149) — not re-covered here.
 *
 * Mirrors the discipline of `personas.test.ts`: stub `globalThis.fetch`
 * with a recorder so we can both inspect what the client *sent* and
 * stage what the server replied with.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteSchedulerJob,
  patchSchedulerJob,
  type NewSchedulerJob,
  type SchedulerJobRow,
} from "./scheduler";

type FetchInit = RequestInit & { method?: string; body?: BodyInit | null };

interface RecordedCall {
  url: string;
  init: FetchInit;
}

function makeFetchStub(
  responder: (init: FetchInit) => Response | Promise<Response>,
): { fn: ReturnType<typeof vi.fn>; calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const safeInit = (init ?? {}) as FetchInit;
    calls.push({ url, init: safeInit });
    return responder(safeInit);
  });
  return { fn, calls };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Stable sample runtime row — mirrors the backend `JobOut` for a
 * `qzone.daily_publish` runtime job. Reused so a contract drift shows
 * up in one place. */
const SAMPLE_ROW: SchedulerJobRow = {
  name: "qzone-grantley",
  cron: "0 9 * * 1,3,5",
  timezone: "Asia/Shanghai",
  action_kind: "run_tool",
  next_fire_at: null,
  last_status: "ok",
  action_type: "qzone.daily_publish",
  enabled: true,
  persona_id: "grantley",
  prompt_template: "写点今天的碎碎念",
  qq_account: "10001",
  last_run_at_ms: 1_777_593_600_000,
  last_run_ok: true,
  last_qzone_url: "https://user.qzone.qq.com/10001/mood/abc",
  last_error: null,
  source: "runtime",
};

beforeEach(() => {
  vi.unstubAllGlobals();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("patchSchedulerJob", () => {
  it("PATCHes /admin/scheduler/jobs/{name} with the partial body", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, SAMPLE_ROW));
    vi.stubGlobal("fetch", fn);

    const row = await patchSchedulerJob("qzone-grantley", {
      cron: "30 8 * * *",
      prompt_template: "换个说法",
    });

    expect(calls[0]?.init.method).toBe("PATCH");
    expect(calls[0]?.url).toContain("/admin/scheduler/jobs/qzone-grantley");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({ cron: "30 8 * * *", prompt_template: "换个说法" });
    // Response parses into the refreshed row.
    expect(row.name).toBe("qzone-grantley");
    expect(row.action_type).toBe("qzone.daily_publish");
  });

  it("passes the forward-compat image_ref_labels / jitter_minutes through untouched", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, SAMPLE_ROW));
    vi.stubGlobal("fetch", fn);

    const patch: Partial<NewSchedulerJob> = {
      image_ref_labels: ["autumn-street", "cat"],
      jitter_minutes: 15,
    };
    await patchSchedulerJob("qzone-grantley", patch);

    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body.image_ref_labels).toEqual(["autumn-street", "cat"]);
    expect(body.jitter_minutes).toBe(15);
  });

  it("forwards an empty patch without dropping the body", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, SAMPLE_ROW));
    vi.stubGlobal("fetch", fn);

    await patchSchedulerJob("qzone-grantley", {});
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({});
  });

  it("encodes the job name so dotted/hyphenated names round-trip", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, SAMPLE_ROW));
    vi.stubGlobal("fetch", fn);

    await patchSchedulerJob("qzone.daily-a b", { enabled: false });
    expect(calls[0]?.url).toContain(
      "/admin/scheduler/jobs/qzone.daily-a%20b",
    );
  });

  it("rethrows on 404 (config jobs aren't editable here)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, {
        error: "not_found",
        resource: "runtime_scheduler_job",
        id: "missing",
      }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(
      patchSchedulerJob("missing", { cron: "* * * * *" }),
    ).rejects.toThrow();
  });

  it("rethrows on 422 (invalid cron)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(422, { error: "invalid_cron", message: "bad" }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(
      patchSchedulerJob("qzone-grantley", { cron: "nonsense" }),
    ).rejects.toThrow();
  });
});

describe("deleteSchedulerJob", () => {
  it("DELETEs /admin/scheduler/jobs/{name} with no body and parses the envelope", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { ok: true, deleted: "qzone-grantley" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await deleteSchedulerJob("qzone-grantley");
    expect(calls[0]?.init.method).toBe("DELETE");
    expect(calls[0]?.url).toContain("/admin/scheduler/jobs/qzone-grantley");
    expect(calls[0]?.init.body ?? undefined).toBeUndefined();
    expect(result).toEqual({ ok: true, deleted: "qzone-grantley" });
  });

  it("encodes the job name in the path", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { ok: true, deleted: "a b" }),
    );
    vi.stubGlobal("fetch", fn);

    await deleteSchedulerJob("a b");
    expect(calls[0]?.url).toContain("/admin/scheduler/jobs/a%20b");
  });

  it("rethrows on 404 (config jobs can't be deleted here)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, {
        error: "not_found",
        resource: "runtime_scheduler_job",
        id: "missing",
      }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(deleteSchedulerJob("missing")).rejects.toThrow();
  });
});
