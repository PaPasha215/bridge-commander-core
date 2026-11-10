# Changelog

## 1.3.0 — public-readiness candidate, 2026-11-10

- Metadata-only device receipts: claim and acknowledge before execution; preserve UNKNOWN after a crash and never automatically replay an effect.
- Bound concurrent work, retained output and receipt storage; require the matching durable Cloud protocol.
- First remote sign-in needs explicit folder configuration; init creates a private read-only sharing config.
- Bound text/batch reads, edits, spreadsheet XML/output and PDF converter output/time; reject entity declarations including UTF-16.
- Public macOS build requires Developer ID and accepted notarization; existing installations are not updated by a release build.

## 1.2.1 — 2026-11-10

- Content searches validate each discovered file against allowed roots, including symlinks.
- Read-only mode rejects force_terminate before any process signal.
- Device API requests reject HTTP redirects so Bearer tokens cannot reach another origin.

## 1.2.0 — 2026-10-31

- `bridge-commander remote --cloud URL`: connect a machine to a hosted Bridge Commander cloud with outgoing HTTPS
  only (device sign-in with a code confirmed in the browser, long-poll for calls). The device keeps applying its own
  config. Credentials are stored in `~/.config/bridge-commander/device.json` (0600); `--logout` forgets them.

## 1.1.0 — 2026-10-31

- Background search: `start_search`, `get_more_search_results` (paging, tail), `stop_search`, `list_searches`.
  Large trees no longer block a call; results stream in while the walk continues.
- `read_file` reads Word `.docx` (text, headings, tables), Excel `.xlsx`/`.xlsm` (rows, `sheet`), PDF (`pdftotext` when installed,
  otherwise the PDFKit built into macOS) and http(s) URLs (`is_url`).
- `edit_block` failures show the closest text and a character diff `{-sent-}{+in file+}`.
- `read_process_output` can show retained output by line or tail (`offset`, `length`) without consuming it.
- `get_recent_tool_calls` and `get_usage_stats` read the audit log, which now also keeps shortened arguments
  and the first line of each result (never file contents, written text or command output).
- URL reading refuses non-public addresses (also after redirects); Office XML with DTDs is refused.

## 1.0.0 — 2026-10-31

First public release.

- 20 MCP tools: files (read/write/append/edit_block/move/mkdir/list/info), search (names, contents),
  terminal sessions (start, interact, read output, terminate, list), system processes (list, kill),
  config (get, runtime set), nodes (list/ping).
- Transports: stdio; Streamable HTTP with a mandatory token (Bearer header or secret URL) and Origin checks.
- Remote nodes through a persistent `worker` over SSH or any command; per-node rules enforced on the node.
- Guards: allowed directories with symlink-safe resolution, read-only mode, blocked command list,
  output/size limits, JSONL audit log.
- No third-party dependencies; single-file `bridge-commander.pyz` build.
