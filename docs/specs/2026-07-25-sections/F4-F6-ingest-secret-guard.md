# F4-F6 — Ingest secret guard and per-document sensitivity tier

> Design section of `docs/specs/2026-07-25-agent-memory-safety-ui-design.md`.
> Global constraints (PII, production safety, quality gates, style) are inherited from
> section 4 of that document and are not restated here.

## Trust boundary — an ingest-time secret guard and a per-document sensitivity tier

### 1. Goal

Today the brain is a trust sink with no trust boundary: anything piped into `brain ingest-stdin` (a Slack thread containing a rotated-but-still-live `xoxb-` token, a Krisp transcript where someone read a connection string aloud, a `.env` accidentally passed to `brain ingest`) is stored verbatim in `documents.content`, duplicated across `chunks.content`, written to the on-disk vault mirror, and — unless the doc happens to be `draft` — published to the rendered Quartz wiki. Separately, there is a live hosted-egress path (`BRAIN_EMBEDDER=voyage` POSTs raw chunk text to Voyage AI) and an MCP path (`brain_show` returns full bodies to whatever model is driving the session) with no per-document gate at all. This section adds two guard rails: (a) an **ingest-time secret guard** that scans content *before* it is hashed, chunked, embedded, or mirrored, sharing its pattern set with the repo's existing pre-commit PII gate; and (b) a **per-document sensitivity tier** — a single new column — that is enforced at the three egress boundaries that actually leave the machine or the process. The user-visible outcome: pasting a credential into the brain produces a loud, actionable refusal instead of a silent permanent copy in four places, and marking a note `confidential` keeps its body off the hosted embedder, out of MCP responses, and off the published wiki.

### 2. Current state

**Secret detection already exists — in bash, in one place, and only for git.** `scripts/hooks/pre-commit` is a two-stage gate wired via `git config core.hooksPath scripts/hooks` (`scripts/hooks/README.md:31-37`). Stage 1 is deterministic (`scripts/hooks/pre-commit:44-71`) and knows exactly these patterns, all in a single `grep -nEi` alternation at `scripts/hooks/pre-commit:46`:

| Pattern | Regex (verbatim from line 46) |
|---|---|
| AWS access key | `AKIA[0-9A-Z]{16}` |
| AWS temp key | `ASIA[0-9A-Z]{16}` |
| PEM private key | `-----BEGIN [A-Z ]*PRIVATE KEY-----` |
| Slack token | `xox[baprs]-[0-9A-Za-z-]{10,}` |
| OpenAI key | `sk-[A-Za-z0-9]{20,}` |
| OpenAI project key | `sk-proj-[A-Za-z0-9_-]{20,}` |
| Stripe secret (live) | `sk_live_[A-Za-z0-9]{20,}` |
| Stripe restricted (live) | `rk_live_[A-Za-z0-9]{20,}` |
| GitHub PAT (classic) | `ghp_[A-Za-z0-9]{36}` |
| GitHub PAT (fine-grained) | `github_pat_[A-Za-z0-9_]{20,}` |
| GitLab PAT | `glpat-[A-Za-z0-9_-]{18,}` |
| Google API key | `AIza[0-9A-Za-z_-]{35}` |

Plus a real-email heuristic (`scripts/hooks/pre-commit:54-59`) that extracts `[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}` and subtracts an END-anchored synthetic-domain allow set (`example|test|acme|contoso|northwind|invalid` × `.com|.org|.net`, plus `localhost`), and a gitignored substring denylist loop (`scripts/hooks/pre-commit:63-71`). Stage 2 is a `claude -p` semantic pass that fails closed (`scripts/hooks/pre-commit:104-119`).

**None of this is reachable from Python.** `src/brain/ingest/` contains no guard module; `ingest_document` (`src/brain/ingest/__init__.py:421`) goes straight from argument normalization to `h = _content_hash(doc.content)` at line 555 with zero content inspection, and `update_document` (`src/brain/ingest/__init__.py:1568`) likewise hashes at line 1658 with no inspection. Missing: an importable pattern set, a scanner, a redactor, a config mode, escape hatches, and a corpus sweep.

**`documents.draft` is a publish flag, not a confidentiality control.** Migration `007_email_thread_and_draft.sql:11` adds `draft BOOLEAN NOT NULL DEFAULT FALSE` with a partial index at line 15. Its entire enforcement is one branch in the Quartz emitter, `src/brain/quartz_overrides/quartz/plugins/emitters/contentIndex.ts:397` (`if (fm.draft === true) { delete parsed[slug]; continue }`), fed by the frontmatter line written at `src/brain/vault/export.py:453-454`. `brain mark-draft`'s own docstring is explicit that draft docs stay fully visible locally (`src/brain/cli.py:6452-6456`), and nothing in `hybrid_search` (`src/brain/search.py:240`) or `brain_show` (`src/brain/mcp_server.py:544`) filters on it.

**The hosted-egress path is real and unguarded.** `VoyageEmbedder.embed` (`src/brain/embeddings.py:306`) POSTs raw chunk text via `self._client.embed(texts=batch, model=_VOYAGE_MODEL, input_type=input_type)` at `src/brain/embeddings.py:331`. The only caller in the ingest pipeline is `_embed_chunks` (`src/brain/ingest/__init__.py:351`), invoked at `src/brain/ingest/__init__.py:922` — it inspects only the duck-typed `produces_embeddings` flag (line 365), never the document. Missing: any per-document veto.

**The FTS-only machinery I will reuse already exists.** `NullEmbedder` (`src/brain/embeddings.py:344`, flag at line 371) returns `None` placeholders through `_embed_chunks` (`src/brain/ingest/__init__.py:365-366`), bound as SQL NULL by `_insert_chunks` (`src/brain/ingest/__init__.py:397-418`); `hybrid_search` coerces at `src/brain/search.py:324` (`fts_only = fts_only or not getattr(embedder, "produces_embeddings", True)`). I do **not** need to invent NULL-embedding support — only a per-document reason to take that path.

**Frontmatter key registries.** Two, and both must be updated: the export strip set `_EXPORT_OWNED_FRONTMATTER_KEYS` (`src/brain/vault/export.py:31-59`) and sync's `reserved` set (`src/brain/vault/sync.py:994-1006`). Note the Codex-caught regression documented at `src/brain/vault/export.py:44-57` — a key in the export strip set but *not* in sync's reserved set gets silently deleted from user files on the next export. Any new key must go in **both**.

**Existing surfaces I reuse rather than reinvent:** `backfill_app` (`src/brain/cli.py:253-258`, commands at 8455/8509/8599, all with a `--dry-run` idiom); `_set_draft` (`src/brain/cli.py:6476`) as the naming/idempotency template; `resolve_document_prefix` (`src/brain/queries.py:77`); `DocumentRow` (`src/brain/queries.py:55`); `list_documents` (`src/brain/queries.py:520`); `BrainError` (`src/brain/errors.py:16`); the "must be one of" env-validation idiom at `src/brain/config.py:1601-1610`.

### 3. User-visible surface

#### 3.1 Guard mode config

`BRAIN_SECRET_GUARD` ∈ `warn | redact | reject | off`. **Default: `warn`.**

Justification: `reject` as default would be hostile and, worse, *lossy in a hidden way* — `sk-[A-Za-z0-9]{20,}` matches plenty of legitimate prose (any 23-char alphanumeric run after "sk-", e.g. a Stripe *docs* excerpt in a note), and a user bulk-running `brain ingest-dir ~/Documents` would get a mid-run abort on file 400 of 900 with no obvious remedy. `redact` as default is the worst option: it silently mutates the note body, changes `content_hash`, and destroys information the user may have deliberately kept (this repo's own `.pii-allowlist.txt` exists precisely because these regexes false-positive). `warn` is loud, lossless, and reversible — it prints a finding table to stderr, records nothing, stores the document unchanged, and tells the user the exact command to re-run under `reject` or `redact`. Users who want a hard boundary set `BRAIN_SECRET_GUARD=reject` in `.env`, which is a one-line opt-in documented in `.env.example`.

#### 3.2 New flags on existing ingest commands

Added to `ingest` (`src/brain/cli.py:2031`), `ingest-dir` (`:2072`), `ingest-stdin` (`:2139`), `ingest-gmail` (`:2220`):

```
--allow-secrets       BOOL, default False.
                      Help: "Skip the ingest-time secret guard for THIS
                             invocation (BRAIN_SECRET_GUARD). Findings are
                             still printed; nothing is redacted or refused."
```

Added to `ingest`, `ingest-dir`, `ingest-stdin` only (a `--sensitivity` on a bulk Gmail pull is a footgun; see Open Questions Q4):

```
--sensitivity TEXT    default "normal", one of: normal|confidential.
                      Help: "Sensitivity tier for the ingested document(s).
                             'confidential' keeps the body off the hosted
                             embedder, out of MCP brain_show by default, and
                             off the published wiki."
```

#### 3.3 New commands

```
brain mark-confidential <id>     # id: str, 6+ hex chars (Argument, required)
brain mark-normal <id>           # id: str, 6+ hex chars (Argument, required)
brain list --sensitivity TEXT    # new Option on the existing `brain list`
brain backfill scan-secrets      # new command under the existing backfill sub-app
```

`brain backfill scan-secrets` flags:

```
--apply           BOOL, default False.
                  Help: "Actually apply the action (default: read-only report)."
--action TEXT     default "report", one of: report|mark-confidential|redact.
                  Help: "What --apply does to each hit. 'report' never writes."
--limit INT       default 0 (= no limit), min 0.
                  Help: "Stop after N documents scanned."
--json            BOOL, default False.
                  Help: "Emit machine-readable JSON instead of the table."
```

#### 3.4 Literal output samples

`warn` mode on ingest (stderr; the existing stdout success line at `src/brain/cli.py:2069` is **unchanged**):

```
⚠  secret guard: 2 finding(s) in "Deploy runbook" — stored UNCHANGED (BRAIN_SECRET_GUARD=warn)
   line 14, col 12-52   aws_access_key_id     AKIA****************
   line 31, col 1-27    private_key_header    -----BEGIN … PRIVATE KEY-----
   Re-run with BRAIN_SECRET_GUARD=redact to strip them, or =reject to refuse.
ingested: runbook.md → 3f2a9c11-...
```

`reject` mode (exit code 1, nothing written):

```
✗ secret guard: refusing to ingest "Deploy runbook" — 2 finding(s) (BRAIN_SECRET_GUARD=reject)
   line 14, col 12-52   aws_access_key_id     AKIA****************
   line 31, col 1-27    private_key_header    -----BEGIN … PRIVATE KEY-----
   Fix the source, or pass --allow-secrets, or add `allow_secrets: true` to the note's frontmatter.
```

`brain backfill scan-secrets` (read-only default):

```
scanning 1376 document(s) — READ ONLY (pass --apply to act)

  id        kind     title                              findings
  3f2a9c11  manual   Deploy runbook                     2  aws_access_key_id, private_key_header
  8d41ba07  slack    #infra — rotating the CI token     1  slack_token
  b02f77e3  krisp    Platform sync 2026-03-04           1  openai_key

3 document(s) with findings / 1376 scanned / 0 written
next: brain backfill scan-secrets --apply --action mark-confidential
```

`brain mark-confidential` / `brain mark-normal` (exact shape mirrors `_set_draft`, `src/brain/cli.py:6502` and `:6515`):

```
marked 3f2a9c11 as confidential (was normal)
3f2a9c11 is already confidential
```

#### 3.5 JSON shapes

`brain backfill scan-secrets --json`:

```json
{
  "scanned": 1376,
  "flagged": 3,
  "written": 0,
  "action": "report",
  "applied": false,
  "documents": [
    {
      "id": "3f2a9c11-...",
      "title": "Deploy runbook",
      "source_kind": "manual",
      "findings": [
        {"kind": "aws_access_key_id", "line": 14, "col_start": 12, "col_end": 52,
         "preview": "AKIA****************"}
      ]
    }
  ]
}
```

`brain list --json` gains one additive key per row, `"sensitivity": "normal"|"confidential"` (added to the dict literal at `src/brain/cli.py:5721-5728`).

MCP `brain_show` on a confidential doc, default (`redact_confidential` unset):

```json
{
  "id": "3f2a9c11-...", "title": "Deploy runbook", "content": null,
  "content_type": "markdown", "tags": ["ops"], "source_path": "...",
  "ingested_at": "2026-03-04T10:00:00+00:00", "source_kind": "manual",
  "sensitivity": "confidential",
  "withheld": "body withheld: sensitivity=confidential. Re-call with include_confidential=true to retrieve it."
}
```

#### 3.6 Backward-compatibility risk and how it is avoided

| Risk | Guard |
|---|---|
| `brain ingest` stdout line changes → breaks `tests/test_cli_ingest.py` and any script parsing it | All guard output goes to **stderr**; the stdout `f"{verb}: {path.name} → {result.document_id}"` at `src/brain/cli.py:2069` is byte-identical. |
| `brain_show` consumers break on a new key | `sensitivity` is **only emitted when it is not `"normal"`** — exactly the additive discipline used for `summary` at `src/brain/mcp_server.py:645-650`. Normal docs get a byte-identical payload. `content` becomes `null` **only** for confidential docs, which cannot exist before this migration. |
| `brain list --json` consumers break | Additive key, always present, always a string. Existing keys and ordering unchanged. |
| `warn` default changes ingest exit codes | It does not: `warn` never raises and never changes the exit code. Only `reject` exits 1, and only when explicitly configured. |
| Vault mirror files churn on the next `brain vault export` | `sensitivity:` is emitted **only when non-`normal`** (same rule as `draft`, `src/brain/vault/export.py:453`), so all 1376 existing mirrors are byte-identical after the change. |
| Existing hybrid search results shift | **Sensitivity does not filter `hybrid_search` at all.** The local CLI is inside the trust boundary — same posture as `draft` (`src/brain/cli.py:6452-6456`). Zero eval-metric movement, so `tests/eval/baselines/ci.json` is untouched. |

### 4. Module layout

| Path | New/changed | Purpose | Est. lines |
|---|---|---|---|
| `src/brain/secret_patterns.py` | **new** | Canonical, importable pattern registry: `SecretPattern` dataclass, `SECRET_PATTERNS` tuple (the 12 patterns above + the email heuristic), `SYNTHETIC_EMAIL_DOMAIN_RE`, and `egrep_alternation() -> str` which reproduces the exact `grep -nEi` string the bash hook uses. Placed at `src/brain/` (not under `ingest/`) because both ingest **and** the git hook consume it — DRY per CLAUDE.md "extract anything used in 2+ places". | ~120 |
| `src/brain/ingest/guard.py` | **new** | `SecretFinding`, `scan_secrets`, `redact_secrets`, `GuardMode`, `apply_guard`. Pure logic, zero DB, zero I/O → 95% coverage target. | ~180 |
| `src/brain/errors.py` | changed | Add `class SecretGuardError(BrainError)` and `class SensitivityError(BrainError)`. | +14 |
| `src/brain/config.py` | changed | Add `DEFAULT_SECRET_GUARD = "warn"`, `_VALID_SECRET_GUARD_MODES`, field `secret_guard: str`, parse block mirroring `:1601-1610`, entry in the `_load_field_dict` dict (`:1925` region). | +26 |
| `src/brain/migrations/025_document_sensitivity.sql` | **new** | One column + CHECK + partial index. | ~22 |
| `src/brain/ingest/__init__.py` | changed | Guard call site in `ingest_document` (before `:555`) and `update_document` (before `:1658`); `sensitivity` threaded into `_insert_new_document`/`_update_doc_in_place`; sensitivity veto inside `_embed_chunks`. Currently 1926 lines — **already over the 800 cap**. Net add ~70. See Open Questions Q6. | +70 |
| `src/brain/queries.py` | changed | `DocumentRow.sensitivity: str = "normal"`; `sensitivity` filter kwarg + SELECT column on `list_documents` (`:520`); new `iter_documents_for_secret_scan()` keyset iterator; `set_document_sensitivity()`. | +85 |
| `src/brain/cli_sensitivity.py` | **new** | `mark-confidential` / `mark-normal` / `backfill scan-secrets` bodies, registered onto the existing `app` and `backfill_app`. New file rather than growing `cli.py` (9760 lines). | ~210 |
| `src/brain/cli.py` | changed | `--allow-secrets` / `--sensitivity` options on 4 ingest commands; `--sensitivity` on `list`; `import` + registration of `cli_sensitivity`. | +55 |
| `src/brain/vault/export.py` | changed | `_DocumentForExport.sensitivity`; SELECT column (`:125`); `fields["sensitivity"]` when non-normal (next to `:453`); add `"sensitivity"` to `_EXPORT_OWNED_FRONTMATTER_KEYS` (`:31`). | +12 |
| `src/brain/vault/sync.py` | changed | Add `"sensitivity"` to `reserved` (`:994`); read it off frontmatter into the column for vault-tier rows. | +18 |
| `src/brain/mcp_server.py` | changed | `include_confidential: bool = False` param on `brain_show` (`:544`); withhold branch before the payload build at `:635`. | +34 |
| `src/brain/quartz_overrides/quartz/plugins/emitters/contentIndex.ts` | changed | Extend the drop branch at `:397` to `if (fm.draft === true \|\| fm.sensitivity === "confidential")`. | +8 |
| `scripts/hooks/pre-commit` | changed | Replace the inline regex at `:46` with a marker-delimited block that a parity test asserts against `egrep_alternation()`. | ~+8 |
| `.env.example`, `docs/specs/…`, `README` docs split | changed | Document `BRAIN_SECRET_GUARD`. | +15 |

### 5. Design detail

#### 5.1 The shared pattern registry (`src/brain/secret_patterns.py`)

```python
"""Canonical secret/credential regex registry shared by ingest and the git hook."""

@dataclass(frozen=True)
class SecretPattern:
    kind: str          # "aws_access_key_id", "slack_token", ...
    regex: str         # the raw POSIX-ERE-compatible source, hook-shareable
    label: str         # human label for the CLI table
    preview_head: int  # chars of the match kept verbatim in a SAFE preview

SECRET_PATTERNS: tuple[SecretPattern, ...] = (...)   # the 12 rows of §2

def compiled_patterns() -> tuple[tuple[SecretPattern, re.Pattern[str]], ...]: ...
def egrep_alternation() -> str: ...
```

Every `regex` string is copied **verbatim** from `scripts/hooks/pre-commit:46` so `egrep_alternation()` — a plain `"|".join(p.regex for p in SECRET_PATTERNS)` wrapped in `(...)` — reproduces the hook's existing alternation character-for-character. The 12 regexes are POSIX-ERE and Python-`re` compatible without translation (verified: no `\d`, no lookaround, no lazy quantifiers).

**How the pre-commit hook keeps working.** The hook must run with no venv, offline, and from a `git commit` in any shell — so it cannot `import brain`. Instead the hook's regex lives between two literal marker comments and a Python test enforces byte-equality:

```bash
# --- BEGIN GENERATED: brain.secret_patterns.egrep_alternation() — do not edit by hand ---
_SECRET_RE='(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|…|AIza[0-9A-Za-z_-]{35})'
# --- END GENERATED ---
```

`tests/test_secret_pattern_hook_parity.py` reads `scripts/hooks/pre-commit`, extracts the single-quoted literal between the markers, and asserts `== egrep_alternation()`. Adding a pattern to `SECRET_PATTERNS` turns that test red until the hook block is regenerated (`python -m brain.secret_patterns --emit-egrep` prints the exact line). No behavior change to the hook's stages 2/3, no runtime dependency, no drift. The email heuristic and denylist loop stay bash-only — the ingest guard deliberately does **not** scan for emails (the corpus is *made of* real emails; flagging them would produce ~1376 findings and zero signal).

#### 5.2 The scanner (`src/brain/ingest/guard.py`)

```python
"""Ingest-time secret detection and redaction."""

@dataclass(frozen=True)
class SecretFinding:
    kind: str        # SecretPattern.kind
    label: str
    line: int        # 1-indexed
    col_start: int   # 1-indexed, inclusive
    col_end: int     # 1-indexed, inclusive
    preview: str     # SAFE — never the secret

@dataclass(frozen=True)
class GuardOutcome:
    content: str                    # possibly redacted; identical under warn/off
    findings: tuple[SecretFinding, ...]
    redacted: bool

def scan_secrets(text: str) -> list[SecretFinding]: ...
def redact_secrets(text: str) -> tuple[str, list[SecretFinding]]: ...
def apply_guard(content: str, *, mode: str, allow: bool, title: str) -> GuardOutcome: ...
def format_findings(findings: Sequence[SecretFinding], *, title: str, mode: str) -> str: ...
```

`scan_secrets` splits on `str.splitlines()` and runs each compiled pattern per line (line-scoped so `line`/`col` are trivially correct and a pathological single-line 10 MB blob still bounds each regex to that line). Returns findings sorted by `(line, col_start, kind)` — deterministic ordering is asserted in tests. **Immutability:** `redact_secrets` never mutates its input; it builds a new string.

**Safe preview rule** (the one thing that must not leak): `preview = matched[:p.preview_head] + "*" * min(len(matched) - p.preview_head, 20)`. `preview_head` is 4 for keyed formats (`AKIA`, `sk-`, `ghp_`) and 0 for `private_key_header` (whose match is the literal PEM banner, itself non-secret, so it is echoed whole with the middle elided). The preview is **never** derived from the trailing entropy of the match, and its length is capped at 24 chars so a `*`-count cannot be used to reconstruct the key length precisely.

Redaction replaces the matched span with `[REDACTED:<kind>]`.

#### 5.3 Hook points and the ordering argument

**`ingest_document`** — the guard runs immediately before `src/brain/ingest/__init__.py:555` (`h = _content_hash(doc.content)`), i.e. after the `source_kind` defaulting block (`:530-538`) and after the gmail-thread `source_external_id` override (`:549-553`), so `--sensitivity`/frontmatter opt-outs are already resolved:

```python
    guard = apply_guard(
        doc.content, mode=secret_guard, allow=allow_secrets, title=doc.title
    )
    if guard.redacted:
        doc = replace(doc, content=guard.content)   # dataclasses.replace — new object
    h = _content_hash(doc.content)
```

**`update_document`** — the guard runs inside the `new_content is not None` branch at `src/brain/ingest/__init__.py:1651`, after the `stripped` emptiness check (`:1654-1656`) and **before** `new_hash = _content_hash(new_content)` at `:1658`.

**Why the ordering is load-bearing for `content_hash` idempotency.** `content_hash` is the dedup key on the stdin path (`ingest_document` docstring rule 4, `src/brain/ingest/__init__.py:466-468`) and the collision key in `update_document` (`:1659-1666`). If redaction ran *after* hashing, the stored hash would describe the un-redacted text while the stored body was redacted — re-ingesting the same source would compute the hash of the *original* text, match the stored row, and short-circuit to `skip` (`:909-912`), permanently freezing the redacted body while claiming it is up to date. Worse, `_resolve_ingest_action`'s `body_changed` flag (`:644`) would be wrong, so the vault mirror (`:582-586`) and the graph sync (`:616-621`) would never fire. Hashing the **post-guard** bytes makes redaction a normal content transform: the hash describes exactly what is stored, and re-ingesting the same raw source produces the same redacted body → the same hash → a correct no-op. It also means chunking (`:916`) and embedding (`:922`) — both downstream of `h` — only ever see redacted text, so a secret never reaches Voyage even under `redact` mode.

#### 5.4 Escape hatches

1. **Per-invocation:** `--allow-secrets` sets `allow=True` on `apply_guard`, which returns the input unchanged with `findings` populated. Findings are **still printed** (an escape hatch that hides evidence is a worse hatch). Threaded as a plain keyword into `ingest_document`/`update_document` — `allow_secrets: bool = False`.
2. **Per-note frontmatter:** `allow_secrets: true` in a vault note's YAML. Read in `apply_guard`'s caller via a tiny helper `_frontmatter_allows_secrets(doc)` that checks `doc.metadata.get("allow_secrets") is True` — sync already deposits non-reserved frontmatter keys into `documents.metadata` for vault-tier rows (`src/brain/vault/sync.py:1007-1010`), so this needs **no** registry change and no new parsing. The effective allow is `allow_secrets_flag or _frontmatter_allows_secrets(doc)`. Documented as: use this for a note that is *about* credentials (a rotation runbook quoting a key format), not to silence the guard globally.

#### 5.5 Corpus sweep

`brain backfill scan-secrets` lands under the existing `backfill_app` (`src/brain/cli.py:253-258`, help text "One-shot data-hygiene utilities for legacy rows") — it is exactly that, and the sub-app already establishes the `--dry-run`-style read-only idiom (`:8457`, `:8601`). It is read-only by default; `--apply` is required to write, and `--action` selects what "write" means.

The iterator is keyset-paged so 1376 documents never load at once:

```sql
SELECT d.id::text, d.title, d.content, s.kind, d.sensitivity
FROM documents d
LEFT JOIN sources s ON s.id = d.source_id
WHERE d.id > %s
ORDER BY d.id
LIMIT %s
```

`--action mark-confidential --apply` issues, per hit:

```sql
UPDATE documents SET sensitivity = 'confidential' WHERE id = %s AND sensitivity <> 'confidential'
```

`--action redact --apply` routes through `update_document(conn, document_id=..., new_content=redacted, embedder=..., vault_root=cfg.vault_path)` rather than raw SQL — that is the only path that re-chunks, re-embeds, re-hashes, and regenerates the mirror consistently (`src/brain/ingest/__init__.py:1586-1618`). It requires an embedder and is documented as the slow option.

#### 5.6 Migration `025_document_sensitivity.sql`

**Level names: exactly two — `normal` and `confidential`.** Justification: three levels (`public`/`internal`/`secret`) implies a lattice, and a lattice needs comparison operators, per-boundary thresholds, and a policy language — none of which a single-user local knowledge base has any use for. There is exactly one question each boundary asks ("may this body leave?"), so exactly one bit is needed. The column is `TEXT` with a `CHECK` rather than a boolean so a future third level is a `CHECK` swap in a new migration instead of a type change; `normal` (not `false`) is the default so the values read correctly in frontmatter and JSON.

```sql
-- Migration 025 — per-document sensitivity tier (trust boundary).
--
-- Additive only. Every existing row becomes 'normal' via the column DEFAULT,
-- which is exactly the pre-migration behavior (no boundary refuses anything).
--
-- Two levels by design (spec §5.6): 'normal' | 'confidential'. The CHECK is
-- named so a future third level is a named-constraint swap in a LATER
-- migration -- never an edit to this file.
--
-- The partial index mirrors idx_documents_draft from migration 007: the
-- confidential subset is expected to stay small, and every consumer
-- (`brain list --sensitivity confidential`, the embed veto, the export
-- frontmatter writer) filters on equality to 'confidential'.

BEGIN;

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'normal';

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_sensitivity_check;

ALTER TABLE documents
    ADD CONSTRAINT documents_sensitivity_check
    CHECK (sensitivity IN ('normal', 'confidential'));

CREATE INDEX IF NOT EXISTS idx_documents_sensitivity
    ON documents (sensitivity) WHERE sensitivity <> 'normal';

COMMIT;
```

(The `DROP CONSTRAINT IF EXISTS` before `ADD CONSTRAINT` is what makes the file re-runnable — `ADD CONSTRAINT` has no `IF NOT EXISTS` form in PostgreSQL 16.)

#### 5.7 Enforcement at each boundary

**(1) Hosted embedder — recommendation: NULL embedding, FTS-only, ingest succeeds with a loud notice.**

Rejecting the ingest loses the note entirely (the user must choose between the brain and their privacy — a false choice). Falling back to a local embedder is the option that *sounds* best and is actually the worst: it would write an Arctic 1024-dim vector into a column whose other 1375 rows hold Voyage vectors, and cosine distance between two different models' embedding spaces is meaningless — every confidential doc would land at a garbage rank forever, silently. Storing NULL is the only option that is correct at every layer, and the machinery already exists (`_embed_chunks`'s `None` placeholders at `src/brain/ingest/__init__.py:365-366`, NULL-bound by `_insert_chunks` at `:397-418`, and the column is nullable pre-finalize per `queries.finalize_embedding_index`).

Implementation — `_embed_chunks` gains one keyword and one branch:

```python
def _embed_chunks(
    embedder: Embedder, texts: list[str], *, hosted_egress_blocked: bool = False
) -> list[list[float] | None]:
    if hosted_egress_blocked or not getattr(embedder, "produces_embeddings", True):
        return [None] * len(texts)
    ...
```

The caller at `src/brain/ingest/__init__.py:922` computes `hosted_egress_blocked = sensitivity == "confidential" and is_hosted_embedder(embedder)`, where `is_hosted_embedder` is a new one-liner in `src/brain/embeddings.py` returning `isinstance(embedder, VoyageEmbedder)` — a concrete-class check is correct here because "hosted" is a property of the specific backend, not of the Protocol, and adding a hosted backend means adding it to this function (one place, Open/Closed).

The user is told, on stderr, once per ingest:

```
⚠  confidential document + BRAIN_EMBEDDER=voyage — body NOT sent to Voyage AI.
   Chunks stored with NULL embeddings; this doc is findable by full-text
   search only. Switch to BRAIN_EMBEDDER=arctic (local) and run
   `brain reembed` to give it vectors.
```

`brain reembed` must honor the same veto or it would immediately undo the guard: its chunk iterator gains `AND d.sensitivity = 'normal'` when the active embedder is hosted, and it prints a trailing `N chunk(s) skipped (confidential, hosted embedder)`. Critically, `finalize_embedding_index` applies `NOT NULL` on `chunks.embedding` — so with a hosted embedder **and** any confidential document, finalize must be skipped with an explicit message, exactly as it already is for the FTS-only backend (`src/brain/cli.py:2430-2436`).

**(2) MCP `brain_show` — default withhold, explicit opt-in.**

`brain_show` (`src/brain/mcp_server.py:544`) gains `include_confidential: bool = False`. When the resolved doc has `sensitivity == "confidential"` and the flag is false, `payload["content"] = None`, `payload["withheld"] = "body withheld: sensitivity=confidential. Re-call with include_confidential=true to retrieve it."`, and `payload["sensitivity"] = "confidential"`. Title, tags, id, source, and `summary` still return — the model can still *reason about* the doc's existence and ask the user, which is the desired behavior; it just cannot exfiltrate the body on its own initiative. Default-false is the whole point: a tool that defaults open is not a boundary. Interaction logging (`:616-631`) is unchanged and still fires — a withheld open is still an open.

This requires `fetch_document` to return the column, so `DocumentRow` (`src/brain/queries.py:55`) gains `sensitivity: str = "normal"` (defaulted, so `list_documents`'s lighter projection at `:551-560` compiles unchanged).

**(3) Published wiki — same seam as `draft`.**

The frontmatter writer (`src/brain/vault/export.py:453`) gains, right beside the `draft` line:

```python
    if doc.sensitivity != "normal":
        fields["sensitivity"] = doc.sensitivity
```

and the Quartz emitter's existing drop branch (`contentIndex.ts:397`) becomes:

```ts
          if (fm.draft === true || fm.sensitivity === "confidential") {
            delete parsed[slug]
            continue
          }
```

Following the identical seam means the doc vanishes from the Explorer tree, the graph view, and the site's full-text search in one place, with the same minimum-blast-radius reasoning already documented in the comment at `contentIndex.ts:390-396`. Note honestly: **the rendered HTML page for the slug is still emitted** — this hides the doc from every index, exactly as `draft` does, and does not delete the page. If the wiki is served publicly, a direct-URL guess still reaches it. Callout in §7.

**(4) Vault frontmatter round-trip.**

Author writes `sensitivity: confidential` in a vault note's YAML. Round-trip:
- **Export (DB → file):** written by `_build_frontmatter` (above), and `"sensitivity"` is added to `_EXPORT_OWNED_FRONTMATTER_KEYS` (`src/brain/vault/export.py:31-59`) so the value is canonical from the DB and the freeform merge at `:444` cannot shadow it.
- **Sync (file → DB):** `"sensitivity"` is added to sync's `reserved` set (`src/brain/vault/sync.py:994-1006`) so it does **not** get dumped into `documents.metadata`, and sync's vault-tier upsert writes it to the column with a validation coercion: any value other than the two literals is coerced to `"normal"` and logged at WARNING (never raised — a typo in one note must not abort a whole `vault sync --watch` pass).

Both registries change together. This is the exact failure mode documented at `src/brain/vault/export.py:44-57` — a key in one set but not the other silently destroys user content — and `tests/test_vault_frontmatter_registry_parity.py` will assert the invariant directly.

**(5) CLI set/clear/list.**

`brain mark-confidential <id>` / `brain mark-normal <id>` are a direct structural copy of `mark-draft`/`mark-published` (`src/brain/cli.py:6448-6473`) delegating to a shared `_set_sensitivity(id_prefix, *, level: str)` that mirrors `_set_draft` (`:6476-6515`): resolve the prefix, idempotent no-op with `f"{label} is already {level}"`, otherwise `UPDATE ... SET sensitivity=%s WHERE id=%s` followed by `regenerate_vault_file(conn, doc_id, vault_path=cfg.vault_path, force=True)` so the mirror's frontmatter picks up the change (this is deliberately *not* routed through `update_document`, which has no `new_sensitivity` parameter and would need one for no benefit — see Open Questions Q5). `brain list --sensitivity confidential` adds a `sensitivity` kwarg to `list_documents` (`src/brain/queries.py:520`) appending `d.sensitivity = %s` to the existing parameterized `where` list at `:533-540`.

#### 5.8 Error handling

- `SecretGuardError(BrainError)` — raised only by `reject` mode. Caught at each CLI ingest site and rendered via `typer.secho(..., fg="red", err=True)` + `raise typer.Exit(code=1)`, matching the `stdin was empty` shape at `src/brain/cli.py:2169-2170`.
- `SensitivityError(BrainError)` — raised by `_set_sensitivity` and the sync coercion path on an invalid level, so the CLI maps it without a framework-specific type escaping the library layer.
- Config: an invalid `BRAIN_SECRET_GUARD` raises `ConfigError` at load, mirroring `src/brain/config.py:1601-1610` — a typo surfaces at startup, not mid-ingest.
- No bare `except`. The guard itself catches nothing (pure `re`, no I/O). The sync coercion catches nothing either — it is a value check, not an exception path.

### 6. Edge cases and failure modes

1. **Regex false positive on legitimate prose.** A note quoting Stripe's docs contains `sk_live_ABCDEFGHIJKLMNOPQRSTUV`. Under the `warn` default: printed, stored unchanged, exit 0 — no data loss. This is precisely why `warn` is the default and `redact` is not.
2. **`redact` mode changes `content_hash`, so the same file re-ingests as "updated" forever?** No. The hash is computed on post-guard bytes (§5.3), and redaction is deterministic (same input → same output), so the second ingest produces an identical hash and the file-path branch resolves to `skip`. A regression test asserts exactly this.
3. **`--allow-secrets` on `ingest-dir` over 900 files.** The flag is per-invocation and applies to every file in the walk. Intended, and the help text says so; `warn` findings still print per file so the user retains the audit trail.
4. **Confidential doc ingested while `BRAIN_EMBEDDER=voyage`, then the user switches to `arctic`.** The chunks hold NULL. `brain reembed` picks them up normally (its whole purpose) because the veto only applies when the *active* embedder is hosted. No manual repair needed.
5. **`finalize_embedding_index` would apply `NOT NULL` while confidential NULLs exist.** Finalize is skipped with an explicit message when the active embedder is hosted and `SELECT EXISTS(SELECT 1 FROM documents WHERE sensitivity <> 'normal')`. Without this the guard would either crash `brain reembed` or force the user to choose between the index and the boundary.
6. **A user hand-edits a vault note to `sensitivity: seCRET`.** Sync coerces to `"normal"`, logs WARNING with the note path, and continues. It does **not** raise — a single bad note must not abort a `vault sync --watch` pass over 1376 files. The next export rewrites the frontmatter to the coerced value, so the drift self-heals visibly.
7. **`brain backfill scan-secrets --apply --action redact` hits a document whose redacted body collides with another document's `content_hash`.** `update_document` raises `ValueError` (`src/brain/ingest/__init__.py:1663-1666`). The sweep catches `ValueError` per document, records it in an `errors` list, and continues — one poisoned doc must not abort a 1376-document sweep. Final line reports `N error(s)`.
8. **A 40 MB PDF extraction with a single 12 MB "line".** `scan_secrets` is line-scoped; each pattern is linear with no backtracking-prone construct (verified: all 12 are anchored-prefix + bounded/greedy character classes, no nested quantifiers), so this is O(n) per pattern, ~12n total. No catastrophic backtracking is reachable. A `--limit` on the sweep bounds wall-clock for the corpus pass.
9. **Empty / whitespace-only content.** `scan_secrets("")` returns `[]`; `apply_guard` returns the input unchanged. The existing empty-stdin check (`src/brain/cli.py:2168-2170`) and the empty-chunks short-circuit (`src/brain/ingest/__init__.py:917-921`) are untouched.
10. **MCP client calls `brain_show(include_confidential=True)` without the user asking.** Nothing technically stops it — the flag is a *speed bump and an audit signal*, not authentication. It makes the exfiltration explicit in the tool-call log rather than implicit. Stated plainly in §7 and in the tool docstring.

### 7. Security and safety

| Risk | Guard |
|---|---|
| A finding preview leaks the secret into logs/stdout/CI | Previews are constructed by the fixed rule in §5.2 (bounded head + capped asterisks), never from the match tail. `tests/test_ingest_guard.py::test_preview_never_contains_full_secret` asserts, for every pattern, that the full matched string does not appear as a substring of `preview` or of `format_findings(...)`. |
| Test fixtures containing real-looking secrets get committed | All fixtures use synthetic values with the *correct shape but invalid checksums/prefixes* (e.g. `AKIAAAAAAAAAAAAAAAAA`, `sk-` + 24 `x`). CLAUDE.md rule 15 applies; the repo's own pre-commit hook is the backstop — and note the guard's fixtures **will** trip it, so they go in `.pii-allowlist.txt` (which is exactly what that committed list is for, per `scripts/hooks/README.md:24`). |
| The sweep silently mutates 1376 documents | `--apply` is required to write anything; the default `--action report` cannot write even with `--apply`. `--action redact` additionally routes through `update_document`, which rebuilds chunks + mirror transactionally. |
| `redact` mode destroys a legitimate note | It is never the default; it is opt-in twice over (env var, or an explicit `--action redact --apply`); and every finding is printed before any write. |
| SQL injection via `--sensitivity` / `--action` | All values are validated against a closed literal set before any query, and every query is parameterized (`%s` + tuple) — including the sweep's keyset pagination and the `set_document_sensitivity` UPDATE. No user string is ever concatenated into SQL. |
| The corpus sweep runs against the production DB | It is read-only by default. Tests run only against the port-5434 `second_brain_test` fixture. No test performs `DROP`/`TRUNCATE`/unbounded `DELETE` against any database. |
| Confidential bodies still reach `brain ask` / `brain audio` / graph extraction (all local Ollama) | **Out of scope and intentional.** Those are local-process paths inside the trust boundary. Documented as a known limit; extending the tier to them is Open Question Q3. |

**What this does NOT protect against — stated plainly.** This is a guard rail, not a security control:
- **No encryption.** `documents.content` is plaintext in PostgreSQL, and `./data/postgres` is an unencrypted host bind-mount. Anyone with filesystem or `psql` access reads confidential bodies trivially.
- **The vault mirror is still on disk in plaintext.** Marking a doc confidential does not delete or encrypt `<vault>/_ingested/.../*.md`.
- **The wiki page is still rendered.** Confidential docs vanish from every *index* (Explorer, graph, search) via the same seam `draft` uses; the HTML file for the slug is still emitted and reachable by direct URL.
- **`brain search` / `brain show` / `brain list` still return confidential bodies in full.** The local CLI is inside the boundary by design.
- **A cooperating LLM can bypass `brain_show`'s default** by passing `include_confidential=true`. The flag makes egress explicit and auditable; it does not authenticate.
- **The secret guard is regex-based and will miss things** — a base64'd credential, a password in prose ("the prod password is hunter2"), an internal token format not in the 12 patterns. It catches the well-known shapes and nothing else.
- **Already-ingested secrets are not retroactively protected.** `brain backfill scan-secrets` finds them; it does not un-send anything already POSTed to Voyage AI, and it does not rewrite git history or prior wiki builds.

### 8. Test plan

**Red-first failing tests (written and confirmed failing before any implementation):**

- `tests/test_ingest_guard.py::test_ingested_document_body_retains_pasted_api_key` — ingest a synthetic doc whose body contains `AKIAAAAAAAAAAAAAAAAA` with `BRAIN_SECRET_GUARD=redact`, then `SELECT content FROM documents WHERE id=%s` and assert the key is absent. **Fails today** because no guard exists — the key round-trips verbatim. This is the bug.
- `tests/test_sensitivity_egress.py::test_confidential_doc_is_not_sent_to_hosted_embedder` — a `FakeVoyageEmbedder` recording-double (a fake conforming to the `Embedder` Protocol plus `is_hosted_embedder` recognition, **not** a monkeypatch of `VoyageEmbedder`) ingests a doc with `sensitivity="confidential"`; assert `fake.embed_calls == []` and `SELECT embedding FROM chunks WHERE document_id=%s` is all NULL. **Fails today** — `documents.sensitivity` does not exist and `_embed_chunks` embeds unconditionally.

**Full suite:**

| File | Asserts |
|---|---|
| `tests/test_secret_patterns.py` (new, pure logic → 95%) | Every `SecretPattern` compiles; each matches its canonical synthetic positive and rejects a near-miss negative; `egrep_alternation()` is deterministic and contains each `regex` exactly once. |
| `tests/test_secret_pattern_hook_parity.py` (new) | The marker-delimited literal in `scripts/hooks/pre-commit` equals `egrep_alternation()` byte-for-byte. Turns red the moment a pattern is added without regenerating the hook. |
| `tests/test_ingest_guard.py` (new, pure logic → 95%) | `scan_secrets` returns correct 1-indexed line/col for a multi-line body; finding order is deterministic; **the full secret never appears in `preview` or in `format_findings` output, for every pattern**; `redact_secrets` does not mutate its input and is idempotent (`redact(redact(x)) == redact(x)`); empty/whitespace input returns `[]`; `apply_guard` under `off` and under `allow=True` returns the input unchanged but still reports findings; `reject` raises `SecretGuardError`. |
| `tests/test_ingest_guard_pipeline.py` (new, real `test_db`) | The red-first redaction test above; **`content_hash` idempotency** — ingest the same raw file twice under `redact` and assert the second call returns `created=False, body_changed=False` (the ordering regression test for §5.3); chunks contain only redacted text; `warn` mode stores the body unchanged and exits 0; `--allow-secrets` bypasses `reject`; `allow_secrets: true` in `documents.metadata` bypasses `reject`; `update_document` with a secret-bearing `new_content` is guarded before hashing. |
| `tests/test_cli_ingest_guard.py` (new) | Typer `CliRunner`: `reject` exits 1 with the literal `✗ secret guard:` prefix on stderr; **stdout on a clean ingest is byte-identical to the pre-change format** (the backward-compat assertion); `--allow-secrets` exits 0. |
| `tests/test_migration_025_sensitivity.py` (new, real `test_db`) | Column exists with default `'normal'`; every pre-existing row reads `'normal'`; the CHECK rejects `'secret'` with `psycopg.errors.CheckViolation`; `idx_documents_sensitivity` exists; the migration is re-runnable (apply twice, no error). |
| `tests/test_sensitivity_egress.py` (new, real `test_db`) | The red-first hosted-embedder test; a **normal** doc under the same fake hosted embedder **is** embedded (proves the veto is scoped); under a local fake embedder a confidential doc **is** embedded (the veto is about hosted egress, not sensitivity per se); `brain reembed` skips confidential chunks under a hosted embedder and reports the skip count; finalize is skipped when confidential + hosted. |
| `tests/test_mcp_sensitivity.py` (new, real `test_db`) | `brain_show` on a confidential doc returns `content is None` + the `withheld` string; `include_confidential=True` returns the body; a **normal** doc's payload dict is byte-identical to the pre-change payload (no `sensitivity` key) — the additive-key contract; interaction logging still fires on a withheld open. |
| `tests/test_vault_sensitivity_roundtrip.py` (new, real `test_db`) | Export writes `sensitivity: confidential` only when non-normal; a normal doc's mirror file is byte-identical to the pre-change output; file → sync → DB round-trips the value; an invalid value coerces to `normal` with a WARNING and does not raise; `"sensitivity"` is in **both** `_EXPORT_OWNED_FRONTMATTER_KEYS` and sync's `reserved`. |
| `tests/test_vault_frontmatter_registry_parity.py` (new) | Asserts the standing invariant that every export-owned key is also sync-reserved — the generalized guard against the `summary` regression class documented at `src/brain/vault/export.py:44-57`. |
| `tests/test_cli_sensitivity.py` (new, real `test_db`) | `mark-confidential` / `mark-normal` set the column, regenerate the mirror, and are idempotent with the exact `is already <level>` string; unknown prefix exits 1; `brain list --sensitivity confidential` filters; `brain list --json` carries the additive key. |
| `tests/test_backfill_scan_secrets.py` (new, real `test_db`) | Default run writes **nothing** (assert the column and bodies are unchanged after the command); `--apply --action mark-confidential` flips only the hit rows; `--apply --action redact` on a hash-colliding doc records an error and continues over the remaining docs; `--json` shape matches §3.5; `--limit` bounds the scan. |
| `tests/test_config.py` (extend) | `BRAIN_SECRET_GUARD` defaults to `warn`; each valid mode parses; an invalid value raises `ConfigError` at load. |
| Quartz overlay e2e (extend the existing contentIndex overlay test) | A mirror with `sensitivity: confidential` is absent from `contentIndex.json`; a `normal` one is present; a `draft: true` one is still absent (no regression to the existing branch). |

Coverage targets: `secret_patterns.py` and `ingest/guard.py` ≥ 95% (pure logic); `cli_sensitivity.py` ≥ 85%; the `ingest/__init__.py` and `mcp_server.py` deltas fully covered by the pipeline/MCP suites. All fixtures synthetic (`AKIAAAAAAAAAAAAAAAAA`, `alice@example.com`, invented titles), added to `.pii-allowlist.txt` so the repo's own pre-commit gate stays green.

### 9. Open questions — with recommended answers

**Q1. Should the guard also run the email heuristic from `scripts/hooks/pre-commit:54-59`?**
**No.** The corpus is largely Gmail and Krisp — real email addresses are the *content*, not a leak. Enabling it would produce findings on essentially every ingested document and train the user to ignore the guard entirely. The email pattern stays bash-only (it exists to police *source code*, a different threat model). Documented as a deliberate exclusion in `guard.py`'s module docstring.

**Q2. Should the redaction path also rewrite the on-disk vault mirror for already-mirrored docs?**
**Yes, and it already does for free** — `--action redact` routes through `update_document`, whose `vault_root` parameter regenerates the mirror (`src/brain/ingest/__init__.py:1607-1618`). The sweep passes `vault_root=cfg.vault_path`. No extra work; call it out in the plan so nobody "optimizes" it away by writing raw SQL.

**Q3. Should sensitivity gate the local LLM paths (`brain ask`, `brain audio`, `brain enrich`, graph concept extraction)?**
**Not in this release.** Those are local Ollama — no egress off the machine — so gating them buys no confidentiality and would degrade the brain's core value on exactly the documents the user cares most about. Revisit only if a hosted chat backend is ever added; at that point the gate belongs in `brain.chat.chat_json` (one chokepoint, `src/brain/chat.py`), not scattered across six callers. Record this as a deliberate scope line in the spec so a later reviewer does not read it as an oversight.

**Q4. Should `ingest-gmail` accept `--sensitivity`?**
**No.** It ingests a *batch* matched by a query; a single tier applied to an unknown-size batch is a footgun in both directions (over-marking cripples search; under-marking gives false assurance). Users mark specific docs afterward with `brain mark-confidential`, or use `brain backfill scan-secrets --apply --action mark-confidential` to mark by evidence. `--allow-secrets` **is** offered on `ingest-gmail`, since a bulk pull is exactly where a `reject`-mode false positive would abort a long-running job.

**Q5. Should sensitivity be a parameter on `update_document` instead of a direct UPDATE in `_set_sensitivity`?**
**Direct UPDATE + `regenerate_vault_file`.** `update_document` is already a 300-line function with 14 keyword parameters; adding a 15th for a column that never triggers re-chunking or re-embedding adds branch surface for no behavioral gain. `_set_draft` sets the precedent in the other direction (`src/brain/cli.py:6505-6511` uses `update_document`) — but `draft` genuinely participates in `update_document`'s `fields_changed`/mirror-trigger logic, and sensitivity does not. If a reviewer disagrees, the cost of switching is ~15 lines; note it as a low-stakes reversible call.

**Q6. `src/brain/ingest/__init__.py` is already 1926 lines — does this section have to split it?**
**No — and it must not try.** The 800-line rule is real, but the file is 2.4× over it *today*, and a split is a large, risky, cross-cutting refactor already tracked as deferred work (the same deferral recorded for `cli.py` / `mcp_server.py` in the GraphRAG memory notes). Bundling it into a safety change would make the diff unreviewable and put the trust boundary at risk of being reverted along with the refactor. This section adds ~70 lines to that file and puts all *new* logic in new small modules (`secret_patterns.py` ~120, `ingest/guard.py` ~180, `cli_sensitivity.py` ~210, all well under target). The `ingest/__init__.py` split stays on the deferred list; flag it explicitly in the release notes so it is not mistaken for the rule being ignored.

**Q7. Should `hybrid_search` grow a `--sensitivity` filter for symmetry?**
**No.** The local CLI is inside the trust boundary (identical posture to `draft`, `src/brain/cli.py:6452-6456`), and adding a filter to `hybrid_search` risks moving eval metrics and forcing a `tests/eval/baselines/ci.json` re-record — a real cost for zero security gain. `brain list --sensitivity confidential` covers the actual need ("show me what I've marked").
