import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import helpers  # noqa: F401  (puts src on sys.path)
from bridge_commander import remote


class RemoteBasics(unittest.TestCase):
    def test_device_token_is_not_forwarded_through_http_redirects(self):
        received = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        target = HTTPServer(("127.0.0.1", 0), Target)

        class Redirect(Target):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/collect" % target.server_port)
                self.send_header("Content-Length", "0")
                self.end_headers()

        source = HTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, source):
            threading.Thread(target=lambda s=server: s.serve_forever(poll_interval=0.01), daemon=True).start()
        try:
            status, _ = remote._post("http://127.0.0.1:%d/device/poll" % source.server_port,
                                     {}, token="AUDIT_DEVICE_TOKEN", timeout=2)
            self.assertEqual(received, [])
            self.assertEqual(status, 302)
        finally:
            for server in (source, target):
                server.shutdown()
                server.server_close()

    def test_cloud_url_must_be_https_except_localhost(self):
        self.assertEqual(remote.check_cloud_url("https://cloud.example/"), "https://cloud.example")
        self.assertEqual(remote.check_cloud_url("http://localhost:8800"), "http://localhost:8800")
        for bad in ("http://cloud.example", "ftp://x", ""):
            with self.assertRaises(SystemExit):
                remote.check_cloud_url(bad)

    def test_credentials_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "device.json")
            remote.save_credentials({"cloud": "https://c", "device_token": "t"}, path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(remote.load_credentials(path)["device_token"], "t")
            self.assertIsNone(remote.load_credentials(os.path.join(tmp, "missing.json")))

    def test_device_info(self):
        info = remote.device_info("box")
        self.assertEqual(info["name"], "box")
        self.assertIn("version", info)

    def test_fallback_must_be_same_domain(self):
        ok = remote.check_fallback("https://bridgecommander.dev", "https://edge.bridgecommander.dev/")
        self.assertEqual(ok, "https://edge.bridgecommander.dev")
        self.assertEqual(remote.check_fallback("https://direct.c.dev", "https://c.dev"), "https://c.dev")
        for bad in ("https://evil.example", "https://bridgecommander.dev.evil.example", "http://edge.bridgecommander.dev"):
            with self.assertRaises(SystemExit):
                remote.check_fallback("https://bridgecommander.dev", bad)


class FakeCloud:
    """Stands in for _post: `down` routes raise like a blocked network, others serve one call then idle."""
    def __init__(self, down, stop):
        self.down, self.stop, self.calls, self.results = set(down), stop, [], []
        self.served = False

    def __call__(self, url, body, token=None, timeout=None):
        body = json.loads(body) if isinstance(body, bytes) else body
        base, path = url.rsplit("/device/", 1)
        self.calls.append((base, path))
        if base in self.down:
            raise OSError("timed out")
        if path == "result":
            self.results.append((base, body["id"]))
            return 200, {}
        if not self.served:
            self.served = True
            now = time.time()
            return 200, {"calls": [{"id": "c1", "tool": "get_config", "args": {},
                                    "created_at": now, "expires_at": now + 60}]}
        return 200, {"calls": []}


class Box:
    def call(self, tool, args, from_hub=False):
        return [{"type": "text", "text": "ok"}], False


class Failover(unittest.TestCase):
    P, F = "https://bridgecommander.dev", "https://edge.bridgecommander.dev"

    def run_serve(self, cloud, check, polls, clock=None):
        stop = threading.Event()
        cloud.stop = stop
        orig = cloud.__call__

        def post(*a, **kw):
            if sum(1 for c in cloud.calls if c[1] == "poll") >= polls:
                stop.set()
            return orig(*a, **kw)
        log = []
        out = mock.Mock(write=lambda s: log.append(s), flush=lambda: None)
        with mock.patch.object(remote, "_post", post):
            remote.serve(Box(), {"cloud": self.P, "fallback": [self.F], "device_token": "t"}, out=out, stop=stop,
                         check=check, clock=clock or (lambda: 0), journal=remote.ReceiptJournal(":memory:", "test"))
        for _ in range(50):  # result threads
            if cloud.results:
                break
            threading.Event().wait(0.02)
        return "".join(log)

    def test_switches_to_fallback_when_primary_is_blocked(self):
        cloud = FakeCloud(down=[self.P], stop=None)
        healthy = {self.P, self.F}  # the primary looks fine at start, then gets blocked
        log = self.run_serve(cloud, check=lambda u: u in healthy and (u == self.F or not cloud.calls), polls=3)
        self.assertEqual(cloud.calls[0], (self.P, "poll"))
        self.assertEqual(cloud.calls[1], (self.F, "poll"))
        self.assertIn((self.F, "c1"), cloud.results)
        self.assertIn("route: %s -> %s" % (self.P, self.F), log)

    def test_returns_to_primary_when_it_is_back(self):
        cloud = FakeCloud(down=[self.P], stop=None)
        now = [0]

        def check(url):
            return url == self.F or not cloud.down

        def clock():
            now[0] += remote.PRIMARY_RECHECK  # every tick is "later"
            return now[0]
        orig = cloud.__call__

        def heal(*a, **kw):  # primary comes back after the first switch
            if len(cloud.calls) >= 2:
                cloud.down.clear()
            return orig(*a, **kw)
        cloud.__call__ = heal
        log = self.run_serve(cloud, check=check, polls=4, clock=clock)
        self.assertIn("primary is back", log)
        self.assertEqual(cloud.calls[-1][0], self.P)

    def test_starts_on_the_backup_when_the_primary_is_blocked(self):
        cloud = FakeCloud(down=[self.P], stop=None)
        self.run_serve(cloud, check=lambda u: u == self.F, polls=2)
        self.assertEqual(cloud.calls[0], (self.F, "poll"))

    def test_without_fallback_it_just_retries(self):
        stop = threading.Event()
        attempts = []

        def post(url, *a, **kw):
            attempts.append(url)
            stop.set()
            raise OSError("down")
        with mock.patch.object(remote, "_post", post):
            remote.serve(Box(), {"cloud": self.P, "device_token": "t"}, out=mock.Mock(), stop=stop,
                         check=lambda u: self.fail("no health checks without fallback"),
                         journal=remote.ReceiptJournal(":memory:", "test"))
        self.assertEqual(attempts, [self.P + "/device/poll"])

    def test_poll_http_errors_log_status_without_payload_or_execution(self):
        stop, out, box = threading.Event(), io.StringIO(), mock.Mock()
        secret = "SENSITIVE_RESPONSE_BODY"
        body = {"error": secret, "url": "https://private.invalid/token?secret=hidden",
                "calls": [{"id": "must-not-run", "tool": "write_file", "args": {}}]}
        journal = remote.ReceiptJournal(":memory:", "http-diagnostics")
        try:
            with mock.patch.object(remote, "_post", side_effect=[(code, body) for code in (403, 429, 503, 401)]), \
                    mock.patch.object(stop, "wait", return_value=False) as wait:
                with self.assertRaises(remote.Revoked):
                    remote.serve(box, {"cloud": self.P, "device_token": "SENSITIVE_DEVICE_TOKEN"},
                                 out=out, stop=stop, journal=journal)
            self.assertEqual(wait.call_args_list, [mock.call(1), mock.call(2), mock.call(4)])
            self.assertTrue(stop.is_set())  # 401 still stops instead of retrying.
            box.call.assert_not_called()
            self.assertEqual(journal.pending(), [])
            log = out.getvalue()
            for code, delay in ((403, 1), (429, 2), (503, 4)):
                self.assertRegex(log, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z cloud poll failed "
                                 r"\(HTTP %d\); retrying in %ds" % (code, delay))
            for private in (secret, "SENSITIVE_DEVICE_TOKEN", "private.invalid", "?secret=", "/device/poll"):
                self.assertNotIn(private, log)
        finally:
            journal.close()


if __name__ == "__main__":
    unittest.main()
