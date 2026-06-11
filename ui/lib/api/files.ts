/**
 * File API client — talks to the gateway's web-chat file store
 * (`POST /v1/files` + `GET /v1/files/{id}`, see
 * `gateway/routes/files.py`). The composer uploads a picked / dragged
 * file here, gets back a stable `{fileId, url, …}`, and embeds the
 * `url` as an attachment so the assistant can see it and the history
 * can render it after a refresh.
 *
 * Both calls ride the admin-session cookie (no API key) — the
 * `/v1/files` prefix is on the gateway's admin-session bridge — so we
 * just forward the cookie with `credentials: "include"`, exactly like
 * `streamChatCompletions` in `@/lib/api/chat`.
 */

import { CorlinmanApiError, GATEWAY_BASE_URL } from "@/lib/api";

/** Shape returned by `POST /v1/files` (camelCased from the wire's
 *  snake_case `{file_id, url, name, mime, size}`). */
export interface UploadedFile {
  fileId: string;
  /** Browser-fetchable serve URL. The wire value is server-relative
   *  (`/v1/files/<id>`); it is prefixed with `GATEWAY_BASE_URL` here so
   *  `<img>`/download links resolve in split-origin dev setups too —
   *  prod (same origin, empty base) is unaffected. Request building
   *  strips the prefix back off (`content-parts.attachmentRef`). */
  url: string;
  name: string;
  mime: string;
  size: number;
}

/** Wire shape of the `POST /v1/files` 201 body (snake_case). */
interface UploadFileResponse {
  file_id: string;
  url: string;
  name: string;
  mime: string;
  size: number;
}

/** Pull a human-readable message out of the gateway's
 *  `{"error": {code, message}}` envelope, falling back to the raw text
 *  / a status line — mirrors how `apiFetch` builds its error string. */
function errorMessage(raw: string, status: number): string {
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as { error?: { message?: string } };
      const msg = parsed?.error?.message;
      if (typeof msg === "string" && msg) return msg;
    } catch {
      // Not JSON — fall through to the raw body below.
    }
    return raw;
  }
  return `file upload failed: ${status}`;
}

/**
 * Upload one file to `POST /v1/files` and resolve to its stable
 * `{fileId, url, …}`. Throws {@link CorlinmanApiError} on any non-2xx,
 * carrying the gateway's error message + status + request id so the
 * caller can surface a localized failure.
 *
 * When `onProgress` is supplied the upload runs over `XMLHttpRequest`
 * (the only browser primitive that reports upload progress — `fetch`
 * has no upload-progress event); otherwise it takes the simpler
 * `fetch` path. Either way the body is one `multipart/form-data` part
 * named `file`, the field name the route reads.
 */
export async function uploadChatFile(
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<UploadedFile> {
  const form = new FormData();
  form.append("file", file);

  const wire = onProgress
    ? await uploadWithProgress(form, onProgress)
    : await uploadWithFetch(form);

  return {
    fileId: wire.file_id,
    url: wire.url.startsWith("/") ? `${GATEWAY_BASE_URL}${wire.url}` : wire.url,
    name: wire.name,
    mime: wire.mime,
    size: wire.size,
  };
}

/** `fetch` path — no upload-progress events, but the simplest wire. */
async function uploadWithFetch(form: FormData): Promise<UploadFileResponse> {
  const res = await fetch(`${GATEWAY_BASE_URL}/v1/files`, {
    method: "POST",
    credentials: "include",
    // NB: do NOT set content-type — the browser fills in the multipart
    // boundary itself; an explicit header would break the part parsing.
    body: form,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new CorlinmanApiError(
      errorMessage(text, res.status),
      res.status,
      res.headers.get("x-request-id") ?? undefined,
    );
  }
  return (await res.json()) as UploadFileResponse;
}

/** `XMLHttpRequest` path — reports `onProgress(0..1)` as the body
 *  streams up, then resolves with the same parsed response shape. */
function uploadWithProgress(
  form: FormData,
  onProgress: (fraction: number) => void,
): Promise<UploadFileResponse> {
  return new Promise<UploadFileResponse>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${GATEWAY_BASE_URL}/v1/files`);
    xhr.withCredentials = true;
    xhr.responseType = "text";

    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (e.lengthComputable && e.total > 0) {
        onProgress(Math.min(1, e.loaded / e.total));
      }
    };

    const traceId = (): string | undefined =>
      xhr.getResponseHeader("x-request-id") ?? undefined;

    xhr.onload = () => {
      const status = xhr.status;
      const body = xhr.responseText ?? "";
      if (status >= 200 && status < 300) {
        onProgress(1);
        try {
          resolve(JSON.parse(body) as UploadFileResponse);
        } catch {
          reject(
            new CorlinmanApiError(
              "file upload: malformed server response",
              status,
              traceId(),
            ),
          );
        }
        return;
      }
      reject(
        new CorlinmanApiError(errorMessage(body, status), status, traceId()),
      );
    };

    xhr.onerror = () => {
      // Network-level failure: no status, no body.
      reject(new CorlinmanApiError("file upload: network error", 0, traceId()));
    };
    xhr.onabort = () => {
      reject(new CorlinmanApiError("file upload aborted", 0, traceId()));
    };

    xhr.send(form);
  });
}
