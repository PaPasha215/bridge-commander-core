# Free client beta 1.3.1

This public snapshot contains the Bridge Commander client and local MCP server. It uses the MIT license and Python standard library. Use a maintained Python 3.12 or newer on macOS/Linux.

Hosted service: https://bridgecommander.dev. OAuth MCP endpoint: https://bridgecommander.dev/mcp. Support: support@bridgecommander.dev.

The client stays running in a foreground terminal. Initialization creates a private, explicitly scoped read-only configuration. Enabling writes also permits commands with your OS user rights; folder rules are not a command sandbox. Run only one remote client per OS profile.

Client1.3.1 reports UTC time, HTTP status and retry delay for failed device polls. This diagnostic omits response bodies and credentials and preserves the existing retry, authorization and execution rules. It does not repair an external provider's access denial.

Client1.3.1 passed81 macOS tests, including sanitized HTTP failure diagnostics. Earlier1.3.0 evidence covers compatibility tests (80 on macOS;79 and one optional PDF skip on Linux) and a real Linux flow through public HTTPS, delivered login email, device code approval, DCR/PKCE OAuth, scoped reading/search, authorized writes and duplicate rejection, actual command output/exit status, refresh rotation and access/account revocation. The disposable fixture was removed.

The refreshed existing Cloud connection passed a keyed scratch write, duplicate/conflict protection, exact read-back, durable status lookup and a command with observed output/exit plus cleanup. The owner also reported a successful fresh iPad inventory/file read through Bridge without RDC. These are existing-account checks, not independent acceptance of every mobile device or a saved public submission. ChatGPT Directory publication and saved-submission acceptance remain unfinished. An MCP client must support custom remote servers and OAuth. No general latency/SLA or native app distribution is promised by this beta.

Runtime derives from audited core commit65b3f10bb28f4e8e30f6d215c4d542c354dfcc1a; client1.3.1 adds the HTTP poll diagnostic above. Initial documentation/checksum snapshotf4806d1e246fd1bb6dc529068a62b638c1a46787. This export includes no internal Git history or service state. The public clone URL is adapted to this repository.
