import { afterEach, describe, expect, it, vi } from "vitest";

import { uploadChatFile } from "./files";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("uploadChatFile", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the original non-empty File under the multipart file field", async () => {
    const calls: RequestInit[] = [];
    const fetchStub = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(init ?? {});
      return jsonResponse(201, {
        file_id: "abc123",
        url: "/v1/files/abc123",
        name: "image.png",
        mime: "image/png",
        size: 11,
      });
    });
    vi.stubGlobal("fetch", fetchStub);

    const file = new File(["hello-bytes"], "image.png", { type: "image/png" });
    await uploadChatFile(file);

    expect(fetchStub).toHaveBeenCalledWith(
      "/v1/files",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
      }),
    );
    const body = calls[0]?.body;
    expect(body).toBeInstanceOf(FormData);
    const uploaded = (body as FormData).get("file");
    expect(uploaded).toBeInstanceOf(File);
    expect(uploaded).toBe(file);
    expect((uploaded as File).name).toBe("image.png");
    expect((uploaded as File).size).toBe(11);
  });

  it("sends the original File through the progress XHR path", async () => {
    let sentBody: XMLHttpRequestBodyInit | null = null;

    class FakeXMLHttpRequest {
      upload = {} as XMLHttpRequestUpload;
      status = 201;
      responseText = JSON.stringify({
        file_id: "abc123",
        url: "/v1/files/abc123",
        name: "image.png",
        mime: "image/png",
        size: 11,
      });
      withCredentials = false;
      responseType: XMLHttpRequestResponseType = "";
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;

      open = vi.fn();
      getResponseHeader = vi.fn(() => null);

      send(body?: XMLHttpRequestBodyInit | null): void {
        sentBody = body ?? null;
        this.onload?.();
      }
    }

    vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest);

    const file = new File(["hello-bytes"], "image.png", { type: "image/png" });
    await uploadChatFile(file, () => undefined);

    expect(sentBody).toBeInstanceOf(FormData);
    const uploaded = (sentBody as unknown as FormData).get("file");
    expect(uploaded).toBe(file);
    expect((uploaded as File).size).toBe(11);
  });
});
