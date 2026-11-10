"""Command line: bridge-commander serve | worker | remote | init | token | check | version."""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from . import __version__, remote
from .config import ConfigError, DEFAULTS, config_path, http_token, load
from .mcp import Server, is_loopback, make_http_server, serve_stdio
from .nodes import serve_worker
from .receipts import ReceiptError
from .tools import Toolbox


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bridge-commander", description=__doc__)
    parser.add_argument("--config", help="config file (default: $BRIDGE_COMMANDER_CONFIG or "
                                         "~/.config/bridge-commander/config.json)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("worker", help="run as a node worker for a hub (stdin/stdout JSON lines)")
    remote = sub.add_parser("remote", help="connect this machine to a Bridge Commander cloud (outgoing HTTPS only)")
    remote.add_argument("--cloud", help="cloud URL (default: $BRIDGE_COMMANDER_CLOUD or the saved one)")
    remote.add_argument("--fallback", action="append", default=None,
                        help="backup URL of the same cloud, used when the main one is unreachable (repeatable; "
                             "default: $BRIDGE_COMMANDER_CLOUD_FALLBACK, comma-separated, or the saved ones)")
    remote.add_argument("--name", help="device name shown in the dashboard (default: hostname)")
    remote.add_argument("--logout", action="store_true", help="forget this device's credentials and exit")
    remote.add_argument("--no-browser", action="store_true", help="print the sign-in link instead of opening it")
    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.add_argument("--directory", action="append", help="folder to share (repeatable; default: ~/BridgeFiles)")
    init.add_argument("--allow-writes", action="store_true", help="enable file changes and commands with your OS user rights")
    token = sub.add_parser("token", help="generate an HTTP access token")
    token.add_argument("--write", metavar="FILE", help="save it to FILE with mode 0600 instead of printing")
    sub.add_parser("check", help="validate the config and test every node")
    sub.add_parser("version")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "version":
            print(__version__)
        elif args.cmd == "init":
            init_config(config_path(args.config), args.force, args.directory, args.allow_writes)
        elif args.cmd == "token":
            new_token(args.write)
        else:
            run(args)
    except (ConfigError, ReceiptError) as exc:
        sys.exit("bridge-commander: config error: %s" % exc)
    except KeyboardInterrupt:
        pass


def run(args):
    if getattr(args, "logout", False):
        remote.CREDENTIALS.expanduser().unlink(missing_ok=True)
        print("device credentials removed (revoke the device in the dashboard to cut access at once)")
        return
    if args.cmd == "remote" and not config_path(args.config).is_file():
        raise ConfigError("create an explicit sharing config first: bridge-commander init --directory PATH "
                          "(read-only by default)")
    cfg = load(args.config)
    if args.cmd == "remote" and not cfg["allowedDirectories"]:
        raise ConfigError("remote requires at least one explicitly allowed directory")
    toolbox = Toolbox(cfg, config_path(args.config))
    try:
        if args.cmd == "worker":
            serve_worker(toolbox, sys.stdin.buffer, sys.stdout.buffer)
        elif args.cmd == "remote":
            run_remote(args, toolbox)
        elif args.cmd == "check":
            print("config OK: %s" % (config_path(args.config) if config_path(args.config).exists() else "defaults"))
            print("allowed directories: %s" % (toolbox.files.roots or "unrestricted"))
            print("read-only: %s" % cfg["readOnly"])
            print(toolbox.list_nodes())
        elif args.transport == "stdio":
            serve_stdio(Server(toolbox))
        else:
            host = args.host or cfg["http"]["host"]
            port = cfg["http"]["port"] if args.port is None else args.port
            token = http_token(cfg)
            if not token:
                raise ConfigError("HTTP needs a token: set BRIDGE_COMMANDER_TOKEN or http.tokenFile "
                                  "(create one with `bridge-commander token --write FILE`)")
            httpd = make_http_server(Server(toolbox), host, port, token, cfg["http"]["allowedOrigins"])
            if not is_loopback(host):
                print("warning: listening on %s, reachable from the network; keep the token secret" % host,
                      file=sys.stderr)
            print("bridge-commander %s: MCP endpoint http://%s:%d/mcp" % (__version__, host, httpd.server_port),
                  file=sys.stderr)
            try:
                httpd.serve_forever()
            finally:
                httpd.server_close()
    finally:
        toolbox.close()


def run_remote(args, toolbox):
    creds = remote.load_credentials()
    cloud = args.cloud or os.environ.get("BRIDGE_COMMANDER_CLOUD") or (creds or {}).get("cloud")
    if not cloud:
        raise ConfigError("remote needs a cloud URL: --cloud https://... or BRIDGE_COMMANDER_CLOUD")
    cloud = remote.check_cloud_url(cloud)
    env = os.environ.get("BRIDGE_COMMANDER_CLOUD_FALLBACK")
    fallback = args.fallback or (env.split(",") if env else None)
    if fallback is not None:
        fallback = [remote.check_fallback(cloud, u.strip()) for u in fallback if u.strip()]
    if not creds or creds.get("cloud") != cloud:
        routes = [cloud] + (fallback or [])
        via = (len(routes) > 1 and remote.pick_route(routes)) or cloud  # the main address may be blocked here
        creds = remote.sign_in(via, args.name, open_browser=not args.no_browser)
        creds["cloud"] = cloud
        remote.save_credentials(creds)
    if fallback is not None:
        if fallback != creds.get("fallback"):
            creds["fallback"] = fallback
            remote.save_credentials(creds)
    try:
        remote.serve(toolbox, creds)
    except remote.Revoked:
        remote.CREDENTIALS.expanduser().unlink(missing_ok=True)
        sys.exit("bridge-commander: this device was revoked in the dashboard; run `remote` again to reconnect")


def init_config(path, force, directories=None, allow_writes=False):
    if path.exists() and not force:
        sys.exit("bridge-commander: %s already exists (use --force to overwrite)" % path)
    cfg = {k: v for k, v in DEFAULTS.items() if k != "blockedCommands"}
    roots = [Path(d).expanduser().resolve() for d in directories or [Path.home() / "BridgeFiles"]]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    cfg["allowedDirectories"] = [str(root) for root in roots]
    cfg["readOnly"] = not allow_writes
    cfg["http"] = dict(cfg["http"], tokenFile=str(path.parent / "token"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print("wrote %s (blockedCommands uses the built-in default list)" % path)


def new_token(target):
    value = secrets.token_urlsafe(32)
    if not target:
        print(value)
        return
    path = Path(target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    print("token saved to %s" % path)
