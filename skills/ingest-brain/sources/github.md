# GitHub PRs, issues and READMEs

Part of the [`ingest-brain`](../SKILL.md) skill. Its safety rules apply to this recipe too, and
so does its section on the context cost of the MCP→stdin chain when you use the connector.

**Prefer the CLI**: `brain ingest-github` reads through the user's own `gh` login, and only
the user's own items (author, assignee or reviewer). It **refuses without `--repo OWNER/REPO`
or `--mine`, and `--since`**; `--repo` narrows to those repositories, never to everyone's items
there. It stops at 500 documents a run, keeps only the user's own comments unless
`--with-thread`, and stores any repository that is not public as **confidential**. Start with
a short `--since` and a `--dry-run`: each new document costs a summary and concept extraction.

**Without `gh`, use the GitHub MCP connector** (`search_pull_requests` / `search_issues`, then
`pull_request_read` / `issue_read`). The recipe is the scope: only the user's own items
(`author:@me`, `assignee:@me` or `reviewed-by:@me`, never an organisation) with an
`updated:>=YYYY-MM-DD` bound; refuse a search with no date bound. Body: the title line, the
description and the user's own comments; state goes in `--metadata`, never the body. Pass
`--sensitivity confidential` for any repository that is not public (private, internal, or
visibility unknown), and `--content-type github_issue` for an issue:

```bash
echo "<owner>/<repo>#<n>: <title>

<description>

<the user's own comments>" | brain ingest-stdin --source github \
  --external-id "<owner>/<repo>#<n>" --title "<owner>/<repo>#<n> — <title>" \
  --content-type github_pr --date YYYY-MM-DD --tag github \
  --metadata '{"repo": "<owner>/<repo>", "state": "<state>"}'
```
