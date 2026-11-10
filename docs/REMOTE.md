# Remote access

There are three ways to reach Bridge Commander from somewhere else.

## 1. Stdio over SSH (simplest, no open ports)

For clients that start a local command (Claude Code, Claude Desktop, Cursor, …):

```bash
claude mcp add office-mac -- ssh -T office-mac python3 bin/bridge-commander.pyz serve
```

SSH carries the MCP stream; authentication is your SSH key.

## 2. Hub and nodes

One machine runs the server; the others run `worker` and are listed under `nodes` in the hub config
(see the README). The client connects to the hub only and picks a machine per call with `node`.
This is the right shape when the assistant should manage several machines through one connector.

## 3. Streamable HTTP (for ChatGPT, claude.ai and other URL-based clients)

```bash
python3 bridge-commander.pyz token --write ~/.config/bridge-commander/token
# config.json: "http": {"tokenFile": "~/.config/bridge-commander/token"}
python3 bridge-commander.pyz serve --transport http
# bridge-commander 1.1.0: MCP endpoint http://127.0.0.1:8765/mcp
```

The endpoint accepts the token in two ways:

- `Authorization: Bearer <token>` — for clients that let you set a header or API key;
- the secret URL `https://<public host>/mcp/<token>` — for clients that only take a URL. Choose
  "No authentication" in such a client: the URL itself is the credential.

Expose the loopback port with a tunnel you already trust, for example:

| tunnel | command (example) |
|---|---|
| Cloudflare Tunnel | `cloudflared tunnel --url http://127.0.0.1:8765` |
| Tailscale Funnel | `tailscale funnel 8765` |
| ngrok | `ngrok http 8765` |
| your own reverse proxy (nginx/Caddy with TLS) | proxy `/mcp` to `127.0.0.1:8765` |

The server speaks plain HTTP; TLS is the tunnel's job. `GET /health` returns `{"status": "ok"}` without
authentication, for tunnel health checks.

### Client notes

- **ChatGPT** (Settings → Connectors → developer mode → add MCP server): URL `https://<public host>/mcp/<token>`,
  authentication "No authentication". ChatGPT caches the tool list; after upgrading Bridge Commander, refresh
  the connector and start a new chat.
- **claude.ai** (Settings → Connectors → Add custom connector): URL `https://<public host>/mcp/<token>`.
- **MCP Inspector / browser-based clients** send an `Origin` header; add that origin to `http.allowedOrigins`.

Status: these client flows follow the MCP Streamable HTTP specification (JSON responses, no SSE stream) and were
tested with the official MCP Python SDK client. They have not yet been tested end-to-end with ChatGPT or claude.ai.

## Running it as a service

- Linux: [examples/bridge-commander.service](../examples/bridge-commander.service) (systemd user unit).
- macOS: [examples/com.example.bridge-commander.plist](../examples/com.example.bridge-commander.plist) (launchd agent).
