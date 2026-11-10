import json
import os
import re
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request

from helpers import SRC, Sandbox, worker_command, write_json

from bridge_commander.mcp import Server, make_http_server

TOKEN = "t" * 32


def rpc(rid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class Protocol(Sandbox):
    def setUp(self):
        super().setUp()
        self.server = Server(self.box)

    def test_lifecycle(self):
        init = self.server.handle(rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(self.server.handle(rpc(1, "initialize", {"protocolVersion": "1999-01-01"}))
                         ["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = self.server.handle(rpc(2, "tools/list"))["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertTrue({"read_file", "edit_block", "start_process", "search_code", "list_nodes"} <= names)
        self.assertNotIn("node", tools[1]["inputSchema"]["properties"])  # no nodes configured
        self.assertEqual(self.server.handle(rpc(3, "ping"))["result"], {})

    def test_errors(self):
        self.assertEqual(self.server.handle(rpc(1, "nope"))["error"]["code"], -32601)
        self.assertEqual(self.server.handle(rpc(1, "tools/call", {"name": "nope"}))["error"]["code"], -32602)
        self.assertEqual(self.server.handle(rpc(1, "tools/call", {}))["error"]["code"], -32602)
        self.assertEqual(self.server.handle({"id": 1})["error"]["code"], -32600)
        result = self.server.handle(rpc(1, "tools/call", {"name": "read_file", "arguments": {}}))["result"]
        self.assertTrue(result["isError"])
        batch = self.server.handle([rpc(1, "ping"), {"jsonrpc": "2.0", "method": "x"}, rpc(2, "ping")])
        self.assertEqual([r["id"] for r in batch], [1, 2])


class Stdio(unittest.TestCase):
    def test_subprocess_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = write_json(os.path.join(tmp, "c.json"), {"allowedDirectories": [tmp], "auditLog": None})
            self.roundtrip(dict(os.environ, PYTHONPATH=SRC, BRIDGE_COMMANDER_CONFIG=cfg))

    def roundtrip(self, env):
        lines = [rpc(1, "initialize", {"protocolVersion": "2025-03-26"}),
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 rpc(2, "tools/call", {"name": "get_config", "arguments": {"bogus": 1}}),
                 rpc(3, "tools/call", {"name": "list_sessions", "arguments": {}})]
        out = subprocess.run([sys.executable, "-m", "bridge_commander", "serve"], env=env, timeout=20,
                             input="".join(json.dumps(l) + "\n" for l in lines) + "not json\n",
                             capture_output=True, text=True).stdout
        replies = {r.get("id"): r for r in map(json.loads, out.splitlines())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "bridge-commander")
        self.assertTrue(replies[2]["result"]["isError"])  # unknown argument
        self.assertIn("No sessions", replies[3]["result"]["content"][0]["text"])
        self.assertEqual(replies[None]["error"]["code"], -32700)


class Http(Sandbox):
    def setUp(self):
        super().setUp()
        self.httpd = make_http_server(Server(self.box), "127.0.0.1", 0, TOKEN, ["https://allowed.example"])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def test_auth(self):
        bearer = {"Authorization": "Bearer " + TOKEN}
        self.assertEqual(self.post("/mcp", rpc(1, "ping"))[0], 401)
        self.assertEqual(self.post("/mcp", rpc(1, "ping"), {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.post("/mcp/wrong", rpc(1, "ping"))[0], 401)
        self.assertEqual(self.post("/other", rpc(1, "ping"), bearer)[0], 404)
        self.assertEqual(self.post("/mcp", rpc(1, "ping"), dict(bearer, Origin="https://evil.example"))[0], 403)
        self.assertEqual(self.post("/mcp", rpc(1, "ping"), dict(bearer, Origin="https://allowed.example"))[0], 200)
        status, body = self.post("/mcp", rpc(7, "ping"), bearer)
        self.assertEqual((status, body["id"]), (200, 7))
        status, body = self.post("/mcp/" + TOKEN, rpc(8, "tools/list"))
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["tools"])
        self.assertEqual(self.post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, bearer)[0], 202)

    def test_tool_call_and_health(self):
        with urllib.request.urlopen(self.base + "/health", timeout=5) as resp:
            self.assertEqual(json.loads(resp.read()), {"status": "ok"})
        status, body = self.post("/mcp/" + TOKEN, rpc(1, "tools/call", {
            "name": "write_file", "arguments": {"path": self.p("h.txt"), "content": "via http"}}))
        self.assertFalse(body["result"]["isError"])
        with open(self.p("h.txt")) as f:
            self.assertEqual(f.read(), "via http")

    def test_token_required(self):
        with self.assertRaises(ValueError):
            make_http_server(Server(self.box), "127.0.0.1", 0, None)


class Nodes(Sandbox):
    def setUp(self):
        super().setUp()
        self.remote_root = os.path.join(self.tmp, "remote")
        os.mkdir(self.remote_root)
        worker_cfg = write_json(os.path.join(self.tmp, "worker.json"),
                                {"allowedDirectories": [self.remote_root], "auditLog": None})
        self.extra_nodes = {"box": {"command": worker_command(worker_cfg), "description": "test worker"},
                            "dead": {"command": [sys.executable, "-c", "import sys; sys.stderr.write('no route\\n')"]}}
        self.box.close()
        self.cfg["nodes"] = self.extra_nodes
        from bridge_commander.tools import Toolbox
        self.box = Toolbox(self.cfg)

    def test_forwarding(self):
        specs = {s["name"]: s for s in self.box.specs()}
        self.assertEqual(specs["read_file"]["inputSchema"]["properties"]["node"]["enum"], ["local", "box", "dead"])
        path = os.path.join(self.remote_root, "r.txt")
        self.ok("write_file", node="box", path=path, content="remote!")
        self.assertEqual(self.ok("read_file", node="box", path=path), "remote!")
        # The worker enforces its own allowed directories, not the hub's.
        self.assertIn("outside allowed", self.err("read_file", node="box", path=self.p("x")))
        self.assertIn("outside allowed", self.err("read_file", path=path))

    def test_remote_sessions_persist(self):
        text = self.ok("start_process", node="box", command="%s -i -q" % sys.executable, timeout_ms=5000)
        pid = int(re.search(r"PID (\d+)", text).group(1))
        self.assertIn("42", self.ok("interact_with_process", node="box", pid=pid, input="print(6*7)"))
        self.assertIn(str(pid), self.ok("list_sessions", node="box"))
        self.ok("force_terminate", node="box", pid=pid)

    def test_node_status(self):
        text = self.ok("list_nodes")
        self.assertRegex(text, r"box — ok: .*v1\.\d+\.\d+ \(test worker\)")
        self.assertIn("dead — unavailable: node dead closed the connection: no route", text)
        self.assertIn("unknown node", self.err("list_directory", node="nowhere", path="/"))

    def test_worker_restarts_after_crash(self):
        self.ok("get_config", node="box")
        self.box.nodes["box"].proc.kill()
        self.box.nodes["box"].proc.wait()
        self.ok("get_config", node="box")


if __name__ == "__main__":
    unittest.main()
