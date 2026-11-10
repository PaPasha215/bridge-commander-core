# Free client beta 1.3.0

This public snapshot contains the Bridge Commander client and local MCP server. It uses the MIT license and Python standard library. Use a maintained Python 3.12 or newer on macOS/Linux.

Hosted service: https://bridgecommander.dev. OAuth MCP endpoint: https://bridgecommander.dev/mcp. Support: support@bridgecommander.dev.

The client stays running in a foreground terminal. Initialization creates a private, explicitly scoped read-only configuration. Enabling writes also permits commands with your OS user rights; folder rules are not a command sandbox. Run only one remote client per OS profile.

Verified evidence covers clean client compatibility tests (80 on macOS;79 and one optional PDF skip on Linux) and a real Linux flow through public HTTPS, delivered login email, device code approval, DCR/PKCE OAuth, scoped reading/search, authorized writes and duplicate rejection, actual command output/exit status, refresh rotation and access/account revocation. The disposable fixture was removed.

ChatGPT directory publication and fresh mobile/changed-catalog acceptance are separate, unfinished steps. An MCP client must support custom remote servers and OAuth. No general latency/SLA or native app distribution is promised by this beta.

Runtime source matches audited core commit65b3f10bb28f4e8e30f6d215c4d542c354dfcc1a; documentation/checksum snapshotf4806d1e246fd1bb6dc529068a62b638c1a46787. This export includes no internal Git history or service state. The public clone URL is adapted to this repository.
