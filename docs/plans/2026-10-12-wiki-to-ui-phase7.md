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
cannot serve another device's browser *(round 7: "another device" overclaimed — a program can
send a loopback Host by hand; the allowlist is a rebinding defence and the token is the guard)*: `ui/context.py` `host_allowlist("0.0.0.0")` returns the
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
| Review round 1 — docs | `f48d858` | this file, the spec |
| Audit round 2 — code | `2bf1f12` | `ui/server.py` (IPv6 probe), security/CLI tests |
| Audit round 2 — docs | `afbc8f3` | `vault-and-wiki.md`, the spec (A7), CHANGELOG, CLAUDE.md, pointer test |
| Audit round 2 — plan | `77074bb` | this file: §10.8, the §10.9 catalog |
| Audit round 2 — §10.3 | `5402c8f` | this file |
| Review round 3 — deps | `3994ea3` | `pyproject.toml` (starlette ≥ 1.7), pin test |
| Review round 3 — UI | `5627743` | `ui/context.py` (`bind_address`), `ui/server.py` (`_probe_socket`), `ui/lan.py`, `cli_ui.py`, guide, CHANGELOG, CLI tests |
| Review round 3 — plan | `c282288` | this file: §10.9 validator, §10.10 |
| Audit round 4 — tests | `fd2f064` | `test_ui_lan.py`, `test_cli_ui.py`, `test_reading_surface_pointers.py`, CLAUDE.md hops |
| Audit round 4 — plan | `7363980` | this file: §10.1, §10.3, §10.8 legend, §10.9, §10.11 |
| Review round 5 — code | `d9a3004` | `ui/context.py` (canonical, bracketed allowlist), `ui/security.py` (bytes compare, `is_loopback`), `ui/server.py` (socket factory), `cli_ui.py` (token charset), CHANGELOG, guide, tests |
| Review round 5 — tests | `932c10a` | `test_ui_security.py` (literal Origin; `bind_origin` spellings) |
| Review round 5 — plan | `593022a` | this file: §10.9, §10.12 |
| Review round 6 — code | `c21c52f` | `cli_ui.py` (`PHONE_LINE`), `ui/context.py` (lower-cased hostnames), `ui/server.py` (probe closes on setsockopt), `test_cli_ui.py`, `test_ui_security.py` |
| Review round 6 — plan | `af22825` | this file: §10.9, §10.13 |
| Audit round 7 — code + docs | `ab560d4` | `ui/lan.py` (notice), `ui/context.py` (`ipv4_shorthand`, comment), `cli_ui.py` (refusal), guide, cli-reference, CHANGELOG, spec Q1, plan §3, tests |
| Audit round 7 — plan | `9d8024f` | this file: §10.9, §10.14 |
| Review round 7 — fixes | `89e4786` | `cli_ui.py` (doc-comment order), CHANGELOG (LOCALHOST), blank lines in two test files |
| Review round 7 — plan | **the commit that added this row** | this file: §10.14 |

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
- **W1 (approved by team-lead 2026-10-13): `::` gets exactly `0.0.0.0`'s Host allowlist.**
  `host_allowlist("::")` was `{"::"}` — a Host value no browser sends, since starlette keeps the
  brackets on IPv6 literals — so a `--host ::` server refused every request, this machine's
  included. All three wildcards (`brain.ui.context.WILDCARD_HOSTS`) now get exactly the loopback
  allowlist; a test asserts EQUALITY with `0.0.0.0`'s, so the IPv6 wildcard can never accept more.
  *(Review round 3: the allowlist is ALL they share. `::` is IPv6-only — asyncio sets
  `IPV6_V6ONLY` — so its local URL is `[::1]` and IPv4 clients are refused at connect; and W1
  needs `starlette>=1.7`, the first release that keeps the brackets on an IPv6 Host. The
  heading said "behaves exactly like `0.0.0.0`" until then.)*
- **W2 (approved 2026-10-13): one helper derives the URL and the Origin.**
  `brain.ui.context.bind_origin` is the single source for the URL `brain ui` prints and opens
  (`server.browser_url`) and the Origin the write guard expects (`build_context`'s
  `allowed_origin`). A wildcard maps to its loopback literal. Before, a wildcard bind printed and
  opened `http://0.0.0.0:<port>/` (400 here) and expected an Origin no browser sends, so it could
  be read only by typing another URL, and never written. Security tests: the LAN Host is still
  refused on every wildcard; a foreign Origin is still refused on writes; the derived loopback
  Origin is accepted. `tests/test_ui_bind_wiring.py` proves `build_context` uses both helpers.

### 10.3 Verification (exit codes read from files)

*Each measurement is bound to the commit it ran on. The run on this branch's final head is in the review-loop report for that head: a line in this file cannot record a run on the commit that contains it, and every round so far has added a commit after its run.*

*Two measurements, each bound to the commit it ran on. A line in this file cannot record a run on the commit that contains it, so the newest one is bound to the commit before the one that wrote it, and `git diff --stat 77074bb..<this commit>` shows only this file.*

**On `77074bb` (audit round 2's plan commit — plan-only; its last code-bearing ancestor is `afbc8f3`, and `git diff --stat afbc8f3..77074bb` touches only this file), 2026-10-13:** `ruff check` → 0; `mypy src/` (mypy 2.4.0) → 0, 225 files; full default suite on `second_brain_t_p7b` → **8,923 passed, 4 skipped, 306 deselected, 94.54% coverage**, exit 0; CI's browser command → **197 passed**, exit 0; `tests/test_ci_workflow.py` → 22 passed. End to end on the `brain demo` sandbox: `--lan` (200 with the token, 403 without, 400 for a foreign Host); `--host ::` binds, prints the notice and `http://[::1]:<port>/#t=…`, serves `[::1]` (200), refuses a foreign Host (400) and passes the write guard with the loopback Origin; `--host 0.0.0.0` prints a `127.0.0.1` URL, 400 from the LAN address, write guard accepts the loopback Origin and refuses a foreign one (403).

**On the pre-rebase head `99957eb` (the first closeout):**

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
Every one is written out — file, exact change, test, expected red assertion — in §10.9.

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
| M-3 stale pre-rebase SHAs | §10.1 re-pointed; no other stale SHA in src/tests/docs (`grep -rnE '4b288e1|05fb151|be6270c|5e07b6c|b862b79|99957eb'` hits only this plan's history notes — true at `f48d858`; round 2 then added `05fb151` to
`tests/test_cli_ui.py`, repointed in round 3, §10.10) |
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
guarded by the W1 tests. Every round-1 mutation is written out in §10.9.

### 10.8 Audit round 2 (2026-10-13)

The phase-7 audit of `9baa2ba` (AUDIT FAILED). Items already closed by round 1 were re-verified at
`f48d858`; everything else is fixed in `2bf1f12` (code) and `afbc8f3` (docs).

| Item | Status |
|---|---|
| HIGH-1 Host allowlist on a LAN bind not pinned | **fixed** — exact-equality test for a LAN IP; a foreign Host refused with 400 through the real app on LAN, `0.0.0.0`, `::` and `[::]` binds |
| M-2 panel half of the `#t=` fix had no regression test (A20/A21) | **fixed** — loopback `--token` prints the tokened URL; `serve` opens it, loopback and LAN |
| M-3 `--lan` no-network test never asserted the message (A26/A27) | **fixed** — asserts `SystemExit` (no traceback) first, then the "Pass --host" remedy |
| M-4 `vault-and-wiki.md` still said brain-up opens the browser; table default unpinned (A24) | **fixed** — three passages corrected; the table row is pinned |
| M-5 guide token contradiction | **verified closed** at `f48d858` (round 1, M-1) |
| M-6 setup order | **verified closed** at `f48d858` (round 1, M-2) |
| M-7 stale SHAs; scratchpad-only mutations; §10.3 count | **fixed** — every mutation of all three rounds is in §10.9; §10.3 is bound to the commit each measurement ran on (`77074bb` since `5402c8f`; a line here cannot record a run on its own commit) |
| L-8 `brain ui --host ::` died in `resolve_port` | **fixed** — the probe binds the server's address family; tests for a free and a busy IPv6-wildcard port |
| L-9 spec silent on the A7 deviation | **fixed** — §8.3 and C5 annotated; the Q1 annotation from round 1 re-verified |
| L-10 CHANGELOG `[Unreleased]` empty | **fixed** |
| L-11 CLAUDE.md "touched none of those five" | **fixed** — the anchored grep returned 4 at `1a1d068`; the digit is removed, per the ceiling section's rule |
| L-12 unbracketed IPv6 from `browser_url` | **verified closed** by W2 at `f48d858`; a dedicated `::1` test added |
| L-13 Phone line unpinned on loopback (A30) | **fixed** — absent on loopback, present on `--lan` |

**A-numbers and F-numbers** are the auditor's mutation and finding IDs, not this plan's; they are
defined here so the rows above can be read without the audit report. Each names the mutation that
SURVIVED before the fix, and the catalog entry (§10.9) that now kills it:

| ID | Surviving mutation | Killed by |
|---|---|---|
| A20 | the panel drops the token on loopback (`cli_ui.py`, `browser_url(..., "" if loopback else token)`) | M-2/A20 |
| A21 | `serve` opens the URL without the token on loopback | M-2/A21 |
| A24 | the `BRAIN_OPEN_BROWSER` table row states default `1` | M-4/A24 |
| A26 | the `--lan` failure path loses its `secho` (exit 1, no message) | M-3/A26 |
| A27 | the `--lan` failure path loses its `try`/`except` (a traceback) | M-3/A27 |
| A30 | the Phone line prints on every bind | L-13 |
| F8 | the wildcard check narrowed to `== "0.0.0.0"` | R4-4 |
| F14 | the probe returns its target instead of `getsockname()` | R4-1 |
| F16 | README's intro reverted to listing the wiki as an equal reader | R4-2, R4-3 |

`A18` was cited in the round-4 brief as referenced here; it is not — `git grep -n A18` and
`git show 5402c8f:<this file> | grep A18` both return nothing — so there is nothing to define or
remove.

**Mutation evidence (round 2):** 13 guards, each proven on all five clauses, run against
`afbc8f3` on a clean tree (§10.9). None was mis-scoped.

### 10.9 Mutation catalog — every mutation, reproducible

Each entry is one mutation as the harness applied it: the file, the exact text replaced (Python
`repr`), its replacement, how many times the replaced text occurs at the round's SHA (`count`; the
harness replaces every occurrence when it is above 1), the test(s) run, and the text that must
appear in the failure. The five clauses, every time: the targeted tests pass unmutated; the
replaced text occurs exactly `count` times and `git diff --numstat` shows the change landed; the
tests go red; the failure contains the "red at" text; the file is restored and the tests pass
again. Apply a mutation at the SHA named for its round.

**Validated.** The script below checks every entry's `count` against `git show <SHA>:<file>`,
and, since round 5, that every entry which no longer applies at HEAD is marked SUPERSEDED.
Run from the repository root with `python` and the script on stdin. Result at `c282288` (round 3): **59/59, exit 0.** Result at `7363980` (round 4): 63/63, exit 0. At `593022a` (round 5, HEAD check added): 74/74, 16
superseded and marked, exit 0. At `af22825` (round 6): 83/83, 17 superseded and marked, exit 0. Result at the commit that added
this sentence (round 7): **round 0 22/22, round 1 17/17, round 2 13/13, round 3 7/7, round 4 4/4,
round 5 11/11, round 6 9/9, round 7 9/9 — 92/92; 18 entries superseded at HEAD, every one marked;
exit 0.** The HEAD check again flagged the round's one casualty (R3-7) before it was marked. The HEAD check earned its place on its
first run after round 6: it flagged L-13, which round 6 had made inapplicable, before it was marked.
It checks the replaced TEXT only; an entry whose text still applies but whose red-at assertion moved
carries a NOTE line instead (R4-4, L-6 panel).
*(Review round 3, item 5: M19 was an annotation, not exact text, so it matched 0 times; it is
now `'--read-only'` with count 2 — re-verified in round 4.)* Entries marked SUPERSEDED still validate at their
round's SHA, which is what the script checks; at HEAD their text or test has moved, for the reason given.

```python
"""Validate plan §10.9 two ways.

1. Each mutation's `replace` text occurs `count` times at its round's SHA.
2. At HEAD, an entry whose text no longer occurs `count` times carries a
   SUPERSEDED line saying why — so a reader re-running it at HEAD is told.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

PLAN = Path("docs/plans/2026-10-12-wiki-to-ui-phase7.md")
FENCE = "`" * 3  # spelled out: a literal one would end this script's own code block
text = PLAN.read_text(encoding="utf-8")
catalog = text[text.index("### 10.9"):]
rounds = re.findall(
    rf"\*\*Round (\d) — against `([0-9a-f]{{7}})`.*?\n{FENCE}text\n(.*?)\n{FENCE}",
    catalog,
    re.S,
)


def show(sha: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{sha}:{path}"], capture_output=True, text=True, check=True
    ).stdout


failures = 0
superseded = 0
for number, sha, body in rounds:
    entries = [e for e in body.strip().split("\n\n") if e.strip()]
    matched = 0
    for entry in entries:
        lines = entry.splitlines()
        fields = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            if key.strip() in {"file", "replace", "count"}:
                fields[key.strip()] = value.strip()
        old = ast.literal_eval(fields["replace"])
        want = int(fields["count"])
        got = show(sha, fields["file"]).count(old)
        if got == want:
            matched += 1
        else:
            failures += 1
            print(f"MISMATCH round {number} {lines[0]!r}: {got} != {want} at {sha}")
        marked = any(line.strip().startswith("SUPERSEDED") for line in lines)
        superseded += marked
        if show("HEAD", fields["file"]).count(old) != want and not marked:
            failures += 1
            print(f"UNMARKED round {number} {lines[0]!r}: no longer applies at HEAD")
    print(f"round {number} @ {sha}: {matched}/{len(entries)} entries match their count")
print(f"superseded at HEAD, and marked: {superseded}")
sys.exit(1 if failures else 0)
```

**Round 0 — against `8768393`** (the rebased closeout; every mutated file is byte-identical to the
pre-rebase `99957eb` the harness first ran on — `git diff --stat 99957eb 8768393` touches three
files, `.gitignore`, `pyproject.toml` and `tests/test_dev_dependency_pins.py`, which is master's
`a4ab179` delta, and no round-0 mutation uses any of them. *(Said "touches only
`tests/test_dev_dependency_pins.py`" until review round 5: that count came from a path-filtered
diff.)* M19 removes BOTH occurrences; the
first run removed one and survived (§10.4).

```text
M1 brain-up default does not open wiki
  file:    src/brain/templates/bin/brain-up.sh
  replace: 'BRAIN_OPEN_BROWSER:-0}" == \'1\''
  with:    'BRAIN_OPEN_BROWSER:-1}" == \'1\''
  count:   1
  test:    tests/test_bin_scripts.py::test_brain_up_does_not_open_the_wiki_by_default
  red at:  brain-up opened the wiki by default

M2 brain-up banner leads with brain ui
  file:    src/brain/templates/bin/brain-up.sh
  replace: "echo '   read:       brain ui   (search, read and edit — opens a browser)'\n"
  with:    ''
  count:   1
  test:    tests/test_bin_scripts.py::test_brain_up_banner_names_brain_ui_before_the_wiki
  red at:  assert read and

M3 brain-up opt-in still opens
  file:    src/brain/templates/bin/brain-up.sh
  replace: 'BRAIN_OPEN_BROWSER:-0}" == \'1\' ]]'
  with:    'BRAIN_OPEN_BROWSER:-0}" == \'never\' ]]'
  count:   1
  test:    tests/test_bin_scripts.py::test_brain_up_still_opens_the_wiki_on_request
  red at:  open.calls

M4 setup next steps name brain ui
  SUPERSEDED at HEAD by round 1 (M-2): the next-step line it deletes was replaced by a `read:` line, and its test renamed; use round 1's "M-2 setup names brain ui before the wiki"
  file:    src/brain/setup.py
  replace: '    typer.echo("     brain ui                       # read, search and edit in a browser")\n'
  with:    ''
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_setup_next_steps_name_brain_ui
  red at:  assert "brain ui" in steps

M4b setup wiki url labelled legacy
  SUPERSEDED at HEAD by round 1 (M-2): its test was renamed to test_setup_report_names_brain_ui_before_the_legacy_wiki_url
  file:    src/brain/setup.py
  replace: '  (legacy — read in brain ui)'
  with:    ''
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_setup_report_puts_brain_ui_before_the_wiki_url
  red at:  assert wiki and "legacy"

M5 capture docstring twin
  file:    src/brain/_capture_command.py
  replace: '``brain ui`` tree without "Show ingested" on'
  with:    'Quartz UI without the "Show ingested" toggle'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_capture_docstring_names_brain_ui_not_quartz
  red at:  assert "brain ui" in text

M6 MCP brain_capture description
  file:    src/brain/mcp_server.py
  replace: '(visible in the ``brain ui``\n    tree without "Show ingested" on)'
  with:    '(visible in the Quartz UI\n    without the "Show ingested" toggle)'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_mcp_capture_description_names_brain_ui_not_quartz
  red at:  assert "brain ui" in text

M7 README ships no wildcard recipe
  file:    README.md
  replace: 'brain ui --lan --read-only                # read from your phone: LAN address + a token'
  with:    'brain ui --host 0.0.0.0 --token <secret>  # a shared secret is required off loopback'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_docs_do_not_ship_the_wildcard_phone_recipe[README.md]
  red at:  documents `brain ui --host 0.0.0.0`

M7b cli-reference ships no wildcard recipe
  file:    docs/cli-reference.md
  replace: 'brain ui --lan --read-only                # serve to the LAN (e.g. a phone), token minted'
  with:    'brain ui --host 0.0.0.0 --token <secret>'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_docs_do_not_ship_the_wildcard_phone_recipe[cli-reference.md]
  red at:  documents `brain ui --host 0.0.0.0`

M15 cli-reference links the phone guide
  file:    docs/cli-reference.md
  replace: '[Reading your brain from a phone](guides/brain-ui-on-your-phone.md).'
  with:    'the phone guide.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_docs_point_at_the_phone_guide_and_lan[cli-reference.md]
  red at:  assert "brain-ui-on-your-phone.md" in text

M16 docs index lists the guide
  file:    docs/README.md
  replace: '| [guides/brain-ui-on-your-phone.md](guides/brain-ui-on-your-phone.md) |'
  with:    '| guides |'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_docs_index_lists_the_phone_guide
  red at:  assert 'brain-ui-on-your-phone.md' in

M17 README calls brain ui primary
  file:    README.md
  replace: '`brain ui` is the primary reading surface:'
  with:    '`brain ui` is a reading surface:'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_readme_names_brain_ui_as_the_reading_surface
  red at:  assert "primary" in section.lower()

M18 vault-and-wiki marks the wiki legacy
  file:    docs/vault-and-wiki.md
  replace: '> **The wiki is now a legacy view.**'
  with:    '> **The wiki.**'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_vault_and_wiki_marks_the_wiki_as_legacy
  red at:  assert "brain ui" in intro and "legacy"

M19 guide keeps --read-only advice
  file:    docs/guides/brain-ui-on-your-phone.md
  replace: '--read-only'
  with:    ''
  count:   2
  test:    tests/test_reading_surface_pointers.py::test_phone_guide_covers_the_q1_contract
  red at:  phone guide is missing '--read-only'

M8 --lan mints a real token
  file:    src/brain/cli_ui.py
  replace: 'token = token or lan_mod.new_token()'
  with:    'token = token or "x"'
  count:   1
  test:    tests/test_cli_ui.py::test_lan_binds_the_lan_address_with_a_minted_token
  red at:  --lan must always run with a token

M9 --lan and --host are exclusive
  file:    src/brain/cli_ui.py
  replace: 'source.name == "COMMANDLINE"'
  with:    'False'
  count:   1
  test:    tests/test_cli_ui.py::test_lan_and_host_together_is_a_usage_error
  red at:  assert result.exit_code == 2

M10 lan.py refuses loopback/unspecified
  file:    src/brain/ui/lan.py
  replace: 'if address.is_loopback or address.is_unspecified:'
  with:    'if False:'
  count:   1
  test:    tests/test_ui_lan.py::test_a_loopback_or_unspecified_answer_is_refused
  red at:  DID NOT RAISE

M11 wildcard notice
  SUPERSEDED at HEAD by round 1 (`ALL_INTERFACES` moved to `context.WILDCARD_HOSTS`) and round 4 (R4-4)
  file:    src/brain/cli_ui.py
  replace: 'if host.strip().lower() in lan_mod.ALL_INTERFACES:'
  with:    'if False:'
  count:   1
  test:    tests/test_cli_ui.py::test_wildcard_bind_warns_that_other_devices_are_refused
  red at:  assert "--lan" in result.output

M12 browser_url carries the token
  SUPERSEDED at HEAD by round 1 (`bind_origin`, percent-encoding); use round 1's L-6 and L-3 entries
  file:    src/brain/ui/server.py
  replace: 'return f"{url}#t={token}" if token else url'
  with:    'return url'
  count:   1
  test:    tests/test_cli_ui.py::test_browser_url_carries_the_token_whenever_one_is_set
  red at:  assert browser_url(LAN_IP

M13 serve opens the tokened url
  file:    src/brain/ui/server.py
  replace: 'url = browser_url(host, port, context.token)'
  with:    'url = f"http://{host}:{port}/"'
  count:   1
  test:    tests/test_cli_ui.py::test_serve_opens_the_tokened_url
  red at:  assert opened ==

M14 wildcard bind refuses a phone Host
  SUPERSEDED at HEAD by round 1 (W1 rewrote host_allowlist); use round 1's W1 entries
  file:    src/brain/ui/context.py
  replace: 'if host in LOOPBACK_HOSTS or host == "0.0.0.0":  # noqa: S104 — compared, not bound\n        return LOOPBACK_HOSTS'
  with:    'if host in LOOPBACK_HOSTS:\n        return LOOPBACK_HOSTS\n    if host in ("0.0.0.0", "::"):\n        return frozenset({"*"})'
  count:   1
  test:    tests/test_ui_security.py::test_a_wildcard_bind_refuses_another_devices_host_header
  red at:  assert response.status_code == 400

M14b LAN bind accepts its own Host
  SUPERSEDED at HEAD by round 5 (host_allowlist returns the bracketed/normalised `address`); use R5-1, R5-2
  file:    src/brain/ui/context.py
  replace: '    return frozenset({host})\n'
  with:    '    return LOOPBACK_HOSTS\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_lan_address_bind_accepts_its_own_host_header
  red at:  assert response.status_code == 200

```

**Round 1 — against `4974f24`.** The M-2 entry is the corrected mutation; the first one was
invalid (§10.7).

```text
W1 :: gets 0.0.0.0's allowlist
  SUPERSEDED at HEAD by round 5 (the check reads the normalised `address`, not `host`); use R5-4
  file:    src/brain/ui/context.py
  replace: 'if host in LOOPBACK_HOSTS or host in WILDCARD_HOSTS:'
  with:    'if host in LOOPBACK_HOSTS or host == "0.0.0.0":  # noqa: S104'
  count:   1
  test:    tests/test_ui_security.py::test_an_ipv6_wildcard_gets_exactly_the_ipv4_wildcards_allowlist
  red at:  assert host_allowlist(wildcard) ==

W1 LAN Host still refused on wildcards (no wider)
  SUPERSEDED at HEAD by round 5 (same rewrite); the equivalent mutation targets `address`
  file:    src/brain/ui/context.py
  replace: '    if host in LOOPBACK_HOSTS or host in WILDCARD_HOSTS:\n        return LOOPBACK_HOSTS\n'
  with:    '    if host in WILDCARD_HOSTS:\n        return LOOPBACK_HOSTS | frozenset({"192.0.2.10"})\n    if host in LOOPBACK_HOSTS:\n        return LOOPBACK_HOSTS\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_wildcard_bind_refuses_another_devices_host_header
  red at:  assert response.status_code == 400

W2 loopback Origin accepted on wildcard writes
  file:    src/brain/ui/security.py
  replace: 'if origin != ctx.allowed_origin:'
  with:    'if origin != ctx.allowed_origin.replace("127.0.0.1", "0.0.0.0").replace("[::1]", "[::]"):'
  count:   1
  test:    tests/test_ui_security.py::test_a_wildcard_bind_accepts_a_write_from_the_url_it_opens
  red at:  assert response.status_code == 404

W2 foreign Origin still refused
  file:    src/brain/ui/security.py
  replace: 'if origin != ctx.allowed_origin:'
  with:    'if origin != ctx.allowed_origin and not origin.startswith("http://192.0.2."):'
  count:   1
  test:    tests/test_ui_security.py::test_a_wildcard_bind_still_refuses_a_foreign_origin_on_writes
  red at:  assert response.status_code == 403

W2 build_context uses bind_origin
  file:    src/brain/ui/server.py
  replace: 'allowed_origin=bind_origin(host, port),'
  with:    'allowed_origin=f"http://{host}:{port}",'
  count:   1
  test:    tests/test_ui_bind_wiring.py::test_build_context_derives_origin_and_hosts_from_the_bind
  red at:  assert context.allowed_origin == origin

W2 build_context uses host_allowlist (wildcard)
  SUPERSEDED at HEAD by round 5 (same rewrite); the equivalent mutation targets `address`
  file:    src/brain/ui/context.py
  replace: 'if host in LOOPBACK_HOSTS or host in WILDCARD_HOSTS:'
  with:    'if host in LOOPBACK_HOSTS:'
  count:   1
  test:    tests/test_ui_bind_wiring.py::test_build_context_derives_origin_and_hosts_from_the_bind
  red at:  assert context.allowed_hosts == hosts

bind_origin brackets IPv6
  SUPERSEDED at HEAD by round 5 (bind_origin brackets the normalised `address`); use R5-7
  file:    src/brain/ui/context.py
  replace: '        url_host = f"[{host}]"'
  with:    '        url_host = host'
  count:   1
  test:    tests/test_ui_security.py::test_bind_origin_brackets_ipv6_and_passes_other_hosts_through
  red at:  assert bind_origin("::1", 8765)

L-6 browser_url loopback for wildcards
  file:    src/brain/ui/server.py
  replace: 'url = f"{bind_origin(host, port)}/"'
  with:    'url = f"http://{host}:{port}/"'
  count:   1
  test:    tests/test_cli_ui.py::test_browser_url_for_a_wildcard_bind_is_a_loopback_url
  red at:  assert browser_url(wildcard, 8765

L-6 panel prints loopback URL
  NOTE at HEAD (round 6): still applies, but goes red at `assert "http://0.0.0.0" not in result.output`, which now runs first — see R6-2
  file:    src/brain/ui/server.py
  replace: 'url = f"{bind_origin(host, port)}/"'
  with:    'url = f"http://{host}:{port}/"'
  count:   1
  test:    tests/test_cli_ui.py::test_wildcard_bind_prints_a_loopback_url_not_the_bind_address
  red at:  assert "http://127.0.0.1:8765/#t=x" in

L-3 token percent-encoded (unit)
  file:    src/brain/ui/server.py
  replace: "quote(token, safe='')"
  with:    'token'
  count:   1
  test:    tests/test_cli_ui.py::test_browser_url_percent_encodes_the_token
  red at:  assert url ==

L-3 token reaches api.js intact (browser)
  pytest:  run with `-m browser` — the module is browser-marked and deselected by default
  file:    src/brain/ui/server.py
  replace: "quote(token, safe='')"
  with:    'token'
  count:   1
  test:    tests/test_ui_browser_api.py::test_a_token_with_reserved_characters_reaches_the_api_intact
  red at:  the page threw while reading the token

M-2 setup names brain ui before the wiki
  file:    src/brain/setup.py
  replace: '    typer.echo("   read:       brain ui   (search, read and edit in a browser)")\n    if wiki_installed:\n        typer.echo(f"   wiki url:   http://localhost:{wiki_port}  (legacy — read in brain ui)")\n        typer.echo(f"   start wiki: caddy run --config {brain_home}/Caddyfile")\n'
  with:    '    if wiki_installed:\n        typer.echo(f"   wiki url:   http://localhost:{wiki_port}  (legacy — read in brain ui)")\n        typer.echo(f"   start wiki: caddy run --config {brain_home}/Caddyfile")\n    typer.echo("   read:       brain ui   (search, read and edit in a browser)")\n'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_setup_report_names_brain_ui_before_the_legacy_wiki_url
  red at:  assert _read_line(lines) < wiki[0]

L-4 pid fallback names brain-status
  file:    src/brain/templates/bin/brain-up.sh
  replace: "PID_PENDING='not written yet — check brain-status'"
  with:    "PID_PENDING='?'"
  count:   1
  test:    tests/test_bin_scripts.py::test_brain_up_banner_says_where_to_look_when_a_pid_is_not_written_yet
  red at:  assert "pid ?" not in

L-2 pattern catches --host ::
  file:    tests/test_reading_surface_pointers.py
  replace: '(?:0\\.0\\.0\\.0|\\[?::\\]?)'
  with:    '(?:0\\.0\\.0\\.0)'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_wildcard_recipe_pattern_catches_every_wildcard
  red at:  assert _WILDCARD_RECIPE.search(line)

L-2 pattern spares ::1
  file:    tests/test_reading_surface_pointers.py
  replace: '(?![0-9A-Za-z.:\\]])'
  with:    ''
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_wildcard_recipe_pattern_spares_real_addresses
  red at:  assert not _WILDCARD_RECIPE.search(line)

M-1 guide: URL is a password
  file:    docs/guides/brain-ui-on-your-phone.md
  replace: '- **Treat the printed URL as a password.**'
  with:    '- **Keep the printed URL handy.**'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_phone_guide_covers_the_q1_contract
  red at:  phone guide is missing 'Treat the printed URL as a password'

M-1 guide: the URL is the token
  file:    docs/guides/brain-ui-on-your-phone.md
  replace: '**But the URL is the token, and the browser can keep the URL.**'
  with:    '**The browser may keep the URL.**'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_phone_guide_covers_the_q1_contract
  red at:  phone guide is missing 'the URL is the token'

```

**Round 2 — against `afbc8f3`.**

```text
HIGH-1 LAN allowlist is exactly the bound address
  SUPERSEDED at HEAD by round 5 (non-loopback allowlists hold the normalised `address`); use R5-1
  file:    src/brain/ui/context.py
  replace: '    return frozenset({host})\n'
  with:    '    return frozenset({host, "*"}) if "." in host else frozenset({host})\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_lan_bind_allowlist_is_exactly_the_bound_address
  red at:  assert host_allowlist("192.0.2.10") ==

HIGH-1 LAN bind refuses a foreign Host (app)
  SUPERSEDED at HEAD by round 5 (same rewrite); use R5-2
  file:    src/brain/ui/context.py
  replace: '    return frozenset({host})\n'
  with:    '    return frozenset({host, "*"}) if "." in host else frozenset({host})\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_loopback_bind_refuses_a_foreign_host_header[192.0.2.10]
  red at:  assert response.status_code == 400

HIGH-1 wildcard binds refuse a foreign Host (app)
  SUPERSEDED at HEAD by round 5 (the check reads `address`); the equivalent mutation targets `address`
  file:    src/brain/ui/context.py
  replace: '    if host in LOOPBACK_HOSTS or host in WILDCARD_HOSTS:\n        return LOOPBACK_HOSTS\n'
  with:    '    if host in WILDCARD_HOSTS:\n        return LOOPBACK_HOSTS | frozenset({"*"})\n    if host in LOOPBACK_HOSTS:\n        return LOOPBACK_HOSTS\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_loopback_bind_refuses_a_foreign_host_header[0.0.0.0]
  red at:  assert response.status_code == 400

M-2/A20 panel carries the token on loopback
  file:    src/brain/cli_ui.py
  replace: 'url=browser_url(host, resolved_port, token),'
  with:    'url=browser_url(host, resolved_port, "" if loopback else token),'
  count:   1
  test:    tests/test_cli_ui.py::test_loopback_with_a_token_prints_the_tokened_url
  red at:  assert "http://127.0.0.1:8765/#t=s3cret" in

M-2/A21 serve opens the tokened URL on loopback
  file:    src/brain/ui/server.py
  replace: 'url = browser_url(host, port, context.token)'
  with:    'url = browser_url(host, port, "" if is_loopback(host) else context.token)'
  count:   1
  test:    tests/test_cli_ui.py::test_serve_opens_the_tokened_url[127.0.0.1]
  red at:  assert opened ==

M-3/A26 --lan failure prints the remedy
  file:    src/brain/cli_ui.py
  replace: '            typer.secho(str(exc), fg="red", err=True)\n            raise typer.Exit(1) from exc\n        # Minted'
  with:    '            raise typer.Exit(1) from exc\n        # Minted'
  count:   1
  test:    tests/test_cli_ui.py::test_lan_without_a_network_exits_one_with_the_override
  red at:  assert "Pass --host" in result.output

M-3/A27 --lan failure is not a traceback
  file:    src/brain/cli_ui.py
  replace: '        try:\n            host = lan_mod.detect_lan_address()\n        except BrainError as exc:\n            typer.secho(str(exc), fg="red", err=True)\n            raise typer.Exit(1) from exc\n'
  with:    '        host = lan_mod.detect_lan_address()\n'
  count:   1
  test:    tests/test_cli_ui.py::test_lan_without_a_network_exits_one_with_the_override
  red at:  assert isinstance(result.exception, SystemExit)

L-8 port probe uses the IPv6 family
  file:    src/brain/ui/server.py
  replace: 'family = socket.AF_INET6 if ":" in address else socket.AF_INET'
  with:    'family = socket.AF_INET'
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_port_finds_a_free_port_on_the_ipv6_wildcard
  red at:  assert resolve_port(free, host=wildcard

L-8 port probe still sees a busy IPv6 port
  file:    src/brain/ui/server.py
  replace: '        except OSError:\n            return False\n    return True\n'
  with:    '        except OSError:\n            return True\n    return True\n'
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_port_sees_a_busy_ipv6_wildcard_port
  red at:  assert resolve_port(busy, host=

L-12 browser_url brackets an IPv6 literal
  SUPERSEDED at HEAD by round 5 (bind_origin brackets `address`); use R5-7
  file:    src/brain/ui/context.py
  replace: '        url_host = f"[{host}]"'
  with:    '        url_host = host'
  count:   1
  test:    tests/test_cli_ui.py::test_browser_url_brackets_an_ipv6_literal
  red at:  assert browser_url("::1", 8765, "") ==

L-13 no Phone line on loopback
  SUPERSEDED at HEAD by round 6 (the Phone line is printed from `PHONE_LINE`, and the test asserts the constant); use R6-3
  file:    src/brain/cli_ui.py
  replace: '    if lan:\n        typer.echo("  Phone'
  with:    '    if True:\n        typer.echo("  Phone'
  count:   1
  test:    tests/test_cli_ui.py::test_the_phone_line_is_printed_only_for_lan
  red at:  assert "Phone" not in loopback.output

M-4/A24 table states the 0 default
  file:    docs/vault-and-wiki.md
  replace: '| `BRAIN_OPEN_BROWSER` | `0` |'
  with:    '| `BRAIN_OPEN_BROWSER` | `1` |'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_vault_and_wiki_documents_the_brain_up_default_truthfully
  red at:  the env table must state BRAIN_OPEN_BROWSER's default

M-4 no stale 'opens the browser'
  file:    docs/vault-and-wiki.md
  replace: 'and starts the\nbuild watcher.'
  with:    'starts the\nbuild watcher, and opens the browser.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_vault_and_wiki_documents_the_brain_up_default_truthfully
  red at:  assert 'and opens the browser' not in

```

**Round 3 — against `5627743`.**

```text
R3-1 starlette floor excludes 1.6.0
  file:    pyproject.toml
  replace: '"starlette>=1.7,<2",'
  with:    '"starlette>=0.47,<2",'
  count:   1
  test:    tests/test_dev_dependency_pins.py::test_starlette_floor_excludes_the_release_that_breaks_ipv6_hosts
  red at:  admits starlette 1.6.0

R3-2 bind_address strips brackets
  SUPERSEDED at HEAD by round 5 (bind_address also canonicalises); use R5-3
  file:    src/brain/ui/context.py
  replace: '        return host[1:-1]\n'
  with:    '        return host\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_bracketed_host_is_bound_without_its_brackets
  red at:  assert started.context_kwargs["host"] == bound

R3-3 the CLI normalises the host once
  file:    src/brain/cli_ui.py
  replace: '    host = bind_address(host)\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui.py::test_a_bracketed_host_is_bound_without_its_brackets
  red at:  assert started.context_kwargs["host"] == bound

R3-4 [::1] binds as ::1
  file:    src/brain/cli_ui.py
  replace: '    host = bind_address(host)\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui.py::test_a_bracketed_loopback_needs_no_token
  red at:  assert started.context_kwargs["host"] == "::1"

R3-5 the probe socket is IPv6-only
  file:    src/brain/ui/server.py
  replace: '        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)\n'
  with:    '        pass\n'
  count:   1
  test:    tests/test_cli_ui.py::test_the_probe_socket_is_ipv6_only_like_the_real_bind
  red at:  assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1

R3-6 the probe socket uses the bind's family
  SUPERSEDED at HEAD by round 5 (the socket comes from an injected `socket_factory`); the equivalent mutation targets `socket_factory(family`
  file:    src/brain/ui/server.py
  replace: '    family = socket.AF_INET6 if ":" in address else socket.AF_INET\n    sock = socket.socket(family'
  with:    '    family = socket.AF_INET\n    sock = socket.socket(family'
  count:   1
  test:    tests/test_cli_ui.py::test_the_probe_socket_is_ipv6_only_like_the_real_bind
  red at:  assert sock.family == socket.AF_INET6

R3-7 the :: notice says IPv6-only
  SUPERSEDED at HEAD by round 7 (the notice was reworded to name the token as the guard); its test still asserts "IPv6-only", so the equivalent mutation removes that clause from the new text
  file:    src/brain/ui/lan.py
  replace: '"is refused with 400 — and `::` is IPv6-only, so an IPv4 phone cannot even "\n    "connect. Use --lan'
  with:    '"is refused with 400. Use --lan'
  count:   1
  test:    tests/test_cli_ui.py::test_the_ipv6_wildcard_notice_says_ipv4_devices_cannot_connect
  red at:  assert "IPv6-only" in result.output

```

**Round 4 — against `fd2f064`.**

```text
R4-1 F14 the probe returns the LOCAL end, not the target
  file:    src/brain/ui/lan.py
  replace: '        return str(sock.getsockname()[0])\n'
  with:    '        return str(target[0])\n'
  count:   1
  test:    tests/test_ui_lan.py::test_the_real_probe_reads_the_local_end_of_a_route
  red at:  assert udp_route_probe(("127.0.0.2", 9)) ==

R4-2 F16 README intro does not list the wiki as an equal
  file:    README.md
  replace: 'You read it through the CLI or the [local web UI](#local-web-ui); the older [rendered wiki](docs/vault-and-wiki.md) still builds, as a legacy view.'
  with:    'You get the same corpus through the CLI, a [local web UI](#local-web-ui), or a [rendered wiki](docs/vault-and-wiki.md).'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_readme_intro_names_the_wiki_legacy_not_an_equal_reader
  red at:  README's intro lists the wiki as an equal reading surface again

R4-3 F16 README intro calls the wiki legacy
  file:    README.md
  replace: 'the older [rendered wiki](docs/vault-and-wiki.md) still builds, as a legacy view.'
  with:    'the [rendered wiki](docs/vault-and-wiki.md) still builds.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_readme_intro_names_the_wiki_legacy_not_an_equal_reader
  red at:  README's intro must call the rendered wiki legacy

R4-4 F8 the notice fires on every wildcard
  NOTE at HEAD (round 6): still applies, but goes red at `assert WILDCARD_NOTICE in result.output` — the test now asserts the notice by constant
  file:    src/brain/cli_ui.py
  replace: '    if host.strip().lower() in WILDCARD_HOSTS:\n'
  with:    '    if host.strip().lower() == "0.0.0.0":\n'
  count:   1
  test:    tests/test_cli_ui.py::test_wildcard_bind_warns_that_other_devices_are_refused
  red at:  assert "--lan" in result.output

```

**Round 5 — against `932c10a`.**

```text
R5-1 allowlist brackets an IPv6 literal (equality)
  file:    src/brain/ui/context.py
  replace: '        return frozenset({f"[{address}]"})\n'
  with:    '        return frozenset({address})\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_loopback_allowlist_is_exactly_the_canonical_host_header
  red at:  assert host_allowlist(given) == expected

R5-2 an IPv6 bind serves its own URL (middleware)
  file:    src/brain/ui/context.py
  replace: '        return frozenset({f"[{address}]"})\n'
  with:    '        return frozenset({address})\n'
  count:   1
  test:    tests/test_ui_security.py::test_an_ipv6_bind_serves_its_own_url_and_refuses_others[[fd00::5]:8765-200]
  red at:  assert response.status_code == status

R5-3 bind_address canonicalises IP literals
  file:    src/brain/ui/context.py
  replace: '        return str(ipaddress.ip_address(bare))\n'
  with:    '        return bare\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_canonical_loopback_is_loopback
  red at:  assert host_allowlist(spelling) == LOOPBACK_HOSTS

R5-4 host_allowlist normalises its input
  file:    src/brain/ui/context.py
  replace: '    address = bind_address(host)\n    if address in LOOPBACK_HOSTS or address in WILDCARD_HOSTS:\n'
  with:    '    address = host\n    if address in LOOPBACK_HOSTS or address in WILDCARD_HOSTS:\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_loopback_allowlist_is_exactly_the_canonical_host_header
  red at:  assert host_allowlist(given) == expected

R5-5 bind_origin normalises its input
  file:    src/brain/ui/context.py
  replace: '    address = bind_address(host)\n    if address == "0.0.0.0":'
  with:    '    address = host\n    if address == "0.0.0.0":'
  count:   1
  test:    tests/test_ui_security.py::test_bind_origin_brackets_ipv6_and_passes_other_hosts_through
  red at:  assert bind_origin("0:0:0:0:0:0:0:1", 8765)

R5-6 is_loopback normalises its input
  file:    src/brain/ui/security.py
  replace: '    return bind_address(host.strip()).lower() in LOOPBACK_HOSTS\n'
  with:    '    return host.strip().lower() in LOOPBACK_HOSTS\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_canonical_loopback_is_loopback
  red at:  assert is_loopback(spelling)

R5-7 an IPv6 write from the browser's literal Origin passes
  file:    src/brain/ui/context.py
  replace: '    elif ":" in address:\n        url_host = f"[{address}]"\n'
  with:    '    elif ":" in address:\n        url_host = address\n'
  count:   1
  test:    tests/test_ui_security.py::test_an_ipv6_bind_accepts_a_write_from_its_own_origin
  red at:  assert response.status_code == 404

R5-8 no IPv6 support is a clean BrainError
  file:    src/brain/ui/server.py
  replace: '    try:\n        probe = _probe_socket(address, socket_factory)\n    except OSError as exc:\n'
  with:    '    probe = _probe_socket(address, socket_factory)\n    if False:\n        exc = OSError()\n'
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_port_reports_a_host_without_ipv6_cleanly
  red at:  Address family not supported

R5-9 the token is compared as bytes
  file:    src/brain/ui/security.py
  replace: '            if not secrets.compare_digest(\n                supplied.encode("utf-8", "surrogateescape"),\n                ctx.token.encode("utf-8", "surrogateescape"),\n            ):'
  with:    '            if not secrets.compare_digest(supplied, ctx.token):'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_ascii_token_is_refused_not_a_server_error
  red at:  TypeError

R5-10 the CLI refuses a non-ASCII token
  file:    src/brain/cli_ui.py
  replace: '    if token and not (token.isascii() and token.isprintable() and token == token.strip()):'
  with:    '    if token and not (token.isprintable() and token == token.strip()):'
  count:   1
  test:    tests/test_cli_ui.py::test_a_token_that_cannot_travel_in_a_header_is_a_usage_error[caf\xe9]
  red at:  assert result.exit_code == 2

R5-11 the CLI refuses edge whitespace in a token
  file:    src/brain/cli_ui.py
  replace: '    if token and not (token.isascii() and token.isprintable() and token == token.strip()):'
  with:    '    if token and not (token.isascii() and token.isprintable()):'
  count:   1
  test:    tests/test_cli_ui.py::test_a_token_that_cannot_travel_in_a_header_is_a_usage_error[ padded]
  red at:  assert result.exit_code == 2

```

**Round 6 — against `c21c52f`.**

```text
R6-1 the wildcard notice is absent on non-wildcard binds
  file:    src/brain/cli_ui.py
  replace: '    if host.strip().lower() in WILDCARD_HOSTS:\n'
  with:    '    if True:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_wildcard_bind_does_not_print_the_wildcard_warning
  red at:  assert WILDCARD_NOTICE not in result.output

R6-2 a wildcard panel never prints the bind address
  file:    src/brain/ui/server.py
  replace: 'url = f"{bind_origin(host, port)}/"'
  with:    'url = f"http://{host}:{port}/"'
  count:   1
  test:    tests/test_cli_ui.py::test_wildcard_bind_prints_a_loopback_url_not_the_bind_address
  red at:  assert "http://0.0.0.0" not in result.output

R6-3 the Phone line is absent on loopback
  file:    src/brain/cli_ui.py
  replace: '    if lan:\n        typer.echo(f"  Phone'
  with:    '    if True:\n        typer.echo(f"  Phone'
  count:   1
  test:    tests/test_cli_ui.py::test_the_phone_line_is_printed_only_for_lan
  red at:  assert PHONE_LINE not in loopback.output

R6-4 the Phone line is present on --lan
  file:    src/brain/cli_ui.py
  replace: '    if lan:\n        typer.echo(f"  Phone'
  with:    '    if False:\n        typer.echo(f"  Phone'
  count:   1
  test:    tests/test_cli_ui.py::test_the_phone_line_is_printed_only_for_lan
  red at:  assert PHONE_LINE in lan.output

R6-5 vault-and-wiki has no '→ open browser'
  file:    docs/vault-and-wiki.md
  replace: '               # build (if needed) → start build watcher. Opens the wiki only\n               # with BRAIN_OPEN_BROWSER=1. Idempotent.'
  with:    '               # build (if needed) → start build watcher → open browser.\n               # Idempotent.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_vault_and_wiki_documents_the_brain_up_default_truthfully
  red at:  assert "→ open browser" not in text

R6-6 LOCALHOST serves its own URL (middleware)
  file:    src/brain/ui/context.py
  replace: '        return bare.lower()\n'
  with:    '        return bare\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_hostname_bind_is_matched_case_insensitively[LOCALHOST-localhost:8765-200]
  red at:  assert response.status_code == status

R6-7 --host LOCALHOST binds as localhost (CLI)
  file:    src/brain/ui/context.py
  replace: '        return bare.lower()\n'
  with:    '        return bare\n'
  count:   1
  test:    tests/test_cli_ui.py::test_an_upper_case_localhost_binds_as_localhost
  red at:  assert started.context_kwargs["host"] == "localhost"

R6-8 a setsockopt failure closes the socket
  file:    src/brain/ui/server.py
  replace: '        sock.close()\n        name = '
  with:    '        name = '
  count:   1
  test:    tests/test_cli_ui.py::test_a_socket_that_cannot_be_configured_is_closed_and_reported
  red at:  the probe socket leaked

R6-9 a setsockopt failure is reported as configure, not open
  file:    src/brain/ui/server.py
  replace: '        raise BrainError(\n            f"cannot configure an {name} probe socket for --host {address} "\n            f"({exc.strerror or exc})"\n        ) from exc\n'
  with:    '        raise\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_socket_that_cannot_be_configured_is_closed_and_reported
  red at:  assert "configure" in message

```

**Round 7 — against `ab560d4`.**

```text
R7-1 the route target is not loopback
  file:    src/brain/ui/lan.py
  replace: '_ROUTE_TARGET = ("192.0.2.1", 9)'
  with:    '_ROUTE_TARGET = ("127.0.0.1", 9)'
  count:   1
  test:    tests/test_ui_lan.py::test_the_route_target_is_a_documentation_address_off_the_lan
  red at:  assert any(address in net for net in documentation)

R7-2 the route target is not a LAN address
  file:    src/brain/ui/lan.py
  replace: '_ROUTE_TARGET = ("192.0.2.1", 9)'
  with:    '_ROUTE_TARGET = ("192.168.1.1", 9)'
  count:   1
  test:    tests/test_ui_lan.py::test_the_route_target_is_a_documentation_address_off_the_lan
  red at:  assert any(address in net for net in documentation)

R7-3 the notice names the token as the guard
  file:    src/brain/ui/lan.py
  replace: '"network; the token is what keeps other clients out. Use --lan, or "'
  with:    '"network. Use --lan, or "'
  count:   1
  test:    tests/test_ui_lan.py::test_the_wildcard_notice_says_the_token_is_the_guard
  red at:  assert "token" in WILDCARD_NOTICE

R7-4 the notice does not claim other devices are kept out
  file:    src/brain/ui/lan.py
  replace: '"a phone\'s browser cannot use this bind: a wildcard address accepts only "'
  with:    '"other devices cannot use this bind: a wildcard address accepts only "'
  count:   1
  test:    tests/test_ui_lan.py::test_the_wildcard_notice_says_the_token_is_the_guard
  red at:  assert "other devices cannot use this bind" not in WILDCARD_NOTICE

R7-5 the guide says the token keeps other clients out
  file:    docs/guides/brain-ui-on-your-phone.md
  replace: 'wildcard, a LAN address, `--lan` — it is the token that keeps other clients out.'
  with:    'wildcard, a LAN address, `--lan` — the bind decides who can connect.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_phone_guide_covers_the_q1_contract
  red at:  phone guide is missing 'it is the token that keeps other clients out'

R7-6 the CLI refuses an IPv4 shorthand
  file:    src/brain/cli_ui.py
  replace: '    if reading is not None:\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_canonical_ipv4_literal_is_refused
  red at:  assert result.exit_code == 2

R7-7 the shorthand check asks the IPv4 resolver
  file:    src/brain/ui/context.py
  replace: '            host, None, family=socket.AF_INET, flags=socket.AI_NUMERICHOST'
  with:    '            host, None, family=socket.AF_INET6, flags=socket.AI_NUMERICHOST'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_canonical_ipv4_literal_is_refused
  red at:  assert result.exit_code == 2

R7-8 a hostname is not refused as a shorthand
  file:    src/brain/ui/context.py
  replace: '    except socket.gaierror:\n        return None\n'
  with:    '    except socket.gaierror:\n        return host\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_numeric_looking_hostname_that_is_not_an_address_still_binds
  red at:  assert result.exit_code == 0

R7-9 the refusal names what the system reads
  file:    src/brain/cli_ui.py
  replace: 'f"it as {reading}); write the address out in full."'
  with:    'f"it as something else); write the address out in full."'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_canonical_ipv4_literal_is_refused[0-0.0.0.0]
  red at:  assert means in result.output

```

### 10.10 Review round 3 (2026-10-13)

The phase-7 review of `5402c8f`: one MEDIUM, four LOW. Fixed in `3994ea3` (deps) and `5627743`
(UI), recorded here.

| Item | Fix |
|---|---|
| 1 MEDIUM — starlette floor too low for W1 | `starlette>=1.7,<2`. Measured: on 1.6.0 the two `::` security tests fail (`[::1]:8765` split to `[`, 400); on 1.7.0 they pass; the UI + MCP set (143 tests) passes on 1.7.0 and on 1.8.0. mcp 2.3.0 asks `>=0.27` and sse-starlette `>=0.49.1`, so 1.7 narrows nothing they need; `pip install --dry-run -e ".[dev,browser]"` resolves cleanly. A new test pins the floor (excludes 1.6.0, admits 1.7.0). No other test bounded starlette. |
| 2 LOW — `--host [::]` died in uvicorn | the CLI normalises the host once (`context.bind_address`); `bind_origin` re-brackets for URLs. `[::1]` was the same pre-existing failure and is fixed by the same line |
| 3 LOW — `::` is IPv6-only (V6ONLY) | the probe socket sets `IPV6_V6ONLY=1` like asyncio's bind, asserted on the option (the behavioural difference is Linux-only: macOS reports both free — measured; Linux NOT measured, no image cached locally). The notice, guide, CHANGELOG and docstrings now say `::` is IPv6-only, its local URL is `[::1]`, and IPv4 clients are refused at connect |
| 4 LOW — `05fb151` in `tests/test_cli_ui.py` | repointed to `5742c91`. Whole-repo `git grep -nE '4b288e1|05fb151|be6270c|5e07b6c|b862b79|99957eb'` now hits only this plan's history notes |
| 5 LOW — M19 not exact text | exact text with an explicit count; all 59 entries validated (§10.9) |

**Mutation evidence (round 3):** 7 guards, each on all five clauses, against `5627743` on a clean
tree (§10.9).

### 10.11 Audit round 4 (2026-10-13)

The phase-7 audit of `5402c8f`: every prior finding closed; these were new. Fixed in `fd2f064`
(tests, CLAUDE.md) and the commit that added this section (plan).

| Item | Fix |
|---|---|
| 1 MEDIUM F14 — real probe aimed at its own answer | aimed at `127.0.0.2`, expects `127.0.0.1` (measured on macOS; 127.1.2.3 likewise) |
| 2 MEDIUM F16 — README intro unpinned, falsifying the spec's "pinned by a test" | pinned both ways: no "or a [rendered wiki]", and "legacy" beside it |
| 3 LOW F8 — wildcard notice tested on `0.0.0.0` only | parametrized over `0.0.0.0`, `::`, `[::]` |
| 4 LOW — CLAUDE.md setup.py row one hop short | 1,363 (`2475d11`) → 1,364 (`4974f24`) promoted; mcp_server.py's hop promoted to 4,938 (`2475d11`); nothing later touches either |
| 5 LOW — plan records | §10.1 promoted (`f48d858`) and completed; §10.3's `77074bb` labelled plan-only; §10.8 already bound per measurement (fixed in round 3) and now carries the A/F-number legend; the L-3 browser entry notes `-m browser`; M19 verified exact; `A18` absent (legend note); superseded entries marked; validator re-run, 63/63 |

**Mutation evidence (round 4):** 4 guards, each on all five clauses, against `fd2f064` on a clean
tree (§10.9). R4-4 goes red on exactly the two IPv6 parametrizations, `0.0.0.0` staying green.

### 10.12 Review round 5 (2026-10-13)

The phase-7 review of `7363980`. Fixed in `d9a3004` and `932c10a`.

| Item | Fix |
|---|---|
| 1 MEDIUM — an IPv6 bind refused its own URL (a phase-7 regression) | the allowlist holds the Host as it arrives: bracketed and canonical (`[fd00::5]`). `bind_address` canonicalises IP literals, and `host_allowlist`, `bind_origin` and `is_loopback` normalise their input, so `0:0:0:0:0:0:0:1` is loopback and `FD00:0:0::5` is `[fd00::5]`. Pinned by equality tests and through the real middleware: own URL 200, a foreign Host and a neighbour address 400, a write from the browser's literal Origin passes the guard. The guide's "the allowlist holds the address you bound" is true, and says in which form. **E2E unmeasured:** this Mac has only `::1` and link-local `fe80::` addresses (a link-local bind needs a zone ID, which browsers do not reliably carry in a URL), so the middleware tests stand in for it |
| 2 LOW — CHANGELOG W2 bullet under Dependencies | moved back under Fixed |
| 3 LOW — "touches only one file" | three files; corrected in §10.9 with the reason it was wrong |
| 4 LOW — `@needs_ipv6`; raw OSError on a host without IPv6 | decorator added; `_probe_socket` takes an injected `socket_factory`, and a socket that cannot be CREATED is a `BrainError` naming the family. Tested with a factory raising EAFNOSUPPORT, no production module patched |
| 5 pre-existing — non-ASCII `--token` gave 500 on every call | the CLI refuses a token that is not printable ASCII or has edge whitespace (exit 2); the guard compares bytes. Both recorded under Fixed in the CHANGELOG |

**Mutation evidence (round 5):** 11 guards, each on all five clauses, against `932c10a` on a clean
tree (§10.9). Before running them, two of this round's own tests were found weak and fixed in
`932c10a`: nothing pinned `bind_origin`'s normalisation, and the IPv6 write test computed its
Origin with the helper under test, so it could only agree with itself.

### 10.13 Review round 6 (2026-10-13)

The phase-7 review of `593022a`. Fixed in `c21c52f`.

| Item | Fix |
|---|---|
| 1 MEDIUM — a vacuous negative assertion | the non-wildcard test asserted `"Host allowlist"` absent, a phrase `WILDCARD_NOTICE` lost in `8748b70`, so `if True:` on the notice check passed all 150 tests. It now asserts `WILDCARD_NOTICE` itself absent, on `--lan` AND loopback, while the wildcard test asserts it present. **Sweep of every other negative check in this phase's tests:** the Phone line is asserted by the `PHONE_LINE` constant both ways; `"http://0.0.0.0" not in` now runs before its positive twin, so a revert goes red AT it (R6-2); `"→ open browser" not in` had never been mutated on its own and now is (R6-5); `"Traceback" not in result.output` was **vacuous** — CliRunner keeps an uncaught exception in `result.exception` and never writes its traceback to `output` — and is removed, `SystemExit` being the real guard. The rest name text that a proven mutation produces (`"Quartz"` — M5/M6; `"or a [rendered wiki]"` — R4-2; `"and opens the browser"` — M-4; `"pid ?"` — L-4; `127.0.0.1` not in `WILDCARD_HOSTS`, beside an equality check) or predate the phase |
| 2 LOW pre-existing — `--host LOCALHOST` refused its own URL | `bind_address` lower-cases hostnames, so all four helpers agree; pinned through the real middleware (`LOCALHOST` → `localhost` 200; `MyMac.LOCAL` → `mymac.local` 200, a foreign Host 400) and at the CLI |
| 3 — setsockopt after creation | the probe closes the socket and reports "cannot configure", distinct from "cannot open"; tested with an injected factory whose socket raises `ENOPROTOOPT` on `setsockopt` |

**Mutation evidence (round 6):** 9 guards, each on all five clauses, against `c21c52f` on a clean
tree (§10.9). R6-5's first run reported red NOT at its target: a harness-needle error, not a guard
gap — the needle used the wrong quote style. The failure was at exactly the targeted assertion
(`assert "→ open browser" not in text`); the needle was corrected and the whole round re-run.

### 10.14 Audit round 7 (2026-10-13)

The audit's interim findings on `7363980`, still open at `af22825`; #1 (the vacuous wildcard
test) was already closed by round 6. Fixed in `ab560d4`.

| Item | Fix |
|---|---|
| 2 MEDIUM — `_ROUTE_TARGET` unpinned | a recording probe asserts the target `detect_lan_address` passes is in an RFC 5737 documentation range and is not loopback, link-local, unspecified or RFC 1918. `("127.0.0.1", 9)` and `("192.168.1.1", 9)` both go red (R7-1, R7-2). The F14 real-probe test is kept: it pins the OTHER half — that the probe reads the local end of the route — and needs a target unlike its answer, which `127.0.0.2` is |
| 3 LOW — "reachable from this machine and from nowhere else" | false: the auditor measured a LAN client sending `Host: 127.0.0.1:<port>` plus the token getting 200. The Host allowlist is a browser DNS-rebinding defence; on every non-loopback bind the token is the guard. Corrected in `context.py`'s comment, the notice (which now says so — pinned, R7-3/R7-4), the guide (pinned, R7-5), cli-reference, the CHANGELOG, the spec's Q1 annotation and this plan's §3. `brain ui`'s other claims ("cannot serve a phone") were narrowed to the browser case they are true of |
| 4 LOW — `--host 0` | refused (exit 2), with `127.1`, `0x7f.1` and `010.0.0.1`: `ipv4_shorthand` finds IPv4 literals that numeric-only `getaddrinfo` accepts and `ipaddress` does not, and the error names what the system would bind. Refusing beat canonicalising on evidence: platforms disagree on `010.0.0.1` (8.0.0.1 under `inet_aton`, 10.0.0.1 under macOS `getaddrinfo`), so no canonical form is safe to guess. A hostname is not refused (R7-8) |
| `::0`, `0:0:0:0:0:0:0:0` | verified closed by round 5 and now pinned: canonicalised to `::`, notice printed, `http://[::1]:<port>/` printed |

**Mutation evidence (round 7):** 9 guards, each on all five clauses, against `ab560d4` on a clean
tree (§10.9).

**Review round 7 (of `af22825`), folded into the same round** — fixed in `89e4786`; no guard was
added, so no mutation either, and the validator still reads 92/92, 18 marked, exit 0:

| Item | Fix |
|---|---|
| LOW — `PHONE_LINE` split `ui_app` from its `#:` doc-comment | moved above it; each `#:` block documents its own symbol |
| LOW — CHANGELOG missing round 6's user-visible fix | `--host LOCALHOST` / mixed-case hostnames refusing their own URL, under Fixed; round 7's `--host 0` refusal was already under Added |
| NIT — triple blank lines | five in `tests/test_cli_ui.py` (the reviewer's lines) and one more in `tests/test_ui_lan.py`, all added by this phase (none at `1a1d068`), collapsed to two. **ruff 0.16.10 — the version CI resolves — does not flag them under `--select E303 --preview`**; they were found by scanning every test file the phase touched for three consecutive blank lines |
