/* The fetch wrapper: token header, JSON content type, error normalization. */

/* The token arrives in the URL FRAGMENT, which is never sent to the server,
   never written to an access log, and never leaked in a Referer. We read it,
   hold it in memory, and strip it from the address bar immediately.

   This runs at MODULE EVALUATION time, which is what keeps it ahead of
   store.readUrl(): main.js calls readUrl() from boot(), and boot() cannot run
   until every module in the graph — this one included — has been evaluated. So
   the fragment is already stripped, and `location.search` is intact, exactly as
   when both lived in one file. */
let token = "";
if (location.hash.startsWith("#t=")) {
  token = decodeURIComponent(location.hash.slice(3));
  history.replaceState(null, "", location.pathname + location.search);
}

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers["X-Brain-UI-Token"] = token;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  /* TWO PARSE FAILURES, TWO ANSWERS — and conflating them was a real defect.
     On an ERROR response, a body that is not JSON is expected and tolerated: a
     proxy, a gateway or Starlette itself can answer with HTML or plain text,
     and the error path below falls back to `response.statusText`. That swallow
     is deliberate.
     On a SUCCESSFUL response it is not tolerable. Every route under ui/ returns
     `JSONResponse` — verified, there is no 204 and no empty-body 200 — so a 2xx
     whose body will not parse is a broken server contract, and returning `null`
     made the app pretend it had data. The caller then destructures off `null`
     and throws a raw TypeError ("Cannot read properties of null") from wherever
     it happened to touch the payload, which is arbitrarily far from the cause;
     `loadFacets` swallows it entirely and the filter dropdowns just stay empty.
     Failing here names the actual problem at the point it is detectable. */
  let payload = null;
  let unparseable = false;
  try { payload = await response.json(); } catch (e) { unparseable = true; }
  if (!response.ok) {
    const err = new Error((payload && payload.error && payload.error.message) || response.statusText);
    err.code = (payload && payload.error && payload.error.code) || "http_error";
    err.status = response.status;
    err.payload = payload;
    throw err;
  }
  if (unparseable) {
    /* Same error SHAPE as the failure above — `code`, `status`, `payload` — so
       every existing caller's `catch (error) { toast(error.message) }` reports
       it without a new branch. */
    const err = new Error(
      `the server answered ${response.status} with a body that is not JSON`
    );
    err.code = "malformed_response";
    err.status = response.status;
    err.payload = null;
    throw err;
  }
  return payload;
}
