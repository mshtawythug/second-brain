# RM-16 — Brief delivery: email to self (or a private Slack webhook), opt-in

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

The ticket is the spec (`docs/specs/2026-10-23-roadmap-completion-design.md`, RM-16; §2.3 settings
leaves, §2.5 egress rules). This plan is the task list, the decisions the ticket left open, and the
risks. Branch `feat/rm-16-brief-delivery`, from `second-brain/master` at `92438fe` (PR #78 merged).

## Files

| File | Change |
|---|---|
| `src/brain/settings/brief_delivery.py` | **New leaf.** `BriefDeliverySettings` (`channel`, `include_todos`, `recipient`, `slack_webhook_url` with `repr=False`) and `load_brief_delivery_settings(env)`. |
| `src/brain/brief_delivery.py` | **New.** `DeliveryChannel` Protocol, `DeliveryError` / `DeliveryAuthError`, `GwsEmailChannel`, `SlackWebhookChannel`, `run_gws` (the real runner), `render_delivery`, `deliver_brief`, `DeliveryOutcome`, `make_channel`. |
| `src/brain/brief_publish.py` | `build_brief` delivers the published payload after the page is written, and marks watch hits notified only when delivery did not fail. Two keyword seams: `delivery_env`, `delivery_channel`. |
| `src/brain/ingest/gmail.py` | `_build_query` appends `-(from:me subject:"[brain brief]")`, and `list_messages` always sends `q` (the dead `if q:` branch is gone). Net +2 against `92438fe` (669 → 671; budget +5). |
| `src/brain/cli_ingest.py` | `ingest-gmail`'s help names the skipped `[brain brief]` mail: one sentence reworded in place, **net-zero** (a CLAUDE.md ceiling-table file; its row gains a descriptive hop). |
| `src/brain/doctor_delivery.py` | **New leaf.** The `brief delivery` doctor row, beside `doctor_watcher.py` (same shape: imports `doctor_runtime._check`, composed in lazily). |
| `src/brain/doctor_runtime.py` | The wiring only: one deferred import, one call, a docstring line (+5 of the shared +60 budget, §2.2). The first draft put the probe here (+37) and was moved out so the nine later probe tickets keep the budget. Net +4 against `92438fe` (563 → 567). |
| `tests/delivery_isolation_plugin.py` | Forces `BRAIN_BRIEF_DELIVERY=none` per test, like `rerank_isolation_plugin.py`. Registered in `tests/conftest.py`. |
| Tests | `tests/test_settings_brief_delivery.py`, `tests/test_brief_delivery.py`, `tests/test_brief_publish_delivery.py`, `tests/test_delivery_isolation.py`, additions to `tests/test_cli_ingest_gmail.py` and `tests/test_doctor_runtime.py`. |
| Docs | `docs/configuration.md` (three knobs), `docs/cli-reference.md` (`brain brief --wiki` delivers), `skills/brain-proactivity/SKILL.md`, `CHANGELOG.md`, the backlog doc row, Appendix B in a follow-up commit. |

One file in the CLAUDE.md ceiling table is touched, `cli_ingest.py`, net-zero (review round 1). `cli.py` and
`_brain-brief-fg.sh` are unchanged.
No migration.

## Decisions

- **D1, channel (RM-16.D1, the ticket's):** `none` (default) | `email` (through `gws gmail +send`,
  to `BRAIN_USER_EMAIL` only) | `slack` (an incoming webhook the owner binds to a private channel or
  a DM).
- **D2, the Slack POST goes through the transport, not `httpx.Client`.** `httpx.Client` logs every
  request at INFO as `HTTP Request: POST <full url>` on the `httpx` logger, and the webhook URL's
  path IS the secret. `SlackWebhookChannel` builds an `httpx.Request` and calls
  `transport.handle_request` directly (`httpx.HTTPTransport` in production, `httpx.MockTransport` in
  tests), so no httpx logger ever sees the URL. The timeout (10 s) rides on the request's `timeout`
  extension. A transport does not follow redirects, so the POST cannot be redirected elsewhere.
  The sentinel test runs with every logger at DEBUG.
- **D3, a failed delivery leaves the watch hits unnotified.** RM-15 marks every unnotified hit the
  build read once the page is written. With delivery configured and failing, that would mark hits
  the owner was never sent. So `build_brief` marks only when delivery is off or succeeded; when it
  failed, the hits stay pending, and the next brief lists and sends them again (at-least-once). The
  page written today still lists them. Delivery itself never calls `mark_notified`, and the marked
  set is still exactly `view.pending`, so RM-15's AC6 holds whenever delivery is off or works. A
  configuration error (a bad `BRAIN_BRIEF_DELIVERY` value, a non-Slack URL) counts as a failure: the
  owner asked for delivery and did not get it.
- **D4, the recipient is one address.** `gws gmail +send --to` takes a comma-separated list, so a
  `BRAIN_USER_EMAIL` of `a@x,b@y` would send the brief to a second person. The leaf refuses a value
  with a comma, whitespace, angle brackets or no single `@` (`ConfigError` naming the variable). At
  send time `build_brief` also requires `settings.recipient == cfg.user_email` (both read
  `BRAIN_USER_EMAIL`; a mismatch is refused, not sent).
- **D5, suggestions are body-derived.** `suggest_next_steps` builds its prompt from todo TEXT, so
  its output is body-derived (§2.5 rule 3). Suggestions are sent only with
  `BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS=true`, like the todo text. By default the message carries the
  open-todo COUNT only.
- **D6, what is sent.** The gated payload `build_brief` writes to the page, never the terminal one:
  the date, the counts, capture titles, pinned titles and the watch section, through the same
  `brief.watch_section_lines` the page uses (so the page and the message cannot word a hit
  differently, and the D14 fetch-limit line, never on the page, is never sent). Then "Open `brain
  brief` for detail." RM-17's entities section does not exist yet; RM-17 adds it to
  `render_delivery`.
- **D7, Slack escaping.** Slack's `text` treats `<…>` as a link or mention (`<!channel>`), so `&`,
  `<` and `>` are escaped (Slack's documented three). Email is plain text, unescaped. Control
  characters and newlines inside a title are collapsed to spaces in both, so a title cannot forge a
  line.
- **D8, the gws runner is new, not `vault.derived_links.real_gws_runner`.** That runner's timeout
  message joins the argv, which here holds the message body. `run_gws` raises `DeliveryError` with a
  fixed message and a content-free `log_detail` (`exit 2`, `timed out after 30s`, `gws CLI not
  found on PATH`). gws's stderr is read only to classify an authorization failure
  (`DeliveryAuthError`), never kept. The binary is found with `shutil.which` (soft dependency).
- **D9, failure never fails the brief.** `deliver_brief` catches `DeliveryError`, and
  `build_brief` catches the leaf's `ConfigError`; each logs ONE class-only WARNING
  (`describe_failure`). An authorization failure's WARNING names `bin/gws-reauth`; a configuration
  error's names `brain doctor`, which prints the message (variable names only, never values).
- **D10, the doctor row** reports the channel and its prerequisite: `off`; `email` with `gws` on
  `PATH` or not; `slack` with the webhook set (never the URL); a configuration error as a WARN with
  the leaf's message. It never fails doctor.
- **D11, the Gmail exclusion** is appended to every query `_build_query` returns, so `q` is never
  empty any more (`list_messages` always sends it). Since review round 1 it is
  `-(from:me subject:"[brain brief]")`: only the owner's own brief mail is skipped.
- **D12, the test isolation plugin.** `build_brief` reads `os.environ` when no `delivery_env` is
  given (the CLI path). `tests/delivery_isolation_plugin.py` sets `BRAIN_BRIEF_DELIVERY=none` and
  deletes the other two knobs per test, so a developer's `.env` can never make the suite send mail.
- **D13, drafts are never delivered** (sec-82 1). The published payload lists draft documents (no
  brief reader filters `documents.draft`), and that is the owner's own vault page. What LEAVES the
  machine drops them: `_deliver` looks up which of the payload's documents are drafts (one
  parameterized query, only when delivery is on) and `brief_delivery.without_drafts` removes their
  captures, pins, todos and watch hits, so the counts drop with them. A failed lookup skips delivery
  (fail closed). A watch's "and N more" count may still include drafts: it is a count, not a title.
  The suggestions were derived from every todo and cannot be traced to one, so when a draft's todo
  is removed the suggestions are dropped too (round 2, R2-1); they are not re-derived, which would
  put an LLM call on the delivery path.
- **D14, delivery off is decided by `BRAIN_BRIEF_DELIVERY` alone** (rev-82 H1). `_deliver` reads
  `load_delivery_channel` first and returns, silently, when it is `none`; only then does it load
  and validate the other knobs. `brain doctor` still validates them all and WARNs.

## TDD order

1. Settings leaf: defaults, each choice, the Slack prefix, the email-needs-`BRAIN_USER_EMAIL` rule,
   the one-address rule, the URL never in a message or `repr`.
2. `render_delivery`: subject, gated content, no todo text or suggestions by default, both with the
   opt-in, escaping, newline collapse, the watch section.
3. Channels: an injected runner (exact argv), the real `run_gws` against a fake `gws` script on a
   tmp path (success, exit code, auth stderr, timeout, missing), `httpx.MockTransport` (one POST,
   JSON body, non-2xx, transport error, timeout extension).
4. `deliver_brief`: off sends nothing and logs nothing; failure → one WARNING; auth → names
   `bin/gws-reauth`.
5. `build_brief`: a fake channel gets the gated payload; failure leaves the page and the hits
   unnotified; success and off mark; a `ConfigError` degrades; no delivery without `wiki`.
6. Sentinel: the webhook URL in no log record (every logger at DEBUG), no exception message, no
   `repr`.
7. Gmail query builder; doctor row; isolation plugin.

## Acceptance criteria → tests

| AC | Test |
|---|---|
| 1 `none` sends and prints nothing | `test_delivery_off_sends_nothing_and_logs_nothing`, `test_build_brief_with_delivery_off_calls_no_channel` |
| 2 one `gws gmail +send`, `--to` the owner, fixed subject, no confidential title or todo | `test_email_channel_runs_one_gws_send_to_the_owner`, `test_build_brief_delivers_the_gated_payload_only` |
| 3 one POST through `MockTransport`; a non-Slack URL is a `ConfigError` | `test_slack_channel_posts_once`, `test_a_non_slack_webhook_url_is_a_config_error` |
| 4 a failing channel: page written, one WARNING, auth names `bin/gws-reauth` | `test_build_brief_survives_a_failing_channel`, `test_an_auth_failure_warning_names_gws_reauth` |
| 5 the URL in no log line or error | `test_webhook_url_never_reaches_a_log_record_or_an_error` |
| 6 every Gmail query excludes the subject | `test_every_gmail_query_excludes_the_brief_subject` |

## Deviations

1. **No SMTP.** The ticket (RM-16.D1, G8) specifies email through the existing `gws` CLI, so there is no SMTP code and no new dependency. Email tests use an injected runner or a fake `gws` script on a tmp path.
2. **The Slack POST goes through httpx's transport, not `httpx.Client`** (the ticket says "httpx POST"): `Client` logs the full URL at INFO, and the URL is the secret (D2).
3. **Additions the ticket does not list:** D3 (a failed delivery leaves watch hits unnotified), D4 (one recipient, no leading `-`), D5 (suggestions count as body text), D7 (Slack escaping and one-line titles), D13 (drafts are never delivered), link previews off, and the doctor row in its own leaf (`doctor_delivery.py`) to save `doctor_runtime.py`'s shared budget.
4. **`tests/test_doctor_runtime.py::test_all_healthy_yields_no_failures`** enumerates the exact runtime-check list; it now includes `brief delivery`.
5. **RM-17's entities section does not exist yet**; RM-17 adds it to `render_delivery`.
6. **Synthetic `hooks.slack.com` test URLs are assembled from parts.** The leaf must accept that host and AC3 needs a valid URL, but GitHub push protection rejects Slack's documented placeholder as a literal. Non-Slack cases use `hooks.example.invalid`. Nothing in the tests or the E2E run contacts Slack or Google.
7. **D11 renames an existing test.** `tests/test_cli_ingest_gmail.py::test_list_messages_no_scope_omits_q_param` encoded the old "no `q`" behaviour that AC6 removes; it is now `test_list_messages_no_scope_sends_only_the_brief_exclusion` and asserts `q == BRIEF_EXCLUSION`.
8. **Drafts are removed at the delivery-payload boundary, not from the local page** (sec-82 1, D13). The page is the owner's own vault note and keeps listing drafts as before; only what leaves the machine drops them.
9. **The email body stays in `gws`'s argv** (sec-82 4). `gws gmail +send --help` offers the body only as `--body <TEXT>`, with no stdin or file option, so the body is visible to a local `ps` for at most the 30 s timeout. Accepted on a single-user machine.
10. **The DNS lookup is not covered by the 10 s Slack timeout** (sec-82 7). httpx's timeout bounds connect, read, write and pool; `getaddrinfo` runs before it and is bounded by the OS resolver. Accepted, no code change.

## Risks

- Self-sent mail can be spam-filtered; the fixed subject lets the owner write a filter.
- `gws`'s send scope is not verified on this machine (ingest only reads). `bin/gws-reauth` asks for
  `gmail.modify`, which covers sending; the WARNING names it.
- The body is passed in argv, so another local user's `ps` could see it while `gws` runs (≤ 30 s);
  see Deviations 9.
- The DNS lookup is outside the Slack timeout; see Deviations 10.
- With a broken channel the same hits are re-listed daily until it is fixed (D3, by design).

## Review round 1 (rev-82, aud-82, sec-82 at `8200c1d`)

- H1: D14. Delivery off is silent whatever the other knobs say.
- H2 (+ aud 1, sec 2): `SlackWebhookChannel.send` builds the request and the transport inside the
  guard and maps `httpx.HTTPError`, `httpx.InvalidURL` and `OSError` (a bad `SSL_CERT_FILE`, an SSL
  error) to `DeliveryError`; it closes only a transport it built.
- aud 1b: the leaf refuses whitespace, control characters and DEL anywhere in the webhook URL,
  requires `httpx.URL(raw).host == "hooks.slack.com"`, and caps the URL at httpx's own 65,536
  characters (so a longer path is refused too); any httpx parse error is the same `ConfigError`.
- aud 4: `run_gws` decodes with `errors="replace"`.
- sec 1: D13. sec 3: `gws` argv glues each value to its flag (`--to=…`, `--subject=…`, `--body=…`),
  and the recipient cannot start with `-`. sec 4: Deviations 9. sec 5: the Slack body sets
  `unfurl_links` and `unfurl_media` to false. sec 6: D11's narrower exclusion, named in
  `ingest-gmail`'s help and `docs/cli-reference.md`. sec 7: Deviations 10.
- aud 2: the Slack host gate is pinned by `api.slack.com`, `evilhooks.slack.com` and
  `hooks.evil-slack.com` cases; the `endswith("slack.com")` mutation dies.
- **Two of aud 1b's layers are redundant by design, and a mutation shows it.** Dropping the
  `httpx.URL(raw).host` comparison alone survives: `urlsplit`'s hostname check refuses every case
  once whitespace, control characters, userinfo and a port are refused, and no parser differential
  between the two is known. Dropping the explicit 65,536-character cap alone survives too: httpx
  refuses a longer URL itself (`InvalidURL`), which the leaf maps to the same `ConfigError`. Both
  stay as defence in depth, as the fix list asked.
- Cleanups: the dead `if q:` branch, a stray blank line in `doctor_runtime.py`, the line deltas
  recomputed (Files table).
