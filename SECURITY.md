# Security Policy

## Supported versions

Second Brain is pre-1.0. Only the latest minor line receives security fixes.

| Version | Supported                                    |
| ------- | -------------------------------------------- |
| 0.2.x   | ✅ Fixes land on the latest `0.2.x` release. |
| 0.1.x   | ❌ Please upgrade.                           |

`pipx upgrade secondbrain-py` (or `pip install --upgrade secondbrain-py`) moves
you to the supported line.

## Reporting a vulnerability

**Please do not open a public issue for a security report.** A public issue is
visible to everyone the moment it is filed, including before a fix exists.

Report privately through GitHub:

1. **Preferred — private vulnerability reporting.** Go to the repository's
   **Security** tab and choose *Report a vulnerability*. This opens a private
   advisory visible only to you and the maintainer, and carries a built-in fix
   and coordinated-disclosure workflow.

   <!-- TODO (maintainer): private vulnerability reporting is NOT yet enabled on
        this repository, so the direct advisory link below 404s for anyone who
        is not an admin. Enable it under Settings -> Code security -> Private
        vulnerability reporting, verify the URL resolves, then uncomment the
        line below and drop step 2. Linking a surface that 404s is worse than
        no link — same discipline as the commented Discussions link in
        .github/ISSUE_TEMPLATE/config.yml.

        https://github.com/mshtawythug/second-brain/security/advisories/new
   -->

2. **Until that setting is enabled** — send a private message to the repository
   owner on GitHub ([`@mshtawythug`](https://github.com/mshtawythug)) saying you
   have a security report, *without* the details, and the private advisory
   channel will be opened for you.

This project deliberately publishes **no contact email address**. Everything
routes through GitHub, which needs no address published in order to work.

Useful things to include, when you have them: affected version
(`brain --version`), affected component (CLI, `brain-mcp`, ingest, vault sync,
the Quartz wiki), a minimal reproduction, and the impact you believe it has.

## What to expect

- **Acknowledgement** — within 7 days.
- **Assessment and a remediation plan** — within 30 days of acknowledgement.
- **Fix and advisory** — coordinated disclosure. You will be credited in the
  advisory and the CHANGELOG unless you ask not to be.

This is a single-maintainer project worked on in personal time, so the above is
best-effort rather than a contractual SLA. There is **no bug bounty** and no
paid triage.

## Security model — what this project actually is

Second Brain is a **local-first** tool. It runs a database on your machine,
ingests your personal documents into it, and optionally serves a rendered wiki
over a local HTTP port. The interesting boundaries are therefore local ones, and
knowing where they sit is what makes a report actionable.

- **The corpus is a local Postgres container.** `docker-compose.yml` binds it to
  the loopback host port `55432` with the development credentials `brain:brain`.
  That is intentional for a single-user database reachable only from the machine
  it runs on. It becomes a real risk the moment the binding is changed to
  `0.0.0.0`, the container is published to a LAN, or the host is shared — at
  that point the credentials protect nothing. Keep it on loopback.

- **The optional wiki has no authentication.** `brain wiki install` renders the
  vault with Quartz and serves it through Caddy (see `docs/vault-and-wiki.md`).
  There is no login, no session, and no per-document access control: **anyone who
  can reach that port reads the entire personal corpus**, including ingested
  email, meeting transcripts, and chat threads. Bind it to loopback, or put your
  own authenticating proxy in front of it. Do not expose it to a LAN or to the
  internet.

- **`brain-mcp` is a stdio server, not a network listener.** It speaks MCP over
  stdin/stdout to whichever process spawns it, so its trust boundary is that
  agent. An agent with MCP access to your brain can read every non-draft
  document in it. Grant that access the way you would grant filesystem access.

- **Secrets.** `.env` is gitignored and is the only place credentials belong.
  `VOYAGE_API_KEY` is the sole outbound-service credential, and only when
  `BRAIN_EMBEDDER=voyage`.

- **Data egress.** With the default `arctic` embedder (local Ollama) and with
  `BRAIN_EMBEDDER=none`, **no document content leaves the machine**. With
  `BRAIN_EMBEDDER=voyage`, chunk text is sent to Voyage AI for embedding.
  `brain ask`, `brain enrich`, `brain audio`, and the GraphRAG extractors send
  snippets to whichever Ollama model is configured — local by default.

### In scope

- SQL injection, or any path where untrusted input reaches a query
  unparameterized.
- Path traversal in ingest, vault writes, or wiki export — anything that writes
  outside the vault or reads outside the configured roots.
- Arbitrary code execution triggered by ingesting a crafted PDF, DOCX, or
  Markdown document.
- Secrets (API keys, `.env` contents, credentials) leaked into logs, the vault,
  the wiki output, or an error message.
- The MCP server or the wiki exposing documents that are marked draft or are
  otherwise excluded from publication.
- Privilege or tenancy escalation across the GraphRAG tenant boundary.

### Out of scope

- The default `brain:brain` credentials on the loopback-bound local Postgres.
  Documented, intentional, and not a finding on their own.
- Anything requiring the attacker to already have shell access as your user — at
  that point they can read the vault directly.
- User-initiated exposure of the wiki port, or of the database port, to a
  network. That is a deployment choice, documented above.
- Supply-chain issues in third-party models, base images, or PyPI dependencies.
  Report those upstream; we will pick up the fix. (Dependabot watches our
  dependency manifests — see `.github/dependabot.yml`.)
- Denial of service achieved by feeding the tool an enormous local file.
