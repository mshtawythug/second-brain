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
| `src/brain/ingest/gmail.py` | `_build_query` appends `-subject:"[brain brief]"` (+2 lines; budget +5). |
| `src/brain/doctor_delivery.py` | **New leaf.** The `brief delivery` doctor row, beside `doctor_watcher.py` (same shape: imports `doctor_runtime._check`, composed in lazily). |
| `src/brain/doctor_runtime.py` | The wiring only: one deferred import, one call, a docstring line (+5 of the shared +60 budget, §2.2). The first draft put the probe here (+37) and was moved out so the nine later probe tickets keep the budget. |
| `tests/delivery_isolation_plugin.py` | Forces `BRAIN_BRIEF_DELIVERY=none` per test, like `rerank_isolation_plugin.py`. Registered in `tests/conftest.py`. |
| Tests | `tests/test_settings_brief_delivery.py`, `tests/test_brief_delivery.py`, `tests/test_brief_publish_delivery.py`, `tests/test_delivery_isolation.py`, additions to `tests/test_cli_ingest_gmail.py` and `tests/test_doctor_runtime.py`. |
| Docs | `docs/configuration.md` (three knobs), `docs/cli-reference.md` (`brain brief --wiki` delivers), `skills/brain-proactivity/SKILL.md`, `CHANGELOG.md`, the backlog doc row, Appendix B in a follow-up commit. |

No file in the CLAUDE.md ceiling table is touched. `cli.py` and `_brain-brief-fg.sh` are unchanged.
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
  empty any more (`list_messages` always sends it).
- **D12, the test isolation plugin.** `build_brief` reads `os.environ` when no `delivery_env` is
  given (the CLI path). `tests/delivery_isolation_plugin.py` sets `BRAIN_BRIEF_DELIVERY=none` and
  deletes the other two knobs per test, so a developer's `.env` can never make the suite send mail.

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

## Risks

- Self-sent mail can be spam-filtered; the fixed subject lets the owner write a filter.
- `gws`'s send scope is not verified on this machine (ingest only reads). `bin/gws-reauth` asks for
  `gmail.modify`, which covers sending; the WARNING names it.
- The body is passed in argv, so another local user's `ps` could see it while `gws` runs (≤ 30 s).
  The machine is single-user; a stdin body would need a `gws` feature it does not document.
- With a broken channel the same hits are re-listed daily until it is fixed (D3, by design).
