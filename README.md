# Bridge Commander

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI assistant
(Claude Desktop, Claude Code, ChatGPT connectors, any MCP client) hands on your machines:

- **Files**: read (text, images, Word, Excel, PDF, URLs, line ranges, tail), write, append, exact-text `edit_block`,
  move, mkdir, info
- **Search**: find files by name/glob, grep contents with regex and context; background searches with paging
- **History**: recent tool calls and usage statistics from the audit log
- **Terminal sessions**: long-running and interactive commands with incremental output and stdin
- **Processes**: list and kill system processes
- **Remote nodes**: one server fans out to other machines over SSH (or any command), each with its own rules
- **Two transports**: stdio for local clients, Streamable HTTP with a token for remote clients

No dependencies beyond the Python standard library. Use a maintained Python 3.12 or newer; the source also has compatibility checks on 3.9. The client ships as one `.pyz` file.

> [Русская версия](README.ru.md)

## Quick start

```bash
git clone https://github.com/PaPasha215/bridge-commander-core && cd bridge-commander-core
python3 scripts/build_pyz.py                 # -> dist/bridge-commander.pyz
python3 dist/bridge-commander.pyz init       # writes ~/.config/bridge-commander/config.json
```

Or install as a command: `pipx install .` (then use `bridge-commander` instead of `python3 …pyz`).

### Claude Code

```bash
claude mcp add bridge-commander -- python3 /absolute/path/bridge-commander.pyz serve
```

### Claude Desktop

Add to `claude_desktop_config.json` (see [examples/claude_desktop_config.json](examples/claude_desktop_config.json)):

```json
{"mcpServers": {"bridge-commander": {"command": "python3", "args": ["/absolute/path/bridge-commander.pyz", "serve"]}}}
```

### Over SSH (a server somewhere else, no open ports)

```bash
claude mcp add my-server -- ssh my-server python3 bridge-commander.pyz serve
```

### Remote HTTP clients (ChatGPT, claude.ai connectors, anything with a URL)

```bash
python3 bridge-commander.pyz token --write ~/.config/bridge-commander/token
python3 bridge-commander.pyz serve --transport http          # http://127.0.0.1:8765/mcp
```

Then publish the loopback port through a tunnel you trust. See [docs/REMOTE.md](docs/REMOTE.md).

### Hosted cloud (no server of your own)

The free beta uses [Bridge Commander Cloud](https://bridgecommander.dev). On a new macOS/Linux profile:

```bash
curl -fL --output bridge-commander.pyz https://bridgecommander.dev/download/bridge-commander.pyz &&
python3 -I bridge-commander.pyz init --directory ~/BridgeFiles &&
python3 -I bridge-commander.pyz remote --cloud https://bridgecommander.dev
```

Verify the downloaded 1.3.0 artifact against [SHA256SUMS](SHA256SUMS). Keep the MIT [LICENSE](LICENSE) with redistributed copies. Setup stops if a download or initialization fails. Inspect an existing configuration before changing it; do not add `--force` to this sequence.

The machine connects out over HTTPS, you confirm a code in the browser, and the chat connects to the cloud's MCP
URL with OAuth. The device still enforces its own config file. `remote --logout` forgets the device credentials;
revoking the device in the dashboard cuts access at once.

Backup route: `remote --fallback https://edge.<cloud address>` (repeatable; saved with the credentials). When the
main address stops answering (a VPN drops, a network blocks the IP), the client switches to the first backup whose
`/health` answers, and every 5 minutes checks whether the main address is back. A backup must be on the cloud's
own domain, so the device token never goes to another host.

## Configuration

`~/.config/bridge-commander/config.json` (or `--config FILE`, or `$BRIDGE_COMMANDER_CONFIG`). Every key is optional.

The table lists underlying defaults for a manually supplied partial configuration. `init` instead writes an explicit private configuration for `~/BridgeFiles` with read-only enabled. `--allow-writes` also permits commands with your OS user's rights. Run one remote client per OS profile: `--config` selects file rules, while saved device credentials are shared by that profile.

| key | default | meaning |
|---|---|---|
| `allowedDirectories` | `["~"]` | Paths the tools may touch. Symlinks are resolved first, so they cannot escape. `[]` = unrestricted. Also the default working directory (first entry). |
| `readOnly` | `false` | Refuse writes, edits, moves, new processes, input and kills. |
| `blockedCommands` | `sudo`, `mkfs`, `shutdown`, … | First word of every command segment is checked. A seat belt, not a sandbox. |
| `defaultShell` | `$SHELL` / `powershell.exe` | Shell for `start_process` (`sh`-like, `cmd`, `powershell`, `pwsh`). |
| `fileReadLineLimit` | `1000` | Max lines per `read_file` call. |
| `maxFileSizeBytes` | `10485760` | Max size to read, write or search in a single file. |
| `maxProcessOutputBytes` | `1048576` | Output kept per session (oldest bytes are dropped first). |
| `maxSessions` | `20` | Terminal sessions kept at once. |
| `allowConfigChanges` | `false` | Allow the model to call `set_config_value` (in memory only). |
| `auditLog` | `~/.local/state/bridge-commander/audit.jsonl` | One JSON line per call: time, tool, node, ok, duration, arguments (strings cut to 200 chars; written text and process input only as a length), first line of the result (not for reads). Readable with `get_recent_tool_calls`. `null` disables it. |
| `http` | `{"host": "127.0.0.1", "port": 8765, "tokenFile": null, "allowedOrigins": []}` | HTTP transport. The token comes from `$BRIDGE_COMMANDER_TOKEN` or `tokenFile`. |
| `nodes` | `{}` | Remote machines, see below. |

`bridge-commander check` validates the file and pings every node.

## Remote nodes

A *hub* runs the MCP server. Each node runs `bridge-commander worker`, reached by any command that connects
stdin/stdout — normally `ssh`. Every tool gains a `node` argument (`local` or a node name).

```json
{
  "nodes": {
    "build-box": {
      "command": ["ssh", "-T", "-o", "BatchMode=yes", "build-box", "python3 ~/bin/bridge-commander.pyz worker"],
      "description": "Linux build server"
    }
  }
}
```

- The worker applies **its own** config on its machine (allowed directories, read-only, blocked commands).
  The hub cannot widen them.
- The SSH connection is kept open, so terminal sessions on a node survive between calls. If the link drops it is
  re-opened on the next call; sessions that lived in the old worker are gone.
- Copy the worker with `scp dist/bridge-commander.pyz build-box:bin/`. Nothing else has to be installed.

## Tools

| tool | what it does |
|---|---|
| `get_config` / `set_config_value` | show settings, host and nodes / change a setting at runtime (if allowed) |
| `list_nodes` | check that every node answers |
| `read_file`, `read_multiple_files` | text with `offset`/`length` (negative offset = tail), images as image content; `.docx`, `.xlsx` (`sheet`), PDF (built in on macOS, `pdftotext` on Linux/Windows), http(s) URLs (`is_url`) |
| `write_file` | `rewrite` (atomic, keeps permissions) or `append`; creates parent directories |
| `edit_block` | replace exact text; checks the number of occurrences; handles CRLF files; on a miss shows the closest text and a character diff |
| `create_directory`, `list_directory`, `move_file`, `get_file_info` | the usual |
| `search_files`, `search_code` | name/glob search; regex or literal content search with context lines |
| `start_search`, `get_more_search_results`, `stop_search`, `list_searches` | the same searches in the background, paged, for big trees |
| `start_process` | run a command in a session; returns PID, status (`running`, `waiting for input`, `exited with code N`) and output |
| `interact_with_process`, `read_process_output` | send a line of input / get new output (or any retained lines with `offset`) |
| `force_terminate`, `list_sessions` | stop a session and its children / list sessions |
| `list_processes`, `kill_process` | system processes |
| `get_recent_tool_calls`, `get_usage_stats` | what was done recently (arguments, result start) and per-tool statistics, from the audit log |

## Security

Read [docs/SECURITY.md](docs/SECURITY.md) before exposing it to a network. In short: this is remote shell access by
design. `allowedDirectories` confines the file tools, but commands started with `start_process` run with your
user's full rights. Use a dedicated user, `readOnly`, and a secret token; keep the HTTP listener on loopback
behind an authenticated tunnel.

## Development

```bash
python3 -m unittest discover -s tests -v    # stdlib only
python tests/sdk_compat.py                  # needs `pip install mcp`; checks the official SDK client
```

Candidate 1.3.0: 80 tests pass on macOS with Python 3.9 and 3.12. On Linux with Python 3.12,
79 tests pass and one PDF test is skipped because the optional converter is absent. These are
isolated fixture checks. A real Linux CLI path through public HTTPS, delivered login email,
device approval, OAuth, scoped read/search, authorized text changes and access revocation has also been checked.
Fresh ChatGPT/mobile host acceptance and public capacity remain separate checks. The hosted route is Bridge Commander Cloud over OAuth.

PDF text: `pdftotext` (poppler) when present, otherwise the PDFKit built into macOS. On Windows install poppler
(a Windows build of poppler with its `bin` folder on PATH, e.g. `scoop install poppler`) to read PDF; everything else needs
nothing beyond Python.

## License

MIT — see [LICENSE](LICENSE).

CI: [ci/github-actions-test.yml](ci/github-actions-test.yml) runs the tests on Linux and macOS. To enable it,
copy it to `.github/workflows/test.yml` (pushing workflow files needs a token with the `workflow` scope).

## First free public beta — client 1.3.0

The initial macOS/Linux
path is a foreground Python `.pyz` in a terminal, a fresh private configuration and
`~/BridgeFiles` read-only. Use a maintained, patched Python; public installer acceptance
is separate from compatibility tests. Do not run `remote` after a failed `init`, overwrite
an existing configuration, or run two clients with the same credentials. Folder rules
bound file tools; system/process/history queries have a broader read scope.

The client runs in the foreground terminal. Closing it stops new requests. An Apple Developer account is not needed to use this Python client or the ChatGPT plugin.


Remote sign-in now requires an explicit sharing configuration. Start with
`bridge-commander init --directory /path/to/shared-folder`; the generated configuration is
read-only and private. `--allow-writes` also enables commands with the OS user's permissions.
The remote protocol requires the matching Cloud receipt API and finite delivery deadlines.
Private metadata receipts prevent automatic replay across reconnect/restart; full outputs are
kept only in bounded memory. Windows remote journal ACL acceptance is still pending.

Support: [support@bridgecommander.dev](mailto:support@bridgecommander.dev). Include the client version, OS, error time and call identifier when available; omit passwords, tokens and private file contents.
