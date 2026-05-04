#!/usr/bin/env python3
"""
metrics_parser.py — Droid SessionEnd hook script.

Reads the SessionEnd payload from stdin, joins it with
~/.factory/sessions/<cwd>/<sid>.settings.json + .jsonl, builds a
session-eval doc, and POSTs it to MCP_METRICS_URL.

Spec: docs/superpowers/specs/2026-05-03-client-side-evals-design.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs

# ── Multiplier table (spec §5.3) ──────────────────────────────────────────────
BASE_RATIOS = {
    "input":          1.00,
    "cache_creation": 1.25,
    "cache_read":     0.10,
    "output":         5.00,
    "thinking":       5.00,
}

MODEL_TIER = {
    # Currently active
    "claude-sonnet-4-6":      ("sonnet-4.6",     1.2),
    "gpt-5.5":                ("gpt-5.5",        2.0),
    "gpt-5.3-codex":          ("gpt-5.3-codex",  0.7),
    "gemini-3.1-pro":         ("gemini-3.1-pro", 0.8),
    "droid-core-glm-5.1":     ("glm-5.1",        0.55),
    "droid-core-kimi-k2.6":   ("kimi-k2.6",      0.4),
    "droid-core-minimax-m2.7": ("minimax-m2.7",  0.12),
    # Legacy / additional
    "claude-opus-4-6":        ("opus-4.6",       2.0),
    "claude-opus-4-5":        ("opus-4.5",       2.0),
    "claude-sonnet-4-5":      ("sonnet-4.5",     1.0),
    "claude-haiku-4-5":       ("haiku-4.5",      0.4),
    "gpt-5.5-pro":            ("gpt-5.5-pro",   12.0),
    "gpt-5.4":                ("gpt-5.4",        1.0),
    "gpt-5.4-mini":           ("gpt-5.4-mini",   0.3),
    "gpt-5.2":                ("gpt-5.2",        0.7),
    "gpt-5.2-codex":          ("gpt-5.2-codex",  0.7),
    "gemini-3-flash":         ("gemini-3-flash", 0.2),
    "droid-core-kimi-k2.5":   ("kimi-k2.5",      0.25),
    "droid-core-minimax-m2.5": ("minimax-m2.5",  0.12),
    # claude-opus-4-7 deliberately omitted — 50% off promo, revisit
}


def fmt_tokens(n: int) -> str:
    """Format token count the way Droid /cost does."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1_000:.1f}K"
    return f"{n/1_000_000:.1f}M"


def get_user_email() -> str:
    """git config user.email, fall back to <whoami>@unknown.local."""
    try:
        email = subprocess.check_output(
            ["git", "config", "--get", "user.email"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if email:
            return email
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return f"{os.environ.get('USER', 'unknown')}@unknown.local"


def parse_mcp_tools(transcript_path: str) -> dict:
    """Walk transcript JSONL; aggregate MCP tool counts + errors."""
    counts: dict[str, int] = defaultdict(int)
    use_id_to_name: dict[str, str] = {}
    errors = 0
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "message":
                    continue
                for block in rec.get("message", {}).get("content", []) or []:
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name", "")
                        # Droid names MCP tools "<server>___<tool>" (triple
                        # underscore) in current versions; older docs/format
                        # uses "mcp__<server>__<tool>". Built-ins (Read,
                        # Glob, Edit, Bash, TodoWrite, ...) have neither.
                        if "___" in name or name.startswith("mcp__"):
                            counts[name] += 1
                            use_id_to_name[block.get("id", "")] = name
                    elif btype == "tool_result":
                        tid = block.get("tool_use_id", "")
                        if tid in use_id_to_name and block.get("is_error"):
                            errors += 1
    except FileNotFoundError:
        return {"total": 0, "errors": 0, "unique_names": 0, "tools": []}

    tools = sorted(
        [{"name": n, "calls": c} for n, c in counts.items()],
        key=lambda t: -t["calls"],
    )
    return {
        "total":        sum(counts.values()),
        "errors":       errors,
        "unique_names": len(counts),
        "tools":        tools,
    }


def build_doc(payload: dict) -> dict | None:
    """Assemble the session-eval doc. Returns None to skip phantom sessions."""
    session_id      = payload.get("session_id") or ""
    cwd             = payload.get("cwd", "")
    transcript_path = payload.get("transcript_path", "")
    permission_mode = payload.get("permission_mode", "")
    message_count   = payload.get("message_count", 0)

    if not session_id or message_count == 0:
        return None  # phantom session — skip

    settings_path = transcript_path.replace(".jsonl", ".settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}

    model = settings.get("model", "unknown")
    model_tier, multiplier = MODEL_TIER.get(model, ("unknown", 1.0))

    tu = settings.get("tokenUsage", {})
    tokens = {
        "input":          tu.get("inputTokens",          0),
        "cache_creation": tu.get("cacheCreationTokens",  0),
        "cache_read":     tu.get("cacheReadTokens",      0),
        "output":         tu.get("outputTokens",         0),
        "thinking":       tu.get("thinkingTokens",       0),
    }
    tokens_display = {k: fmt_tokens(v) for k, v in tokens.items()}

    base = sum(tokens[k] * BASE_RATIOS[k] for k in tokens)
    factory_billable = round(base * multiplier)

    # `domain` is the MCP scope from mcp.json (same concept, different name).
    # Fall back to the cwd directory name if no MCP scope is configured.
    domain = get_mcp_scope() or (Path(cwd).name if cwd else "unknown")

    return {
        "session_id":                       session_id,
        "user":                             get_user_email(),
        "domain":                           domain,
        "model":                            model,
        "model_tier":                       model_tier,
        "permission_mode":                  permission_mode,
        "tokens":                           tokens,
        "tokens_display":                   tokens_display,
        "factory_billable_tokens":          factory_billable,
        "factory_billable_tokens_display":  fmt_tokens(factory_billable),
        "message_count":                    message_count,
        "mcp_tool_calls":                   parse_mcp_tools(transcript_path),
    }


def get_mcp_scope() -> str | None:
    """
    Read ~/.factory/mcp.json and return the `scope=` query param of the first
    non-disabled MCP server. This is what Droid is currently pointing at —
    same concept as `domain` in our doc, just a different name in the URL.
    """
    mcp_config_path = Path.home() / ".factory" / "mcp.json"
    try:
        with open(mcp_config_path) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    for entry in (cfg.get("mcpServers") or {}).values():
        if entry.get("disabled"):
            continue
        url = entry.get("url", "")
        if not url:
            continue
        try:
            parsed = urlparse(url)
            scope_values = parse_qs(parsed.query).get("scope")
            if scope_values:
                return scope_values[0]
        except Exception:
            continue
    return None


def resolve_metrics_url() -> str | None:
    """
    Resolve where to POST the session-eval doc.

    Priority:
      1. MCP_METRICS_URL env var (explicit override — useful for multi-MCP setups)
      2. Auto-derived from ~/.factory/mcp.json: take the first non-disabled MCP
         server, swap its /mcp path for /session-eval, drop query params.
         This means devs only configure the MCP URL once (in Droid), and the
         metrics URL falls out for free.
      3. None — caller logs and skips the POST.
    """
    explicit = os.environ.get("MCP_METRICS_URL")
    if explicit:
        return explicit

    mcp_config_path = Path.home() / ".factory" / "mcp.json"
    try:
        with open(mcp_config_path) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    for entry in (cfg.get("mcpServers") or {}).values():
        if entry.get("disabled"):
            continue
        url = entry.get("url", "")
        if not url:
            continue
        try:
            parsed = urlparse(url)
        except Exception:
            continue
        if not parsed.scheme or not parsed.netloc:
            continue
        # Swap path /mcp → /session-eval (other paths left alone in case the
        # user's MCP is mounted at /something-else, in which case we still
        # try /session-eval at the same host:port).
        new_path = "/session-eval"
        return urlunparse(parsed._replace(path=new_path, query="", fragment=""))

    return None


def post_doc(doc: dict) -> None:
    """POST the doc to the resolved metrics URL. Fail silently — hooks shouldn't block Droid."""
    url = resolve_metrics_url()
    if not url:
        print(
            "[metrics_parser] no metrics URL resolved (MCP_METRICS_URL unset and "
            "no usable entry in ~/.factory/mcp.json); skipping post",
            file=sys.stderr,
        )
        return
    body = json.dumps(doc).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[metrics_parser] posted {doc['session_id']} → status {resp.status}", file=sys.stderr)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[metrics_parser] post failed for {doc.get('session_id')}: {e}", file=sys.stderr)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"[metrics_parser] invalid stdin JSON: {e}", file=sys.stderr)
        return
    doc = build_doc(payload)
    if doc is None:
        return  # phantom session, nothing to record
    post_doc(doc)


if __name__ == "__main__":
    main()
