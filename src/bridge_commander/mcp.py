"""Minimal MCP server: JSON-RPC 2.0 over stdio and over Streamable HTTP (JSON responses)."""
import hmac
import ipaddress
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 32 * 1024 * 1024
INSTRUCTIONS = (
    "Bridge Commander gives access to files, search and terminal sessions on this machine"
    " and on configured nodes. Use absolute paths. Prefer edit_block for small changes,"
    " start_process + read_process_output for long commands. Call get_config to see limits."
)


class Server:
    def __init__(self, toolbox):
        self.toolbox = toolbox

    def handle(self, msg):
        """Handle one JSON-RPC message (or a batch). Returns the response, or None for notifications."""
        if isinstance(msg, list):
            replies = [r for r in (self.handle(m) for m in msg) if r is not None]
            return replies or None
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None  # a response to a request we never send; ignore
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None  # notification (initialized, cancelled, ...)
        try:
            result = self.dispatch(method, params)
        except KeyError as exc:
            return _error(rid, -32602, "unknown tool: %s" % exc.args[0]) if method == "tools/call" else \
                _error(rid, -32601, "method not found: %s" % method)
        except (TypeError, ValueError, AttributeError) as exc:
            return _error(rid, -32602, "invalid params: %s" % exc)
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def dispatch(self, method, params):
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "bridge-commander", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.toolbox.specs()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise ValueError("tools/call requires a tool name")
            content, is_error = self.toolbox.call(name, params.get("arguments") or {})
            return {"content": content, "isError": is_error}
        if method in ("resources/list", "prompts/list"):
            return {method.split("/")[0]: []}
        raise KeyError(method)


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _parse(raw):
    try:
        return json.loads(raw), None
    except ValueError:
        return None, _error(None, -32700, "parse error")


# -- stdio ---------------------------------------------------------------------
def serve_stdio(server, stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    lock = threading.Lock()

    def answer(raw):
        msg, err = _parse(raw)
        reply = err or server.handle(msg)
        if reply is not None:
            with lock:
                stdout.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                stdout.flush()

    # Each request gets a thread so a slow tool call does not block ping/cancel or other calls.
    pending = []
    for raw in iter(stdin.readline, b""):
        if raw.strip():
            pending = [t for t in pending if t.is_alive()]
            thread = threading.Thread(target=answer, args=(raw,), daemon=True)
            thread.start()
            pending.append(thread)
    for thread in pending:  # stdin closed: finish answering what was already asked
        thread.join()


# -- Streamable HTTP -----------------------------------------------------------
def make_http_server(server, host, port, token, allowed_origins=()):
    if not token:
        raise ValueError("the HTTP transport requires a token (BRIDGE_COMMANDER_TOKEN or http.tokenFile)")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "bridge-commander/" + __version__

        def log_message(self, fmt, *args):  # keep tokens in URLs out of logs
            sys.stderr.write("http %s %s\n" % (self.command, args[1] if len(args) > 1 else ""))

        def _send(self, code, body=None, ctype="application/json"):
            data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
            self.send_response(code)
            if data:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            header = self.headers.get("Authorization", "")
            supplied = header[7:].strip() if header.lower().startswith("bearer ") else None
            if path.startswith("/mcp/"):  # secret-URL form for clients that cannot send headers
                supplied, path = path[5:], "/mcp"
            if path != "/mcp":
                return "not_found"
            if supplied is None or not hmac.compare_digest(supplied.encode(), token.encode()):
                return "unauthorized"
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                return "forbidden"  # browsers: blocks DNS-rebinding and cross-site calls
            return None

        def _reject(self, problem):
            code = {"not_found": 404, "unauthorized": 401, "forbidden": 403}[problem]
            if code == 401:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer realm="bridge-commander"')
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(code, {"error": problem})

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/health":
                return self._send(200, {"status": "ok"})
            problem = self._authorized()
            if problem:
                return self._reject(problem)
            self._send(405, {"error": "this server does not offer an SSE stream; use POST"})

        def do_DELETE(self):
            problem = self._authorized()
            self._reject(problem) if problem else self._send(405)

        def do_POST(self):
            problem = self._authorized()
            if problem:
                return self._reject(problem)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._send(413 if length > MAX_BODY else 400, _error(None, -32600, "bad request body"))
            msg, err = _parse(self.rfile.read(length))
            reply = err or server.handle(msg)
            if reply is None:
                return self._send(202)
            self._send(200, json.dumps(reply, ensure_ascii=False).encode("utf-8"))

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
