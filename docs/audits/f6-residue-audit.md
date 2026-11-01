# The F6 draft/sensitivity residue audit

Moved verbatim from `CLAUDE.md` on 2026-10-29 to keep the instruction files Claude Code
loads under its size limit (record: `docs/plans/2026-10-29-slim-claude-md.md`). For
this text's history before that date, run `git log -p -- CLAUDE.md`.

### The F6 draft/sensitivity residue audit — a review checkpoint, re-derived

Same shape as the ceiling table, same failure mode, so the same discipline: a
standing audit that **carries no row set**, because a row set here rots exactly
like a Lines cell. Every `documents` read that filters `draft` is a read of the
browseable corpus; almost every such read is also a place a *confidential* row
could travel. **Re-derive the set — never inherit it, including from this
paragraph or from a reviewer's brief:**

```
grep -rn "draft = FALSE\|draft=FALSE\|draft IS FALSE\|NOT d.draft" src/brain/
```

For each hit, the question is per-STATEMENT, not per-file: does *this* statement
either carry a sensitivity predicate, or carry a written reason it does not? A
file-level answer is not an answer — `connect.py` and `related.py` each contain
both gated and deliberately-ungated statements.

Three outcomes are legitimate and only the third is a defect:

1. **Gated** — a `sensitivity` sibling in the same statement.
2. **Ungated with a recorded reason**, because the gate belongs at a different
   layer or would be wrong here. Two live examples, both verified 2026-08-21:
   `connect.py`'s three scoring queries (the gate is at the read boundary,
   `iter_suggestions`, which gates BOTH endpoints — reasoned at its call site);
   and `ui/queries.py`'s `_DISCOVERABLE_ANY_SENSITIVITY`, the deliberate
   permissive half of a frozen pair.
3. **Ungated with no reason recorded** — the defect. It reads identically to (2)
   from the outside, which is the whole problem: the reviewer cannot tell a
   considered omission from an oversight without re-deriving the data flow.

**Known residue at `7f4d859`, judged not a hole:** `related.py`'s
`_corpus_common_lexemes` (two statements) scans every non-draft title including
confidential ones. It is safe *and* gating it would be wrong — but on **two**
load-bearing legs, not the three this paragraph asserted until 2026-08-21:

1. ~~"A lexeme qualifies only by appearing in more than `_CORPUS_FREQ_THRESHOLD`
   of the corpus, so no member is distinctive to any document."~~ **False on a
   small corpus, and it was stated with no corpus-size qualifier.**
   `_CORPUS_FREQ_THRESHOLD = 0.025` and the filter is `ndoc > total * 0.025`
   over non-draft titled docs, so at **`total <= 39`** the threshold falls below
   1 and a lexeme appearing in exactly **one** document — possibly a
   confidential one — qualifies as "common". A demo sandbox or a fresh corpus is
   squarely in that range. Re-derive with
   `grep -n '_CORPUS_FREQ_THRESHOLD\|threshold_ndoc' src/brain/related.py`.
2. **The set is consumed purely subtractively.** `_build_self_tsquery` only ever
   `continue`s past a member, so membership can *remove* a token from a query
   and can never add one.
3. **Nothing from it reaches an emitted artifact.** It filters query terms; it
   is never a value that gets rendered.

**The conclusion is unchanged** — legs 2 and 3 hold at every corpus size and are
each independently sufficient, which is why leg 1 being false does not open a
hole. It is corrected rather than deleted because it was presented as
load-bearing, and a reader who checks one leg should not find it is the one that
does not hold. Excluding confidential titles from
the *denominator* would change the ranking of the non-confidential corpus for
users who have no confidential document at all. Recorded here rather than in
`related.py` on purpose: when this was written that module was over the ceiling
and its own docstring said extract before the next addition. The 2026-10-15
extraction brought it back under; the note stays here, where readers of this
audit already look.


**RM-46 (sec-118 #2), 2026-10-31: three statements gated, one exemption narrowed.**

- `mcp_server._resolve_proposal_dst` (both statements: the id-prefix `LIKE` and the
  title match) now carry `not_confidential_sql('d')` unless the caller passes
  `brain_link_proposal(include_confidential=true)`. Before, a 6-hex prefix or a guessed
  title named a confidential document's id and title back: an enumeration oracle, on
  stdio as well as HTTP. Neither statement filters `draft`, so the grep above does not
  list them; they are recorded here because they are the same egress class.
- `brain_graphrag_entities` keeps its stdio exemption (entity names are a separate tier;
  see its docstring), but **over HTTP without `--include-confidential` it is gated**:
  `list_entities(public_only=True)` keeps only entities with at least one
  non-confidential mention (`graph_rag/public_entities.public_mention_sql`). The same
  rule filters every entity list a graph retrieval returns
  (`withhold_confidential_entities`), and every by-prefix lookup answers a confidential
  document as an unknown prefix (`McpState.hide_confidential`). `brain_graphrag_stats`'
  top-10 entity names go through the same filter (`graph_stats(public_only=…)`, rev-118
  #10); its counts stay corpus-wide, a documented residual.
- **Global graph retrieval, stdio and HTTP (rev-118 round 2):** `graph_rag/global_.py`
  returned stored community SUMMARIES, which are generated from member names and
  representative titles, for communities that `list_communities` withholds whole. Only
  their `doc_ids` were gated. `_retrieve_global` now drops, before the ranking cut, every
  community `communities_with_confidential_members` names. That is `list_communities`'
  own predicate (`confidential_member_sql`), now shared. Over HTTP, theme summaries drawn
  from a withheld entity name are dropped too (`public_entities._public_themes`).
- **People and timeline names over HTTP (sec-118b):** `queries.resolve_person_to_keys`
  gains `public_only`. In HTTP hide mode, a person no non-confidential document names is
  unknown (search, recall, timeline and themes), and `person_keys` is dropped from results.
  `timeline_entities.resolve_entities` (moved out of `timeline.py`) gains the same
  public-mention filter on its substring match. The resolver's own docstring explains why
  stdio stays permissive: identities come from the directory, which the local owner
  already holds.
