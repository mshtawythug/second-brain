# Wiki → `brain ui` consolidation — phase 7 (R1, Demote) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

*Written 2026-10-12 against `master` @ `1a1d068` (phase 5 merged). Contract:
`docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md` §1.3 (R1), §8.3 (A7), §10.2 R-4,
§11 (phase-7 row), §12 Q1. Branch: `feat/wiki-to-ui-phase7`. Every count below is SHA-bound or
replaced by the command that derives it.*

## 0. What phase 7 is, in one paragraph

§1.3 defines **R1 — Demoted**: "`brain ui` is primary. The wiki still builds and serves; nothing
in the daily loop points at it." §11's phase-7 row: *"Stop pointing the daily loop at the wiki.
Nothing deleted. Gate: A7 has landed. Exit: User reads in `brain ui` by default."* Q1 (R-4) is
answered by the user (2026-10-12): **LAN phone reading is replaced by a documented
`brain ui --host`/`--token`**; a small convenience is allowed if justified, auth stays, and
`brain ui` never binds a non-loopback address without a token. **Out of scope:** phases 8 and 9 —
nothing is deleted, no launchd job is installed or removed, no Quartz build step is removed.

## 1. The A7 gate — landed, with evidence

A7 (§8.3 G1) asked for "a client-side draft buffer (`localStorage`) that preserves unsaved text
across a 409, plus a way to see both versions". **Phase 4 closed G1 by a different mechanism, on
measured evidence**, and that mechanism is what this plan treats as the gate:

| Piece | Where | Test |
|---|---|---|
| The finding that redirected the fix: a 409 never discarded the edit; the toast's advice to reopen did | `src/brain/ui/static/js/inspector.js:33-38` | `tests/test_ui_browser.py:1093` `test_a_conflict_keeps_the_editor_and_the_users_text` |
| Consent before the reopen that destroys the draft | `inspector.js:48-52` (`openNote`), `:382` `confirmDiscardDraft` | `tests/test_ui_browser.py:1107` `test_reopening_with_unsaved_changes_asks_before_discarding`; over-reach guard `test_opening_a_note_with_NO_unsaved_changes_does_not_prompt` |
| A save path that keeps the text: "Replace on disk" with a fresh hash | `inspector.js:408` `overwriteOnDisk`, wired `:483` | `tests/test_ui_browser.py:1143`, `:1172` (typing during the round trip), `:1245` (the confirm names the file and what is lost) |
| Landing commit | `git log --oneline -S confirmDiscardDraft -- src/brain/ui/static/js/inspector.js` → `3b16527` | — |

**Recorded deviation, not a gap for R1:** there is no `localStorage` buffer, so a *tab close* or
*crash* mid-edit still loses unsaved text, and "see both versions" is reduced to the confirm naming
the file. G1 was specifically the 409 path ("Losing writing is the fastest way to make the user
distrust the new surface") and that path is closed. A crash-survival buffer is a separate
feature; it is listed in §7 as a known limit, not built here.

## 2. Inventory — every place the daily loop points at the wiki

Method: `grep -rn -i -E "wiki_url|wiki_port|WIKI_PORT|:8080|wiki url|quartz ui|show ingested|the wiki" src/brain bin`
(excluding `src/brain/wiki/`, `quartz_overrides/`, migrations), the same over `README.md`, `docs/`,
`skills/`, `AGENTS.md`, plus reading `src/brain/templates/bin/*.sh`,
`src/brain/templates/launchd/com.brain.brief.plist.j2` and `cli_ui.py`. "wiki-link"/`[[…]]`
hits are vault *format*, not pointers at the wiki, and are excluded — `brain ui` renders them
(`src/brain/ui/render.py:24`, inline rule) and changing the format is R4, a non-goal.

| # | Surface | What it does today | Decision | Reason |
|---|---|---|---|---|
| 1 | **`brain-up`** (`src/brain/templates/bin/brain-up.sh:202-212`, banner `:215-221`) | Waits for the wiki, then **opens `http://localhost:8080` in a browser by default** (`BRAIN_OPEN_BROWSER` defaults to `1`); banner's first line is `wiki: <url>` | **REPOINT.** Opening the wiki becomes opt-in (`BRAIN_OPEN_BROWSER=1`); the banner leads with `read: brain ui` and labels the wiki line legacy | The single strongest daily-loop pointer: every `brain-up` puts the wiki in front of the user. `brain ui` is foreground-only by design (`ui/server.py` `serve` docstring), so `brain-up` cannot start it — it names it |
| 2 | **`brain setup` final report** (`src/brain/setup.py:921-923`, next steps `:929-932`) | Prints `wiki url` + `start wiki: caddy run …`; the next steps never mention `brain ui` | **REPOINT.** A `read: brain ui` line printed **ahead of** the wiki block; the wiki URL stays (still installed), labelled legacy. *(Review round 1, M-2: the first version added `brain ui` to the next steps, BELOW the wiki URL, while the module docstring claimed "ahead of" — corrected to the order the docstring states, and the test now asserts the order.)* | The first thing a new install reads. Wiki lines go with Q8/phase 9 |
| 3 | **`brain capture` callback docstring** (`src/brain/_capture_command.py:68-69`) | "visible in the Quartz UI without the "Show ingested" toggle" | **REPOINT** to `brain ui`'s tree (which has the same toggle — `ui/static/js/tree.js:38-57`) | *Corrected during execution:* this row first said `brain capture --help` shows the sentence. It does not — `capture_app` passes `help=` to `typer.Typer`, which overrides the callback docstring (measured: `brain capture --help` contains no "Quartz"). It is still repointed, net zero, because it is the MCP description's twin (#4); the test pins the docstring and says why |
| 4 | **MCP `brain_capture` description** (`src/brain/mcp_server.py:1652`) | Same sentence, shown to every MCP client | **REPOINT**, net-zero lines | Same; MCP descriptions are a listed candidate |
| 5 | **README** (`README.md:14`, `:143-149`) | Lists the wiki beside `brain ui` as equals; documents `brain ui --host 0.0.0.0 --token <secret>` | **REPOINT + FIX.** `brain ui` named the reading surface, wiki "legacy, still builds"; the LAN recipe replaced (see §3 — the documented one **cannot serve a phone**) | |
| 6 | **`docs/cli-reference.md` `## brain ui`** (`:708-724`) | Same broken `0.0.0.0` recipe | **FIX + DOCUMENT** `--lan`, link the phone guide | |
| 7 | **`docs/vault-and-wiki.md`** intro (`:5-7`) and `## Wiki (rendered view, optional)` (`:136`) | Presents the wiki as the way to view the vault | **DOCUMENT.** One status note: `brain ui` is primary; the wiki still builds and serves until R2. No section removed (§9.5 splits the file at R3) | |
| 8 | **`docs/README.md`** index (`:9`) | Describes vault-and-wiki only | **DOCUMENT.** Add the new phone guide row | |
| 9 | `brain brief --wiki` + launchd `com.brain.brief` (`templates/bin/_brain-brief-fg.sh:38`) | Writes `<vault>/daily/<YYYY>/<date>-brief.md` and prints its **path**. The plist (`com.brain.brief.plist.j2`) has **no notification** — verified, no `osascript`/URL — only log redirection | **LEAVE** | Prints a vault path, not a wiki URL; the page is a vault note `brain ui` renders once the watcher syncs it. The flag name `--wiki` is a misnomer, but renaming it breaks the installed launchd wrapper — that is launchd work, phase 8 |
| 10 | `brain review weekly` | Writes `<vault>/reviews/<week>.md`, prints path | **LEAVE** | Same as #9 |
| 11 | `brain doctor` wiki-freshness check (`doctor_runtime.py:654-702`) | FAILs on a stale Quartz build | **LEAVE** | The wiki still builds in R1, so a stale build is still a true failure. §9.5 removes it at R2/R3. It names no reading URL |
| 12 | `brain-status` (`templates/bin/brain-status.sh:79-83`), `brain-monitor` (`src/brain/bin/monitor.py:257-259`), `brain-down` (`templates/bin/brain-down.sh:89`) | Report wiki URL reachability / Caddy | **LEAVE** | Diagnostics of a daemon that still runs; they report, they do not direct reading. §9.5 deletes them |
| 13 | `brain wiki install`, `brain setup --wiki-port/--skip-wiki`, setup prompt "Install the wiki UI?" (`setup.py:762`) | Install-time | **LEAVE** | Install-time, not the daily loop; Q8 (phase 8) and phase 9 own them |
| 14 | `brain ui` Publish tab copy (`ui/static/index.html`) | Says Publish is deferred because the wiki is a Quartz build | **LEAVE** | Q10, phase 8 (§9.6: false only at R2) |
| 15 | Skills: `consult-brain`, `brain-graph` | No wiki reading pointer (grep returns 0 non-wikilink hits) | **LEAVE** | Agents read through CLI/MCP, not a browser |
| 16 | Skills: `brain-maintenance` "Wiki build" section, `brain-authoring` draft quarantine ("hidden from the wiki"), `brain-proactivity` `--wiki` flag, `brain-todo`, `ingest-brain`, `elicit-brain` | Describe wiki *maintenance* or the wiki's draft filter | **LEAVE** | Accurate while the wiki builds; none tells the user to read there. `brain-maintenance`'s `bin/brain-up` line stays true after #1 |
| 17 | Other MCP descriptions (`mcp_server.py:82,2638-2666`, F6 notes) | Say Quartz serves review pages to another audience | **LEAVE** | Security rationale that is still true while the wiki serves |
| 18 | Config | No "reading URL" setting exists; `BRAIN_WIKI_PORT` is read only by the bin scripts and monitor | **N/A** | Nothing to repoint |
| 19 | `brain --help` root and `brain ui --help` | Root help names no surface; `ui` help is neutral | **LEAVE** | Not a wiki pointer |
| 20 | `docs/configuration.md:163,167`, `AGENTS.md`, `CLAUDE.md` | Operator / contributor docs about the build | **LEAVE** | Not the daily reading loop |

## 3. Q1 — reading from a phone

**A measured defect in the recipe the docs ship today.** `README.md:148` and
`docs/cli-reference.md:713` document `brain ui --host 0.0.0.0 --token <secret>`. That bind
cannot serve another device: `ui/context.py` `host_allowlist("0.0.0.0")` returns the
**loopback** names, so `TrustedHostMiddleware` rejects a phone's `Host: <lan-ip>` header.
Measured 2026-10-12 against `create_app` through `TestClient`: bind `0.0.0.0`, `Host:
192.0.2.10` → **400 Invalid host header**; bind `192.0.2.10`, same request → **200**. Mutations
also fail on a `0.0.0.0` bind from anywhere, because `allowed_origin` becomes
`http://0.0.0.0:<port>`, an origin no browser sends. The allowlist is right — widening it is the
DNS-rebinding hole `host_allowlist`'s docstring names — so the fix is the recipe, not the guard.

**The working recipe** is `brain ui --host <this machine's LAN IP> --token <secret>`: the
allowlist and the origin both become that IP.

**Ruling Q1-a — add `--lan`, and justify it.** `brain ui --lan`:
- binds the machine's primary LAN IPv4, found with the standard UDP-`connect` routing probe
  (no packet is sent), in a new `src/brain/ui/lan.py`; refuses with a remediation if the probe
  fails or yields loopback/unspecified;
- uses `--token` if given, otherwise **generates** one (`secrets.token_urlsafe(24)`), so the
  non-loopback-needs-a-token rule can never be skipped by `--lan`;
- is mutually exclusive with `--host` (usage error, exit 2);
- prints the phone URL `http://<lan-ip>:<port>/#t=<token>` — the existing panel already prints
  the `#t=` form for any non-loopback bind.

Justification: (1) the obvious recipe silently fails (above); (2) finding the LAN IP by hand is
the friction that would push the user back to the zero-config wiki; (3) a generated token keeps a
weak or reused secret out of shell history. It adds one flag and one small module
(`wc -l src/brain/ui/lan.py` — re-derive, do not inherit a digit), no dependency.

**Ruling Q1-b — warn on `--host 0.0.0.0` / `::`.** Not an error (it still serves the machine
itself on `localhost`), but a startup notice naming `--lan` / `--host <LAN IP>`, so the trap is
visible at the moment someone walks into it.

**Not changed:** the token is held **in memory only** (`ui/static/js/api.js:12-16`, stripped from
the address bar at load). A phone that reloads the tab, or a bookmark taken from the address bar
after load, has no token and every API call returns 403. Persisting it (e.g. `sessionStorage`) is
a security decision this phase does not make; the guide documents the behaviour and §7 records
it as a limit to raise with the user.

**The guide** — `docs/guides/brain-ui-on-your-phone.md`: the `--lan` command and the manual
`--host <ip> --token` form; where the token comes from and how it travels (URL fragment → header;
never in a URL query or log); caveats — plain HTTP, so trusted networks only and the token is
readable on the wire; anyone with the URL reads the brain; confidential bodies are withheld off
loopback unless `--include-confidential`; `--read-only` recommended for phone reading; the Mac
must keep `brain ui` running in a foreground terminal (no daemon, by design); macOS may prompt to
allow incoming connections; use the printed IP URL, not `<name>.local` (the allowlist holds the
IP only); a reload drops the token. And: **the wiki still builds and serves until phase 8**,
reachable as before.

## 4. Exit criterion, made testable

"User reads in `brain ui` by default" is a habit, and only the user can confirm it — that
confirmation is the phase-9 gate (§11), not this one. What this phase can pin is that **no
surface in the daily loop sends them anywhere else**, and that the LAN replacement works:

| # | Criterion | Pinned by |
|---|---|---|
| E1 | `brain-up` with default env does **not** open the wiki in a browser | `tests/test_bin_scripts.py` — stub `open`/`xdg-open`/`curl` on `PATH`, assert the opener log is empty |
| E2 | `brain-up`'s banner names `brain ui` as the reading surface, ahead of the wiki line | same file, asserts on stdout order |
| E3 | `BRAIN_OPEN_BROWSER=1` still opens the wiki (opt-in kept, nothing removed) | same file |
| E4 | `brain setup`'s report names `brain ui` ahead of the legacy wiki URL | `tests/test_reading_surface_pointers.py`, through `_print_final_report` (asserts the order) |
| E5 | The MCP `brain_capture` description (and its CLI docstring twin) name `brain ui`, not Quartz | `tests/test_reading_surface_pointers.py` (new) |
| E6 | README / cli-reference / guide no longer present `--host 0.0.0.0 --token` as the remote recipe, and point at the phone guide | `tests/test_reading_surface_pointers.py` |
| E7 | `brain ui --lan` binds a LAN address, always with a non-empty token, and prints `http://<ip>:<port>/#t=<token>`; `--lan --host` is exit 2 | `tests/test_cli_ui.py` |
| E8 | A `0.0.0.0` bind refuses a foreign Host and a LAN-IP bind accepts its own — the fact the recipe rests on | `tests/test_ui_security.py` |
| E9 | `--host 0.0.0.0` prints the notice naming `--lan` | `tests/test_cli_ui.py` |

## 5. Tasks (TDD — test first, see it fail, then implement)

One teammate can carry this; it is small and the files barely overlap. Order:

1. **`ui/lan.py` + `--lan` + 0.0.0.0 notice** — tests in `test_lan.py`-style unit tests inside
   `tests/test_ui_lan.py` (probe injected, never a real socket to the network), `test_cli_ui.py`
   (E7, E9; `serve`/`build_context` replaced with pytest `monkeypatch`, which CLAUDE.md rule 13
   permits), `test_ui_security.py` (E8). Files: `src/brain/ui/lan.py` (new),
   `src/brain/cli_ui.py`.
2. **`brain-up`** — E1–E3. File: `src/brain/templates/bin/brain-up.sh` (header comment + default +
   banner).
3. **setup + capture + MCP text** — E4, E5. Files: `src/brain/setup.py` (**over the ceiling** —
   see §6), `src/brain/_capture_command.py`, `src/brain/mcp_server.py` (net-zero line edit).
4. **Docs** — E6. Files: `README.md`, `docs/cli-reference.md`, `docs/vault-and-wiki.md`,
   `docs/README.md`, `docs/guides/brain-ui-on-your-phone.md` (new).
5. **Closeout** — §7 of this file, `CLAUDE.md` ceiling rows, CHANGELOG if it tracks unreleased CLI
   flags.

## 6. Ceiling (CLAUDE.md "File-size ceiling")

- `src/brain/ui/lan.py` — new, well under 800.
- `src/brain/cli_ui.py` — 214 at `1a1d068` (`git show 1a1d068:src/brain/cli_ui.py | wc -l`);
  stays under — re-derive with `wc -l src/brain/cli_ui.py`.
- `src/brain/setup.py` — **listed** (trail 1,356 at `f8c76c0`, untouched since —
  `git log --oneline f8c76c0..1a1d068 -- src/brain/setup.py` is empty). It grows by the
  `read: brain ui` line and its comment. Requires the inline comment at the growth site, a module-docstring pointer, and
  a new descriptive hop on its row.
- `src/brain/mcp_server.py` — **listed**; the edit is net zero, but the promotion rule still
  applies on touch: promote its trailing descriptive hop to `4,938 (f0abf64)` (the merge; verified
  `git show "${s}:src/brain/mcp_server.py" | wc -l` = 4,938, and nothing after it touched the
  file), then add a descriptive hop for this change.
- `_capture_command.py` — not listed (`git show 1a1d068:src/brain/_capture_command.py | wc -l`),
  net zero.

## 7. Guards that must be proven able to fail (five clauses each: passes unmutated; the mutation
lands, verified by diff; the guard goes red; red at the assertion it targets; one entry per claim)

| # | Guard | Mutation |
|---|---|---|
| M1 | E1 brain-up does not open the wiki | restore default `BRAIN_OPEN_BROWSER:-1` |
| M2 | E2 banner leads with `brain ui` | delete the `read:` line |
| M3 | E3 opt-in still opens | change the opt-in comparison to never match |
| M4 | E4 setup names `brain ui` | delete the `brain ui` line (round 1: move it below the wiki block) |
| M5 | E5 capture docstring twin | revert to the Quartz sentence |
| M6 | E5 MCP description | revert to the Quartz sentence |
| M7 | E6 docs | reintroduce `--host 0.0.0.0 --token` in README |
| M8 | E7 generated token | generate `""` |
| M9 | E7 `--lan`/`--host` exclusive | delete the check |
| M10 | `lan.py` rejects a loopback probe result | delete the check |
| M11 | E9 notice | delete the notice |

## 8. Risks

- **The LAN probe picks the wrong interface** (VPN up, several NICs). Mitigation: the address is
  printed; `--host <ip>` remains the manual override and the guide says so.
- **`brain-up` users who relied on the auto-open.** Mitigation: `BRAIN_OPEN_BROWSER=1` restores it;
  the banner says so. Nothing is removed.
- **A generated token changes every run**, so a phone bookmark breaks on restart. Mitigation:
  pass `--token` for a stable one; the guide says so and notes it then lands in shell history.
- **Ceiling bookkeeping drift** — handled in §6 in the same commit as the growth.

## 9. Environment and guardrails

Worktree `.claude/worktrees/phase7`, own Python 3.11 venv. Test DB
`postgresql://brain:brain@localhost:5434/second_brain_t_evalb` (sibling `…_evalc` if locked).
Review round 1 ran in a second worktree, `.claude/worktrees/phase7-fix` (branch
`fix/phase7-review-r1`), because another session was running the full suite inside the first, and
used `second_brain_t_auditmeas`.
Prod 55432 and the live vault are off-limits; CLI exercised only against `brain demo` (55433).
No `DROP DATABASE`, no `compose down -v`, no launchd changes, no PII, no push.

## 10. Closeout

### 10.1 What shipped

SHAs are the commits on `feat/wiki-to-ui-phase7` after its rebase onto `second-brain/master`
`a4ab179` (2026-10-13). The pre-rebase SHAs this table first carried are not on the branch.

| Task | Commit | Surfaces |
|---|---|---|
| Plan | `14a59be` | this file |
| 1 — `--lan`, wildcard notice, opened-URL fix | `5742c91` | `src/brain/ui/lan.py` (new), `cli_ui.py`, `ui/server.py` `browser_url` |
| 2 — `brain-up` | `01f18a2` | `templates/bin/brain-up.sh` |
| 4 — docs | `ce124e2` | README, `docs/cli-reference.md`, `docs/README.md`, `docs/vault-and-wiki.md`, `docs/guides/brain-ui-on-your-phone.md` (new) |
| 3 — setup / capture / MCP text | `2475d11` | `setup.py`, `_capture_command.py`, `mcp_server.py`, CLAUDE.md ceiling rows |
| Closeout | `8768393` | this file, §10 |
| Token ruling in the guide | `2572d83` | guide, §10.5, pointer test |
| Stale test pointer | `e671288` | `cli_ui.py` docstring |
| Import-purity test | `9baa2ba` | `tests/test_cli_ui.py`, `ui/__init__.py` comment |
| Review round 1 — code | `8748b70` | `ui/context.py` (`WILDCARD_HOSTS`, `bind_origin`), `ui/server.py`, `ui/lan.py`, `cli_ui.py`, security/CLI/wiring/browser tests |
| Review round 1 — setup, brain-up, guide | `4974f24` | `setup.py`, `brain-up.sh`, guide, CLAUDE.md, pointer + bin tests |
| Review round 1 — docs | **the commit that added this row** | this file, the spec |

Docs landed before task 3's commit so that `tests/test_reading_surface_pointers.py`, which pins
both, is green at every commit.

### 10.2 Rulings and corrections made during execution

- **A fix outside the inventory, on the Q1 path.** `ui/server.py` `serve` opened
  `http://host:port/` **without** the `#t=` fragment, so any tokened server — and so every
  `--lan` run with the default `--open` — opened a page whose every API call was refused. The
  panel also printed the fragment only for non-loopback binds. Both now come from one
  `browser_url(host, port, token)`. Regression tests: `test_browser_url_carries_the_token_whenever_one_is_set`,
  `test_serve_opens_the_tokened_url`.
- **Typer vendors click.** `click.core.ParameterSource` is a different enum from the one Typer's
  context returns, so an `is ParameterSource.COMMANDLINE` check never matched and `--lan --host`
  silently passed. Compared by name; reasoned inline in `cli_ui.py`; mutation M9 pins it.
- **Row #3 was wrong** — corrected in place in §2: the capture sentence is a docstring that
  `--help` never shows.
- **W1 (approved by team-lead 2026-10-13): `::` behaves exactly like `0.0.0.0`.**
  `host_allowlist("::")` was `{"::"}` — a Host value no browser sends, since starlette keeps the
  brackets on IPv6 literals — so a `--host ::` server refused every request, this machine's
  included. All three wildcards (`brain.ui.context.WILDCARD_HOSTS`) now get exactly the loopback
  allowlist; a test asserts EQUALITY with `0.0.0.0`'s, so the IPv6 wildcard can never accept more.
- **W2 (approved 2026-10-13): one helper derives the URL and the Origin.**
  `brain.ui.context.bind_origin` is the single source for the URL `brain ui` prints and opens
  (`server.browser_url`) and the Origin the write guard expects (`build_context`'s
  `allowed_origin`). A wildcard maps to its loopback literal. Before, a wildcard bind printed and
  opened `http://0.0.0.0:<port>/` (400 here) and expected an Origin no browser sends, so it could
  be read only by typing another URL, and never written. Security tests: the LAN Host is still
  refused on every wildcard; a foreign Origin is still refused on writes; the derived loopback
  Origin is accepted. `tests/test_ui_bind_wiring.py` proves `build_context` uses both helpers.

### 10.3 Verification (exit codes read from files)

*Measured on the pre-rebase head `99957eb`. The verification of the audited head is in the review-loop report, not here: a line in this file cannot record a run on the commit that contains it.*

- `ruff check` → 0; `mypy src/` → 0 (225 files).
- Full default suite, `TEST_DATABASE_URL=…/second_brain_t_evalb`: **8,884 passed, 4 skipped,
  305 deselected, 94.54% coverage**, exit 0.
- CI's browser command (the 14 explicit modules, `-m browser --no-cov`): **196 passed**, exit 0.
- `tests/test_ci_workflow.py`: 22 passed.
- End to end against the `brain demo` sandbox (55433, FTS-only, throwaway vault — never prod or
  the live vault): `brain ui --lan --read-only` printed `http://<LAN-IP>:18765/#t=<32 chars>`;
  `/api/health` 200 with the token, 403 without; `/api/tree` 200. `--host 0.0.0.0 --token …`
  printed the notice; a request to the LAN address → **400**, to `localhost` → 200.
  `--lan --host …` → exit 2.

### 10.4 Mutation evidence (five clauses each)

Harness: baseline green on all 27 targeted tests; each mutation applied exactly once and
confirmed by `git diff --numstat`; targeted tests red; red at the named assertion; every file
restored and the post-run green. **22/22 guards proven**: M1–M11 as §7, plus M4b (setup legacy
label), M7b (cli-reference wildcard), M12 (`browser_url`), M13 (`serve`), M14/M14b (the two
allowlist pins) and M15–M19 (docs). The M-numbers are the harness's, not a contiguous range of §7.

**One mutation was mis-scoped, not one guard.** M19 first removed a single `--read-only` from the
guide and survived, because the guide says it twice and the guard is a presence check.
Re-run removing both occurrences: red at `phone guide is missing '--read-only'`. Recorded rather
than smoothed over.

### 10.5 Known limits — recorded, not fixed

- **The UI token lives in memory only** (`ui/static/js/api.js:12-16`). A phone that reloads gets
  403 until the full `#t=` URL is reopened. **Ruled 2026-10-13 (team-lead): the PAGE keeps it in
  memory only** — it is not persisted to `sessionStorage`. *(Review round 1, M-1: this entry and
  the guide first said "a lost or lent phone keeps no key", while recommending a bookmark of the
  URL that carries the token. Both are corrected: the page never persists the token, but a
  bookmark or a history entry of the printed URL does, and browser sync copies it. The guide now
  says to treat the URL as a password.)*
- **No crash-survival draft buffer** (§1): a tab close mid-edit still loses unsaved text. A7's
  409 path is closed; this is a separate feature.
- **`brain brief --wiki`** keeps its name (§2 #9); renaming it is launchd work for phase 8.
- **The exit criterion's human half** — that the user actually reads in `brain ui` — is confirmed
  at the phase-9 gate, not here.

### 10.6 CLI surface changed (for the memory update, CLAUDE.md rule 11)

`brain ui --lan` (new flag). `brain-up` no longer opens the wiki unless `BRAIN_OPEN_BROWSER=1`
(default flipped from 1 to 0); its banner leads with `read: brain ui`, and a pid not yet written
reads "not written yet — check brain-status" instead of `?`. `brain setup`'s report prints
`read: brain ui` ahead of the legacy wiki URL. A wildcard `--host` (`0.0.0.0`, `::`) now prints
and opens a loopback URL and accepts writes from it (W1/W2).

### 10.7 Review round 1 (2026-10-13)

Findings M-1…M-4 and L-1…L-6 from the phase code review of `9baa2ba`, all fixed:

| Finding | Fix |
|---|---|
| M-1 guide contradicts itself on the token | rewritten (§10.5); pins updated |
| M-2 setup docstring claimed an order the report did not print | report reordered; test asserts the order |
| M-3 stale pre-rebase SHAs | §10.1 re-pointed; no other stale SHA in src/tests/docs (`grep -rnE '4b288e1|05fb151|be6270c|5e07b6c|b862b79|99957eb'` hits only this plan's history notes) |
| M-4 spec not updated | Q1 answered, R-4 mitigated, §11 phase-7 row EXITED (subject to the loop closing) |
| L-1 notice false for `::` | reworded; W1 makes "loopback names only" true for every wildcard |
| L-2 recipe regex | catches `--host ::`/`[::]`, spares `::1`; parametrized both ways |
| L-3 token not percent-encoded | `quote(token, safe="")`; unit test + a browser test through the real `api.js` |
| L-4 stale pid comment / bare `?` | comment rewritten; fallback names `brain-status`; test |
| L-5 plan counts | every count SHA-bound or a command |
| L-6 wildcard printed/opened URL 400s | W2; loopback URL printed and opened; test |

**Mutation evidence (round 1):** 17 guards, each proven on all five clauses — W1 equality; W1
LAN-Host refusal (a widening mutation); W2 loopback-Origin acceptance; W2 foreign-Origin refusal;
`build_context` using `bind_origin` and `host_allowlist`; IPv6 bracketing; L-6 `browser_url` and
panel; L-3 unit and browser; M-2 order; L-4 fallback; L-2 both directions; M-1 two pins.
**One mutation was invalid, not one guard:** M-2's first mutation moved the `read:` line inside
the `if wiki_installed:` block and raised `IndentationError` — red, but not at the assertion.
Re-run moving it below the whole block: red at `assert _read_line(lines) < wiki[0]`
(`assert 7 < 5`). L-1 is a wording fix and has no dedicated guard; the behaviour it describes is
guarded by the W1 tests.
