# skg-mcp-evals

Droid plugin that captures session token usage and MCP tool-call metrics on `SessionEnd`
and POSTs them to a configured MCP server intake endpoint, which upserts the data
into a Couchbase collection (`AI-Eval.telemetry.Client_side_evals`).

## What it captures

One doc per Droid session, keyed by `session_id`. Resumes upsert the same doc — no duplicates.

- **Identity:** `user`, `domain`, `model`, `model_tier`, `permission_mode`
- **Tokens:** `input`, `cache_creation`, `cache_read`, `output`, `thinking` (raw + display)
- **Aggregate:** `factory_billable_tokens` (computed via published model multipliers, matches `/cost` "Factory approx")
- **MCP tool calls:** count per tool, total, error count

Phantom sessions (`message_count == 0`) are skipped — Droid spawns short-lived internal sessions that we don't record.

## Install (recommended — via Droid's plugin UI)

In Droid, type `/plugin` to open the plugin browser, then:

1. **Marketplaces** tab → paste `<your-org>/skg-mcp-evals` → click **ADD**
2. **Browse** tab → install `evals`
3. `/hooks` → approve the `SessionEnd` hook (one-time)

That's it. No env vars, no shell config — the parser auto-resolves the metrics URL from your existing Droid MCP config (`~/.factory/mcp.json`), swapping `/mcp` for `/session-eval` on whichever MCP server you've already wired up.

Auto-update is on by default, so when fixes get pushed to GitHub your local copy stays current.

## Optional — override the metrics URL

If you have multiple MCP servers configured and want to force the metrics POSTs to a specific one, set this in your shell rc:

```bash
export MCP_METRICS_URL="http://your-mcp-host:8002/session-eval"
```

When set, this takes priority over the auto-derived URL.

## Install (manual / local dev mode)

For working on the plugin itself before pushing to GitHub:

```bash
ln -sfn /path/to/skg-mcp-evals ~/.factory/plugins/skg-mcp-evals
chmod +x /path/to/skg-mcp-evals/hooks/metrics_parser.py
```

Then enable in `~/.factory/settings.json`:

```json
{ "enabledPlugins": { "skg-mcp-evals": true } }
```

Restart Droid sessions to load.

## Verify

After ending a Droid session with at least one user message, check Couchbase:

```sql
SELECT META().id, factory_billable_tokens_display, mcp_tool_calls.total
FROM `AI-Eval`.`telemetry`.`Client_side_evals`
ORDER BY message_count DESC LIMIT 5;
```

## Spec

See `docs/superpowers/specs/2026-05-03-client-side-evals-design.md` in the `mcp-skg` repo.
