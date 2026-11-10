# Security model

Bridge Commander lets a language model run commands and change files. Treat access to it like SSH access
for the user it runs as.

## What each control does — and does not do

| control | protects | does **not** protect |
|---|---|---|
| `allowedDirectories` | File tools (`read_file`, `write_file`, `edit_block`, `list_directory`, search, move…). Paths are resolved with `realpath`, so symlinks and `..` cannot leave the allowed roots. Also bounds the `cwd` of `start_process`. | Commands. `start_process` runs a shell; `cat /etc/passwd` works if your user can read it. |
| `readOnly` | Refuses every mutating tool: writes, edits, moves, `start_process`, `interact_with_process`, `kill_process`, `force_terminate`, `set_config_value`. | Nothing is executed in read-only mode, so there is no gap here except what reading itself reveals. |
| `blockedCommands` | Stops obvious mistakes (`sudo`, `mkfs`, `shutdown`, …) by checking the first word of each segment split on `; && || | & $( \``. | A determined model can bypass it (`python -c`, `env`, scripts). It is a seat belt. |
| HTTP token | Only clients that know it can call the HTTP endpoint (Bearer header or secret URL `/mcp/<token>`), compared in constant time. Minimum 24 characters. | Anyone who learns the token or the secret URL. Rotate it by writing a new token file and restarting. |
| `allowedOrigins` | Requests from web pages (with an `Origin` header) are rejected unless listed — prevents DNS-rebinding and cross-site calls to a local server. | Non-browser clients, which send no `Origin`. |
| Per-node config | Each worker enforces its own rules on its own machine; the hub cannot widen them. | A compromised hub still reaches whatever each node allows. |
| Audit log | One line per call (tool, node, success, time, first 200 chars of the target path/command). File mode 0600. | It is local and appendable by the same user; ship it elsewhere if you need tamper evidence. Command lines may contain secrets you typed. |

## Recommendations

1. Run it as a **dedicated OS user** that owns only what the assistant should touch.
2. Start with `"readOnly": true` on machines where you only need to look.
3. Keep `allowedDirectories` narrow (a projects folder, not `/` or `~`).
4. Keep the HTTP listener on `127.0.0.1` and publish it only through an authenticated tunnel. HTTP mode refuses
   to start without a token.
5. Never paste the token or a `/mcp/<token>` URL into chats, issues or screenshots.
6. Leave `allowConfigChanges` off unless you want the model to widen its own access.
7. Review the audit log now and then.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository rather than a public issue.

## URL reading and the audit log (1.1.0)

- `read_file` with `is_url=true` fetches only http(s) URLs. The address is checked when the connection is made
  (so redirects and DNS rebinding are covered), IPv4-mapped IPv6 is unwrapped first, and loopback, private,
  link-local (cloud metadata), reserved and multicast addresses are refused. Proxies from the environment are not
  used. A read-only node therefore cannot be used to reach its own local services. On nodes where
  `start_process` is allowed, commands can reach anything anyway.
- On Windows, if certificate verification fails because the root certificate is not installed yet, one HEAD
  request to `https://host:port/` of the same, already checked public host is made through PowerShell (Windows
  installs roots on first use; the address is passed in an environment variable, never as script text, and
  redirects are off), then the fetch is retried once.
- Office files are parsed with the standard library; documents that declare a DTD or entities are refused.
- The audit log stores tool names, arguments and the first line of each result. File contents, text written or
  replaced (`content`, `old_string`, `new_string`), process input and the output of reads, searches and sessions
  are not stored; only their length or nothing. The file is created with mode 0600.


## Security fixes 1.2.1

Content searches revalidate every discovered file, including symlinks, against the configured roots.
This is a path boundary check, not OS isolation against a hostile concurrent filesystem writer.
Remote device POSTs reject all HTTP redirects; configured fallback routes are selected explicitly.
Read-only mode rejects force_terminate even for an existing managed session.
Cloud authorization, quotas, transport and public-release acceptance are maintained in the separate
Cloud repository; an updated device client alone does not establish Cloud security or public readiness.
