# skg-mcp-evals — Droid plugin marketplace

A single-plugin Droid marketplace for capturing per-session token usage and MCP tool-call metrics, posted to an MCP server's `/session-eval` endpoint and stored in Couchbase (`AI-Eval.telemetry.Client_side_evals`).

## Plugins

- **`evals`** (`plugins/evals/`) — `SessionEnd` hook script that reads the session's `settings.json` + transcript, computes Factory billable tokens via a per-model multiplier table, and POSTs the eval doc to a configured MCP server. See [`plugins/evals/README.md`](plugins/evals/README.md) for install details and the multiplier formula.

## Install

In Droid (CLI or desktop), run `/plugin` and:

1. **Marketplaces** tab → paste `https://github.com/couchbaselabs/skg-mcp-evals` → click **ADD**
2. **Browse** tab → find `evals` → install
3. `/hooks` → approve the `SessionEnd` hook (one-time)

The plugin auto-resolves the metrics URL from `~/.factory/mcp.json` (swaps `/mcp` for `/session-eval` on the configured MCP server). Set `MCP_METRICS_URL` in your shell to override.

## What gets captured

One doc per Droid session, keyed by `session_id`. Resumes upsert the same doc — no duplicates. Phantom sessions (`message_count == 0`) are skipped.

Fields per doc:

- Identity — `user`, `domain` (= MCP scope), `model`, `model_tier`, `permission_mode`
- Tokens (raw + display) — `input`, `cache_creation`, `cache_read`, `output`, `thinking`
- Aggregate — `factory_billable_tokens` (matches `/cost` "Factory approx" within rounding)
- MCP tool calls — total, errors, unique names, per-tool counts

## Verify

After ending a real Droid session with at least one tool call:

```sql
SELECT META().id, factory_billable_tokens_display, mcp_tool_calls.total, domain
FROM `AI-Eval`.`telemetry`.`Client_side_evals`
ORDER BY message_count DESC LIMIT 5;
```

## Spec

Design and rationale live in the `mcp-skg` repo at `docs/superpowers/specs/2026-05-03-client-side-evals-design.md`.
