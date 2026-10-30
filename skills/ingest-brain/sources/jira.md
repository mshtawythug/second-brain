# Jira issues — MCP→stdin chain

Part of the [`ingest-brain`](../SKILL.md) skill. Its safety rules, and its section on the
context cost of the MCP→stdin chain, apply to this recipe too.

The user's own tickets, through the Atlassian MCP connector (`searchJiraIssuesUsingJql`,
then `getJiraIssue`). There is no Jira CLI and no flag to enforce scope, so **this recipe
is the scope** — follow it exactly.

**The JQL.** Use this one unless the user narrows it further:

`(assignee = currentUser() OR reporter = currentUser()) AND updated >= -30d`

Keep the parentheses: without them `AND` binds tighter and the time bound applies to
`reporter` only. A narrower JQL is fine (a shorter window, one project, one key), but it
must still name the user and carry a time bound. **Refuse a JQL with no time bound**, and
tell the user why. Project-wide pulls (every issue in a project) are not part of this recipe.

**What goes in the body:** the key and summary, the description, and the user's own
comments. Other people's comments go in only when the user asks for them. Status and
assignee change often, so they go in `--metadata`, never in the body:

```bash
echo "<KEY>: <summary>

<description>

<the user's own comments>" | brain ingest-stdin \
  --source jira \
  --external-id "<site-host>/<ISSUE-KEY>" \
  --title "<ISSUE-KEY> — <summary>" \
  --content-type jira_issue \
  --sensitivity confidential \
  --date YYYY-MM-DD \
  --tag jira \
  --metadata '{"site": "<site-host>", "issue_key": "<ISSUE-KEY>", "status": "<status>", "assignee": "<name>"}'
```

- **Always `--sensitivity confidential`.** Work-tracker content belongs to the employer:
  confidential keeps it off a hosted embedder and out of generated pages and unprompted
  listings. Only the user can ask for `normal`.
- **Dedup is `(jira, <site-host>/<ISSUE-KEY>)`**, so the same key on two sites stays two
  documents. Re-ingesting a changed issue updates it in place.
- **An unchanged body prints `skipped (already ingested): <title> → <document-id>` and
  leaves metadata alone.** If only the status or assignee moved, patch it without a
  re-chunk, passing the id after `→`:
  `brain edit <document-id> --metadata '{"status": "Done"}'`.

The 30-day bound keeps a run to tens of issues; batch small, as for Krisp.
