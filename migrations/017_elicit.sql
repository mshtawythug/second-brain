-- 017_elicit.sql — tacit-knowledge elicitation gap-state table.
-- Mutable lifecycle (surfaced → snoozed → dismissed → resolved); NOT in interactions
-- (that table is append-only). See docs/specs/2026-06-01-tacit-knowledge-elicitation-design.md.

CREATE TABLE IF NOT EXISTS elicitation_gaps (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    signal_kind       TEXT NOT NULL
                      CHECK (signal_kind IN ('delta','orphan','contradiction','user_flagged')),
    target_type       TEXT NOT NULL
                      CHECK (target_type IN ('person','org','project','topic','tool','doc')),
    target_id         TEXT NOT NULL,
    score             DOUBLE PRECISION NOT NULL CHECK (score >= 0),
    evidence_ids      TEXT[] NOT NULL DEFAULT '{}',
    rationale         TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'surfaced'
                      CHECK (status IN ('surfaced','snoozed','dismissed','resolved')),
    snoozed_until     TIMESTAMPTZ,
    resolved_note_id  UUID REFERENCES documents(id) ON DELETE SET NULL,
    first_surfaced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS elicitation_gaps_tenant_signal_target_idx
    ON elicitation_gaps (tenant_id, signal_kind, target_id)
    WHERE status <> 'resolved';

CREATE INDEX IF NOT EXISTS elicitation_gaps_queue_idx
    ON elicitation_gaps (tenant_id, status, score DESC)
    WHERE status IN ('surfaced','snoozed');
