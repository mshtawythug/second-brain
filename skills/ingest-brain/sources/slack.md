# Slack threads — MCP→stdin chain

Part of the [`ingest-brain`](../SKILL.md) skill. Its safety rules, and its section on the
context cost of the MCP→stdin chain, apply to this recipe too.

Same pattern with the Slack MCP. Fetch a thread, then:

```bash
echo "<thread content>" | brain ingest-stdin \
  --source slack \
  --external-id "<channel-id>/<thread-ts>" \
  --title "Slack — <#channel> — <first message snippet>" \
  --content-type thread \
  --date YYYY-MM-DD \
  --metadata '{"channel": "#engineering", "participants": ["U1", "U2"]}'
```

Pick `--external-id` so it's stable across re-ingests. Channel-id + thread-ts
is the canonical Slack identifier; thread-ts alone is also unique within a
workspace.
