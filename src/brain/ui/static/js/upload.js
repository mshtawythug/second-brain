/* The raw upload request (RM-13, Ingest spec §5.2, §8).
 *
 * NOT through `api()`: that wrapper always sends JSON, and the upload is the one
 * request whose body is the file's own bytes, `application/octet-stream` (the
 * single Content-Type exception the server allows, §7.2). The `File` object is
 * handed to `fetch` as the body, never read into memory with a FileReader, so a
 * 25 MiB PDF is streamed by the browser rather than copied into the page.
 *
 * The name travels percent-encoded in `X-Brain-Filename`: a header must be
 * ASCII, and the server uses it only for its extension and display stem.
 * Responses and errors are read by api.js's `readResponse`, so a refusal has
 * the same `code` / `status` / `message` shape as every other call.
 */

import { api, getToken, readResponse } from "/static/js/api.js";

export async function uploadFile(file) {
  const headers = {
    "Content-Type": "application/octet-stream",
    "X-Brain-Filename": encodeURIComponent(file.name),
  };
  const token = getToken();
  if (token) headers["X-Brain-UI-Token"] = token;
  const response = await fetch("/api/ingest/uploads", {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: file,
  });
  return readResponse(response);
}

/* Drop a staged file the user removed. `{}` because every mutation needs JSON. */
export function discardUpload(uploadId) {
  return api(`/api/ingest/uploads/${encodeURIComponent(uploadId)}`, {
    method: "DELETE", body: {},
  });
}
