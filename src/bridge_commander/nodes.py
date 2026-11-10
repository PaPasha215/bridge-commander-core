"""Remote nodes: a persistent `bridge-commander worker` reached through any command (usually ssh).

Wire format, one JSON object per line in each direction:
  -> {"id": 1, "tool": "read_file", "args": {...}}
  <- {"id": 1, "content": [...], "isError": false}
The worker enforces its own config (allowed directories, read-only, blocked commands).
"""
import json
import queue
import subprocess
import threading
from collections import deque

from .files import ToolError

DEFAULT_TIMEOUT_MS = 60000


class Node:
    def __init__(self, name, spec):
        self.name = name
        self.command = spec["command"]
        self.description = spec.get("description", "")
        self.timeout_ms = spec.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        self.proc = None
        self.lines = None
        self.stderr = deque(maxlen=20)
        self.next_id = 0
        self.lock = threading.Lock()

    def _start(self):
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        self.stderr.clear()
        threading.Thread(target=_pump, args=(self.proc.stdout, self.lines.put), daemon=True).start()
        threading.Thread(target=_pump, args=(self.proc.stderr, lambda l: l and self.stderr.append(
            l.decode("utf-8", "replace").rstrip())), daemon=True).start()

    def _reset(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        self.proc = None

    def call(self, tool, args):
        wait_ms = max(self.timeout_ms, int(args.get("timeout_ms") or 0) + 15000)
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._reset()
                try:
                    self._start()
                except OSError as exc:
                    raise ToolError("node %s: cannot run %s: %s" % (self.name, self.command[0], exc))
            self.next_id += 1
            request = json.dumps({"id": self.next_id, "tool": tool, "args": args}) + "\n"
            try:
                self.proc.stdin.write(request.encode("utf-8"))
                self.proc.stdin.flush()
            except OSError:
                self._reset()
                raise ToolError("node %s is unavailable%s" % (self.name, self._why()))
            while True:
                try:
                    line = self.lines.get(timeout=wait_ms / 1000.0)
                except queue.Empty:
                    self._reset()
                    raise ToolError("node %s did not answer within %d ms; its worker was restarted "
                                    "(terminal sessions on it were lost)" % (self.name, wait_ms))
                if line is None:
                    self._reset()
                    raise ToolError("node %s closed the connection%s" % (self.name, self._why()))
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue  # e.g. a login banner printed by the remote shell
                if isinstance(reply, dict) and reply.get("id") == self.next_id:
                    return reply.get("content") or [], bool(reply.get("isError"))

    def _why(self):
        tail = [l for l in self.stderr if l][-3:]
        return (": " + " | ".join(tail)) if tail else ""

    def close(self):
        with self.lock:
            self._reset()


def _pump(stream, sink):
    for line in iter(stream.readline, b""):
        sink(line)
    sink(None)  # EOF marker


def serve_worker(toolbox, stdin, stdout):
    """Worker side: answer requests from the hub until stdin closes."""
    for raw in iter(stdin.readline, b""):
        try:
            req = json.loads(raw)
            rid, tool, args = req["id"], req["tool"], req.get("args") or {}
        except (ValueError, KeyError, TypeError):
            continue
        content, is_error = toolbox.call(tool, args, from_hub=True)
        stdout.write((json.dumps({"id": rid, "content": content, "isError": is_error}) + "\n").encode("utf-8"))
        stdout.flush()
