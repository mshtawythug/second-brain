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

## Review round 1 (PR #107)

The security review (sec-107) and the code review (rev-107) found nine items. All are fixed
in one commit on this branch, with the rulings below.

**Checked on this macOS before coding.** These checks used a 2 s synthetic WAV in a scratch
directory:

- `/usr/bin/afinfo` and `/usr/bin/afconvert` both read `/dev/fd/N` from an fd opened
  `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`. `afinfo` prints `File: /dev/fd/N`, not the name.
- `/dev/fd/N` shares the fd's offset. After `afinfo` reads, `afconvert` on the same fd fails
  with `Couldn't open input file`. Rewinding the fd (`lseek(fd, 0)`) before each tool fixes
  this. So the fixed-name hardlink fallback was not needed.
- Under `RLIMIT_FSIZE`, `afconvert` writes exactly up to the limit, then exits **0**. It is
  not killed by SIGXFSZ. A truncated WAV is therefore judged by its size, never by the exit
  code.

| Item | Fix | Where |
|---|---|---|
| sec F1 (HIGH) disk fill, faked duration | `afconvert` runs with `max_file_bytes = wav_byte_limit(cap) = (cap + 1 s) × 32,000 + 4,096`, set as `RLIMIT_FSIZE` through `run_capped`. The WAV-length check comes before the exit-code check, so a cut WAV is `AudioTooLongError` (rejected). Neither tool gets the name (see F2). The stored `duration_s` is the WAV's length. | `ingest/voice.py`, `ingest/capped_process.py` |
| sec F2 TOCTOU | The memo is opened once `O_NOFOLLOW \| O_NONBLOCK`, checked with `fstat`, and handed to both tools as `/dev/fd/N` with `pass_fds`, rewound before each one. `ELOOP` raises `UnreadableAudioError`. | `ingest/voice.py` |
| sec F3 relative binary | A non-absolute `which("whisper-cli")` is unavailable, with `HINT_RELATIVE_BINARY`. | `ingest/whisper.py` |
| sec F4 stale WAVs | `sweep_stale_tempdirs` runs at the start of an `ingest-folder` run that has an audio folder. It removes this user's `brain-voice-*` real directories older than `max(3 h, the longest a memo can run at the cap)`. The longer bound means a live run at a 600-minute cap is never swept. | `ingest/voice.py`, `cli_ingest_folder.py` |
| sec F5 endless retry | A new `WhisperFailedError(TranscriptionError)` is the only failure counted. The count is per SHA-256 in `$BRAIN_HOME/state/voice-failures.json` (`ingest/voice_failures.py`: atomic write, `0o700` directory, at most 1,000 entries, a malformed file starts over). The third failure raises `RejectedFileError`, and a success clears the count. A tool that cannot start is not counted. This supersedes the D4 "minimum" follow-up. | `ingest/voice_failures.py`, `cli_ingest_folder.py` |
| sec F6 model file | `model_file_problem` requires a regular file owned by this user or root, with no group/other write bit, and not under a configured drop folder. A broken `drop-folders.toml` checks no folder, since the run reports it anyway. Otherwise the model is unavailable, and `brain doctor` warns with the hint. Root is allowed as an owner on purpose: only root could change such a file. | `ingest/whisper.py`, `doctor_voice.py` |
| rev F1 orphaned child | `run_capped` kills the process group on any `BaseException`. Before, `Popen.__exit__` waited out the child: forever on `SystemExit`, and it left the child running on `KeyboardInterrupt`. The new `sigterm_as_system_exit()` wraps the `ingest-folder` run, so a bootout unwinds through the `TemporaryDirectory` and the fd `finally`. This is shared code. The Apple Notes and GitHub readers gain the kill on an exception or Ctrl-C (SIGTERM handling is only around `ingest-folder`), and their `Popen` calls are otherwise unchanged (`pass_fds` and `preexec_fn` are passed only when set). Image OCR does not use `run_capped` (corrected in round 2; the `78d34e6` message overstates it). | `ingest/capped_process.py`, `cli_ingest_folder.py` |
| rev F2 oversized stdout | `TranscriptTooLargeError` (rejected). | `ingest/voice.py` |

**Proof.**

- New regression tests cover each item.
  - The name-injection test fails on the old code.
  - The SystemExit kill test hung for 30 s on the old code.
  - Two real-tool tests run `afinfo` and `afconvert` (skipped off macOS). One covers the
    `/dev/fd` path with the injected name; the other is the `RLIMIT_FSIZE` stop at 20 kB.
- Line and branch coverage is 100% on `capped_process.py`, `voice.py`, `whisper.py`,
  `voice_failures.py`, `doctor_voice.py` and `cli_ingest_folder.py`.
- 30 mutations were run in git-archive copies with `PYTHONPATH` pinned, in batches of 5, and
  all 30 were killed. One, removing the ledger's value check, first survived, because the
  test stored the bad values under a digest no memo had. The test now uses the memo's own
  digest.
- The `O_NONBLOCK` flag was not mutated: removing it would block the FIFO test forever,
  rather than fail it.

**Follow-up (coordinator, same round).**

- The `preexec_fn` is now `capped_process.limit_file_size`. It sets SIGXFSZ to `SIG_DFL`,
  then `RLIMIT_FSIZE`. `Popen`'s default `restore_signals` already resets SIGXFSZ, so the
  hook only matters when that is off, as a test shows.
- On this macOS, `afconvert` still exits 0 with the WAV cut off at the limit; it handles the
  signal itself. `head` under the same hook dies of SIGXFSZ.
- A hit on the limit is `AudioTooLongError` in either of two forms:
  - `returncode == -SIGXFSZ`, whatever the WAV's size;
  - a WAV longer than the cap plus 1 s. A WAV stopped at the limit always is, because the
    limit adds 4,096 bytes on top.
- Any other signal with a short WAV is still `UnreadableAudioError`.
- Boundary tests:
  - exactly 60 s of audio plus `afconvert`'s real 4 KiB header is transcribed;
  - the largest WAV accepted is (cap + 1 s) × 32,000 bytes, and one byte more is rejected;
  - both sizes sit under the limit.
- 5 more mutations, all killed. Wiring the hook first survived: with `restore_signals` on,
  it behaves the same as a bare `setrlimit`. A `restore_signals=False` test now kills it.
  The hook runs in the forked child, which coverage does not trace, so it carries a
  `pragma: no cover` and is tested in a fresh interpreter.

## Review round 2 (PR #107)

sec-107b confirmed all earlier findings fixed at `f06c64d`. Its own real-`afconvert` repro
was killed by SIGXFSZ (`rc -25`) at exactly the limit, and the memo was rejected with
`TMPDIR` left empty. That is the other form of a limit hit, and both forms are handled. The
round's items are all LOW.

**N1, the model check could be raced.** I used the fallback the ruling allows, not
`-m /dev/fd/N`, after reading whisper.cpp's `src/whisper.cpp` at master:

- `whisper_init_from_file_with_params_no_state` opens the model with
  `std::ifstream(path_model)` and reads it sequentially, so `/dev/fd/N` would load.
- But with `WHISPER_USE_COREML`, `whisper_get_coreml_path_encoder` derives the encoder's
  path from the model's: it replaces `.bin` with `-encoder.mlmodelc`. If that load fails,
  `whisper_init_state` returns null unless the build sets `WHISPER_COREML_ALLOW_FALLBACK`.
  From `/dev/fd/N` the derived path is `/dev/fd/N-encoder.mlmodelc`, so on a Core ML build
  every memo would fail, and after three tries be rejected.
- Homebrew's build flags could not be confirmed here (no install, no formula fetched), and
  users can build whisper with Core ML themselves.

So the coordinator ruled for the fallback, and `model_file_problem` now does two things.
It runs inside `resolve_whisper`, and so also in `brain doctor`.

- It resolves the model strictly, then opens it `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` and
  checks it with `fstat`: regular, owned by this uid or root, no group or other write bit.
  A FIFO is "missing" and never blocks.
- It requires every directory up to `/` to be owned by this uid or root and writable by
  no one else. That covers the directories above the path as given and above the file it
  resolves to. A sticky directory (`/tmp`, `1777`) is the exception: others cannot rename
  or remove this user's entries in it.

Whisper is still given the path, so a window remains between the check and whisper's
open. The directory rule closes it to everyone but this user and root. The new hint is
`HINT_MODEL_DIR_UNSAFE`.

`O_NOFOLLOW` on that open is defence in depth. It comes after `resolve(strict=True)`, so
the last component is already not a symlink unless it was swapped in between, which no
deterministic test can stage. It is the one new guard left unmutated.

**N2.** `sweep_stale_tempdirs` skips an entry whose `lstat` raises `OSError` (another run
removed it after the listing). Before, that aborted the run.

**N3.** Both ledger calls are best-effort. An `OSError` prints `warning: the voice-failure
count was not updated/cleared (<class>)` and the run carries on: a failure still moves the
memo back, and a success still counts. `forget` now runs only after `_ingest` succeeds, so a
memo transcribed but not stored keeps its count.

**Docs (rev 3, rev 4, sec INFO).**

- The CHANGELOG no longer says image OCR gains the kill.
- The sweep's age is stated with the long-cap case (about 20 hours at 600 minutes).
- The over-long lines are wrapped.
- The `afconvert` behaviour reads "exits 0 with the file truncated, or is killed by SIGXFSZ,
  depending on the build; both are handled".

**rev-107b's last two items.**

- **4.** `sigterm_as_system_exit`'s restore of `previous or SIG_DFL` was untested, because
  pytest's SIGTERM handler is already `SIG_DFL`. A sentinel Python handler is now installed
  first, and the tests assert it is the one restored, at the unit level and through the
  CLI.
- **6.** The root-owned-model tests use `/bin/sh` (resolved), which is root-owned on macOS
  and Linux, so CI covers that branch.
- `_digest`'s `OSError` sits inside the same best-effort guard. A CLI test has it raise, and
  the memo still goes back.

**Proof.**

- New tests were red first:
  - N1: the directory cases, a sticky parent, a FIFO model and an unreadable directory.
  - N2: an entry removed between the listing and the `stat`.
  - N3: an unwritable ledger on a failure, an unwritable ledger on a success, an ingest
    failure after a good transcription, and an unreadable digest.
- 13 mutations, all killed (9, then 4 for the coordinator's and rev-107b's follow-ups).
- Coverage stays at 100% line and branch on the six modules.
