"""Tool catalogue, argument validation, routing to nodes, and the audit log."""
import getpass
import json
import os
import platform
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from . import __version__
from .config import MUTABLE, check, ConfigError
from .files import Files, ToolError
from .nodes import Node
from .processes import Processes
from .searches import Searches

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
TIMEOUT = {"type": "integer", "minimum": 0, "maximum": 600000, "description": "Milliseconds to wait for output."}

# name: (description, properties, required, kind) — kind: read | write | exec
CATALOGUE = {
    "get_config": ("Show this server's configuration, version, host, user and configured nodes.", {}, [], "read"),
    "set_config_value": ("Change one setting at runtime (only when allowConfigChanges is true; not persisted). "
                         "Keys: allowedDirectories, readOnly, blockedCommands, defaultShell, fileReadLineLimit.",
                         {"key": S, "value": {"description": "New value (JSON)."}}, ["key", "value"], "write"),
    "list_nodes": ("List configured remote nodes and check that each answers.", {}, [], "read"),
    "read_file": ("Read a file: text, images (PNG/JPEG/GIF/WebP), Word .docx (text, headings, tables), Excel "
                  ".xlsx (rows as tab-separated values; sheet = name or 0-based index), PDF (text; built in on macOS, "
                  "pdftotext elsewhere). With is_url=true, path is an http(s) URL to fetch. offset = lines to skip; negative "
                  "offset reads the last N lines. length = max lines (capped by fileReadLineLimit).",
                  {"path": S, "offset": I, "length": {"type": "integer", "minimum": 1}, "is_url": B, "sheet": S},
                  ["path"], "read"),
    "read_multiple_files": ("Read several text files at once.",
                            {"paths": {"type": "array", "items": S, "maxItems": 50}}, ["paths"], "read"),
    "write_file": ("Write a file (mode=rewrite replaces it atomically, mode=append adds to the end). "
                   "Creates parent directories. For big files write in chunks with append.",
                   {"path": S, "content": S, "mode": {"type": "string", "enum": ["rewrite", "append"]}},
                   ["path", "content"], "write"),
    "edit_block": ("Replace exact text in a file. old_string must match exactly and occur expected_replacements "
                   "times (default 1). Include enough context to make it unique.",
                   {"file_path": S, "old_string": S, "new_string": S,
                    "expected_replacements": {"type": "integer", "minimum": 1}},
                   ["file_path", "old_string", "new_string"], "write"),
    "create_directory": ("Create a directory (and parents).", {"path": S}, ["path"], "write"),
    "list_directory": ("List a directory as [DIR]/[FILE] lines, recursing up to depth levels (default 2).",
                       {"path": S, "depth": {"type": "integer", "minimum": 1, "maximum": 10}}, ["path"], "read"),
    "move_file": ("Move or rename a file or directory. Fails if the destination exists.",
                  {"source": S, "destination": S}, ["source", "destination"], "write"),
    "get_file_info": ("Size, type, timestamps, permissions and line count of a path.", {"path": S}, ["path"], "read"),
    "search_files": ("Find files and directories whose name contains pattern (or matches a glob like *.py). "
                     "Skips hidden dirs, .git and node_modules unless include_hidden.",
                     {"path": S, "pattern": S, "include_hidden": B, "max_results": {"type": "integer", "minimum": 1},
                      "timeout_ms": TIMEOUT}, ["path", "pattern"], "read"),
    "search_code": ("Search file contents with a regular expression (or literal=true for plain text). "
                    "Returns path:line:text. file_pattern filters names, e.g. *.ts.",
                    {"path": S, "pattern": S, "file_pattern": S, "ignore_case": B, "literal": B,
                     "context_lines": {"type": "integer", "minimum": 0, "maximum": 10}, "include_hidden": B,
                     "max_results": {"type": "integer", "minimum": 1}, "timeout_ms": TIMEOUT},
                    ["path", "pattern"], "read"),
    "start_search": ("Start a background search and return the first results within timeout_ms (default 3000). "
                     "search_type=files matches names (substring or glob like *.py); search_type=content searches "
                     "inside files (regex, or literal=true). Use for big trees: page with get_more_search_results, "
                     "end early with stop_search.",
                     {"path": S, "pattern": S, "search_type": {"type": "string", "enum": ["files", "content"]},
                      "file_pattern": S, "ignore_case": B, "literal": B,
                      "context_lines": {"type": "integer", "minimum": 0, "maximum": 10}, "include_hidden": B,
                      "max_results": {"type": "integer", "minimum": 1, "maximum": 100000}, "timeout_ms": TIMEOUT},
                     ["path", "pattern"], "read"),
    "get_more_search_results": ("Read results of a search started with start_search. offset = first result line "
                                "(negative = last N lines), length = how many (default 100, max 1000).",
                                {"search_id": S, "offset": I, "length": {"type": "integer", "minimum": 1}},
                                ["search_id"], "read"),
    "stop_search": ("Stop a running search; its results stay readable for 10 minutes.", {"search_id": S},
                    ["search_id"], "read"),
    "list_searches": ("List searches with their status and number of hits.", {}, [], "read"),
    "start_process": ("Run a shell command in a new session. Returns its PID, status and the output produced within "
                      "timeout_ms (default 5000). Long-running or interactive programs keep running: continue with "
                      "read_process_output / interact_with_process. For Python REPLs use `python3 -i`.",
                      {"command": S, "timeout_ms": TIMEOUT, "cwd": S, "shell": S}, ["command"], "exec"),
    "interact_with_process": ("Send a line of input to a session started by start_process and return the new output.",
                              {"pid": I, "input": S, "timeout_ms": TIMEOUT}, ["pid", "input"], "exec"),
    "read_process_output": ("Return new output of a session, waiting up to timeout_ms for some to appear. "
                            "With offset, show retained output by line instead (negative offset = last N lines) "
                            "without consuming it; length caps the lines.",
                            {"pid": I, "timeout_ms": TIMEOUT, "offset": I, "length": {"type": "integer", "minimum": 1}},
                            ["pid"], "read"),
    "force_terminate": ("Terminate a session started by start_process (and its child processes).",
                        {"pid": I}, ["pid"], "exec"),
    "list_sessions": ("List sessions started by start_process.", {}, [], "read"),
    "list_processes": ("List running system processes, optionally filtered by a substring.",
                       {"filter": S}, [], "read"),
    "kill_process": ("Send SIGTERM (or SIGKILL with force=true) to any process by PID.",
                     {"pid": I, "force": B}, ["pid"], "exec"),
    "get_recent_tool_calls": ("Recent tool calls on this node from the audit log: time, tool, arguments (shortened), "
                              "result start, success. Use it to recover context in a new chat. Filter by tool_name "
                              "and since (ISO time, e.g. 2026-10-31T12:00).",
                              {"max_results": {"type": "integer", "minimum": 1, "maximum": 1000}, "tool_name": S,
                               "since": S}, [], "read"),
    "get_usage_stats": ("Usage statistics from the audit log: calls, failures and average duration per tool and "
                        "node.", {}, [], "read"),
}
UNAUDITED = {"get_recent_tool_calls", "get_usage_stats"}
# The audit log keeps arguments and the first line of results, but never file contents or command output.
REDACTED_ARGS = {"content", "old_string", "new_string", "input", "value"}
CONTENT_RESULTS = {"read_file", "read_multiple_files", "read_process_output", "interact_with_process",
                   "get_more_search_results", "start_search", "search_code"}
AUDIT_SCAN_BYTES = 8 * 1024 * 1024


def tool_specs(node_names=()):
    specs = []
    for name, (desc, props, required, kind) in CATALOGUE.items():
        props = dict(props)
        if node_names and name not in ("list_nodes",):
            props["node"] = {"type": "string", "enum": ["local"] + list(node_names),
                             "description": "Where to run: 'local' (default) or a configured node."}
        specs.append({
            "name": name,
            "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
            "annotations": {"readOnlyHint": kind == "read", "destructiveHint": kind != "read",
                            "idempotentHint": kind == "read", "openWorldHint": name in {"read_file", "start_process", "interact_with_process"}},
        })
    return specs


def validate_args(name, args):
    if name not in CATALOGUE:
        raise KeyError(name)
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    args = {k: v for k, v in args.items() if v is not None}  # some clients send null for omitted options
    _, props, required, _ = CATALOGUE[name]
    extra = set(args) - set(props) - {"node"}
    if extra:
        raise ToolError("unknown argument(s): %s" % ", ".join(sorted(extra)))
    missing = [r for r in required if r not in args]
    if missing:
        raise ToolError("missing argument(s): %s" % ", ".join(missing))
    for key, value in args.items():
        rule = props.get(key, S)
        want = rule.get("type")
        ok = {"string": isinstance(value, str), "integer": type(value) is int, "boolean": type(value) is bool,
              "array": isinstance(value, list) and all(isinstance(v, str) for v in value), None: True}[want]
        if not ok:
            raise ToolError("argument %s must be of type %s" % (key, want))
        if "enum" in rule and value not in rule["enum"]:
            raise ToolError("argument %s must be one of %s" % (key, rule["enum"]))
        if want == "integer" and not rule.get("minimum", -2**63) <= value <= rule.get("maximum", 2**63):
            raise ToolError("argument %s is out of range" % key)
    return args


class Toolbox:
    def __init__(self, cfg, config_file=None):
        self.cfg = cfg
        self.config_file = config_file
        self.files = Files(cfg)
        self.processes = Processes(self.files)
        self.searches = Searches(self.files)
        self.nodes = {name: Node(name, spec) for name, spec in cfg["nodes"].items()}
        self.audit_lock = threading.Lock()

    def specs(self):
        return tool_specs(sorted(self.nodes))

    def call(self, name, args, from_hub=False):
        """Run one tool. Returns (content list, is_error). Raises KeyError for unknown tools."""
        started = time.monotonic()
        node = "local"
        try:
            args = validate_args(name, args)
            node = args.pop("node", "local")
            if node != "local":
                if from_hub:
                    raise ToolError("this worker does not forward to other nodes")
                if node not in self.nodes:
                    raise ToolError("unknown node %r" % node)
                content, is_error = self.nodes[node].call(name, args)
            else:
                content, is_error = self._local(name, args), False
        except ToolError as exc:
            content, is_error = [{"type": "text", "text": "Error: %s" % exc}], True
        except KeyError:
            raise
        except Exception as exc:  # unexpected bug: report instead of killing the server
            content, is_error = [{"type": "text", "text": "Error: %s: %s" % (type(exc).__name__, exc)}], True
        if name not in UNAUDITED:
            self._audit(name, node, args, not is_error, started, content)
        return content, is_error

    def _local(self, name, args):
        if name == "get_config":
            result = self.get_config()
        elif name == "set_config_value":
            result = self.set_config_value(**args)
        elif name in ("list_nodes", "get_recent_tool_calls", "get_usage_stats"):
            result = getattr(self, name)(**args)
        elif hasattr(self.files, name):
            result = getattr(self.files, name)(**args)
        elif hasattr(self.searches, name):
            result = getattr(self.searches, name)(**args)
        else:
            result = getattr(self.processes, name)(**args)
        return [{"type": "text", "text": result}] if isinstance(result, str) else result

    def get_config(self):
        info = {
            "version": __version__,
            "hostname": socket.gethostname(),
            "user": _user(),
            "platform": "%s %s (%s)" % (platform.system(), platform.release(), platform.machine()),
            "python": sys.version.split()[0],
            "configFile": str(self.config_file) if self.config_file else None,
            "effectiveAllowedDirectories": self.files.roots or "unrestricted",
            "defaultShellCommand": self.processes.shell_argv("<command>")[:-1],
            "nodes": {n: node.description for n, node in self.nodes.items()},
        }
        info.update({k: v for k, v in self.cfg.items() if k not in ("nodes",)})
        return json.dumps(info, indent=2, ensure_ascii=False)

    def set_config_value(self, key, value):
        if not self.cfg["allowConfigChanges"]:
            raise ToolError("runtime config changes are disabled (allowConfigChanges=false); edit the config file")
        if key not in MUTABLE:
            raise ToolError("key %r cannot be changed at runtime; allowed: %s" % (key, sorted(MUTABLE)))
        try:
            check(key, value)
        except ConfigError as exc:
            raise ToolError(str(exc))
        self.cfg[key] = value
        self.files.refresh()
        return "%s = %s (until restart)" % (key, json.dumps(value))

    def list_nodes(self):
        if not self.nodes:
            return "No remote nodes configured; everything runs locally."
        rows = []
        for name, node in sorted(self.nodes.items()):
            try:
                content, is_error = node.call("get_config", {})
                info = json.loads(content[0]["text"]) if not is_error else {}
                state = "ok: %s@%s, %s, v%s" % (info.get("user"), info.get("hostname"), info.get("platform"),
                                                  info.get("version")) if info else content[0]["text"]
            except (ToolError, ValueError, KeyError, IndexError) as exc:
                state = "unavailable: %s" % exc
            rows.append("%s — %s%s" % (name, state, " (%s)" % node.description if node.description else ""))
        return "\n".join(rows)

    def _audit(self, name, node, args, ok, started, content=()):
        path = self.cfg["auditLog"]
        if not path:
            return
        target = next((args[k] for k in ("path", "file_path", "source", "command", "pid", "key", "search_id")
                       if k in args), None)
        entry = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "tool": name, "node": node,
                 "ok": ok, "ms": int((time.monotonic() - started) * 1000)}
        if target is not None:
            entry["target"] = str(target)[:200]
        entry["args"] = {k: ("<%d chars>" % len(str(v)) if k in REDACTED_ARGS else v[:200] if isinstance(v, str) else v)
                         for k, v in args.items()
                         if k in REDACTED_ARGS or isinstance(v, (str, int, bool)) or (isinstance(v, list) and len(v) <= 20)}
        text = next((c.get("text", "") for c in content if c.get("type") == "text"), "")
        if text and (name not in CONTENT_RESULTS or not ok):
            entry["result"] = text.splitlines()[0][:200] if text.strip() else ""
        try:
            path = os.path.expanduser(path)
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with self.audit_lock:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # auditing must never break a tool call

    def _audit_entries(self):
        path = self.cfg["auditLog"]
        if not path:
            raise ToolError("the audit log is disabled on this node (auditLog=null)")
        path = os.path.expanduser(path)
        try:
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size > AUDIT_SCAN_BYTES:
                    f.seek(size - AUDIT_SCAN_BYTES)
                    f.readline()  # skip the partial first line
                raw = f.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ToolError("cannot read audit log: %s" % exc)
        entries = []
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and "tool" in entry:
                entries.append(entry)
        return entries

    def get_recent_tool_calls(self, max_results=50, tool_name=None, since=None):
        entries = [e for e in self._audit_entries() if (not tool_name or e.get("tool") == tool_name)
                   and (not since or str(e.get("ts", "")) >= since)][-max_results:]
        if not entries:
            return "No recorded calls."
        rows = []
        for e in entries:
            rows.append("%s | %s | %s | %s | %d ms" % (e.get("ts"), e.get("node", "local"), e.get("tool"),
                                                      "ok" if e.get("ok") else "ERROR", e.get("ms", 0)))
            if e.get("args"):
                rows.append("  args: %s" % json.dumps(e["args"], ensure_ascii=False)[:400])
            elif e.get("target"):
                rows.append("  target: %s" % e["target"])
            if e.get("result"):
                rows.append("  result: %s" % e["result"].replace("\n", " ⏎ ")[:300])
        return "%d call(s) on %s, oldest first ('local' = handled on this machine)\n%s" % (
            len(entries), socket.gethostname(), "\n".join(rows))

    def get_usage_stats(self):
        entries = self._audit_entries()
        if not entries:
            return "No recorded calls."
        by_tool, by_node = {}, {}
        for e in entries:
            for key, table in ((e.get("tool"), by_tool), (e.get("node", "local"), by_node)):
                row = table.setdefault(key, [0, 0, 0])
                row[0] += 1
                row[1] += 0 if e.get("ok") else 1
                row[2] += e.get("ms", 0)
        lines = ["%d calls on %s from %s to %s (this machine's audit log, up to the last %d MB; "
                 "'local' = calls handled here)" % (len(entries), socket.gethostname(), entries[0].get("ts"),
                                                    entries[-1].get("ts"), AUDIT_SCAN_BYTES // 2**20), "", "By tool:"]
        for title, table in (("tool", by_tool), ("node", by_node)):
            if title == "node":
                lines += ["", "By node:"]
            for key, (calls, failed, ms) in sorted(table.items(), key=lambda kv: -kv[1][0]):
                lines.append("  %-24s %6d calls  %5d failed  %7d ms avg" % (key, calls, failed, ms // calls))
        return "\n".join(lines)

    def close(self):
        self.searches.close()
        self.processes.close()
        for node in self.nodes.values():
            node.close()


def _user():
    try:
        return getpass.getuser()
    except Exception:
        return None
