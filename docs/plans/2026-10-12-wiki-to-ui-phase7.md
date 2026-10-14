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
| Review round 7 — plan | `6c02fe9` | this file: §10.14 |
| Merge of `second-brain/master` | `136030b` | master's #18–#21 delta (8 files); one conflict in `tests/test_dev_dependency_pins.py`, both sides kept. MERGED, not rebased, so every SHA in this file stays valid |
| Review round 8 — fixes | `a4c2f35` | `cli_ui.py` (empty host, zones), `ui/context.py` (`meaningless_zone`), CHANGELOG, this file's ruff note, `test_cli_ui.py` (runner width, new tests), `test_ui_lan.py` |
| Review round 8 — plan | `08c1ac3` | this file: §10.9, §10.15 |
| Audit round 9 — code | `f1bdbea` | `ui/context.py` (`validate_host`, `HostRefused`), `cli_ui.py`, CHANGELOG, `test_cli_ui.py` (`REFUSED_HOSTS` table, S14, probe options) |
| Audit round 9 — plan | `f73d694` | this file: §10.9 (validator test-existence check), §10.16 |
| Round 9 — review folded in | `2e01ea0` | `ui/context.py` (`validate_host` by resolved address, `address_class`), `ui/security.py`, `cli_ui.py`, CHANGELOG, guide, this file §10.15, `HOST_SPELLINGS` table, class tests |
| Round 9 — E303 | `3f2bf65` | `test_ui_security.py` blank line |
| Round 9 — plan | `6f6e3ba` | this file: §10.9, §10.16 |
| Final — code | `f995721` | `ui/server.py` (only EADDRINUSE is busy; `_bind_failure`), `ui/context.py` (hostname syntax, zoneless link-local), CHANGELOG, `HOST_SPELLINGS` rows, probe-class tests, four E303 spots in untouched test files |
| Final — tests | `98c99f6` | `test_cli_ui.py`: both busy phrasings forbidden; the EACCES remediation asserted |
| Final — plan | `4c164cb` | this file: §10.9, §10.17 |
| S19/S20 — tests | `1d9e65b` | `test_reading_surface_pointers.py`: cli-reference "primary", docs index "legacy" |
| S19/S20 — plan | `ae7dda8` | this file: §10.9, §10.17 |
| Round 10 — code | `733e347` | `ui/context.py` (`classify_addresses`, `literal_addresses`, ASCII/length/idna rules), `ui/security.py`, `ui/server.py` (`resolve_bind_addresses`, per-family probe, serve opens the guard's origin), `cli_ui.py`, CHANGELOG, guide, FAKE_DNS tests |
| Round 10 — tests | `34a85f9` | the ASCII-only pattern; serve's opened origin |
| Round 10 — plan | `c45c3bc` | this file: §10.9, §10.18 |
| Round 11 — code | `4cefe58` | `ui/server.py` (`claim_port`, `bind_listeners`, `_open_listener`, `_listen_socket`; `serve(sockets=…)`; the unreachable EADDRINUSE handler deleted), `ui/context.py` (`bind_origin`), `cli_ui.py` (`_start`), CHANGELOG, guide; `test_cli_ui.py` split into it + `test_cli_ui_host.py` + `test_ui_server_bind.py` + `ui_cli_support.py`; `test_ui_server_preflight.py`, `test_ui_telemetry_persists.py`, `test_ui_security.py` |
| Round 12 — code | `13d1593` | `ui/server.py` (`_MAX_PORT`, `claim_port` range and cap, `_open_listener` closes on any exception, the startup-wrapper comment, uvicorn-version wording), `cli_ui.py` (`--port` 1–65535, stale "probe" comments), `ui/context.py` (stale comment), CHANGELOG, `test_cli_ui.py` (the uvicorn double mirrors `startup(sockets=…)`; a real-uvicorn listener test), `test_ui_server_bind.py` (port-range tests) |
| Round 13 — code | `1264162` | `ui/server.py` (`_listen_socket`: SO_REUSEADDR for specific addresses only; serve's docstring on uvicorn's exit code), CHANGELOG, `test_ui_server_bind.py` (the real-socket overlap test; the options table by address), `test_cli_ui.py` (the real-uvicorn test on an OS-assigned port) |
| Round 14 — code | `969c9b8` | `ui/server.py` (`_loopback_companion`; `bind_listeners` skips a specific address under its own wildcard; docstrings), CHANGELOG, `test_ui_server_bind.py` (companion tests, injected and real; the reverse-order test; the coverage rule on real sockets) |
| Round 15 — code | `6cc04fe` | `ui/server.py` (`_loopback_companion` tolerates EADDRNOTAVAIL), CHANGELOG (the bind window named), `test_ui_server_bind.py` (the missing-loopback regression test) |
| Round 15b — tests | `5bf2c69` | `ui_cli_support.py` (`ipv6_loopback_available`, `needs_ipv6_loopback`), `test_ui_server_bind.py` (the marker on the tests that bind `::1`; the overlap probe's bind inside its handler), `test_cli_ui_host.py` (binds-real rows skip on what they bind) |
| Round 15b — tests | `5367194` | `test_ui_server_bind.py`: the overlap probe's missing-address branch, driven on every platform |
| Rounds 11–15b — plan | `0965b61` (PROMOTED from "the commit that added this row" in round 16) | this file: §10.1, §10.9 (validator: `MOVED` lines; round headers may carry a letter), §10.19–§10.23. Rounds 11 to 15 never got a plan commit of their own: the next round arrived first each time |
| Rounds 11–15b — plan-review fixes | `cb4b6ed` | this file: §10.19 count, §10.23 skips and numbering, the validator's per-test `MOVED` check |
| Round 16 — code | `2af3667` | `ui/context.py` (`LOOPBACK_NAMES`, `is_loopback_alias`; `address_class` demotes an untrusted loopback name), `cli_ui.py` (`LOOPBACK_ALIAS_REASON` in the refusal and the panel), CHANGELOG, guide, cli-reference; tests in `test_cli_ui_host.py`, `test_ui_security.py`, `test_ui_bind_wiring.py`, `test_cli_ui.py`, `test_ui_server_bind.py`, `ui_cli_support.py` |
| Round 16 — plan | `1dfe4c7` (PROMOTED from "the commit that added this row" in round 16b) | this file: §10.1, §10.9 (R11-8 NOTE, round-16 block), §10.24 — superseded in part by round 16b, before any review of it |
| Round 16b — code | `6a46869` | `ui/context.py` (`LOOPBACK_NAMES` = `localhost` only), `cli_ui.py` (`_alias_reason`, the alias panel and Confidential line follow the lens), CHANGELOG, guide, cli-reference; `ui_cli_support.py` (the `build_context` double derives the lens), host, security, wiring and panel tests |
| Merge of `second-brain/master` | `5b13ab9` | master `c30449e` (PRs #22 and #23). One conflict, `CLAUDE.md`'s ceiling table (adjacent rows), both kept; `wiki/build_watcher.py` and `search.py` hops promoted. MERGED, not rebased, so every SHA in this file stays valid |
| Round 16b — tests | `7d1ec3d` | `test_cli_ui_host.py` (the mixed-answer test reads the boxed error as one line), `ui_cli_support.py` (the double's `.get`): two guards the strict sweep at `5b13ab9` found red beside their targets |
| Rounds 16/16b — plan | **the commit that added this row** | this file: §10.1, §10.9 (R16-4 superseded, R16-7 NOTE, round-16b block), §10.24 rewritten for 16, 16b and the merge |

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
superseded and marked, exit 0. At `af22825` (round 6): 83/83, 17 superseded and marked, exit 0. At `9d8024f` (round 7): 92/92, 18 marked, exit 0 — and
again after the merge of `second-brain/master` (`136030b`): unchanged, as a merge preserves every
cited SHA. At `08c1ac3` (round 8): 98/98, 18 marked, exit 0. At `f73d694` (round 9, first half): 106/106, 27 marked,
exit 0. At `6f6e3ba` (round 9 with the review folded in): 116/116, 43
marked, exit 0. At `4c164cb` (the final commit): 125/125, 44 marked, exit 0. At `ae7dda8` (S19/S20): 128/128,
44 marked, exit 0. Result at the commit that added this sentence (round 10): **round 0 22/22,
round 1 17/17, round 2 13/13, round 3 7/7, round 4 4/4, round 5 11/11, round 6 9/9, round 7 9/9,
round 8 6/6, round 9 8/8 + 10/10 + 9/9 + 3/3, round 10 16/16 — 144/144; 54 entries superseded at
HEAD, every one marked; exit 0.** At the round-16 plan commit (rounds 11–15b and 16 added): **201/201, 65 superseded and marked, every round header parsed; exit 0.** At the rounds-16/16b plan commit: **210/210, 66 superseded and marked (R16-4 joined), every round header parsed; exit 0.** The round-header pattern was `Round (\d)` until round 10 — a
"Round 10" block would have been skipped silently; it is `Round (\d+)` now. The address-based restructure superseded 16 entries at once; the
validator listed all of them before any was marked. Its
first run after round 9 flagged nine entries: six whose CLI code moved into `validate_host`, and
three whose text still applies but whose TEST had become a row of the new table — the drift the
text-only check could not see, and the reason it now also checks that each entry's test exists. The HEAD check again flagged the round's one casualty (R3-7) before it was marked. The HEAD check earned its place on its
first run after round 6: it flagged L-13, which round 6 had made inapplicable, before it was marked.
It checks the replaced TEXT only; an entry whose text still applies but whose red-at assertion moved
carries a NOTE line instead (R4-4, L-6 panel).
*(Review round 3, item 5: M19 was an annotation, not exact text, so it matched 0 times; it is
now `'--read-only'` with count 2 — re-verified in round 4.)* Entries marked SUPERSEDED still validate at their
round's SHA, which is what the script checks; at HEAD their text or test has moved, for the reason given.

```python
"""Validate plan §10.9 two ways.

1. Each mutation's `replace` text occurs `count` times at its round's SHA.
2. At HEAD, an entry whose text no longer occurs `count` times, OR whose
   test function no longer exists, carries a SUPERSEDED line saying why — so
   a reader re-running it at HEAD is told. (The test-name half was added in
   review round 9, when consolidating three tests into one table renamed
   tests that still-applicable entries cite.) An entry whose tests MOVED
   (review round 11 split `tests/test_cli_ui.py`) names the new nodes on
   `MOVED … to:` lines, one per `test:` line and in the same order (an
   unmoved test repeats its node), and those are what must exist at HEAD;
   `test:` keeps the node as it was at the round's SHA.
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
    rf"\*\*Round (\d+[a-z]?) — against `([0-9a-f]{{7}})`.*?\n{FENCE}text\n(.*?)\n{FENCE}",
    catalog,
    re.S,
)


def show(sha: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{sha}:{path}"], capture_output=True, text=True, check=True
    ).stdout


failures = 0
superseded = 0
# Every round header must be parsed: a header the pattern above misses (round
# 15b's letter did, until the pattern took one) would drop its whole block from
# validation without a word.
headers = re.findall(r"\*\*Round \S+ — against `", catalog)
if len(headers) != len(rounds):
    failures += 1
    print(f"UNPARSED: {len(headers)} round headers, {len(rounds)} blocks parsed")
for number, sha, body in rounds:
    entries = [e for e in body.strip().split("\n\n") if e.strip()]
    matched = 0
    for entry in entries:
        lines = entry.splitlines()
        fields = {}
        tests = []
        for line in lines[1:]:
            key, _, value = line.partition(":")
            if key.strip() in {"file", "replace", "count"}:
                fields[key.strip()] = value.strip()
            if key.strip() == "test":
                tests.append(value.strip())
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
        moved = [
            line.split(" to: ", 1)[1].strip()
            for line in lines
            if line.strip().startswith("MOVED")
        ]
        # Per test, not per entry: one MOVED line per `test:` line, in order,
        # naming that test's node at HEAD (unchanged when it did not move).
        # `moved or tests` checked only the MOVED nodes, so an entry where one
        # test moved and another vanished passed (plan review of `0965b61`).
        if moved and len(moved) != len(tests):
            failures += 1
            print(
                f"MOVED round {number} {lines[0]!r}: {len(moved)} MOVED line(s) for "
                f"{len(tests)} test(s) — one per test, in order"
            )
        current = moved if len(moved) == len(tests) and moved else tests
        gone = [
            t for t in current
            if f"def {t.split('::')[1].split('[')[0]}(" not in show("HEAD", t.split("::")[0])
        ]
        if show("HEAD", fields["file"]).count(old) != want and not marked:
            failures += 1
            print(f"UNMARKED round {number} {lines[0]!r}: no longer applies at HEAD")
        elif gone and not marked:
            failures += 1
            print(f"UNMARKED round {number} {lines[0]!r}: test gone at HEAD: {gone}")
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
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): serve opens `context.allowed_origin`; use R10-14
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
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): the call is `bind_origin(host, port, resolved)`; the equivalent mutation drops `resolved` (R10-9 is its allowlist twin)
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
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): browser_url returns `_with_token(f"{bind_origin(host, port, resolved)}/", token)`; the equivalent mutation targets that
  file:    src/brain/ui/server.py
  replace: 'url = f"{bind_origin(host, port)}/"'
  with:    'url = f"http://{host}:{port}/"'
  count:   1
  test:    tests/test_cli_ui.py::test_browser_url_for_a_wildcard_bind_is_a_loopback_url
  red at:  assert browser_url(wildcard, 8765

L-6 panel prints loopback URL
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): same rewrite of browser_url
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
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): the panel call is `browser_url(host, resolved_port, token, resolved=resolved)`; the equivalent mutation targets that
  file:    src/brain/cli_ui.py
  replace: 'url=browser_url(host, resolved_port, token),'
  with:    'url=browser_url(host, resolved_port, "" if loopback else token),'
  count:   1
  test:    tests/test_cli_ui.py::test_loopback_with_a_token_prints_the_tokened_url
  red at:  assert "http://127.0.0.1:8765/#t=s3cret" in

M-2/A21 serve opens the tokened URL on loopback
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): serve opens `context.allowed_origin`; use R10-14
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
  MOVED in round 11 (`4cefe58`) to: tests/test_ui_server_bind.py::test_claim_port_binds_a_free_port_on_the_ipv6_wildcard
  NOTE at HEAD (round 11): still applies, but goes red at `port, sockets = claim_port(free, host=wildcard` — the test now claims the port
  file:    src/brain/ui/server.py
  replace: 'family = socket.AF_INET6 if ":" in address else socket.AF_INET'
  with:    'family = socket.AF_INET'
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_port_finds_a_free_port_on_the_ipv6_wildcard
  red at:  assert resolve_port(free, host=wildcard

L-8 port probe still sees a busy IPv6 port
  SUPERSEDED at HEAD by the final commit (`98c99f6`: only EADDRINUSE is busy; the catch-all `except OSError: return False` is gone); use RF-1, RF-3
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
  SUPERSEDED at HEAD by round 9 (the CLI calls context.validate_host instead of bind_address); use R9-4
  file:    src/brain/cli_ui.py
  replace: '    host = bind_address(host)\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui.py::test_a_bracketed_host_is_bound_without_its_brackets
  red at:  assert started.context_kwargs["host"] == bound

R3-4 [::1] binds as ::1
  SUPERSEDED at HEAD by round 9 (same move into validate_host); use R9-4
  file:    src/brain/cli_ui.py
  replace: '    host = bind_address(host)\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui.py::test_a_bracketed_loopback_needs_no_token
  red at:  assert started.context_kwargs["host"] == "::1"

R3-5 the probe socket is IPv6-only
  MOVED in round 11 (`4cefe58`) to: tests/test_ui_server_bind.py::test_the_listen_socket_is_ipv6_only_like_asyncio_makes_it
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
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): the notice condition is `address_class(host) == "wildcard"`; use R9-14
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
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): host_allowlist builds `own` per address class; use R9-16 and the IPv6 equality/middleware tests, which still pin the bracketed form
  file:    src/brain/ui/context.py
  replace: '        return frozenset({f"[{address}]"})\n'
  with:    '        return frozenset({address})\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_loopback_allowlist_is_exactly_the_canonical_host_header
  red at:  assert host_allowlist(given) == expected

R5-2 an IPv6 bind serves its own URL (middleware)
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): same rewrite; the middleware test still pins it
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
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): host_allowlist classifies via address_class; use R9-14, R9-15
  file:    src/brain/ui/context.py
  replace: '    address = bind_address(host)\n    if address in LOOPBACK_HOSTS or address in WILDCARD_HOSTS:\n'
  with:    '    address = host\n    if address in LOOPBACK_HOSTS or address in WILDCARD_HOSTS:\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_non_loopback_allowlist_is_exactly_the_canonical_host_header
  red at:  assert host_allowlist(given) == expected

R5-5 bind_origin normalises its input
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): bind_origin classifies via address_class; the `bind_origin` spelling assertions still pin it
  file:    src/brain/ui/context.py
  replace: '    address = bind_address(host)\n    if address == "0.0.0.0":'
  with:    '    address = host\n    if address == "0.0.0.0":'
  count:   1
  test:    tests/test_ui_security.py::test_bind_origin_brackets_ipv6_and_passes_other_hosts_through
  red at:  assert bind_origin("0:0:0:0:0:0:0:1", 8765)

R5-6 is_loopback normalises its input
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): is_loopback is `address_class(...) == "loopback"`; use R9-15
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
  SUPERSEDED at HEAD by round 11 (`4cefe58`): the probe became `_open_listener`, which creates the socket inside its own `try`; use R11-18
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
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): the notice condition is `address_class(host) == "wildcard"`; the `binds` rows of `HOST_SPELLINGS` also assert the notice ABSENT
  file:    src/brain/cli_ui.py
  replace: '    if host.strip().lower() in WILDCARD_HOSTS:\n'
  with:    '    if True:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_wildcard_bind_does_not_print_the_wildcard_warning
  red at:  assert WILDCARD_NOTICE not in result.output

R6-2 a wildcard panel never prints the bind address
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): browser_url rewritten as above
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
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): `return bare.lower()` now occurs twice (validate_host also lower-cases), so the replace count is 2; use R9-13
  file:    src/brain/ui/context.py
  replace: '        return bare.lower()\n'
  with:    '        return bare\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_hostname_bind_is_matched_case_insensitively[LOCALHOST-localhost:8765-200]
  red at:  assert response.status_code == status

R6-7 --host LOCALHOST binds as localhost (CLI)
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): same — count 2; use R9-13
  file:    src/brain/ui/context.py
  replace: '        return bare.lower()\n'
  with:    '        return bare\n'
  count:   1
  test:    tests/test_cli_ui.py::test_an_upper_case_localhost_binds_as_localhost
  red at:  assert started.context_kwargs["host"] == "localhost"

R6-8 a setsockopt failure closes the socket
  MOVED in round 11 (`4cefe58`) to: tests/test_ui_server_bind.py::test_a_socket_that_cannot_be_configured_is_closed_and_reported
  NOTE at HEAD (round 11): still applies, but goes red at `the socket leaked` — the assertion message dropped "probe"
  file:    src/brain/ui/server.py
  replace: '        sock.close()\n        name = '
  with:    '        name = '
  count:   1
  test:    tests/test_cli_ui.py::test_a_socket_that_cannot_be_configured_is_closed_and_reported
  red at:  the probe socket leaked

R6-9 a setsockopt failure is reported as configure, not open
  SUPERSEDED at HEAD by round 11 (`4cefe58`): the message no longer says "probe socket"; use R11-19
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
  NOTE at HEAD (round 8): still applies, but goes red at `assert "the token is what keeps other clients out" in WILDCARD_NOTICE` — the test now pins the exact phrase
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
  SUPERSEDED at HEAD by round 9 (the check moved into context.validate_host; its rows are in test_a_refused_host_spelling_is_a_usage_error); the equivalent mutation targets `if reading is not None:` there
  file:    src/brain/cli_ui.py
  replace: '    if reading is not None:\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_canonical_ipv4_literal_is_refused
  red at:  assert result.exit_code == 2

R7-7 the shorthand check asks the IPv4 resolver
  SUPERSEDED at HEAD by round 9 (its test became rows of test_a_refused_host_spelling_is_a_usage_error); the mutation itself still applies
  file:    src/brain/ui/context.py
  replace: '            host, None, family=socket.AF_INET, flags=socket.AI_NUMERICHOST'
  with:    '            host, None, family=socket.AF_INET6, flags=socket.AI_NUMERICHOST'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_canonical_ipv4_literal_is_refused
  red at:  assert result.exit_code == 2

R7-8 a hostname is not refused as a shorthand
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): ipv4_shorthand is folded into validate_host's numeric-disagreement rule; use R9-8, R9-10
  file:    src/brain/ui/context.py
  replace: '    except socket.gaierror:\n        return None\n'
  with:    '    except socket.gaierror:\n        return host\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_numeric_looking_hostname_that_is_not_an_address_still_binds
  red at:  assert result.exit_code == 0

R7-9 the refusal names what the system reads
  SUPERSEDED at HEAD by round 9 (the message moved into context.validate_host; its rows are in test_a_refused_host_spelling_is_a_usage_error)
  file:    src/brain/cli_ui.py
  replace: 'f"it as {reading}); write the address out in full."'
  with:    'f"it as something else); write the address out in full."'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_canonical_ipv4_literal_is_refused[0-0.0.0.0]
  red at:  assert means in result.output

```

**Round 8 — against `a4c2f35`.**

```text
R8-1 an empty --host is refused
  SUPERSEDED at HEAD by round 9 (the check moved into context.validate_host); use R9-5
  file:    src/brain/cli_ui.py
  replace: '    if not host.strip():\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_an_empty_host_is_a_usage_error
  red at:  assert result.exit_code == 2

R8-2 the CLI refuses a meaningless zone
  SUPERSEDED at HEAD by round 9 (the check moved into context.validate_host; its rows are in test_a_refused_host_spelling_is_a_usage_error); the equivalent mutation targets `if meaningless_zone(address):` there
  file:    src/brain/cli_ui.py
  replace: '    if meaningless_zone(host):\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_zone_on_the_wildcard_or_loopback_is_a_usage_error
  red at:  assert result.exit_code == 2

R8-3 a zone on loopback is meaningless too
  SUPERSEDED at HEAD by round 9 (its test became the `::1%lo0` row of test_a_refused_host_spelling_is_a_usage_error); the mutation itself still applies
  file:    src/brain/ui/context.py
  replace: '    return address.is_unspecified or address.is_loopback\n'
  with:    '    return address.is_unspecified\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_zone_on_the_wildcard_or_loopback_is_a_usage_error[::1%lo0]
  red at:  assert result.exit_code == 2

R8-4 a link-local zone is left alone
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): ruling N1 REVERSED it — every zone is refused, link-local included; use R9-9, R9-17
  file:    src/brain/ui/context.py
  replace: '    return address.is_unspecified or address.is_loopback\n'
  with:    '    return True\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_zone_on_a_link_local_address_is_left_alone
  red at:  assert result.exit_code == 0

R8-5 the notice names the token by its exact phrase
  file:    src/brain/ui/lan.py
  replace: '"network; the token is what keeps other clients out. Use --lan, or "'
  with:    '"network; the token keeps other clients out. Use --lan, or "'
  count:   1
  test:    tests/test_ui_lan.py::test_the_wildcard_notice_says_the_token_is_the_guard
  red at:  assert "the token is what keeps other clients out" in WILDCARD_NOTICE

R8-6 CLI output assertions hold at a narrow terminal (run with COLUMNS=40)
  SUPERSEDED at HEAD by round 9 (its test became rows of test_a_refused_host_spelling_is_a_usage_error); the width-pin mutation still applies, against e.g. the `127.1` row
  pytest:  run with COLUMNS=40 in the environment — at the default width this passes mutated or not
  file:    tests/test_cli_ui.py
  replace: 'runner = CliRunner(env={"COLUMNS": "200"})\n'
  with:    'runner = CliRunner()\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_non_canonical_ipv4_literal_is_refused[127.1-127.0.0.1]
  red at:  assert "not a canonical IPv4 address" in result.output

```

**Round 9 — against `f1bdbea`.**

```text
S14 bind_address strips only a balanced pair
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_bind_address_strips_only_a_balanced_pair_of_brackets
  file:    src/brain/ui/context.py
  replace: '    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host\n'
  with:    '    bare = host[1:-1] if host.startswith("[") else host\n'
  count:   1
  test:    tests/test_cli_ui.py::test_bind_address_strips_only_a_balanced_pair_of_brackets
  red at:  assert bind_address("[::1") == "[::1"

R9-1 unbalanced brackets are refused
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): its test became rows of `HOST_SPELLINGS`; the mutation itself still applies
  file:    src/brain/ui/context.py
  replace: '        if not balanced:\n'
  with:    '        if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_refused_host_spelling_is_a_usage_error[[::1-unbalanced or misplaced brackets]
  red at:  assert result.exit_code == 2

R9-2 repeated brackets are refused
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): its test became rows of `HOST_SPELLINGS`; the mutation itself still applies
  file:    src/brain/ui/context.py
  replace: '            and host.count("[") == 1 and host.count("]") == 1\n'
  with:    '\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_refused_host_spelling_is_a_usage_error[[[::1]]-unbalanced or misplaced brackets]
  red at:  assert says in result.output

R9-3 brackets around a non-IPv6 value are refused
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): its test became rows of `HOST_SPELLINGS`; the mutation itself still applies
  file:    src/brain/ui/context.py
  replace: '        if not isinstance(inner, ipaddress.IPv6Address):\n'
  with:    '        if inner is None and False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_refused_host_spelling_is_a_usage_error[[127.0.0.1]-brackets are for IPv6 literals only]
  red at:  assert result.exit_code == 2

R9-4 the CLI routes --host through validate_host
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): its test became rows of `HOST_SPELLINGS`; the mutation itself still applies
  file:    src/brain/cli_ui.py
  replace: '        host = validate_host(host)\n'
  with:    '        host = host\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_refused_host_spelling_is_a_usage_error[[localhost]-brackets are for IPv6 literals only]
  red at:  assert result.exit_code == 2

R9-5 validate_host still refuses an empty host
  SUPERSEDED at HEAD by round 9, continued (classification by the RESOLVED address, `3f2bf65`): its test became rows of `HOST_SPELLINGS`; the mutation itself still applies
  file:    src/brain/ui/context.py
  replace: '    if not host.strip():\n        raise HostRefused('
  with:    '    if False:\n        raise HostRefused('
  count:   1
  test:    tests/test_cli_ui.py::test_a_refused_host_spelling_is_a_usage_error[-must not be empty]
  red at:  assert result.exit_code == 2

R9-6 the probe sets SO_REUSEADDR
  MOVED in round 11 (`4cefe58`), renamed in round 13 (`1264162`), to: tests/test_ui_server_bind.py::test_the_listen_socket_sets_its_options_by_address
  NOTE at HEAD (round 13): still applies, but goes red at `assert (reuseaddr in sock.options) is reuse`, on the three specific-address rows only — a wildcard sets no SO_REUSEADDR now
  file:    src/brain/ui/server.py
  replace: '        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n'
  with:    '        pass\n'
  count:   1
  test:    tests/test_cli_ui.py::test_the_probe_socket_sets_the_options_the_real_bind_uses
  red at:  assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in sock.options

R9-7 the probe sets IPV6_V6ONLY for IPv6 only
  MOVED in round 11 (`4cefe58`), renamed in round 13 (`1264162`), to: tests/test_ui_server_bind.py::test_the_listen_socket_sets_its_options_by_address[127.0.0.1-False-True]
  file:    src/brain/ui/server.py
  replace: '        if family == socket.AF_INET6:\n            sock.setsockopt(socket.IPPROTO_IPV6'
  with:    '        if True:\n            sock.setsockopt(socket.IPPROTO_IPV6'
  count:   1
  test:    tests/test_cli_ui.py::test_the_probe_socket_sets_the_options_the_real_bind_uses[127.0.0.1-False]
  red at:  assert (v6only in sock.options) is ipv6

```

**Round 9 — against `3f2bf65`** (continued: the review's findings, folded into round 9 — classify by the resolved address).

```text
R9-8 classify by the RESOLVED address (resolution disabled = spelling only)
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[0-refused-reads it as 0.0.0.0]
  NOTE: weaker than its name. Appending "!" does not disable resolution for a ZONED spelling — the resolver reads everything after `%` as the zone, so `::%!` still resolves to `::`. Valid for its target row (`0`); the faithful mutation is R9-18
  file:    src/brain/ui/context.py
  replace: '            bare, None, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST\n'
  with:    '            bare + "!", None, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[0-refused-reads it as 0.0.0.0]
  red at:  assert result.exit_code == 2

R9-9 every zone is refused, by its own rule
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[::%-refused-zone IDs (%\u2026) are not supported]
  file:    src/brain/ui/context.py
  replace: '    if "%" in host:\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[::%-refused-zone IDs (%\u2026) are not supported]
  red at:  assert expected in result.output

R9-10 a numeric disagreement is refused
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[127.1-refused-reads it as 127.0.0.1]
  file:    src/brain/ui/context.py
  replace: '    if spelled is None or resolved != {spelled}:\n'
  with:    '    if spelled is not None and resolved != {spelled}:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[127.1-refused-reads it as 127.0.0.1]
  red at:  assert result.exit_code == 2

R9-11 surrounding whitespace is refused
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[ 127.0.0.1-refused-leading or trailing whitespace]
  NOTE at HEAD (round 11): still applies, but goes red at `assert expected in result.output` — the padded value is now refused by the next rule too (exit 2, "is not an IP address or a hostname"), so only the phrase tells the rules apart
  file:    src/brain/ui/context.py
  replace: '    if host != host.strip():\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[ 127.0.0.1-refused-leading or trailing whitespace]
  red at:  assert result.exit_code == 2

R9-12 an IPv4-mapped address is refused
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[::ffff:127.0.0.1-refused-IPv4-mapped IPv6 addresses cannot be bound]
  file:    src/brain/ui/context.py
  replace: '    if isinstance(spelled, ipaddress.IPv6Address) and spelled.ipv4_mapped is not None:\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[::ffff:127.0.0.1-refused-IPv4-mapped IPv6 addresses cannot be bound]
  red at:  assert result.exit_code == 2

R9-13 a hostname is lower-cased
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[LOCALHOST-binds-localhost]
  NOTE at HEAD (round 11): still applies, but goes red at `assert result.exit_code == 0` — the un-lowered `LOCALHOST` misses the case-sensitive FAKE_DNS and exits 1
  file:    src/brain/ui/context.py
  replace: '        return bare.lower()\n    resolved'
  with:    '        return bare\n    resolved'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[LOCALHOST-binds-localhost]
  red at:  assert started.context_kwargs["host"] == expected

R9-14 the wildcard is classed by address
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): address_class delegates to classify_addresses; use R10-5
  file:    src/brain/ui/context.py
  replace: '    if parsed.is_unspecified:\n'
  with:    '    if address == "0.0.0.0":\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[::0-wildcard-::]
  red at:  assert (WILDCARD_NOTICE in result.output) is (kind == "wildcard")

R9-15 loopback is classed by address
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): same; use R10-6
  file:    src/brain/ui/context.py
  replace: '    if parsed.is_loopback:\n'
  with:    '    if address in ("127.0.0.1", "::1"):\n'
  count:   1
  test:    tests/test_ui_security.py::test_address_class_is_decided_by_the_address_not_the_spelling[127.0.0.2-loopback]
  red at:  assert address_class(host) == kind

R9-16 a loopback allowlist includes its own address
  file:    src/brain/ui/context.py
  replace: '        return LOOPBACK_HOSTS | {own}\n'
  with:    '        return LOOPBACK_HOSTS\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_hostname_bind_is_matched_case_insensitively[127.0.0.2-127.0.0.2:8765-200]
  red at:  assert response.status_code == status

R9-17 L2: the zone refusal names zone IDs exactly
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[fe80::1%en0-refused-zone IDs (%\u2026) are not supported]
  file:    src/brain/ui/context.py
  replace: 'f"--host {host}: zone IDs (%…) are not supported — a browser cannot "'
  with:    'f"--host {host}: zones are not supported — a browser cannot "'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[fe80::1%en0-refused-zone IDs (%\u2026) are not supported]
  red at:  assert expected in result.output

```

**Round 9 — against `98c99f6`** (the final commit: only "in use" is busy; non-addresses and zoneless link-local refused; R9-18 is the faithful classify-by-spelling mutation). Verified with the STRICT clause 4 (§10.17).

```text
R9-18 classify by the RESOLVED address (faithful: resolution never succeeds)
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[0:0::0-wildcard-::]
  file:    src/brain/ui/context.py
  replace: '    try:\n        infos = socket.getaddrinfo(\n'
  with:    '    try:\n        raise socket.gaierror\n        infos = socket.getaddrinfo(\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[0:0::0-wildcard-::]
  red at:  assert result.exit_code == 0

RF-1 the catch-all OSError -> busy is gone
  SUPERSEDED at HEAD by round 11 (`4cefe58`): `_port_is_free` is gone; `_open_listener` raises `_PortBusy` only for EADDRINUSE; use R11-16
  file:    src/brain/ui/server.py
  replace: '            if exc.errno == errno.EADDRINUSE:\n                return False\n            raise _bind_failure(host, port, exc) from exc\n'
  with:    '            return False\n'
  count:   1
  test:    tests/test_cli_ui.py::test_an_address_this_machine_does_not_own_is_reported_as_such
  red at:  assert "is not an address this machine owns" in message

RF-2 an unowned address is not reported as busy (real probe)
  SUPERSEDED at HEAD by round 11 (`4cefe58`): `_port_is_free` is gone; the same guard on the real bind is R11-21
  file:    src/brain/ui/server.py
  replace: '            if exc.errno == errno.EADDRINUSE:\n                return False\n            raise _bind_failure(host, port, exc) from exc\n'
  with:    '            return False\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[1.2.3.4-unowned-is not an address this machine owns]
  red at:  assert busy not in result.output

RF-3 EADDRINUSE still means busy
  SUPERSEDED at HEAD by round 11 (`4cefe58`): `_port_is_free` is gone; use R11-20
  file:    src/brain/ui/server.py
  replace: '            if exc.errno == errno.EADDRINUSE:\n                return False\n'
  with:    '            if False:\n                return False\n'
  count:   1
  test:    tests/test_cli_ui.py::test_only_address_in_use_means_busy
  red at:  assert "already in use" in _probe_with

RF-4 a name that does not resolve says so
  SUPERSEDED at HEAD by round 11 (`4cefe58`): the bind no longer resolves anything — it binds the classified addresses, so a `gaierror` there cannot happen and its test is deleted; an unresolvable name is reported by `resolve_bind_addresses`, R10-15
  file:    src/brain/ui/server.py
  replace: '                f"--host {host} does not resolve to an address on this machine "\n'
  with:    '                f"--host {host} cannot be bound "\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_name_that_does_not_resolve_is_reported_as_such
  red at:  assert "does not resolve to an address on this machine" in message

RF-5 a privileged port is a permission error
  MOVED in round 11 (`4cefe58`) to: tests/test_ui_server_bind.py::test_a_privileged_port_is_a_permission_error_not_busy
  file:    src/brain/ui/server.py
  replace: '    if exc.errno == errno.EACCES:\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_privileged_port_is_a_permission_error_not_busy
  red at:  assert "ports below 1024 need privileges" in message

RF-6 any other failure names its cause
  MOVED in round 11 (`4cefe58`) to: tests/test_ui_server_bind.py::test_any_other_bind_failure_is_reported_not_called_busy
  file:    src/brain/ui/server.py
  replace: '        f"--host {host} cannot be bound on this machine ({exc.strerror or exc})."\n'
  with:    '        f"--host {host} cannot be bound on this machine."\n'
  count:   1
  test:    tests/test_cli_ui.py::test_any_other_bind_failure_is_reported_not_called_busy
  red at:  assert "Invalid argument" in message

RF-7 a non-address, non-hostname is refused
  SUPERSEDED at HEAD by round 10 (`733e347`: the bind's RESOLVED addresses are threaded through): the check gained a length term (`… or len(bare.rstrip(".")) > _HOSTNAME_MAX`); the equivalent mutation targets the whole condition
  file:    src/brain/ui/context.py
  replace: '        if not _HOSTNAME.fullmatch(bare):\n'
  with:    '        if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[127.0.0.1:8080-refused-is not an IP address or a hostname]
  red at:  assert result.exit_code == 2

RF-8 a zoneless link-local address is refused
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[fe80::1-refused-link-local addresses need a zone ID]
  file:    src/brain/ui/context.py
  replace: '    if isinstance(spelled, ipaddress.IPv6Address) and spelled.is_link_local:\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[fe80::1-refused-link-local addresses need a zone ID]
  red at:  assert result.exit_code == 2

```

**Round 9 — against `1d9e65b`** (the audit's partial sweep, S19/S20: two docs pins). Strict clause 4.

```text
S19 cli-reference calls brain ui the primary reading surface
  file:    docs/cli-reference.md
  replace: 'The primary reading surface: a local, single-user web app over the same corpus.'
  with:    'A local, single-user web app over the same corpus.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_cli_reference_names_brain_ui_the_primary_reading_surface
  red at:  assert "The primary reading surface" in section

S20 the docs index calls the rendered wiki legacy
  file:    docs/README.md
  replace: 'and the legacy Quartz + Caddy rendered wiki (`brain ui` is the primary reading surface).'
  with:    'and the optional Quartz + Caddy rendered wiki.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_docs_index_calls_the_rendered_wiki_legacy
  red at:  assert "the legacy Quartz + Caddy rendered wiki" in row

S20b the docs index names brain ui the primary reading surface
  file:    docs/README.md
  replace: ' (`brain ui` is the primary reading surface).'
  with:    '.'
  count:   1
  test:    tests/test_reading_surface_pointers.py::test_docs_index_calls_the_rendered_wiki_legacy
  red at:  assert "`brain ui` is the primary reading surface" in row

```

**Round 10 — against `34a85f9`** (review round 10 of `4c164cb`: classify a hostname by what it resolves to; odd names refused). Strict clause 4.

```text
R10-1 idna's UnicodeError is caught
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[.-refused-is not an IP address or a hostname]
  file:    src/brain/ui/context.py
  replace: '    except UnicodeError:\n        # The idna codec'
  with:    '    except LookupError:\n        # The idna codec'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[.-refused-is not an IP address or a hostname]
  red at:  assert result.exit_code == 2

R10-2 a non-ASCII host is refused
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[localho\u017ft-refused-must be ASCII]
  file:    src/brain/ui/context.py
  replace: '    if not host.isascii():\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[localho\u017ft-refused-must be ASCII]
  red at:  assert expected in result.output

R10-3 the hostname pattern is ASCII-only
  file:    src/brain/ui/context.py
  replace: '    re.ASCII,\n'
  with:    ''
  count:   1
  test:    tests/test_ui_security.py::test_the_hostname_pattern_is_ascii_only
  red at:  assert not _HOSTNAME.fullmatch(name)

R10-4 a name over 253 characters is refused
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says
  file:    src/brain/ui/context.py
  replace: ' or len(bare.rstrip(".")) > _HOSTNAME_MAX'
  with:    ''
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says
  red at:  assert result.exit_code == 2

R10-5 ANY unspecified address makes a wildcard
  file:    src/brain/ui/context.py
  replace: '    if any(address.is_unspecified for address in addresses):\n'
  with:    '    if all(address.is_unspecified for address in addresses):\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_name_is_classified_by_every_address_it_resolves_to[addresses1-wildcard]
  red at:  assert address_class("some.name", resolved) == kind
  NOTE at HEAD (round 16): still applies, but goes red at `assert address_class("localhost", resolved) == kind` — round 16 judges this table for `localhost`, because an untrusted name can no longer be loopback and so could not show the table's "loopback" row

R10-6 loopback only if ALL addresses are loopback
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_a_name_resolving_to_loopback_and_elsewhere_is_not_loopback
  file:    src/brain/ui/context.py
  replace: '    if all(address.is_loopback for address in addresses):\n'
  with:    '    if any(address.is_loopback for address in addresses):\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_name_resolving_to_loopback_and_elsewhere_is_not_loopback
  red at:  assert result.exit_code == 2

R10-7 the CLI classifies by the resolved addresses
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[0.0.0.0.nip.io-wildcard-0.0.0.0.nip.io]
  file:    src/brain/cli_ui.py
  replace: '    kind = address_class(host, resolved)\n'
  with:    '    kind = address_class(host)\n'
  count:   1
  test:    tests/test_cli_ui.py::test_every_host_spelling_does_what_the_table_says[0.0.0.0.nip.io-wildcard-0.0.0.0.nip.io]
  red at:  assert (WILDCARD_NOTICE in result.output) is (kind == "wildcard")

R10-8 the CLI hands the resolved addresses to build_context
  SUPERSEDED at HEAD by round 11 (`4cefe58`): the `build_context` call moved into `cli_ui._start`; use R11-22
  file:    src/brain/cli_ui.py
  replace: '            resolved=resolved,\n        )\n    except BrainError as exc:'
  with:    '        )\n    except BrainError as exc:'
  count:   1
  test:    tests/test_cli_ui.py::test_the_cli_threads_the_resolved_addresses_into_the_context
  red at:  assert started.context_kwargs["resolved"]

R10-9 build_context's allowlist follows the resolved addresses
  file:    src/brain/ui/server.py
  replace: '        allowed_hosts=host_allowlist(host, resolved),\n'
  with:    '        allowed_hosts=host_allowlist(host),\n'
  count:   1
  test:    tests/test_ui_bind_wiring.py::test_build_context_classifies_a_hostname_by_its_resolved_addresses[0.0.0.0.nip.io-resolved0-http://127.0.0.1:8765-hosts0-False]
  red at:  assert context.allowed_hosts == hosts

R10-10 build_context's loopback lens follows the resolved addresses
  file:    src/brain/ui/server.py
  replace: '    loopback = is_loopback(host, resolved)\n'
  with:    '    loopback = is_loopback(host)\n'
  count:   1
  test:    tests/test_ui_bind_wiring.py::test_build_context_classifies_a_hostname_by_its_resolved_addresses[localhost-resolved1-http://localhost:8765-hosts1-True]
  red at:  assert context.serve_confidential_bodies is confidential

R10-11 the probe binds every resolved family
  SUPERSEDED at HEAD by round 11 (`4cefe58`): the per-address probe became `bind_listeners`; use R11-8 and R11-23
  file:    src/brain/ui/server.py
  replace: '    if addresses:\n        # One probe per resolved address'
  with:    '    if False:\n        # One probe per resolved address'
  count:   1
  test:    tests/test_cli_ui.py::test_the_probe_binds_every_resolved_family
  red at:  assert resolve_port(

R10-12 a name is resolved passively, like the bind
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_resolve_bind_addresses_resolves_a_name_passively_like_the_bind
  file:    src/brain/ui/server.py
  replace: 'socket.SOCK_STREAM, 0, socket.AI_PASSIVE)'
  with:    'socket.SOCK_STREAM, 0, 0)'
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_bind_addresses_resolves_a_name_passively_like_the_bind
  red at:  assert (host, family, kind, flags) ==

R10-13 a literal never reaches the resolver
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_resolve_bind_addresses_never_resolves_a_literal
  file:    src/brain/ui/server.py
  replace: '    if literal:\n        return literal\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_bind_addresses_never_resolves_a_literal
  red at:  raise AssertionError("a literal must not reach the resolver")

R10-14 serve opens the guard's origin
  file:    src/brain/ui/server.py
  replace: '    url = _with_token(f"{context.allowed_origin}/", context.token)'
  with:    '    url = _with_token(f"http://{host}:{port}/", context.token)'
  count:   1
  test:    tests/test_cli_ui.py::test_serve_opens_the_guards_origin_not_the_bind_spelling
  red at:  assert opened ==

R10-15 a hostname that will not resolve is reported
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_resolve_bind_addresses_reports_a_name_that_will_not_resolve
  file:    src/brain/ui/server.py
  replace: '    except socket.gaierror as exc:\n        raise BrainError(\n            f"--host {host} does not resolve'
  with:    '    except OSError as exc:\n        raise BrainError(\n            f"--host {host} cannot resolve'
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_bind_addresses_reports_a_name_that_will_not_resolve
  red at:  assert "does not resolve to an address on this machine" in str(excinfo.value)

R10-16 the resolver's UnicodeError is reported cleanly
  MOVED in round 11 (`4cefe58`) to: tests/test_cli_ui_host.py::test_resolve_bind_addresses_reports_a_unicode_failure_cleanly
  file:    src/brain/ui/server.py
  replace: '    except UnicodeError as exc:\n        raise BrainError(f"--host {host} is not a valid hostname.") from exc\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui.py::test_resolve_bind_addresses_reports_a_unicode_failure_cleanly
  red at:  raise UnicodeError("idna: label empty or too long")

```

**Round 11 — against `4cefe58`** (review round 11 of `c45c3bc`: bind exactly the classified addresses and serve those sockets; `bind_origin`'s unspecified-IPv4 rule; the test split). Strict clause 4.

```text
R11-1 CLI re-resolves the host for the bind
  file:    src/brain/cli_ui.py
  replace: 'port, host=host, auto=auto_port, addresses=resolved'
  with:    'port, host=host, auto=auto_port, addresses=ui_server.resolve_bind_addresses(host)'
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_cli_binds_what_it_classified_even_if_dns_changes_its_answer
  red at:  the host was resolved more than once

R11-2 serve lets uvicorn bind for itself
  file:    src/brain/ui/server.py
  replace: '    server.run(sockets=sockets)\n'
  with:    '    server.run()\n'
  count:   1
  test:    tests/test_cli_ui.py::test_serve_runs_uvicorn_on_the_sockets_it_was_given_and_closes_them
  red at:  serve() let uvicorn bind for itself

R11-3 serve does not close its sockets
  file:    src/brain/ui/server.py
  replace: '        # claim_port, so nothing else would release them.\n        for sock in sockets:\n            sock.close()\n'
  with:    '        # claim_port, so nothing else would release them.\n        pass\n'
  count:   1
  test:    tests/test_cli_ui.py::test_serve_runs_uvicorn_on_the_sockets_it_was_given_and_closes_them
  test:    tests/test_cli_ui.py::test_serve_closes_its_sockets_when_startup_fails
  red at:  serve() left its socket open

R11-4 CLI does not close sockets on a failed start
  file:    src/brain/cli_ui.py
  replace: '        # serve() closes them too; this covers a failure before it runs.\n        for sock in sockets:\n            sock.close()\n'
  with:    '        # serve() closes them too; this covers a failure before it runs.\n        pass\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_cli_closes_its_sockets_when_startup_fails
  red at:  the CLI leaked its socket

R11-5 bind_listeners leaks earlier sockets
  file:    src/brain/ui/server.py
  replace: '    except BaseException:\n        for sock in sockets:\n            sock.close()\n        raise\n'
  with:    '    except BaseException:\n        raise\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_failed_address_closes_every_socket_already_bound
  red at:  a bound socket leaked

R11-6 a failed bind leaks its own socket
  file:    src/brain/ui/server.py
  replace: '    except OSError as exc:\n        sock.close()\n        # ONLY'
  with:    '    except OSError as exc:\n        # ONLY'
  count:   1
  test:    tests/test_ui_server_bind.py::test_an_address_this_machine_does_not_own_is_reported_as_such
  red at:  a socket leaked

R11-7 the claimed socket is not listening
  file:    src/brain/ui/server.py
  replace: '        sock.listen(_BACKLOG)\n'
  with:    ''
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_claimed_socket_is_already_listening
  red at:  the claimed socket is not listening

R11-8 bind the host spelling, not the classified address
  NOTE at HEAD (round 13, recorded in round 16): still applies, but goes red at `does not appear to be an IPv4 or IPv6 address` — since `1264162`, `_listen_socket` parses its address (`ipaddress.ip_address(address)`, to decide SO_REUSEADDR), so the hostname `brain-1.lan` raises ValueError before any bind and the list assertion is never reached. Found by the phase-7 audit of `cb4b6ed`; red at its recorded assertion on `4cefe58`, re-proved with this needle in round 16
  file:    src/brain/ui/server.py
  replace: 'sockets.append(_open_listener(host, str(address), port, socket_factory))'
  with:    'sockets.append(_open_listener(host, host, port, socket_factory))'
  count:   1
  test:    tests/test_ui_server_bind.py::test_claim_port_binds_exactly_the_given_addresses_and_listens
  red at:  sock.bound for sock in factory.made

R11-9 a busy address is skipped, binding a subset
  SUPERSEDED at HEAD by round 14 (`969c9b8`): the loop body moved under the coverage rule (deeper indent); use R14-7
  file:    src/brain/ui/server.py
  replace: '        for address in addresses:\n            sockets.append(_open_listener(host, str(address), port, socket_factory))\n'
  with:    '        for address in addresses:\n            try:\n                sockets.append(_open_listener(host, str(address), port, socket_factory))\n            except _PortBusy:\n                pass\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_busy_address_frees_the_others_before_the_next_port
  red at:  assert port == 8766

R11-10 a hostname with no addresses binds nothing, silently
  file:    src/brain/ui/server.py
  replace: '    if not targets:\n        raise BrainError(f"--host {host} has no address to bind; resolve it first.")\n'
  with:    ''
  count:   1
  test:    tests/test_ui_server_bind.py::test_claim_port_refuses_a_hostname_it_was_not_given_addresses_for
  red at:  DID NOT RAISE

R11-11 bind_origin: any IPv4 address picks 127.0.0.1
  file:    src/brain/ui/context.py
  replace: 'any(a.is_unspecified and a.version == 4 for a in addresses)'
  with:    'any(a.version == 4 for a in addresses)'
  count:   1
  test:    tests/test_ui_security.py::test_a_wildcard_name_opens_the_loopback_of_its_unspecified_family
  red at:  assert bind_origin("brain-1.lan", 8765, addresses) == expected

R11-12 the CLI serves without the claimed sockets
  file:    src/brain/cli_ui.py
  replace: '        sockets=sockets,\n        open_browser=open_browser,\n    )\n'
  with:    '        sockets=[],\n        open_browser=open_browser,\n    )\n'
  count:   1
  test:    tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[ip6-localhost-binds-real-ip6-localhost]
  red at:  started.bound

R11-13 the listen socket drops SO_REUSEADDR
  SUPERSEDED at HEAD by round 13 (`1264162`): the line now sits inside `if not … is_unspecified:`, so deleting it leaves an empty block (a SyntaxError, not a mutation); use R13-3, and R9-6 for the specific rows
  file:    src/brain/ui/server.py
  replace: '        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n'
  with:    ''
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_listen_socket_sets_the_options_asyncio_uses
  red at:  SO_REUSEADDR

R11-14 the listen socket is dual-stack
  file:    src/brain/ui/server.py
  replace: '            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)\n'
  with:    '            pass\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_listen_socket_is_ipv6_only_like_asyncio_makes_it
  red at:  IPV6_V6ONLY

R11-15 --no-auto-port steps anyway
  SUPERSEDED at HEAD by round 12 (`13d1593`): the candidate range became `stop = min(…)`, capped at 65535; use R12-9
  file:    src/brain/ui/server.py
  replace: '    candidates = range(start, start + attempts) if auto else range(start, start + 1)\n'
  with:    '    candidates = range(start, start + attempts)\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_no_auto_port_refuses_with_the_remediation_text
  red at:  DID NOT RAISE

R11-16 every bind failure is busy again
  file:    src/brain/ui/server.py
  replace: '        if exc.errno == errno.EADDRINUSE:\n            raise _PortBusy from exc\n'
  with:    '        raise _PortBusy from exc\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_an_address_this_machine_does_not_own_is_reported_as_such
  red at:  is not an address this machine owns

R11-17 a socket that cannot be configured leaks
  file:    src/brain/ui/server.py
  replace: '        sock.close()\n        name = "IPv6" if family == socket.AF_INET6 else "IPv4"\n'
  with:    '        name = "IPv6" if family == socket.AF_INET6 else "IPv4"\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_socket_that_cannot_be_configured_is_closed_and_reported
  red at:  the socket leaked

R11-18 no IPv6 support is a clean BrainError (was R5-8)
  file:    src/brain/ui/server.py
  replace: '    try:\n        sock = _listen_socket(address, socket_factory)\n    except OSError as exc:\n'
  with:    '    sock = _listen_socket(address, socket_factory)\n    if False:\n        exc = OSError()\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_claim_port_reports_a_host_without_ipv6_cleanly
  red at:  Address family not supported

R11-19 a setsockopt failure is reported as configure (was R6-9)
  file:    src/brain/ui/server.py
  replace: '        raise BrainError(\n            f"cannot configure an {name} socket for --host {address} "\n            f"({exc.strerror or exc})"\n        ) from exc\n'
  with:    '        raise\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_socket_that_cannot_be_configured_is_closed_and_reported
  red at:  assert "configure" in message

R11-20 EADDRINUSE still means busy (was RF-3)
  file:    src/brain/ui/server.py
  replace: '        if exc.errno == errno.EADDRINUSE:\n            raise _PortBusy from exc\n'
  with:    '        if False:\n            raise _PortBusy from exc\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_only_address_in_use_means_busy
  red at:  assert "already in use" in _bind_with

R11-21 an unowned address is not busy, real bind (was RF-2)
  file:    src/brain/ui/server.py
  replace: '        if exc.errno == errno.EADDRINUSE:\n            raise _PortBusy from exc\n'
  with:    '        raise _PortBusy from exc\n'
  count:   1
  test:    tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[1.2.3.4-unowned-is not an address this machine owns]
  red at:  assert busy not in result.output

R11-22 the CLI hands the resolved addresses to build_context (was R10-8)
  file:    src/brain/cli_ui.py
  replace: '        resolved=resolved,\n    )\n\n    if resolved_port != port:'
  with:    '    )\n\n    if resolved_port != port:'
  count:   1
  test:    tests/test_cli_ui_host.py::test_the_cli_threads_the_resolved_addresses_into_the_context
  red at:  assert started.context_kwargs["resolved"]

R11-23 each resolved address binds in its own family (was R10-11)
  file:    src/brain/ui/server.py
  replace: 'sockets.append(_open_listener(host, str(address), port, socket_factory))'
  with:    'sockets.append(_open_listener(host, host, port, socket_factory))'
  count:   1
  test:    tests/test_ui_server_bind.py::test_claim_port_binds_every_resolved_family
  red at:  port, sockets = claim_port(
```

**Round 12 — against `13d1593`** (review round 12 of `4cefe58`: the --open startup wrapper forwards the sockets; --port is 1–65535). Strict clause 4.

```text
R12-1 the --open wrapper drops sockets (double)
  file:    src/brain/ui/server.py
  replace: '            await original_startup(*args, **kwargs)\n'
  with:    '            await original_startup()\n'
  count:   1
  test:    tests/test_cli_ui.py::test_serve_opens_the_tokened_url[127.0.0.1]
  red at:  startup ran without the claimed sockets

R12-2 the --open wrapper drops sockets (real uvicorn)
  file:    src/brain/ui/server.py
  replace: '            await original_startup(*args, **kwargs)\n'
  with:    '            await original_startup()\n'
  count:   1
  test:    tests/test_cli_ui.py::test_real_uvicorn_serves_only_the_claimed_socket
  red at:  uvicorn listened beyond the claimed socket

R12-3 --port has no range
  file:    src/brain/cli_ui.py
  replace: '8765, "--port", min=1, max=65535, help='
  with:    '8765, "--port", help='
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_port_outside_1_to_65535_is_a_usage_error[70000]
  test:    tests/test_ui_server_bind.py::test_a_port_outside_1_to_65535_is_a_usage_error[-1]
  test:    tests/test_ui_server_bind.py::test_a_port_outside_1_to_65535_is_a_usage_error[0]
  red at:  assert result.exit_code == 2

R12-4 claim_port accepts port 0
  file:    src/brain/ui/server.py
  replace: '    if not 1 <= start <= _MAX_PORT:\n'
  with:    '    if not 0 <= start <= _MAX_PORT:\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_claim_port_refuses_a_port_outside_the_range[0]
  red at:  DID NOT RAISE

R12-5 claim_port accepts a port past the top
  file:    src/brain/ui/server.py
  replace: '    if not 1 <= start <= _MAX_PORT:\n'
  with:    '    if not 1 <= start:\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_claim_port_refuses_a_port_outside_the_range[70000]
  red at:  Regex pattern did not match

R12-6 auto-port walks past 65535
  file:    src/brain/ui/server.py
  replace: '    stop = min(start + attempts, _MAX_PORT + 1) if auto else start + 1\n'
  with:    '    stop = start + attempts if auto else start + 1\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_auto_port_stops_at_65535_with_a_message
  red at:  OverflowError

R12-7 the exhausted range is misreported
  file:    src/brain/ui/server.py
  replace: 'f"no free port found in {start}..{stop - 1}; "'
  with:    'f"no free port found in {start}..{start + attempts - 1}; "'
  count:   1
  test:    tests/test_ui_server_bind.py::test_auto_port_stops_at_65535_with_a_message
  red at:  assert "no free port found in 65530..65535" in str(excinfo.value)

R12-8 a non-OSError bind failure leaks the socket
  file:    src/brain/ui/server.py
  replace: '        # interrupt can land here too. Closed, then re-raised unchanged.\n        sock.close()\n'
  with:    '        # interrupt can land here too. Closed, then re-raised unchanged.\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_bind_that_raises_a_non_oserror_still_closes_its_socket
  red at:  the socket leaked

R12-9 --no-auto-port steps anyway (was R11-15)
  file:    src/brain/ui/server.py
  replace: '    stop = min(start + attempts, _MAX_PORT + 1) if auto else start + 1\n'
  with:    '    stop = min(start + attempts, _MAX_PORT + 1)\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_no_auto_port_refuses_with_the_remediation_text
  red at:  DID NOT RAISE
```

**Round 13 — against `1264162`** (review round 13 of `13d1593`: no SO_REUSEADDR on a wildcard bind). Strict clause 4.

```text
R13-1 SO_REUSEADDR always set (the wildcard overlaps again)
  NOTE: measured on macOS; the test skips where the platform refuses the overlap (Linux). R13-2 is the guard every platform runs
  file:    src/brain/ui/server.py
  replace: '        if not ipaddress.ip_address(address).is_unspecified:\n'
  with:    '        if True:\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_wildcard_never_binds_over_another_process_on_loopback[ipv4]
  test:    tests/test_ui_server_bind.py::test_a_wildcard_never_binds_over_another_process_on_loopback[ipv6]
  red at:  DID NOT RAISE

R13-2 SO_REUSEADDR always set (options table)
  file:    src/brain/ui/server.py
  replace: '        if not ipaddress.ip_address(address).is_unspecified:\n'
  with:    '        if True:\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_listen_socket_sets_its_options_by_address[0.0.0.0-False-False]
  test:    tests/test_ui_server_bind.py::test_the_listen_socket_sets_its_options_by_address[::-True-False]
  red at:  assert (reuseaddr in sock.options) is reuse

R13-3 SO_REUSEADDR never set (TIME_WAIT for specific binds)
  file:    src/brain/ui/server.py
  replace: '        if not ipaddress.ip_address(address).is_unspecified:\n'
  with:    '        if False:\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_listen_socket_sets_its_options_by_address[127.0.0.1-False-True]
  test:    tests/test_ui_server_bind.py::test_the_listen_socket_sets_its_options_by_address[::1-True-True]
  red at:  assert (reuseaddr in sock.options) is reuse

R13-4 the IPv6 wildcard is judged by its spelling, not its address
  file:    src/brain/ui/server.py
  replace: '        if not ipaddress.ip_address(address).is_unspecified:\n'
  with:    '        if address != "0.0.0.0":\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_listen_socket_sets_its_options_by_address[::-True-False]
  red at:  assert (reuseaddr in sock.options) is reuse
  NOTE (round 14): first proved on `test_a_wildcard_never_binds_over_another_process_on_loopback[ipv6]` (red at `DID NOT RAISE`), measured on macOS — that test skips where the platform refuses the overlap. Retargeted to the options-table `::` row, which runs on every platform; re-proved at `969c9b8`: PASS
```

**Round 14 — against `969c9b8`** (review round 14 of `1264162`: a wildcard also holds its loopback address; a specific address under its own wildcard is not bound twice). Strict clause 4.

```text
R14-1 no loopback companion (real sockets, reverse order)
  NOTE: measured on macOS; the test skips where the platform refuses the overlap (Linux). R14-2 is the guard every platform runs
  file:    src/brain/ui/server.py
  replace: '                if companion is not None:\n                    sockets.append(companion)\n'
  with:    '                if companion is not None:\n                    companion.close()\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_nothing_can_bind_our_loopback_after_our_wildcard[ipv4]
  test:    tests/test_ui_server_bind.py::test_nothing_can_bind_our_loopback_after_our_wildcard[ipv6]
  red at:  DID NOT RAISE

R14-2 no loopback companion (injected sockets)
  file:    src/brain/ui/server.py
  replace: '                if companion is not None:\n                    sockets.append(companion)\n'
  with:    '                if companion is not None:\n                    companion.close()\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_wildcard_also_holds_its_loopback_address[ipv4]
  test:    tests/test_ui_server_bind.py::test_a_wildcard_also_holds_its_loopback_address[ipv6]
  red at:  assert sockets == factory.made

R14-3 the companion's EADDRINUSE means busy
  file:    src/brain/ui/server.py
  replace: '    except _PortBusy:\n        return None\n'
  with:    '    except _PortBusy:\n        raise\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_companion_refused_as_linux_refuses_it_is_tolerated[ipv4]
  test:    tests/test_ui_server_bind.py::test_a_companion_refused_as_linux_refuses_it_is_tolerated[ipv6]
  red at:  BrainError: port 8765 is already in use

R14-4 every companion failure is tolerated
  file:    src/brain/ui/server.py
  replace: '    except _PortBusy:\n        return None\n'
  with:    '    except (_PortBusy, BrainError):\n        return None\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_any_other_companion_failure_is_reported_and_releases_the_wildcard
  red at:  DID NOT RAISE

R14-5 a specific address under its wildcard is bound again
  file:    src/brain/ui/server.py
  replace: '            if address.version in wildcards and not address.is_unspecified:\n                continue\n'
  with:    ''
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_name_resolving_to_loopback_and_the_wildcard_binds_for_real
  red at:  no free port found

R14-6 the companion is bound on the wildcard's address
  file:    src/brain/ui/server.py
  replace: '    loopback = "::1" if wildcard.version == 6 else "127.0.0.1"\n'
  with:    '    loopback = str(wildcard)\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_wildcard_also_holds_its_loopback_address[ipv4]
  red at:  assert [sock.bound for sock in factory.made]

R14-7 a busy address is skipped, binding a subset (was R11-9)
  NOTE: proved against the sibling test DB `second_brain_t_p7b`, while the full suite held the main one — see §10.22 for the one overlap with the run of record
  file:    src/brain/ui/server.py
  replace: '            sockets.append(_open_listener(host, str(address), port, socket_factory))\n'
  with:    '            try:\n                sockets.append(_open_listener(host, str(address), port, socket_factory))\n            except _PortBusy:\n                continue\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_busy_address_frees_the_others_before_the_next_port
  red at:  assert port == 8766
```

**Round 15 — against `6cc04fe`** (review round 15 of `969c9b8`: the companion tolerates EADDRNOTAVAIL). Strict clause 4.

```text
R15-1 a companion address the host lacks aborts the bind
  file:    src/brain/ui/server.py
  replace: '        if isinstance(cause, OSError) and cause.errno == errno.EADDRNOTAVAIL:\n            return None\n        raise\n'
  with:    '        raise\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_a_companion_address_the_host_lacks_is_tolerated[ipv6]
  test:    tests/test_ui_server_bind.py::test_a_companion_address_the_host_lacks_is_tolerated[ipv4]
  red at:  is not an address this machine owns

R15-2 every companion BrainError is tolerated
  file:    src/brain/ui/server.py
  replace: '        if isinstance(cause, OSError) and cause.errno == errno.EADDRNOTAVAIL:\n'
  with:    '        if isinstance(cause, OSError):\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_any_other_companion_failure_is_reported_and_releases_the_wildcard
  red at:  DID NOT RAISE
```

**Round 15b — against `5367194`** (round 15's reviewer note, tests only: skip, not error, where the host has no `::1`). Strict clause 4.

```text
R15b-1 the ::1 probe asks about the wildcard again
  NOTE: container-only — red only where `::` binds and `::1` does not. Proved in `python:3.11-slim` with `--network none --sysctl net.ipv6.conf.lo.disable_ipv6=1`, on `git archive` copies (base and mutated; the worktree untouched), with a scratch plugin outside the repo marking the selection `nodb` so conftest takes no database lock. Base: 4 skipped. Mutated: 2 FAILED, red at the needle on an `E` line; the 2 overlap tests fall through to the round-15b helper and skip. On a host with `::1` both probes agree and this mutation is invisible
  file:    tests/ui_cli_support.py
  replace: '            sock.bind(("::1", 0))\n'
  with:    '            sock.bind(("::", 0))\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_claim_port_binds_every_resolved_family
  test:    tests/test_cli_ui_host.py::test_every_host_spelling_does_what_the_table_says[ip6-localhost-binds-real-ip6-localhost]
  red at:  Cannot assign requested address

R15b-2 the overlap probe's loopback bind is outside its handler again
  file:    tests/test_ui_server_bind.py
  replace: '        try:\n            held.bind((loopback, 0))\n        except OSError:\n            return False\n'
  with:    '        held.bind((loopback, 0))\n'
  count:   1
  test:    tests/test_ui_server_bind.py::test_the_overlap_probe_reads_a_missing_address_as_no_overlap
  red at:  OSError
```

**Round 16 — against `2af3667`** (the phase-7 audit of `cb4b6ed`: only a trusted name can be a tokenless loopback bind; two branches no test executed). Strict clause 4.

```text
R16-1 any name resolving to loopback is loopback again (CLI token rule)
  file:    src/brain/ui/context.py
  replace: '    if literal_addresses(host) or bind_address(host) in LOOPBACK_NAMES:\n        return False\n    return classify_addresses(resolved or ()) == "loopback"\n'
  with:    '    return False\n'
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_loopback_alias_without_a_token_is_refused
  red at:  assert result.exit_code == 2

R16-2 any name resolving to loopback is loopback again (build_context allowlist and lens)
  file:    src/brain/ui/context.py
  replace: '    if literal_addresses(host) or bind_address(host) in LOOPBACK_NAMES:\n        return False\n    return classify_addresses(resolved or ()) == "loopback"\n'
  with:    '    return False\n'
  count:   1
  test:    tests/test_ui_bind_wiring.py::test_build_context_classifies_a_hostname_by_its_resolved_addresses
  red at:  assert context.allowed_hosts == hosts

R16-3 any name resolving to loopback is loopback again (the classification)
  file:    src/brain/ui/context.py
  replace: '    if literal_addresses(host) or bind_address(host) in LOOPBACK_NAMES:\n        return False\n    return classify_addresses(resolved or ()) == "loopback"\n'
  with:    '    return False\n'
  count:   1
  test:    tests/test_ui_security.py::test_only_a_trusted_name_resolving_to_loopback_is_loopback
  red at:  assert address_class(name, resolved) == kind

R16-4 ip6-localhost is not a trusted name
  SUPERSEDED at HEAD by round 16b (`6a46869`): ip6-localhost is no longer trusted, so the set this deletes it from is `{"localhost"}`; the opposite mutation is R16b-1/R16b-2
  file:    src/brain/ui/context.py
  replace: 'frozenset({"localhost", "ip6-localhost"})'
  with:    'frozenset({"localhost"})'
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_trusted_loopback_name_needs_no_token
  red at:  assert result.exit_code == 0

R16-5 the trusted-name check is case-sensitive
  file:    src/brain/ui/context.py
  replace: 'bind_address(host) in LOOPBACK_NAMES'
  with:    'host in LOOPBACK_NAMES'
  count:   1
  test:    tests/test_ui_security.py::test_only_a_trusted_name_resolving_to_loopback_is_loopback
  red at:  assert address_class(name, resolved) == kind

R16-6 a loopback literal can be an alias
  file:    src/brain/ui/context.py
  replace: '    if literal_addresses(host) or bind_address(host) in LOOPBACK_NAMES:\n        return False\n    return classify_addresses(resolved or ()) == "loopback"\n'
  with:    '    if bind_address(host) in LOOPBACK_NAMES:\n        return False\n    return classify_addresses(resolved or ()) == "loopback"\n'
  count:   1
  test:    tests/test_ui_security.py::test_a_loopback_literal_is_never_an_alias
  red at:  assert not is_loopback_alias(literal, resolved)

R16-7 the refusal does not say why an alias needs a token
  file:    src/brain/cli_ui.py
  replace: '    if not loopback and not token and is_loopback_alias(host, resolved):\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_loopback_alias_without_a_token_is_refused
  red at:  only localhost and ip6-localhost are trusted
  NOTE at HEAD (round 16b): still applies, but goes red at `only localhost is trusted as a loopback name` — round 16b's reason names localhost alone, and the test reads the boxed usage error as one line

R16-8 the panel calls a loopback alias "NOT bound to loopback"
  file:    src/brain/cli_ui.py
  replace: '        alias_of=host if is_loopback_alias(host, resolved) else "",\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_loopback_alias_with_a_token_is_guarded_like_a_network_bind
  red at:  guarded like a network bind

R16-9 an empty resolver answer is returned as an empty bind set
  file:    src/brain/ui/server.py
  replace: '    if not addresses:\n        raise BrainError(f"--host {host} does not resolve to an address on this machine.")\n'
  with:    ''
  count:   1
  test:    tests/test_cli_ui_host.py::test_resolve_bind_addresses_reports_an_empty_answer
  red at:  DID NOT RAISE

R16-10 the busy-port notice is dropped
  file:    src/brain/cli_ui.py
  replace: '    if resolved_port != port:\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui.py::test_a_busy_port_is_named_with_the_one_used_instead
  red at:  was busy — using 8766 instead
```

**Round 16b — against `6a46869`** (the review of `2af3667`: `localhost` is the only trusted name; the alias panel follows the confidential lens; the lead's two edge cases). Strict clause 4.

```text
R16b-1 ip6-localhost trusted again (CLI token rule)
  file:    src/brain/ui/context.py
  replace: 'frozenset({"localhost"})'
  with:    'frozenset({"localhost", "ip6-localhost"})'
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_loopback_alias_without_a_token_is_refused
  red at:  assert result.exit_code == 2

R16b-2 ip6-localhost trusted again (build_context allowlist and lens)
  file:    src/brain/ui/context.py
  replace: 'frozenset({"localhost"})'
  with:    'frozenset({"localhost", "ip6-localhost"})'
  count:   1
  test:    tests/test_ui_bind_wiring.py::test_build_context_classifies_a_hostname_by_its_resolved_addresses
  red at:  assert context.allowed_hosts == hosts

R16b-3 localhost. is trusted as localhost
  file:    src/brain/ui/context.py
  replace: 'bind_address(host) in LOOPBACK_NAMES'
  with:    'bind_address(host).rstrip(".") in LOOPBACK_NAMES'
  count:   1
  test:    tests/test_ui_security.py::test_only_a_trusted_name_resolving_to_loopback_is_loopback
  red at:  assert address_class(name, resolved) == kind

R16b-4 the localhost. refusal does not mention the trailing dot
  file:    src/brain/cli_ui.py
  replace: '    if host.endswith(".") and host.rstrip(".") == "localhost":\n'
  with:    '    if False:\n'
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_loopback_alias_without_a_token_is_refused
  red at:  drop the trailing dot to use localhost

R16b-5 the alias note says withheld under --include-confidential
  file:    src/brain/cli_ui.py
  replace: '("" if serve_confidential else ", confidential bodies withheld")'
  with:    '", confidential bodies withheld"'
  count:   1
  test:    tests/test_cli_ui.py::test_the_panel_never_contradicts_the_confidential_lens_on_an_alias
  red at:  assert ("withheld" in out) is not serve_confidential

R16b-6 the Confidential line calls an alias a non-loopback bind
  file:    src/brain/cli_ui.py
  replace: '    elif alias_of:\n        typer.echo(\n'
  with:    '    elif False:\n        typer.echo(\n'
  count:   1
  test:    tests/test_cli_ui.py::test_the_panel_never_contradicts_the_confidential_lens_on_an_alias
  red at:  assert "non-loopback bind" not in out

R16b-7 an alias under --include-confidential is "SERVED over the network"
  file:    src/brain/cli_ui.py
  replace: 'where = "on a loopback alias" if alias_of else "over the network"'
  with:    'where = "over the network"'
  count:   1
  test:    tests/test_cli_ui.py::test_the_panel_never_contradicts_the_confidential_lens_on_an_alias
  red at:  bodies SERVED on a loopback alias

R16b-8 the CLI double hard-codes the lens again (the test of the tests)
  file:    tests/ui_cli_support.py
  replace: 'serve_confidential_bodies=serve, notices=()'
  with:    'serve_confidential_bodies=False, notices=()'
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_loopback_alias_with_include_confidential_never_says_withheld
  red at:  assert "withheld" not in result.output

R16b-9 a trusted name with a mixed answer gets the alias refusal
  file:    src/brain/cli_ui.py
  replace: '    if not loopback and not token and is_loopback_alias(host, resolved):\n'
  with:    '    if not loopback and not token and any(a.is_loopback for a in resolved):\n'
  count:   1
  test:    tests/test_cli_ui_host.py::test_a_name_resolving_to_loopback_and_elsewhere_is_not_loopback
  red at:  assert "only localhost is trusted" not in flat
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
| NIT — triple blank lines | five in `tests/test_cli_ui.py` (the reviewer's lines) and one more in `tests/test_ui_lan.py`, all added by this phase (none at `1a1d068`), collapsed to two. **ruff 0.16.10 — the version CI resolved at the time — did not flag them under `--select E303 --preview`**; they were found by scanning every test file the phase touched for three consecutive blank lines. *(Corrected in round 8: this cell said 0.16.10 was "the version CI resolves"; since #21 CI resolves **0.17.0**, and 0.17.0 DOES flag E303 under `--preview` — measured, it caught one this phase added in round 8. E303 is preview-only, so CI's default `ruff check` gates it under neither version.)* |

### 10.15 Review round 8 (2026-10-13) and the merge of master

The phase-7 review of `6c02fe9`. Fixed in `136030b` (merge) and `a4c2f35`.

| Item | Fix |
|---|---|
| 1 MEDIUM — two more wildcard spellings with no notice | `--host ""` (asyncio binds `0.0.0.0` AND `::`; the panel printed `http://:8765/`, the allowlist was `{""}`) and `::%en0` (binds every interface; matched no `WILDCARD_HOSTS` spelling). Both refused, exit 2: an empty or whitespace-only host, and `context.meaningless_zone` — a zone on `::` or `::1`. **Refused rather than classified:** classifying would carry two spellings of one address through every helper — zoned for the bind, bare for the checks — while refusing is one check; link-local zones, where a zone is real, pass through (pinned). *(Corrected in round 9: the class was NOT closed here. `::%`, `[::%]`, `0::%`, `::0%` still bound `::` with no notice — `ipaddress` rejects the empty zone, so `meaningless_zone` returned False, while the resolver read `::` — and `::1%` bound loopback but classed as other. Each round had refused one more SPELLING; round 9 classifies by the RESOLVED address, refuses every zone (ruling N1, reversing the link-local exception above), and is what closes the class — §10.16.)* |
| 2 MERGE BLOCKER — conflict with master `6a82db2` | `second-brain/master` MERGED (`136030b`), not rebased, so the catalog's SHAs survive (validator re-run: unchanged). The one conflict, in `tests/test_dev_dependency_pins.py` (both sides appended at EOF and shared the hunk's closing paren), keeps master's `test_ceiling_is_the_next_minor_above_the_measured_version` with its `)` restored, then the starlette floor test. #21's `test_every_capped_dev_pin_is_measured` walks `dev` pins only, so it does not cover the RUNTIME starlette pin, which keeps its own test. All 12 pin tests pass on the merged tree; `git merge-tree --write-tree second-brain/master HEAD` is clean |
| 3 LOW — CLI tests depend on terminal width | measured first: four `test_cli_ui` tests (three of this phase's IPv4 cases, and the older `--help` flags test) fail at `COLUMNS=40` and `60` and pass at 200, because Rich wraps usage errors and help at `COLUMNS`. The module's `runner` is pinned to `COLUMNS=200` for every invoke (the house pattern, as `test_cli_directory.py` does); the file passes at 40 and 60. Other test files the phase touched assert on no Rich-boxed output |
| 4 LOW — `"token" in WILDCARD_NOTICE` | the exact phrase, as the guide twin pins it |
| 5 LOW — "ruff 0.16.10, the version CI resolves" | corrected in §10.14's table. Since #21 CI resolves **0.17.0** — and, contrary to the brief, 0.17.0 DOES flag E303 under `--preview`: measured, it caught one triple blank line this round's own test append introduced, fixed in `a4c2f35`. E303 is preview-only, so CI's default `ruff check` gates it under neither version; every test file the phase touched is clean under both the 0.17.0 preview check and a direct scan |

**Mutation evidence (round 8):** 6 guards, each on all five clauses, against `a4c2f35` on a clean
tree, the whole round run with `COLUMNS=40` exported — R8-6 (the runner's width pin) can only go
red at a narrow terminal, and every other test passes at 40, so the baseline is unaffected (§10.9).

### 10.16 Audit round 9 (2026-10-13)

The audit's sweep of `6c02fe9`. Fixed in `f1bdbea`.

| Item | Fix |
|---|---|
| 1 LOW — unbalanced brackets unpinned (S14) | `[::1` and `::1]` reached the probe and exited 1 with "port already in use"; `[127.0.0.1]` and `[localhost]` had their brackets silently dropped; `[]` became empty; `[[::1]]` became `[::1]`. A host may carry brackets only as ONE balanced pair around an IPv6 literal — everything else is a usage error. S14 itself (`startswith("[")` alone in `bind_address`, which turned `[::1` into the wildcard `::`) is pinned by a direct test of `bind_address`, since `validate_host` now refuses that spelling before `bind_address` sees it |
| Consolidation | every refusal lives in ONE function, `context.validate_host`, which returns the canonical bind address or raises `HostRefused` (the CLI maps it to exit 2). One table, `REFUSED_HOSTS` in `test_cli_ui.py`, covers every refused spelling — empty, whitespace, IPv4 shorthand, meaningless zones, unbalanced/repeated brackets, bracketed non-IPv6 — and replaces the three separate tests, so the next odd spelling is one row |
| 2 pre-existing — `SO_REUSEADDR` unpinned | a recording socket from the injected factory asserts `SO_REUSEADDR` on both families and `IPV6_V6ONLY` on IPv6 only (R9-6, R9-7) |
| CHANGELOG | the refusals are one bullet, matching the one validator |

**Mutation evidence (round 9):** 8 guards, each on all five clauses, against `f1bdbea` on a clean
tree (§10.9).

**The review of `08c1ac3`, folded into round 9 — classify by the resolved address.** Fixed in
`2e01ea0` (and `3f2bf65`, a blank line).

| Item | Fix |
|---|---|
| M1 MEDIUM — the class was still open | `--host '[]'` (closed by `f1bdbea`'s bracket rule) and `::%`, `[::%]`, `0::%`, `::0%`: `ipaddress` rejects the empty zone, so `meaningless_zone` said False, while the resolver read `::` — a wildcard bind with no notice; `::1%` bound loopback but classed as other. **Structural fix:** `validate_host` now resolves the host the way the bind will (numeric-only `getaddrinfo`, `AF_UNSPEC`, never DNS) and judges THAT: a numeric spelling `ipaddress` reads differently is refused (this absorbs `ipv4_shorthand`), and `address_class` decides wildcard / loopback / other from the PARSED address for the notice, `is_loopback`, the allowlist and the origin — `127.0.0.2` is loopback, `0:0::0` the wildcard. Each earlier round had refused one more SPELLING; this ends the class |
| (a) L1 — surrounding whitespace | refused (`' 127.0.0.1'` classed as loopback, then failed as "no free port") |
| (b) N1 — **ruling: no zone IDs at all, link-local included** | a browser cannot open a URL that carries a zone, so a zoned bind's printed URL is unusable; the round-8 exception for link-local zones assumed otherwise and is reversed. CHANGELOG and guide (pinned) say so |
| (c) — IPv4-mapped | `::ffff:127.0.0.1` refused: an IPv6-only socket cannot bind it (measured, `EADDRNOTAVAIL`), although `ipaddress` calls it loopback |
| L2 — the zone refusal's phrase | asserted exactly (`zone IDs (%…) are not supported`), and mutation-proven (R9-17) |
| Table | `HOST_SPELLINGS` holds every spelling the phase has met — refused (with its phrase), binds (notice asserted ABSENT) or wildcard (notice asserted PRESENT) — replacing `REFUSED_HOSTS` and three single-spelling tests |
| CHANGELOG, §10.15 | both said the class closed in round 8; corrected |

**Mutation evidence:** 10 more guards on all five clauses, against `3f2bf65` (R9-8 to R9-17). R9-8
is the headline: disable the resolution step — classify by spelling again — and `0` binds. R9-9
shows the layering: with the zone rule off, `::%` is STILL refused, by the numeric-disagreement
rule, so the test goes red only at the exact phrase.

### 10.17 The final commit (2026-10-13), and a stricter mutation harness

**A flaw in every round's clause 4, found while answering "classify by spelling must turn `::%`
red".** The harness checked "red at the targeted assertion" by finding the 'red at' text
ANYWHERE in pytest's output. A long traceback prints the test's source UP TO the failing line, so
a needle on an EARLIER line of the test also matched. **Every one of the 116 catalogued entries
was re-verified STRICTLY**, each at its own round SHA, in a separate worktree: the needle must be
on the failing `>` line or in its `E` lines. **Result: 116/116 PASS** — the loose check had
produced no false pass in the catalog. It had produced one outside it: R9-18, first proven with the
loose check, really fails at a different line; its needle was corrected before cataloguing. From
here every mutation is run with the strict check, which immediately found two weak assertions in
this commit's own new tests (below).

**R9-8 was weaker than its name** (NOTE on its entry): for a zoned spelling the resolver reads
everything after `%` as the zone, so its `"!"` suffix did not disable resolution. The faithful
classify-by-spelling mutation is R9-18 (`raise socket.gaierror` before the lookup). Alone it
turns nine rows red — `0`, `127.1`, `0x7f.1`, `010.0.0.1`, `::ffff:127.0.0.1`, `::0` and `0:0::0`
(no longer recognised as the wildcard), `0:0:0:0:0:0:0:1`, `FD00:0:0::5` — but **not** `::%`,
which the zone rule refuses independently. With the zone rule ALSO off, all nine zone rows go red
and `::%` fails at `assert 0 == 2`: accepted, binding the IPv6 wildcard with no notice — the
original bug. `::%` has two independent guards.

**The final commit** (`f995721`, `98c99f6`) — team-lead's finding, broadened by the audit:

| Item | Fix |
|---|---|
| Every bind failure was "busy" | `_port_is_free`'s `except OSError: return False` reported an unowned address, an unresolvable name and a privileged port as "already in use" / "no free port found". Now only `EADDRINUSE` is busy. `EADDRNOTAVAIL` (49 on macOS, 99 on Linux, named via `errno`) → "is not an address this machine owns"; `socket.gaierror` → "does not resolve to an address on this machine"; `EACCES` → "permission denied … ports below 1024 need privileges; pass --port 1024 or higher"; anything else → "cannot be bound on this machine (<cause>)". **`EACCES` is deliberately NOT busy:** the auto-port bump moves by one, stays inside the privileged range, and would hide the cause. Each class has a test through the injected socket factory; `EADDRINUSE` keeps its two |
| Non-addresses | `validate_host` refuses a value that is neither an IP address nor an RFC 1123 hostname — `127.0.0.1:8080`, `http://127.0.0.1`, `0.0.0.0/0` — before any bind |
| Zoneless link-local | `fe80::1` is refused: it binds only with a zone, and zones are refused (N1) |
| Table | `HOST_SPELLINGS` gains every case; its "unowned" rows run the REAL probe (`1.2.3.4`, `fd00::5`, `brain-2.lan` → exit 1 with the owning/resolving message — *round 10: `brain-2.lan` did real DNS, and `.lan` resolves on many home routers; it is now `brain-2.invalid` (RFC 6761), resolved through the test module's FAKE_DNS*). `127.0.0.2` is platform-dependent and asserted per platform: macOS owns only `127.0.0.1` of 127/8 (exit 1), Linux binds the whole block (exit 0). Every row forbids BOTH busy phrasings |
| Two weak assertions the strict harness found | forbidding only `"in use"` missed the auto-port wording "no free port found"; asserting "permission denied" could not tell the `EACCES` branch from the generic one (strerror says it too). Both fixed in `98c99f6` |
| Scout law | four triple-blank-line spots (ruff 0.17.0 preview E303) in `test_backup_create`, `test_search_explain`, `test_ui_browser_graph`, `test_ui_static_behaviour` — five blank-line deletions, nothing else; `tests/` is now clean under the preview check |

**Mutation evidence (final):** 9 guards — R9-18 and RF-1 to RF-8 — each on all five clauses with
the STRICT clause 4, against `98c99f6` on a clean tree. RF-1 is the headline: restore the catch-all
OSError → busy and the unowned-address test goes red. L-8 (round 2) is superseded by it and marked.

**S19 and S20 (the audit's partial sweep, both LOW)** — `1d9e65b`. Two repointed sentences were
unpinned: `docs/cli-reference.md`'s `## brain ui` ("The primary reading surface: …") and the
`docs/README.md` row for `vault-and-wiki.md` ("the legacy Quartz + Caddy rendered wiki (`brain ui`
is the primary reading surface)"). Each is pinned, the first scoped to its section and the second
to its row. Three mutations (S19, S20, S20b — the parenthetical alone) pass all five clauses with
the strict clause 4, against `1d9e65b`.

**An outage during this step, recorded because it voided a run.** The Docker daemon stopped
(the disk had filled to 2.3 GB free) while the full suite on `4c164cb` was at about 9%. Every DB
test after that point errored, so that run was stopped and counts for nothing. Team-lead freed
space and restarted Docker; the test DB and its sibling databases survived.

### 10.18 Review round 10 (2026-10-13)

The review of `4c164cb`, folded in on top of `1d9e65b`. Fixed in `733e347` and `34a85f9`.

| Item | Fix |
|---|---|
| 1 MEDIUM — idna's `UnicodeError` escaped | `.`, `..`, `a..b`, a 64-character label: the numeric `getaddrinfo` in `validate_host` idna-encodes and raised `UnicodeError`, a raw traceback with exit 1. Caught → "is not an IP address or a hostname"; names over 253 characters are refused too |
| 2 MEDIUM — hostnames classified by NAME | `0.0.0.0.nip.io`, or any hosts-file name mapped to 0.0.0.0, classed "other" while binding every interface; `localhost` was loopback (no token) wherever it pointed. `validate_host` stays DNS-free; at the bind stage `server.resolve_bind_addresses` resolves the host as asyncio's `create_server` will (`getaddrinfo`, `AI_PASSIVE`; literals never reach it) and `context.classify_addresses` judges EVERY resolved address — any unspecified → wildcard, ALL loopback → loopback, else other; an unresolved name is "other", `localhost` included. The CLI resolves once and threads the result into the token rule, the notice, the probe, `build_context` (loopback lens, Origin, Host allowlist) and the printed URL; `serve` opens `context.allowed_origin`. Tests resolve through an injected `FAKE_DNS`, never real DNS. A pre-existing test asserting `is_loopback("localhost")` by name now asserts the resolved form |
| 3 LOW — probe family by `":" in host` | each resolved address is probed in its own family; a name resolving only to IPv6 (`ip6-localhost`) binds |
| 4 LOW — `_HOSTNAME` without `re.ASCII` | any non-ASCII `--host` is refused, and the pattern is `re.ASCII` — a redundant layer behind the `isascii()` check, so it has its own direct test (R10-3) |
| 5 LOW — `brain-2.lan` did real DNS | now `brain-2.invalid` (RFC 6761), through `FAKE_DNS`; §10.17 corrected |

**Mutation evidence (round 10):** 16 guards, each on all five clauses with the strict clause 4,
against `34a85f9`. Two guards had no test that could fail until `34a85f9` added one (re.ASCII,
serve's opened origin). R10-2's first run named its parametrised node with the raw `ſ`; pytest
escapes non-ASCII in ids (`localho\u017ft`), so nothing ran — re-run with the escaped id, PASS.
Ten earlier entries are superseded by this round and marked.

### 10.19 Review round 11 (2026-10-13)

The review of `c45c3bc`. Fixed in `4cefe58`.

| Item | Fix |
|---|---|
| 1 MEDIUM, security — resolve-again TOCTOU | `--host` was classified on one `getaddrinfo` and `uvicorn.Config(host=NAME)` resolved it AGAIN at `create_server`, so a second answer could bind somewhere the first was not judged to be — a name that answered loopback (no token) could bind a wildcard. The probe is now the bind: `server.claim_port` opens one listening socket per classified address (`_listen_socket`: the address's family, `SO_REUSEADDR`, `IPV6_V6ONLY` for IPv6; bind; listen) and returns them; `serve` hands them to `uvicorn.Server.run(sockets=…)`, keeping host/port in the config for logging only. The port can no longer be taken between a probe and the bind either. Any failure closes every socket already opened (`bind_listeners`); the CLI and `serve` both close them on every exit. Regression test: a resolver answering `127.0.0.1` then `::1` — the CLI must resolve once and the sockets `serve` receives must be on `127.0.0.1`. The `resolve_bind_addresses` docstring, CHANGELOG and guide say so |
| 2 LOW — `bind_origin` | picked `127.0.0.1` if ANY resolved address was IPv4; now only an UNSPECIFIED IPv4 address does, so `(192.0.2.5, ::)` opens `[::1]`. Table of six resolved sets in `test_ui_security.py`, mixed rows included |
| 3 — unreachable handler | `serve`'s `except OSError … EADDRINUSE` could never run (uvicorn catches the bind `OSError` and exits — 3 from uvicorn 0.50, 1 before, measured in the wheels; this row first said "≥ 0.52 … exits 3", and round 12's fix to the docstring was itself wrong, see §10.21). Deleted; bind errors are mapped in our code, on the sockets we bind. `resolve_port` is gone with its docstring claim; `preflight`'s docstring now says the port is claimed before it runs and the CLI closes it on failure, and the preflight ordering test is renamed `…fails_before_the_server_starts` (likewise the telemetry warm-up test, `…before_serving`) |
| 4 — test split | `tests/test_cli_ui.py` (1,176 lines) → itself (command surface, `--lan`, URL, `serve`), `tests/test_cli_ui_host.py` (validation, `HOST_SPELLINGS`, resolution), `tests/test_ui_server_bind.py` (`claim_port`, sockets, bind errors, the TOCTOU test) and `tests/ui_cli_support.py` (runner, `FAKE_DNS`, `install_started`, `needs_ipv6`). Each under 800. `test_ci_workflow.py` passes. Catalog entries citing moved tests carry `MOVED … to:` lines; the embedded validator checks those nodes at HEAD |
| 5 — cleanup | the auditor's untracked `sweep4c.exit` / `sweep4c.out` removed, not committed |

**What the table now proves on a real bind.** `HOST_SPELLINGS` gained a `binds-real` assertion:
the sockets `serve` receives are on exactly the resolved addresses. The fake bind still covers
the classification-only rows. `test_a_name_that_does_not_resolve_is_reported_as_such` is deleted,
because the bind no longer resolves anything (RF-4 superseded). An unresolvable name is reported
by `resolve_bind_addresses` (R10-15) and the `brain-2.invalid` row.

**Mutation evidence (round 11):** 23 guards, each on all five clauses with the strict clause 4,
against `4cefe58` on a clean tree, so `git diff --numstat` shows only the mutation. R11-18 to
R11-23 re-prove, on the new code, guards whose round-5, -6, -9 and -10 entries this round
superseded. The 26 entries whose tests moved were re-run at HEAD on their new nodes: 22 passed
strict as recorded, and 4 go red at a different line, each with a NOTE (L-8, R6-8, R9-11,
R9-13). All 4 were re-run with that needle: PASS. On R11-7, a socket that is bound but not
listening is refused on Linux; on macOS the connect times out instead (measured). So the test
fails with its own message, not on an exception type. Eight entries are newly SUPERSEDED. The
validator is exit 0, and it was itself shown to fail on a wrong `MOVED` target and on a dropped
`MOVED` line.

**Voided runs.** Seven full-suite runs count for nothing:

- **`c45c3bc`:** it was running when a scratchpad cleanup removed the directory holding its log.
  Its exit code could not be read, and it was stopped.
- **`4cefe58`:** stopped at about 35%, on team-lead's instruction, because round 12 changes code.
- **`13d1593`:** stopped at 22% with no failures so far, because round 13 changes code.
- **`1264162`:** stopped at 16% with no failures so far, because round 14 changes code.
- **`969c9b8`, first run:** discounted, because a mutation proof touched the worktree during it
  (§10.22).
- **`969c9b8`, restart:** stopped at 5% with no failures so far, because round 15 changes code.
- **`6cc04fe`:** stopped minutes in, on team-lead's instruction, to fold in round 15b.

The one full suite of record is on round 15b's `5367194` (§10.23).

### 10.20 Review round 12 (2026-10-13)

The review of `4cefe58`. Fixed in `13d1593`.

| Item | Fix |
|---|---|
| 1 LOW–MEDIUM — the `--open` startup wrapper | `startup_then_open` must forward `sockets=` to uvicorn's `startup`. The uvicorn double in `test_cli_ui.py` called `startup()` with nothing, so dropping the arguments passed every test. Against real uvicorn 0.54, though, that bound `config.host:port` as well: the TOCTOU came back on the default path as a silent second bind. The double now mirrors uvicorn (`run(sockets)` → `startup(sockets=sockets)`), and the `--open` test asserts the exact claimed sockets reach `startup`. `test_real_uvicorn_serves_only_the_claimed_socket` runs uvicorn itself and requires it to listen on the claimed socket and nothing else |
| 2 LOW — the port range | `bind` raises `OverflowError` past 65535. That is not an `OSError`, so `_open_listener` leaked its socket and the CLI printed a traceback. `--port` is now `min=1, max=65535`, which also refuses 0: port 0 would have given a different ephemeral port on each address. `claim_port` refuses an impossible start, auto-port stops at 65535 and names the range it searched, and `_open_listener` closes its socket on any exception |
| 3 LOW — "uvicorn ≥ 0.52" | dropped. The replacement, "exits 3", was wrong too; see §10.21 |
| 4 LOW — stale wording | `context.classify_addresses` now names `claim_port` rather than uvicorn, and `cli_ui.py`'s "probe" comments are fixed. The grep of the phase's src left only historical mentions ("this used to probe") and unrelated ones (`lan.udp_route_probe`, the telemetry probe) |

**Mutation evidence (round 12):** 9 guards, each on all five clauses with the strict clause 4,
against `13d1593`. R12-1 and R12-2 are the reviewer's own mutation. One catches it through the
double, the other through real uvicorn. R12-5's first needle was wrong: with the 65535 cap, an
impossible start makes an empty search, which reports "no free port found in 70000..65535". So it
fails on the message, not on `OverflowError`. Re-run with that needle: PASS. R11-15 is
superseded by R12-9.

### 10.21 Review round 13 (2026-10-13)

The review of `13d1593` verified round 12's fixes and found one LOW. Team-lead added two items.
All three are fixed in `1264162`.

| Item | Fix |
|---|---|
| 1 LOW — uvicorn's exit code | `serve`'s docstring said uvicorn "exits 3". Measured in the wheels: 0.30–0.49 call `sys.exit(1)`, and 0.50+ exit with `STARTUP_FAILURE` (3). The pyproject floor is 0.30. It now says "3 from uvicorn 0.50, 1 before". Round 12's fix, which replaced "≥ 0.52" with "exits 3", was itself wrong. §10.19's row 3 is corrected in place, and says so |
| 2 SECURITY, pre-existing — a wildcard could share a port | On macOS a `SO_REUSEADDR` socket bound `0.0.0.0:P` (or `::`) while another process listened on `127.0.0.1:P` (or `::1`); measured on this machine, both families. The loopback URL `brain ui` prints and opens, token in the fragment, then went to that process. `_listen_socket` now sets `SO_REUSEADDR` for a specific address only. A wildcard bind then fails with EADDRINUSE (48, measured), so auto-port moves on, or `--no-auto-port` says the port is in use. Linux refuses that overlap even with the option, so there the change only costs one thing: a wildcard restart while old connections sit in TIME_WAIT steps to the next port. Loopback and specific binds keep the option, and so keep TIME_WAIT restarts. The real-socket test holds a loopback listener and requires `claim_port`'s wildcard to report it in use, or to step past it. Where the platform refuses the overlap anyway, it skips and says so. CHANGELOG, under Fixed. **This closed one bind order only**: a process binding loopback AFTER our wildcard could still take the loopback traffic on macOS. Round 14 closes that order (§10.22) |
| 3 — an ephemeral port for the real-uvicorn test | it binds port 0 via `bind_listeners`, so the OS picks a free port and it cannot collide |

**Mutation evidence (round 13):** 4 guards, each on all five clauses with the strict clause 4,
against `1264162`:

- R13-1: `SO_REUSEADDR` always set. The real-socket test goes red in both families. Measured on
  macOS; the test skips where the platform refuses the overlap.
- R13-2: the options table, wildcard rows.
- R13-3: never set. The table's specific rows go red.
- R13-4: the wildcard judged by spelling. The IPv6 overlap goes red, measured on macOS. In round 14
  it was retargeted to the options-table `::` row, which every platform runs, and re-proved.

R11-13 is superseded: its deletion now leaves an empty block, a SyntaxError rather than a
mutation. R9-6 and R9-7 follow the renamed options test, and were re-run at HEAD: PASS. R9-6 now
goes red on the three specific-address rows only.

### 10.22 Review round 14 (2026-10-13)

The delta review of `1264162`. Fixed in `969c9b8`.

| Item | Fix |
|---|---|
| 1 LOW, security — the other bind order | Once OUR wildcard listened on `0.0.0.0:P` (or `::`), macOS let another process bind `127.0.0.1:P` (`::1`) with `SO_REUSEADDR`, even `SO_REUSEPORT` too, and accept loopback connections: the tokened URL we open, during warm-up or any time after. Measured here, both families. `bind_listeners` now gives each wildcard a loopback companion of its family: `_loopback_companion` binds and listens on `127.0.0.1:P` (`::1:P`) with `SO_REUSEADDR`. It is served like any other socket and closed on every path, the same list `claim_port` returns. A later foreign loopback bind then fails with EADDRINUSE (48, measured). Linux refuses the companion against our own wildcard and refuses the foreign bind too, so exactly that EADDRINUSE is tolerated. It is not "busy", and the refused socket is closed. Any other failure propagates and releases every socket. *(Corrected in round 15: "any other failure" was too strict. EADDRNOTAVAIL, on a host without `::1`, is tolerated too; see §10.23.)* `bind_origin` and the Host allowlist are computed from the RESOLVED addresses, so they are unchanged. One gap is recorded, not closed: a process binding loopback in the instant between our two binds turns the companion into EADDRINUSE too |
| found while fixing it — a specific address under its own wildcard | for a name resolving to `(127.0.0.1, 0.0.0.0)`, binding `127.0.0.1` first made the wildcard's bind (no `SO_REUSEADDR` since round 13) fail on every port. On Linux it always had, since round 11 bound every address. The wildcard covers it, so it is skipped. Proven on real sockets, which works on both platforms |
| 2 — plan notes | R13-1 and R14-1 are marked "measured on macOS; skips where refused". R13-4 is retargeted to the options-table `::` row and re-proved |

Tests that every platform runs use injected sockets: the companion is bound, listening, with
`SO_REUSEADDR` and served; a refusal like Linux's is tolerated and closed; any other failure is
reported and releases the wildcard; the coverage rule holds. A real-socket test covers the
reverse order (it skips where the platform refuses it), and another covers the coverage rule.
Their parametrised ids are `ipv4`/`ipv6`, not the numeric address family (`AF_INET6` is 30 on
macOS and 10 on Linux, which made the node ids platform-specific).

**Mutation evidence (round 14):** 7 guards, each on all five clauses with the strict clause 4,
against `969c9b8`:

- R14-1: the companion removed, on real sockets (macOS).
- R14-2: the same mutation, on injected sockets (every platform).
- R14-3: the companion's EADDRINUSE treated as busy.
- R14-4: every companion failure tolerated.
- R14-5: a specific address bound under its own wildcard. It was first red only at a list
  assertion on fakes, and is now red on real sockets ("no free port found").
- R14-6: the companion put on the wildcard's own address.
- R14-7: replaces R11-9, whose loop text moved.

**A discounted run.** R14-7 was proved against the sibling test database while the first full
suite on `969c9b8` ran, and for its ~10 s `src/brain/ui/server.py` held that mutation on disk.
Several tests spawn subprocesses that import from disk (the `bin/` shims, `cli_ui`'s
import-cost check), so team-lead discounted that run. It was stopped and restarted on
`969c9b8`, and nothing touched the worktree while the restart ran. The rule from here: while a
run of record is going, mutation proofs happen only in a `git archive` copy, never in the
worktree.

### 10.23 Review round 15 (2026-10-13)

The review of `969c9b8`: one LOW. Fixed in `6cc04fe`.

| Item | Fix |
|---|---|
| 1 LOW — the `::1` companion on a host without it | On Linux with IPv6 disabled on `lo` (`net.ipv6.conf.lo.disable_ipv6=1`, or `all`), binding `::` works but the `::1` companion gets EADDRNOTAVAIL. That aborted `claim_port` with a false "--host :: is not an address this machine owns", so `brain ui --host ::` worked at `1264162` and failed at `969c9b8`. `_loopback_companion` now tolerates EADDRNOTAVAIL too, read from the `BrainError`'s `__cause__`. The reason is the same as for EADDRINUSE: with no `::1` on the host, no other process can hold `::1:P` either. Every other errno still propagates (the EINVAL test). The regression test is injected (`::1` refused with EADDRNOTAVAIL): the wildcard alone is returned, still listening and served, and the refused socket is closed. It runs on both families |
| 2 — the CHANGELOG overclaimed | "in either order" now names the microsecond window between the wildcard bind and the companion bind, as the docstring and §10.22 do |

**Mutation evidence (round 15):** 2 guards, each on all five clauses with the strict clause 4,
against `6cc04fe`, proved before the run of record started:

- R15-1: the EADDRNOTAVAIL branch removed. Red at "is not an address this machine owns", in
  both families.
- R15-2: every companion `BrainError` tolerated. The EINVAL test goes red at `DID NOT RAISE`.

**Round 15b (tests only), the reviewer's uncounted note on `6cc04fe`.** `needs_ipv6` probes the
`::` wildcard. Linux with IPv6 disabled on `lo` still binds `::` but has no `::1`, so tests that
bind `::1` themselves failed there instead of skipping. Three changes:

- **A new marker:** `ipv6_loopback_available()` probes `::1`, and the `needs_ipv6_loopback`
  marker guards the tests that bind it.
- **The overlap helper:** the round-13 helper `_overlap_is_possible` binds its loopback inside
  its handler, so a missing address reads as "no overlap possible".
- **The `HOST_SPELLINGS` binds-real rows:** they skip on what they actually bind
  (`ip6-localhost` is `::1`).

A TEST-NET-1 address drives the helper's new branch on every platform (`5367194`).

**Proved on that host, not argued.** In `python:3.11-slim` with `--network none --sysctl
net.ipv6.conf.lo.disable_ipv6=1` (`::` binds, `::1` is refused with errno 99), on `git archive`
copies with deps installed for Linux in a scratch directory:

- `6cc04fe`: 3 failed — `test_claim_port_binds_every_resolved_family`,
  `test_a_wildcard_never_binds_over_another_process_on_loopback[ipv6]`, and the `ip6-localhost`
  table row.
- `5bf2c69`: 153 passed, 6 skipped, 0 failed. **Four** of the skips say "no IPv6 loopback
  (::1)": `test_claim_port_binds_every_resolved_family`,
  `test_a_wildcard_never_binds_over_another_process_on_loopback[ipv6]`,
  `test_nothing_can_bind_our_loopback_after_our_wildcard[ipv6]` and the `ip6-localhost` table row.
  The other two are the `[ipv4]` overlap tests ("this platform refuses…"). *(Corrected after
  review: this line first said "Three". The run's `-rs` output lists the four by file and line —
  `test_ui_server_bind.py:291`, `:533`, `:698`, `test_cli_ui_host.py:223` — and the miscount was
  mine. The log file itself was removed with the container scratch; the lines survive in the
  session's recorded output.)*

A scratch plugin outside the repo marked that selection `nodb`, so conftest took no database
lock. **Mutation evidence:** R15b-1 (container) and R15b-2 (every platform), strict.

**The validator, twice.** Its round-header pattern took digits only, so the `Round 15b` block
would have been skipped silently, with exit 0. The pattern now allows one letter. The
validator also counts round headers against parsed blocks and fails on a difference: a copy
with the old pattern exits 1 with `UNPARSED: 20 round headers, 19 blocks parsed`.

**Plan review of `0965b61` (plan-only fixes).** p7-reviewer found two LOW issues and a nit, and
team-lead added a validator weakness:

- §10.19's voided-runs line said "Three" over seven bullets. It now says seven.
- §10.23 said three skips read "no IPv6 loopback"; there were four. It is corrected in place, with
  the evidence.
- §10.23's rows were numbered 1 and 4 (team-lead's item numbers). They are renumbered 1 and 2.
- **The validator checked MOVED entries per entry, not per test.** `moved or tests` meant an entry
  with any `MOVED` line had only those nodes checked, so a test that neither moved nor survived
  went unnoticed. It now requires one `MOVED` line per `test:` line, in order, and checks every
  node.

Re-proved on a scratch copy of the plan carrying a crafted round-9 entry: one test moved (with its
`MOVED` line), and a second `test:` gone at HEAD with none.

- The **old** validator exited 0 and counted the entry (`round 9 @ f1bdbea: 9/9`).
- The **new** one exits 1, with `MOVED … 1 MOVED line(s) for 2 test(s)` and
  `UNMARKED … test gone at HEAD`.
- On the real plan it exits 0, and every existing `MOVED` entry already met the one-per-test rule.

### Verification of rounds 11–15b (2026-10-13; every exit code read from a file)

All of it ran on `5367194`, with the worktree untouched while the run of record ran:

| Check | Result |
|---|---|
| `ruff check` (0.17.0) | exit 0 |
| `mypy src/` (2.4.0) | exit 0, 225 source files |
| **Full suite — the run of record**, `pytest` on `second_brain_t_auditmeas` | **exit 0**: 9,107 passed, 3 skipped, 306 deselected; coverage 94.62% (floor 85%); 53m28s. `tests/test_ci_workflow.py` passed inside it |
| The `ci.yml` browser command (14 modules, `-m browser --no-cov`) | exit 0, 197 passed |
| Table e2e: every `HOST_SPELLINGS` row through the real `brain ui` on the demo sandbox (55433), sockets read with `lsof` by PID | exit 0, 0 failures. Every serving row listens on exactly the resolved addresses, plus each wildcard's loopback companion (`0.0.0.0` → `0.0.0.0` + `127.0.0.1`; `::` → `::` + `::1`), and its printed URL answers 200. Rows naming an address this Mac does not own (`127.0.0.2`, `fd00::5`) exit 1 with "is not an address this machine owns". Names with no real DNS answer (`brain-1.lan`, `ip6-localhost`) exit 1 with "does not resolve". No `brain ui` process was left behind |
| `git merge-tree --write-tree HEAD second-brain/master` (`6a82db2`) | exit 0, no conflicts |
| The §10.9 validator | exit 0: 191 entries, 65 superseded and marked, every round header parsed |

### 10.24 Rounds 16 and 16b, and the merge of master (2026-10-14)

The phase-7 completion audit of `cb4b6ed` (AUDIT FAILED, three LOW findings) was fixed as round 16
in `2af3667` and recorded in `1dfe4c7`. The fixes were written by the auditor, at team-lead's
direction, and are checked by a different reviewer and auditor. The review of `2af3667` found one
MEDIUM, one LOW and two nits; those are fixed as round 16b in `6a46869`. Master `c30449e` (PRs #22
and #23) was then merged in `5b13ab9`. A strict sweep at the merge found two round-16b guards
going red beside their targets. They are fixed in `7d1ec3d`, which changes tests only, and the
run of record is on `7d1ec3d`. `1dfe4c7`'s
verification table is superseded by the one below: it ran on `2af3667`.

**Round 16 (`2af3667`).**

| Item | Fix |
|---|---|
| 1 LOW, security — a regression against master | Since round 10 a HOSTNAME counted as loopback when every address it resolved to was loopback. A loopback bind runs with no token, allowlists its own name, takes its name as the allowed Origin and serves confidential bodies. So `brain ui --host 127.0.0.1.nip.io`, a public service, did all four. Measured: `GET /api/tree` with `Host: 127.0.0.1.nip.io:<port>` and no token answered 200. Whoever controls such a name's DNS could serve a page from it, re-point it at 127.0.0.1, and pass both the Host check and the Origin check — the DNS rebinding the allowlist exists to stop. On master (`6a82db2`) any name outside `LOOPBACK_HOSTS` needed a token. Now only names in `context.LOOPBACK_NAMES` can be loopback. `address_class` demotes any other loopback-resolving name to "other" (`context.is_loopback_alias`), so the token rule, the Host allowlist (the name alone), the Origin and the confidential lens (withheld) all follow from that one function. The CLI's refusal says why (`cli_ui.LOOPBACK_ALIAS_REASON`). Literals are unchanged: `127.0.0.2` is still loopback |
| 2 LOW — R11-8 drifted | Since round 13 (`1264162`), `_listen_socket` parses its address, so R11-8's mutation raises `ValueError` on the hostname before its list assertion is reached. The entry now has the `NOTE at HEAD` line §10.9 requires |
| 3 LOW — two branches no test ran | `resolve_bind_addresses` on an empty resolver answer is now covered by an injected resolver returning `[]`. The panel's busy-port notice is covered with `claim_port` replaced by pytest `monkeypatch`. `ui/server.py` is at 100% in the run of record |

**Round 16b (`6a46869`), the review of `2af3667`, plus the lead's two edge cases.**

| Item | Fix |
|---|---|
| 1 MEDIUM — `ip6-localhost` was trusted | Round 16 put `ip6-localhost` in `LOOPBACK_NAMES` beside `localhost`. That was wrong for three reasons. macOS's `/etc/hosts` does not define it, so `getaddrinfo` falls through to network DNS and the search domains, where a hostile resolver can point it anywhere. Browsers do not hard-wire it. Master never trusted it. `LOOPBACK_NAMES` is `{"localhost"}`. The literals `::1` and `[::1]` stay tokenless, by address. The reason text, the context.py justification and the CHANGELOG, guide and cli-reference sentences now name localhost alone |
| 2 LOW — the alias panel contradicted the lens | The alias note said "confidential bodies withheld" even under `--include-confidential`, which serves them (`build_context`). The Confidential line also called a loopback alias a "non-loopback bind". The note now mentions withholding only when bodies are withheld. The line reads "withheld (loopback alias, guarded like a network bind)", or "SERVED on a loopback alias (--include-confidential)". The CLI tests could not see this because the `build_context` double in `tests/ui_cli_support.py` hard-coded `serve_confidential_bodies=False`. It now derives the value as `build_context` does, and a `_panel` unit test runs without the double |
| 3 nits | The `--host localhost.` refusal now says to drop the trailing dot (`cli_ui._alias_reason`). In the CHANGELOG, "every spelling of loopback is loopback" now reads "every loopback address literal is loopback" |
| Edge case (a) — spellings of the trusted name | `LOCALHOST` is `localhost`: `validate_host` lower-cases a hostname, as a browser lower-cases the Host header it sends, so it is trusted. `localhost.` is a different name, and a browser sends `Host: localhost.` as typed. `LOOPBACK_HOSTS` does not hold it, so it is "other" (token required), consistent with the Host check. Both are pinned in the CLI and the classification tests |
| Edge case (b) — a trusted name with a mixed answer | `localhost` resolving to `127.0.0.1` and `192.0.2.30` stays "other" and needs a token. It gets the network-bind refusal, not the alias one. Pinned in the classification table and the CLI test (R16b-9) |

**The merge (`5b13ab9`).** Master `c30449e` was merged, not rebased, so every SHA cited here
stays valid. The one conflict was in `CLAUDE.md`'s ceiling table, on adjacent rows: this branch's
rewritten `setup.py` row and PR #23's extended `wiki/build_watcher.py` row. Both were kept. Under
the promotion rule, the two rows master touched have their descriptive hops promoted: `wiki/build_watcher.py`
to 1,110 (`188a64f`), and `search.py` to 823 (`c343f5c`, touched, no net change). Both counts come
from `git show "${sha}:<path>" | wc -l`. The over-800 row set was re-derived on the merged tree and
matches the table.

**Tests changed, not only added.** `test_a_name_is_classified_by_every_address_it_resolves_to`
used to judge its table for `some.name`. An untrusted name can no longer be loopback, so the
table is now judged for `localhost`; node ids are unchanged. R10-5 got a NOTE for the moved line.
`test_the_cli_binds_what_it_classified_even_if_dns_changes_its_answer` now resolves `localhost`
rather than `brain-1.lan`. The trusted-name test is now `localhost` and `LOCALHOST` only.
`ip6-localhost` and `localhost.` joined the alias refusal, which reads the boxed usage error
as one line. The `ip6-localhost` `build_context` row became "other". The `HOST_SPELLINGS` row for
`ip6-localhost` is unchanged: it passes `--token`, as every table row does, and still proves the
real IPv6-only bind.

**Red first.** In `git archive` copies, the round-16 tests failed against `cb4b6ed`'s source: 11
failures. The round-16b tests failed against `2af3667`'s source: 9 failures, including both
panel cases, the `ip6-localhost` and `localhost.` refusals, and the `include_confidential` CLI
test.

**Mutation evidence.**
- Round 16: 10 guards against `2af3667`.
- Round 16b: 9 guards against `6a46869`, including `ip6-localhost` trusted again (R16b-1,
  R16b-2) and the double hard-coding the lens again (R16b-8, the test of the tests).
- Each guard holds on all five clauses with the strict clause 4, in a `git archive` copy, never
  the worktree.
- Round 16b makes R16-4 inapplicable, so it is marked SUPERSEDED. R16-7's needle moved with the
  reason text, so it has a NOTE.
- **Every non-superseded entry of every round was then re-run strict at the merge `5b13ab9`.**
  144 entries: 141 passed. R15b-1 stayed green, as its NOTE says it does on any host with `::1`.
  Two failed, both caused by round 16b's tests:
  - **R16b-9:** the alias refusal is longer, so Rich wrapped "pass --token" across the usage box's
    edge, and that earlier assertion fired first.
  - **R11-22:** the `build_context` double read `kwargs["resolved"]`, so the mutation went red
    as a KeyError inside the double.
  - `7d1ec3d` fixes both. The test now reads the boxed error as one line, and the double uses
    `.get`. R16b-9's needle is now the line the guard actually targets. Both were re-proved
    strict.
- **The sweep was then run again at `7d1ec3d`:** **144 entries; 143 pass all five clauses, and R15b-1 stays green, as its NOTE says it does on any host with `::1`.** The copy's venv had the `browser` extra, so no entry passed by skipping.

**Verification.** Every exit code was read from a file. The run of record is on the committed
`7d1ec3d`, the merge plus the test fix. The worktree venv was reinstalled `[dev,browser]` for
master's pytest-timeout, and nothing touched the worktree while the run went. An earlier run on
`5b13ab9` also exited 0 (9,179 passed). It is superseded, because `7d1ec3d` changed tests after it.

| Check | Result |
|---|---|
| `ruff check` (0.17.0) | exit 0 |
| `mypy src/` (2.4.0) | exit 0, 226 source files |
| **Full suite — the run of record**, `pytest` on `second_brain_t_p7aud3` | **exit 0**: 9,179 passed, 3 skipped, 319 deselected; coverage 94.64% (floor 85%); 21m15s. `ui/server.py`, `ui/security.py` and `ui/lan.py` are at 100% |
| The `ci.yml` browser command as the merged `ci.yml` spells it (15 modules, `-m browser --no-cov`), on `7d1ec3d` | exit 0, 210 passed |
| `tests/test_ci_workflow.py` | exit 0, 22 passed |
| `git merge-tree --write-tree HEAD second-brain/master` (`edd8986`, after `git fetch`; master moved again with PR #25, an eval baseline re-record) | exit 0, no conflicts |
| The §10.9 validator | exit 0: 210 entries, 66 superseded and marked, every round header parsed |
