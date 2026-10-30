# RM-14b — the local reranker runtime: guide, doctor probe, optional launchd job

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, "RM-14b". Depends on RM-14a
(merged, #76). Scope is the ticket's, nothing more: no `brain setup` change, no default change
(`BRAIN_RERANKER` stays `none`), and no eval.

## Files

| File | Change |
|---|---|
| `src/brain/templates/launchd/com.brain.reranker.plist.j2` | **New.** `KeepAlive` + `RunAtLoad`. `ProgramArguments` are fixed: `/usr/bin/env llama-server -m {{ rerank_model_path }} --embedding --pooling rank --reranking --host 127.0.0.1 --port {{ rerank_port }}`. The only variables are the model path and the port (see "Deviation" below), plus `brain_home` and `user_home` (logs, `HOME`). |
| `src/brain/bin/launchd.py` | `_OPTIONAL_LABELS = ("com.brain.reranker",)`. `_ALL_LABELS` gains it, so uninstall sweeps it. `render_plist` gains `rerank_model`. `install_plists(with_reranker=False)` installs the optional label when it is asked for **or when its plist already exists** (sticky), and never removes it. `--with-reranker` refuses a missing GGUF. `install_main(argv)` parses `--with-reranker` with argparse. `resolve_rerank_model(brain_home)`: `BRAIN_RERANK_GGUF` from the process env, then from `$BRAIN_HOME/.env`, then `$BRAIN_HOME/models/bge-reranker-v2-m3-Q8_0.gguf`. |
| `src/brain/settings/rerank.py` | The `DEFAULT_RERANK_GGUF_NAME` constant (a name only, with no env read; the launchd installer resolves the path). |
| `src/brain/doctor_reranker.py` | **New leaf**, in the style of `doctor_delivery.py`. The `reranker` row: `none` → OK "not configured". `voyage` → OK "voyage (hosted), no local runtime". `llamacpp` → `GET {url}/health` with a 2 s timeout: 200 → OK; a connect error or timeout → WARN "not running"; another status → WARN "not ready (HTTP n)". A `ConfigError` → WARN with the message. A WARN, not a FAIL: a reranker that is down never fails a search. |
| `src/brain/doctor_runtime.py` | Within the +≤ 25 budget: it composes the new row in, and `DAEMON_LABELS` becomes installed + optional, so the `daemons` row checks the reranker only when its plist exists. That row already filters on `is_file()`. |
| `docs/guides/local-reranker.md` | **New.** Install, download, verify, run by hand, launchd job, doctor, memory, the port-8081 collision, uninstall. |
| `docs/configuration.md` | A `BRAIN_RERANK_GGUF` row, plus a pointer to the guide. |
| `CHANGELOG.md`, the spec's Appendix B | The `[Unreleased]` entry; RM-14b → DONE (#pr). |

No over-800 file is touched; `setup.py` is not touched either.

## Measured (2026-10-30, this machine, Apple Silicon, macOS 26)

- `brew install llama.cpp`: **llama.cpp 0.7.0** (`llama-server --version`: build 11669, commit
  `620d2225c`), with ggml 0.27.0.
- GGUF: `gpustack/bge-reranker-v2-m3-GGUF`, file `bge-reranker-v2-m3-Q8_0.gguf`, **635,676,416
  bytes** (606 MiB). Its sha256, `a43c7c9b…aaa1d3`, matches the Hugging Face LFS oid.
- Started with the template's flags on 127.0.0.1:8091: `/health` → 200 `{"status":"ok"}`. RSS was
  **822,896 KiB (~804 MiB)** idle and 825,200 KiB after one 3-passage `/v1/rerank`. The reply
  shape `results[{index, relevance_score}]` matches RM-14a's backend. The server was stopped by
  PID.
- **Port 8081 was taken** on this machine by a LanguageTool server (8081 is its default), and
  `llama-server` exited with "couldn't bind HTTP server socket". The guide covers this case.

## Deviation: the port follows `BRAIN_RERANK_URL` (ruled by the coordinator, 2026-10-30)

The ticket fixes the plist at `--port 8081` and makes the model path the only variable. On the
owner's machine 8081 is a LanguageTool server's default port, so a job fixed at 8081 cannot bind
there. RM-14c needs a running reranker.

- **The port** is parsed from `BRAIN_RERANK_URL` (shell, then `$BRAIN_HOME/.env`, then the
  default, 8081) by `settings.rerank.launchd_rerank_port`. The URL must pass the same loopback
  check searches apply, and must name an explicit port in 1024–65535; otherwise the install
  fails before any launchctl call.
- **The host is unchanged:** it stays a literal `127.0.0.1` in the template and is never a
  variable. `render_plist` also refuses a port outside the range.
- The URL is read only when the reranker job is rendered, so a bad value cannot break an
  install that never opted in.

## Tests (TDD, red first)

- `tests/test_doctor_reranker.py`: the five states against an `httpx.MockTransport`, plus
  connect-error and timeout transports. AC 1.
- `tests/test_bin_launchd_reranker.py`:
  - the render has `127.0.0.1`, the port, the three flags and the model path, and is valid XML;
    no `{{` marker is left over, and the host is never a template variable (AC 2);
  - the port's precedence, a non-loopback URL and a bad port are refused, the configured port is
    installed, and a bad URL does not affect an install without the reranker (the deviation);
  - a flagless first install does not write it (AC 3);
  - `--with-reranker` installs and bootstraps it, and a missing GGUF is a `LaunchdError`;
  - a later flagless install re-renders it with a changed model path and keeps it (AC 4);
  - uninstall removes it (AC 3);
  - `install_main(["--with-reranker"])` and the precedence of `resolve_rerank_model`;
  - the doctor `daemons` row includes the reranker only when its plist exists.
- Updated: `tests/test_bin_launchd_retired.py` (the `_ALL_LABELS` tuple).
- Run: those files plus `test_bin_launchd.py`, `test_doctor_runtime.py`, `test_doctor_watcher.py`,
  `test_install_launchd_vault.py` and `test_settings_rerank.py`, under the suite lock.
