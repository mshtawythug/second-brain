# Migrations

Numbered SQL files applied in order by `brain init`.

Naming: `NNN_description.sql` (zero-padded sequence).

Each file is applied verbatim. Migrations are not tracked in a metadata table in v1 — `brain init` is idempotent (uses `CREATE ... IF NOT EXISTS` style or runs against a fresh schema). For schema changes, drop and recreate the DB during development.
