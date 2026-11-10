"""Configuration: one JSON file, validated once at startup."""
import json
import os
import re
from pathlib import Path

DEFAULT_PATH = Path("~/.config/bridge-commander/config.json")

# Best-effort guard against obvious foot-guns. It is not a sandbox: see docs/SECURITY.md.
DEFAULT_BLOCKED = [
    "mkfs", "format", "mount", "umount", "fdisk", "dd", "parted", "diskpart",
    "sudo", "su", "doas", "passwd", "adduser", "useradd", "usermod", "groupadd",
    "chsh", "visudo", "shutdown", "reboot", "halt", "poweroff", "init",
    "iptables", "nft", "ufw", "netsh", "bcdedit", "takeown", "runas", "cipher",
]

DEFAULTS = {
    "allowedDirectories": ["~"],
    "readOnly": False,
    "blockedCommands": DEFAULT_BLOCKED,
    "defaultShell": None,
    "fileReadLineLimit": 1000,
    "maxFileSizeBytes": 10 * 1024 * 1024,
    "maxProcessOutputBytes": 1024 * 1024,
    "maxSessions": 20,
    "allowConfigChanges": False,
    "auditLog": "~/.local/state/bridge-commander/audit.jsonl",
    "http": {"host": "127.0.0.1", "port": 8765, "tokenFile": None, "allowedOrigins": []},
    "nodes": {},
}

# Keys that set_config_value may change at runtime (when allowConfigChanges is true).
MUTABLE = {"allowedDirectories", "readOnly", "blockedCommands", "defaultShell", "fileReadLineLimit"}
NODE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class ConfigError(Exception):
    pass


def config_path(path=None):
    return Path(path or os.environ.get("BRIDGE_COMMANDER_CONFIG") or DEFAULT_PATH).expanduser()


def load(path=None):
    file = config_path(path)
    data = {}
    if file.exists():
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("cannot read %s: %s" % (file, exc)) from None
    elif path:
        raise ConfigError("config file not found: %s" % file)
    return validate(data)


def validate(data):
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(data) - set(DEFAULTS) - {"$schema", "_comment"}
    if unknown:
        raise ConfigError("unknown config keys: %s" % ", ".join(sorted(unknown)))
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in data.items():
        if key == "http":
            if not isinstance(value, dict) or set(value) - set(DEFAULTS["http"]):
                raise ConfigError("http must be an object with keys %s" % sorted(DEFAULTS["http"]))
            cfg["http"].update(value)
        elif key in DEFAULTS:
            cfg[key] = value
    for key, value in cfg.items():
        check(key, value)
    return cfg


def check(key, value):
    def fail(why):
        raise ConfigError("%s: %s" % (key, why))

    if key in ("allowedDirectories", "blockedCommands"):
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            fail("must be a list of non-empty strings")
    elif key in ("readOnly", "allowConfigChanges"):
        if not isinstance(value, bool):
            fail("must be true or false")
    elif key in ("defaultShell", "auditLog"):
        if value is not None and not (isinstance(value, str) and value):
            fail("must be a string or null")
    elif key in ("fileReadLineLimit", "maxFileSizeBytes", "maxProcessOutputBytes", "maxSessions"):
        if type(value) is not int or value < 1:
            fail("must be a positive integer")
    elif key == "http":
        if not isinstance(value["host"], str) or type(value["port"]) is not int or not 0 <= value["port"] < 65536:
            fail("host must be a string and port an integer 0-65535")
        if value["tokenFile"] is not None and not isinstance(value["tokenFile"], str):
            fail("tokenFile must be a string or null")
        if not isinstance(value["allowedOrigins"], list):
            fail("allowedOrigins must be a list")
    elif key == "nodes":
        if not isinstance(value, dict):
            fail("must be an object")
        for name, node in value.items():
            if not NODE_NAME.match(name) or name == "local":
                fail("invalid node name %r (lowercase, not 'local')" % name)
            if not isinstance(node, dict) or set(node) - {"command", "description", "timeoutMs"}:
                fail("node %s must have 'command' and optional 'description', 'timeoutMs'" % name)
            cmd = node.get("command")
            if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
                fail("node %s command must be a non-empty list of strings" % name)
            if "timeoutMs" in node and (type(node["timeoutMs"]) is not int or node["timeoutMs"] < 1000):
                fail("node %s timeoutMs must be an integer >= 1000" % name)


def http_token(cfg):
    """Token for the HTTP transport: environment variable first, then tokenFile."""
    token = os.environ.get("BRIDGE_COMMANDER_TOKEN")
    if not token and cfg["http"]["tokenFile"]:
        try:
            token = Path(cfg["http"]["tokenFile"]).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("cannot read http.tokenFile: %s" % exc) from None
    if token and len(token) < 24:
        raise ConfigError("HTTP token must be at least 24 characters")
    return token or None
