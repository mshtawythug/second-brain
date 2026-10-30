# RM-38: voice memos with local transcription (plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Spec: `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-38" (and RM-33, whose
drop-folder runner this reuses unchanged). Base: `second-brain/master` at `4c4c0a7`.

## What ships

An audio extractor for `.m4a`, `.mp3` and `.wav`, kept out of `brain ingest-dir`'s walk, that
transcribes on this machine: `afinfo` reads the duration, `afconvert` makes a 16 kHz mono WAV,
`whisper-cli` (whisper.cpp) transcribes it. Memos arrive through RM-33's drop folders
(`kind = "voice"`), so the move-first naming, identity checks and rejected/transient split
apply unchanged. Documents are kind `voice`, `content_type` `voice_memo`, **confidential by
default** on every path (`ExtractorSpec.default_sensitivity`, as images), overridable per folder
and by `--sensitivity`.

## Files

New (each well under 800 lines):

| File | What |
|---|---|
| `src/brain/settings/voice.py` | `VoiceSettings(model: Path \| None, max_minutes: int)`, `load_voice_settings(env)`. `BRAIN_WHISPER_MODEL`: unset means voice is off (no default, nothing downloaded); must be absolute or start with `~` (pathlib only, the leaf rule). `BRAIN_WHISPER_MAX_MINUTES`: default 60, 1 to 600. Messages name the variable, never the value. |
| `src/brain/ingest/whisper.py` | The transcription engine, shaped like `ingest/ocr.py`: `WHISPER_CLI = "whisper-cli"`, `WhisperSetup(settings, binary, model)`, `resolve_whisper(env, *, which, is_file)`, `whisper_unavailable_hint(setup)` (off / model missing / binary missing, each with its fix). The **runner** seam `AudioRunner = (argv, timeout, max_stdout_bytes) -> CappedRun`, production default `run_capped` with a minimal env (`PATH`, `HOME`, `LANG`, `TMPDIR`, as `apple_notes.osascript_env`), stdin closed, stderr discarded unread, process group killed on timeout or oversize. |
| `src/brain/ingest/voice.py` | `extract_voice(path, *, whisper=None, runner=..., max_file_bytes=...) -> ExtractedDoc`. Error classes below. |
| `src/brain/doctor_voice.py` | The `voice memos` doctor row (own leaf; `doctor_runtime.py` gets only the import + call, +2 lines of its §2.2 budget). |
| `tests/voice_isolation_plugin.py` | Deletes `BRAIN_WHISPER_MODEL` / `BRAIN_WHISPER_MAX_MINUTES` for every test (as `ocr_isolation_plugin.py`), so a shell's knobs can never make a test shell out. One `pytest_plugins` line in `tests/conftest.py`. |

Edited: `ingest/dispatch.py` (the `voice` spec), `source_kinds.py` (`voice`),
`settings/drop_folders.py` (D2), `cli_ingest_folder.py` (route audio, map errors, skip audio
folders when whisper is unavailable, as it does image folders without OCR), `cli_extract.py`
(a `VoiceError` is a one-line refusal in `brain ingest`), `doctor_drop_folders.py` (audio folders
with no whisper → WARN, as image folders without OCR), `doctor_runtime.py` (+2),
`ui/static/js/results.js` + `discovery.js` (🎙️), `ui/static/js/tree.js` (`_ingested/voice`),
`pyproject.toml` (`live_whisper` marker, like `live_ocr`: in the default suite, self-skipping).

Not touched: `cli.py`, `cli_ingest.py`, `ingest/__init__.py`, `config.py`, `drop_folder.py`
(reused as is). No over-ceiling file grows; `scripts/check_ceiling_table.py` must stay 0.

## The extractor, step by step

1. Resolve whisper from `os.environ` when `whisper` is not passed (as `extract_image` resolves
   OCR). Off, model file missing, or `whisper-cli` not on `PATH` → `WhisperUnavailableError`
   before the file is touched.
2. `lstat` the path: a regular file (no symlink, no FIFO), at most `MAX_FILE_BYTES` (1 GiB; a
   60-minute 44.1 kHz stereo WAV is about 635 MB).
3. `/usr/bin/afinfo <abs path>`, 10 s, stdout capped at 64 KiB. Only the
   `estimated duration: N sec` line is read; stdout is never logged or put in a message (it
   names the file). No line, not finite, or ≤ 0 → `UnreadableAudioError`. Over
   `max_minutes` → `AudioTooLongError` (AC2).
4. `/usr/bin/afconvert -f WAVE -d LEI16@16000 -c 1 <abs path> <tmpdir>/memo.wav`, 60 s, into a
   private `TemporaryDirectory` deleted afterwards (the audio is never copied into the brain or
   the vault). Non-zero → `UnreadableAudioError`. The WAV's own size gives the true duration
   (16 kHz × 2 bytes mono = 32,000 bytes/s), re-checked against the cap, so a header that lies
   to `afinfo` cannot buy a longer transcription.
5. `<whisper-cli> -m <model> -f <wav> -nt -np`, timeout `2 × duration + 60 s`, stdout capped at
   4 MiB. Timeout → `AudioTimeoutError` (AC3); non-zero exit or oversized output →
   `TranscriptionError`.
6. Text: decoded `utf-8`/`replace`, whisper's non-speech `[BLANK_AUDIO]` marker dropped, lines
   stripped and joined. Silence is an empty document (ingest skips it; the drop folder reports
   `empty` and keeps the file in `imported/`).
7. `ExtractedDoc(title=<stem>, content_type="voice_memo", source_path=<resolved>,
   metadata={"audio": {"duration_s", "format"}, "transcription": {"engine": "whisper.cpp",
   "model": <model file name only>}})`. The drop folder replaces the title with the original
   stem, as for screenshots.

Every error is a `VoiceError(ValueError)` with a fixed, content-free message; a cause is kept
only as `__cause__`, printed by class (`describe_failure`). The `TimeoutExpired`-style partial
output is never kept. Every log line is class-only (the G1 guard in
`tests/test_log_privacy_rules.py` runs over the new modules).

## Deterministic vs transient (the drop-folder split)

| Failure | Class | Drop folder |
|---|---|---|
| Over `BRAIN_WHISPER_MAX_MINUTES` | `AudioTooLongError` | `rejected` (stays in `imported/`) |
| Not a regular file, over 1 GiB | `UnreadableAudioError` / `AudioTooLargeError` | `rejected` |
| `afinfo` fails or prints no duration; `afconvert` fails | `UnreadableAudioError` | `rejected` |
| `afinfo` / `afconvert` / whisper timeout | `AudioTimeoutError` | `rejected` (AC3: skipped, class-only WARNING) — D3 |
| whisper exits non-zero / oversized output | `TranscriptionError` | **transient**: moved back, retried — D4 |
| whisper off, model missing, binary missing | `WhisperUnavailableError` | transient; and the folder is skipped up front with the fix, so files are not moved at all (as image folders without OCR) |
| DB / embedder | (existing) | transient (unchanged) |

## Decisions (approved by team-lead, 2026-10-30)

All five approved as proposed. Notes from the approval:

- D1: tested that a long memo started inside the deadline runs to completion while the next
  file is `deferred`; the guide states the worst-case run length (about two hours at the cap).
- D2: tested for both kinds.
- D4: the guard chosen is the **minimum** one the approval allowed: each run prints a WARNING
  naming the memo's `imported/` name (it carries the content hash) and the class, plus a guide
  note. A per-hash failure counter, and a doctor WARN on the last run's failures, need state
  that persists between runs (the runner has none today) and are left as a follow-up.
- D5: the guide and the PR body say plainly that no real model was exercised.

The proposals as they were put:

- **D1 — timeouts ignore the run's remaining deadline.** `run()` hands the ingest step what is
  left of its 480 s deadline; images use `min(60 s, that)`. A 40-minute memo needs up to
  `2 × 2400 + 60` s of whisper by spec, so capping it at the run's remainder would turn a
  normal memo into a permanent `rejected`. Proposal: audio uses the spec's own timeouts; the
  run deadline still bounds *starting* files (later files are `deferred`), and launchd never
  starts a second instance of a job that is still running. Alternative: cap at the remainder
  and treat that cut as transient (retried every 10 minutes, possibly forever).
- **D2 — kind-aware default extensions in `drop-folders.toml`.** Today a folder without
  `extensions` gets the image set whatever its `kind`. Proposal: `kind = "voice"` with no
  `extensions` defaults to `.m4a/.mp3/.wav` (a table keyed by kind, in
  `settings/drop_folders.py`). `--dir PATH` with no config entry keeps the screenshot defaults.
  Without this a voice folder must also list its extensions.
- **D3 — a timeout is `rejected`, not retried.** Matches AC3 ("skips the file") and RM-33's OCR
  timeout. Recovery is `brain ingest <imported path>` (documented in the guide).
- **D4 — a whisper non-zero exit is transient.** Its input is our own freshly converted WAV, so
  a failure there is far more likely the model or binary than the memo; rejecting would
  silently drop every memo while a model file is broken. Cost: a memo that genuinely crashes
  whisper is retried every 10 minutes (the report shows it each time).
- **D5 — the guide's model.** The spec asks the guide to name "a tested small English model and
  its size". No model may be downloaded here and `whisper-cli` is not installed, so I cannot
  test one. Proposal: name `ggml-base.en.bin` (about 148 MB) as the recommended model, say
  plainly it is not exercised by this PR's tests, and leave the `live_whisper` test (reads a
  test-only `BRAIN_LIVE_WHISPER_MODEL`, skips without it or the binary) as the way to verify.

## Tests (TDD, real Postgres for the DB paths, no production-module monkey-patching)

- `tests/test_settings_voice.py`: defaults, unset model = off, `~` expansion, relative path
  refused, max-minutes bounds and junk, messages never quote the value.
- `tests/test_ingest_whisper.py`: resolution (off / model missing / binary missing / ready,
  injected `which` and `is_file`), hints, argv, the minimal env, runner contract with a fake
  `popen` through `run_capped`.
- `tests/test_ingest_voice.py`: AC1 with a fake runner (synthetic `.m4a` path → fake
  transcript), each failure row of the table above (duration line missing / NaN / zero / over
  cap; afinfo/afconvert non-zero; each timeout; whisper non-zero and oversized; the lying-header
  WAV-size recheck; symlink and FIFO and oversized file refused; `[BLANK_AUDIO]` → empty), the
  exact argv and timeouts each binary gets (`2 × duration + 60`), the temp WAV removed on
  success and failure, error messages content-free.
- `tests/test_cli_ingest_folder_voice.py` (real test DB): AC1 end to end through
  `brain ingest-folder --dir` with a configured voice folder → a confidential `voice` document
  titled by the original stem; AC2 over-max → `rejected`, counted, file stays in `imported/`;
  AC3 timeout → `rejected` with a class-only line; D4 transient → moved back; whisper
  unavailable → folder skipped, nothing moved, exit 1; `--sensitivity normal` override.
- AC4: `tests/test_ingest_dispatch.py` extended (audio listed, not walked, confidential
  default) and an `ingest-dir` test that a `.m4a` in the tree is not picked up.
- `tests/test_voice_source.py`: the kind in `VALID_SOURCE_KINDS`, both glyph tables,
  `MONTH_GROUPED_PATHS`. `ADDED_KINDS` / `DEMO_EXEMPT_KINDS` (reason: the demo seeds text, not
  audio) updated.
- `tests/test_doctor_voice.py` and the `doctor_runtime` order test (adds `voice memos`).
- `tests/test_cli_extract.py` (or the existing RM-33 file): `brain ingest x.m4a` with voice off
  is one line, exit 1.
- One `live_whisper` test: a synthetic tone WAV generated in `tmp_path`, real binaries;
  skipped here.
- 100% line + branch coverage on `settings/voice.py`, `ingest/whisper.py`, `ingest/voice.py`,
  `doctor_voice.py`. Mutation-proof (git-archive copies, `PYTHONPATH` pinned, batches ≤ 10): the
  three timeouts, the duration cap (both checks), the size cap, the rejected/transient mapping,
  the confidential default, `in_directory_walks=False`, the minimal env.

## Docs

`docs/guides/drop-folders.md` (a voice-memo section: install, model, folder entry, the cap,
recovery), `docs/configuration.md` (two knobs, the `voice` defaults), `docs/cli-reference.md`
(`ingest-folder` and `ingest` notes), `CHANGELOG.md`, `skills/ingest-brain/SKILL.md` routing
row + `skills/ingest-brain/sources/drop-folders.md`, the spec's Appendix B row,
`docs/plans/2026-10-23-backlog-reconciliation.md` item 5.2. Memory: `cli.md`, `extractors.md`,
`types.md`, each marked "verify merged".
