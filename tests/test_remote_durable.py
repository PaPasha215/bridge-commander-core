import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import helpers  # noqa: F401
from bridge_commander import remote
from bridge_commander.receipts import ReceiptError, ReceiptJournal, device_scope, result_hash


def call(cid="c1", **changes):
    now = time.time()
    return dict({"id": cid, "tool": "write_file", "args": {"content": "PRIVATE_ARGUMENT_CANARY"},
                 "created_at": now, "expires_at": now + 60}, **changes)


class Journal(unittest.TestCase):
    def test_process_crash_after_effect_does_not_authorize_reexecution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, effect = Path(tmp) / "receipts.sqlite3", Path(tmp) / "effect.txt"
            envelope = call()
            program = ("import json,os,sys; from pathlib import Path; "
                       "from bridge_commander.receipts import ReceiptJournal; "
                       "j=ReceiptJournal(sys.argv[1], 'device'); j.begin(json.loads(sys.argv[3])); "
                       "Path(sys.argv[2]).write_text('effect happened once'); os._exit(23)")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            process = subprocess.run([sys.executable, "-c", program, str(path), str(effect), json.dumps(envelope)],
                                     env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(process.returncode, 23, process.stderr)
            journal = ReceiptJournal(path, "device")
            row, fresh = journal.begin(envelope)
            self.assertFalse(fresh)
            self.assertEqual(row["state"], "UNKNOWN")
            self.assertEqual(effect.read_text(), "effect happened once")
            journal.close()

    def test_atomic_claim_crash_recovery_and_metadata_only_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.sqlite3"
            journal = ReceiptJournal(path, "device-a")
            envelope = call()
            barrier = threading.Barrier(8)
            claims = []

            def claim():
                barrier.wait()
                claims.append(journal.begin(envelope)[1])

            threads = [threading.Thread(target=claim) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(claims), 1)
            journal.close()  # simulated crash: the effect could have happened, no completion was recorded
            journal = ReceiptJournal(path, "device-a")
            row, fresh = journal.begin(envelope)
            self.assertFalse(fresh)
            self.assertEqual(row["state"], "UNKNOWN")
            output = [{"type": "text", "text": "PRIVATE_OUTPUT_CANARY"}]
            receipt = journal.complete(envelope["id"], output, False)
            self.assertEqual(receipt["result_hash"], result_hash(output, False))
            self.assertFalse(journal.posted(envelope["id"], {"state": "UNKNOWN"}))
            self.assertEqual(journal.pending()[0]["state"], "COMPLETED")
            journal.close()
            journal = ReceiptJournal(path, "device-a")
            row, fresh = journal.begin(envelope)
            self.assertEqual(row["state"], "COMPLETED")
            self.assertFalse(fresh)
            with self.assertRaisesRegex(ReceiptError, "conflicts"):
                journal.complete(envelope["id"], [{"type": "text", "text": "different"}], False)
            journal.close()
            stored = path.read_bytes()
            self.assertNotIn(b"PRIVATE_ARGUMENT_CANARY", stored)
            self.assertNotIn(b"PRIVATE_OUTPUT_CANARY", stored)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_payload_scope_expiration_and_capacity_cannot_reset_deduplication(self):
        journal = ReceiptJournal(":memory:", "device-a", max_receipts=1)
        envelope = call()
        self.assertTrue(journal.begin(envelope)[1])
        changed = dict(envelope, args={"content": "changed"})
        with self.assertRaisesRegex(ReceiptError, "different payload"):
            journal.begin(changed)
        with self.assertRaisesRegex(ReceiptError, "full"):
            journal.begin(call("c2"))
        with self.assertRaisesRegex(ReceiptError, "expired"):
            journal.begin(call("old", created_at=time.time() - 100, expires_at=time.time() - 1))
        with self.assertRaisesRegex(ReceiptError, "created_at/expires_at"):
            journal.begin({"id": "legacy", "tool": "write_file", "args": {}})
        for args in (0, False, []):
            with self.assertRaisesRegex(ReceiptError, "arguments must be an object"):
                journal.begin(call("invalid-args", args=args))
        journal.close()
        a = {"cloud": "https://one.example", "device_id": 1, "device_token": "token-a"}
        self.assertEqual(device_scope(a), device_scope(dict(a, device_token="rotated")))
        self.assertNotEqual(device_scope(a), device_scope(dict(a, cloud="https://two.example")))
        self.assertNotEqual(device_scope(a), device_scope(dict(a, device_id=2)))

    def test_untrusted_existing_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal"
            target = Path(tmp) / "other"
            target.write_text("not a journal")
            path.symlink_to(target)
            with self.assertRaises(ReceiptError):
                ReceiptJournal(path, "scope")
            self.assertEqual(target.read_text(), "not a journal")
            path.unlink()
            path.write_text("")
            path.chmod(0o644)
            with self.assertRaises(ReceiptError):
                ReceiptJournal(path, "scope")
            path.chmod(0o600)
            path.write_text("NOT_AN_EXECUTION_JOURNAL")
            with self.assertRaisesRegex(ReceiptError, "invalid"):
                ReceiptJournal(path, "scope")
            self.assertEqual(path.read_text(), "NOT_AN_EXECUTION_JOURNAL")


class DeviceRecovery(unittest.TestCase):
    def test_result_endpoint_failure_cannot_grow_output_memory_without_bound(self):
        journal = ReceiptJournal(":memory:", "test")
        stop = threading.Event()
        envelopes = [call("c%d" % n) for n in range(12)]
        peak, attempts = [], []
        remember = remote._remember_result
        box = mock.Mock()
        box.call.return_value = ([{"type": "text", "text": "private output " * 30}], False)
        issued = [0]

        def bounded(cache, cid, body):
            remember(cache, cid, body)
            self.assertTrue(all(isinstance(item[0], bytes) and "content" not in item[1]
                                for item in cache.values()))
            peak.append((len(cache), sum(len(item[0]) for item in cache.values())))

        def post(url, body, token=None, timeout=None):
            body = json.loads(body) if isinstance(body, bytes) else body
            if url.endswith("/ack"):
                return 200, {}
            if url.endswith("/result"):
                attempts.append(body["id"])
                return 503, {}
            if envelopes and len(journal.pending(20)) >= issued[0]:
                issued[0] += 1
                return 200, {"calls": [envelopes.pop(0)]}
            if len(journal.pending(20)) == 12:
                stop.set()
            else:
                stop.wait(0.001)
            return 200, {"calls": []}

        with mock.patch.object(remote, "_post", post), mock.patch.object(remote, "_remember_result", bounded), \
                mock.patch.object(remote, "RESULT_CACHE_COUNT", 2), mock.patch.object(remote, "RESULT_CACHE_BYTES", 1024):
            remote.serve(box, {"cloud": "https://c.example", "device_token": "test"},
                         journal=journal, out=io.StringIO(), stop=stop)
        self.assertTrue(attempts)
        self.assertEqual(box.call.call_count, 12)
        self.assertTrue(all(count <= 2 and size <= 1024 for count, size in peak))
        self.assertEqual(len(journal.pending(20)), 12)

    def test_duplicate_delivery_executes_once_and_ack_failure_never_executes(self):
        class FastStop(threading.Event):
            def wait(self, timeout=None):
                return super().wait(0.001 if timeout == 2 else timeout)

        for ack in (200, 409, "timeout"):
            with self.subTest(ack=ack):
                journal = ReceiptJournal(":memory:", "test")
                stop, result_ready = FastStop(), threading.Event()
                envelope, results = call(), []
                box = mock.Mock()
                box.call.return_value = ([{"type": "text", "text": "result"}], False)
                polls = [0]

                def post(url, body, token=None, timeout=None):
                    body = json.loads(body) if isinstance(body, bytes) else body
                    if url.endswith("/ack"):
                        if ack == "timeout":
                            raise OSError("ACK response lost after server acceptance")
                        return ack, {}
                    if url.endswith("/result"):
                        results.append(body)
                        result_ready.set()
                        stop.set()
                        return 200, {}
                    polls[0] += 1
                    if polls[0] == 1:
                        return 200, {"calls": [envelope, envelope]}
                    result_ready.wait(1)
                    return 200, {"calls": []}

                with mock.patch.object(remote, "_post", post):
                    remote.serve(box, {"cloud": "https://c.example", "device_token": "test"},
                                 journal=journal, out=io.StringIO(), stop=stop)
                self.assertTrue(result_ready.wait(1))
                self.assertEqual(box.call.call_count, 1 if ack == 200 else 0)
                self.assertEqual(results[0]["receipt"]["state"], "COMPLETED" if ack == 200 else "UNKNOWN")

    def test_lost_result_is_retried_from_memory_without_executing_again(self):
        journal = ReceiptJournal(":memory:", "test")
        stop, ready = threading.Event(), threading.Event()
        envelope, attempts, wire_attempts = call(), [], []
        box = mock.Mock()
        output = [{"type": "text", "text": "in-memory output: кириллица 🧊"}]
        box.call.return_value = (output, False)
        delivered = [False]

        def post(url, body, token=None, timeout=None):
            if url.endswith("/ack"):
                return 200, {}
            if url.endswith("/result"):
                wire_attempts.append(body)
                body = json.loads(body) if isinstance(body, bytes) else body
                attempts.append(body)
                if len(attempts) == 1:
                    raise OSError("result response lost")
                ready.set()
                stop.set()
                return 200, {}
            if not delivered[0]:
                delivered[0] = True
                return 200, {"calls": [envelope]}
            ready.wait(3)
            return 200, {"calls": []}

        with mock.patch.object(remote, "_post", post):
            remote.serve(box, {"cloud": "https://c.example", "device_token": "test"},
                         journal=journal, out=io.StringIO(), stop=stop)
        self.assertTrue(ready.is_set())
        self.assertEqual(box.call.call_count, 1)
        self.assertEqual(len(attempts), 2)
        self.assertIsInstance(wire_attempts[0], bytes)
        self.assertIs(wire_attempts[0], wire_attempts[1])
        self.assertIn("кириллица 🧊".encode("utf-8"), wire_attempts[0])
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[1]["content"], output)
        self.assertTrue(attempts[1]["receipt"]["result_available"])

    def test_restart_resends_completed_metadata_without_repeating_or_fabricating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.sqlite3"
            first = ReceiptJournal(path, "device")
            envelope = call()
            first.begin(envelope)
            receipt = first.complete(envelope["id"], [{"type": "text", "text": "private old output"}], False)
            first.close()
            journal = ReceiptJournal(path, "device")
            stop, ready = threading.Event(), threading.Event()
            posts = []
            box = mock.Mock()

            def post(url, body, token=None, timeout=None):
                if url.endswith("/result"):
                    posts.append(body)
                    ready.set()
                    stop.set()
                    return 200, {}
                ready.wait(1)
                return 200, {"calls": []}

            with mock.patch.object(remote, "_post", post):
                remote.serve(box, {"cloud": "https://c.example", "device_token": "test"},
                             journal=journal, out=io.StringIO(), stop=stop)
            self.assertTrue(ready.wait(1))
            box.call.assert_not_called()
            self.assertFalse(posts[0]["receipt"]["result_available"])
            self.assertEqual(posts[0]["receipt"]["result_hash"], receipt["result_hash"])
            self.assertTrue(posts[0]["isError"])
            self.assertNotIn("private old output", json.dumps(posts))


if __name__ == "__main__":
    unittest.main()
