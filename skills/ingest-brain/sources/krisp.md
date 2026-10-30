# Krisp transcripts — MCP→stdin chain

Part of the [`ingest-brain`](../SKILL.md) skill. Its safety rules, and its section on the
context cost of the MCP→stdin chain, apply to this recipe too.

The brain has no direct Krisp integration. Claude orchestrates:

1. Call the Krisp MCP to search/list meetings, fetch transcripts. Use
   whichever Krisp MCP tool fits — your harness exposes them as
   `mcp__claude_ai_Krisp__*`. Don't hardcode parameter names; read your live
   MCP schema for the available tools and parameters.
2. For each meeting, pipe the transcript body into `brain ingest-stdin`:

```bash
echo "<transcript content>" | brain ingest-stdin \
  --source krisp \
  --external-id "<meeting_id>" \
  --title "<short title — usually the meeting topic + date>" \
  --content-type transcript \
  --date YYYY-MM-DD \
  --metadata '{"participants": ["Alice", "Bob"], "duration_min": 42}'
```

Dedup is by `(source_kind='krisp', external_id=<meeting_id>)` — re-ingesting
the same meeting UPDATEs in place. UUID + tags survive.

3. Report counts back to the user: "Ingested N new transcripts, updated M
   existing ones."
