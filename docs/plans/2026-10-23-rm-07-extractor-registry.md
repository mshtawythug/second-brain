# RM-07 — Extractor registry, one source-kind definition, and the stale ingest docstring

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-07 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 2; G2; includes backlog BL-12). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `a8a145b` (RM-01..RM-06 merged).

## Goal

A new file extractor registers in a leaf module (`ingest/dispatch.py`) instead of growing
`ingest/__init__.py`. A source kind is defined once in Python. `ingest_document`'s docstring says
when the summary is really written. Nothing changes for any existing file type or source kind.

## Files

| File | Change |
|---|---|
| `src/brain/ingest/dispatch.py` (new) | `ExtractorFn`, `ExtractorSpec`, `ExtractorRegistry`, `BUILTIN_EXTRACTORS` (text, markdown, pdf, docx, in today's order), and the module functions `register_extractor`, `extract_path`, `supported_extensions(*, directory_walk=False)` bound to one default registry. |
| `src/brain/ingest/__init__.py` | Delete `_EXTRACTORS`, `extract_path`, `supported_extensions`; re-export the two functions from `.dispatch`. BL-12: rewrite the enrichment paragraph of `ingest_document`'s docstring (net zero). Net negative overall, so no growth record. |
| `src/brain/cli_ingest.py` | Import `extract_path` / `supported_extensions` from `.ingest.dispatch`; `ingest-dir` (with no `--ext`) asks for `supported_extensions(directory_walk=True)`. In place, net ≤ 0. |
| `src/brain/vault/links.py` | `_SOURCE_KINDS` becomes an identity re-export of `brain.source_kinds.VALID_SOURCE_KINDS`. |
| `src/brain/source_kinds.py` | Docstring: drop the "genuine separate copy" sentence; name `vault.links` as a re-export. |
| `tests/test_ingest_dispatch.py` (new) | AC1–AC3, the before/after equivalence table, laziness. |
| `tests/test_source_kinds_identity.py` (new) | AC4, plus the pinned kind set and every re-export's identity. |
| `tests/test_ingest_docstring.py` (new) | AC5. |
| `CLAUDE.md` | Ceiling rows: promote `ingest/__init__.py`'s descriptive last hop to `ad67f43` (2,369), add this ticket's hop; add a hop to `cli_ingest.py`'s row. |
| `docs/specs/2026-10-23-brain-ui-ingest-tab-design.md` | §3.1 item 2 names `ingest.dispatch.supported_extensions()` instead of `ingest._EXTRACTORS` (ticket scope). |
| `CONTRIBUTING.md`, `CHANGELOG.md` | "Adding a file extractor or a source kind"; Unreleased entry. |
| Spec Appendix B; backlog BL-12 | Follow-up commit after the PR opens: RM-07 → DONE (#PR); BL-12 → SHIPPED. |

## Design notes beyond the ticket

- **`wrap_errors` is a zero-argument loader, not a tuple.** The ticket types it as
  `tuple[type[Exception], ...]` and also asks for "the same lazy imports" `extract_path` has today.
  Both cannot hold: `PyPdfError` and `PackageNotFoundError` are importable only by importing
  `pypdf` and `docx`, which costs about 230 ms and 190 ms (`python -X importtime`), and today
  neither loads on `import brain.cli`. A tuple built at registration would load both on every
  `brain` invocation. So `wrap_errors: Callable[[], tuple[type[Exception], ...]]`, defaulting to
  "none", is called only when that extractor runs. A test pins that importing `brain.cli` and
  `brain.mcp_server` loads neither package.
- **The error text is unchanged.** A wrapped parser error is re-raised as
  `ValueError(f"malformed {name.upper()}: {exc}")`, which is exactly `malformed PDF: …` and
  `malformed DOCX: …` today. `UnicodeDecodeError` keeps its wrapping for every extractor.
- **A registry object, so tests never mutate the process-wide one.** `ExtractorRegistry(specs)`
  carries `register` / `extract_path` / `supported_extensions`; the module functions delegate to
  `_DEFAULT_REGISTRY`. AC3 runs on a fresh registry built from `BUILTIN_EXTRACTORS`, and once more
  through the module functions with `monkeypatch.setattr` swapping in a fresh registry (standard
  test double, automatic cleanup).
- **Registration validates.** A duplicate extension, an empty `extensions`, or an extension that is
  not lowercase with a leading dot raises `ValueError`; a rejected spec registers nothing (no
  half-registered spec).
- **`supported_extensions()` keeps its order** (`.txt, .md, .markdown, .pdf, .docx`), and with
  `directory_walk=True` it returns the same list today, because all four built-ins walk.
- **No import cycle.** `dispatch.py` imports `ExtractedDoc` only under `TYPE_CHECKING`; the
  extractor modules load at call time, as now.

## Tasks (TDD, RED first)

1. RED: `tests/test_ingest_dispatch.py`, `tests/test_source_kinds_identity.py`,
   `tests/test_ingest_docstring.py`. Confirm they fail on the base.
2. GREEN: `dispatch.py`; re-point `ingest/__init__.py`, `cli_ingest.py`, `vault/links.py`; BL-12;
   `source_kinds.py` docstring.
3. Ceiling rows; docs; CHANGELOG; CONTRIBUTING; Ingest spec §3.1.
4. `ruff check`, `mypy src/`, `python scripts/check_ceiling_table.py`, targeted tests; then the
   full suite once, serially, in a fresh py3.11 `.venv` with `GITHUB_ACTIONS=true CI=true`.
5. Mutation proofs (≥ 4) in a `git archive` copy.
6. E2E: `brain ingest` of a synthetic `.md` and `.txt` into the 5434 test DB on the base and on the
   branch; compare.

## Acceptance criteria → tests

| AC | Test |
|---|---|
| 1. Existing extractor tests pass unchanged | `tests/test_cli_ingest.py`, `tests/test_ingest_{text,markdown,pdf,docx}.py` untouched and green |
| 2. Duplicate extension raises | `test_register_duplicate_extension_raises` |
| 3. Runtime extractor used, listed, and excluded from walks when `in_directory_walks=False` | `test_registered_extractor_is_dispatched_and_listed`, `test_non_walk_extractor_absent_from_directory_walk`, `test_module_functions_use_the_default_registry` |
| 4. `vault.links._SOURCE_KINDS is source_kinds.VALID_SOURCE_KINDS` | `test_vault_links_source_kinds_is_canonical` |
| 5. Docstring no longer says "inside the same transaction" | `test_ingest_document_docstring_says_summary_runs_after_commit` |
| No behaviour change | `test_builtin_dispatch_table_unchanged`, `test_builtin_extract_matches_direct_extractor`, `test_source_kind_set_unchanged` |

## Risks

- A later extractor that forgets `in_directory_walks=False` changes what `brain ingest-dir` picks
  up. That is the field's purpose; the ticket that adds it states its value.
- A new kind is a valid `[[<kind>:<id>]]` prefix (ticket Risks); no kind is added here.
