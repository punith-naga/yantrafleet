# Sarathi MCP Server — the fleet as a tool surface

`sarathi.mcp_server` exposes Sarathi's Toolbox over the
[Model Context Protocol](https://modelcontextprotocol.io), so *external*
agents can query the Yantrika directly — Claude Desktop, Vegaduta,
Quantum Lab, or anything else that speaks MCP over stdio.

## The trinity vision: one agent core, three surfaces

There is exactly one agent core — the evidence-grounded `Toolbox`
(`sarathi/tools.py`), which reads Supabase through a swappable `Transport`
and stamps every result with a `source_id` citation key. Three surfaces
share it:

| Surface | Entry | Consumer |
|---|---|---|
| Copilot API | `uvicorn sarathi.app:app --port 8001` (`POST /ask`) | Fleet console chat |
| Console | `console/index.html` | Human operators |
| **MCP server** | `python -m sarathi.mcp_server` | External agents (Claude Desktop, Vegaduta, Quantum Lab) |

Same data, same statuses (`active | idle | charging | paused | estop |
degraded | fault`), same grounding contract everywhere: numbers originate
in tool payloads, never in a model. External agents receive full JSON
payloads including `source_id`, so their answers can cite the same
evidence keys the in-house tiers do.

## Install & run

```bash
pip install -r copilot/requirements.txt --break-system-packages
# or minimally, for the MCP surface alone:
pip install mcp httpx --break-system-packages

python -m sarathi.mcp_server        # from the copilot/ directory
```

The server speaks MCP over **stdio** — it is meant to be launched by an
MCP host, not by hand. `SUPABASE_URL` / `SUPABASE_KEY` override the
client-safe defaults from `sarathi/config.py`.

## Register in Claude Desktop

Edit the Claude Desktop config file:

* macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
* Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "sarathi-fleet": {
      "command": "python",
      "args": ["-m", "sarathi.mcp_server"],
      "cwd": "/absolute/path/to/yantrafleet/copilot"
    }
  }
}
```

(Use `py` or an absolute interpreter path on Windows if `python` is not
on PATH; `cwd` must be the `copilot/` directory so the `sarathi` package
imports. Alternatively set `"env": {"PYTHONPATH": ".../copilot"}`.)

Restart Claude Desktop; the `sarathi-fleet` tools appear under the tools
icon. Ask e.g. *"Which robots are faulted, and what does R-004's last 30
minutes of telemetry look like?"*

## Tools

All tools return JSON text: `{tool, args, data, source_id, ts}`. On a
backend failure they return `{tool, error, data: null}` instead of
crashing the session.

| Tool | Args | Delegates to |
|---|---|---|
| `fleet_summary` | — | `Toolbox.get_fleet_summary` |
| `query_robots` | `status?, vendor?, limit?` | `Toolbox.query_robots` |
| `query_alerts` | `sev?, limit?` | `Toolbox.query_alerts` |
| `query_incidents` | `state?, limit?` | `Toolbox.query_incidents` |
| `query_commands` | `status?, limit?` | `Toolbox.query_commands` |
| `robot_history` | `robot_id, minutes=30` | `Toolbox.query_telemetry` |

All tools are **read-only**; command execution stays behind the console's
human-approval gate.

## Tests

Offline, in-memory — a real MCP `ClientSession` wired to the real server
via `mcp.shared.memory.create_connected_server_and_client_session`, with
fleet data served by `StaticTransport` (no network):

```bash
cd copilot && python -m pytest tests/test_mcp_server.py -q
```
