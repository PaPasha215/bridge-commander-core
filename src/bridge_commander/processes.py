"""Terminal sessions: long-running commands with incremental output and stdin."""
import os
import re
import signal
import subprocess
import threading
import time

from .files import ToolError

WINDOWS = os.name == "nt"
# Output ending like this and then going quiet usually means a REPL/prompt waits for input.
PROMPT = re.compile(r"(>>> |\.\.\. |[$#%>?:] ?)$")
QUIET_SECONDS = 0.3
MAX_TIMEOUT_MS = 600000
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|&\n]|\$\(|`")


class Session:
    def __init__(self, proc, command, limit):
        self.proc = proc
        self.pid = proc.pid
        self.command = command
        self.started = time.time()
        self.limit = limit
        self.buf = bytearray()
        self.dropped = 0  # bytes discarded from the head when the buffer overflowed
        self.read_pos = 0  # absolute offset already returned to the client
        self.last_output = time.monotonic()
        self.done = False  # set by the pump thread once stdout hit EOF and the process was reaped
        self.cond = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                chunk = self.proc.stdout.read1(65536)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            with self.cond:
                self.buf += chunk
                if len(self.buf) > self.limit:
                    cut = len(self.buf) - self.limit
                    del self.buf[:cut]
                    self.dropped += cut
                self.last_output = time.monotonic()
                self.cond.notify_all()
        self.proc.wait()
        for stream in (self.proc.stdout, self.proc.stdin):
            try:
                stream.close()
            except OSError:
                pass
        with self.cond:
            self.done = True
            self.cond.notify_all()

    @property
    def returncode(self):
        return self.proc.poll()

    def wait_output(self, timeout_ms):
        """Wait until the process exits, the timeout passes, or output goes quiet at a prompt."""
        deadline = time.monotonic() + max(0, min(timeout_ms, MAX_TIMEOUT_MS)) / 1000.0
        with self.cond:
            while not self.done:
                now = time.monotonic()
                if now >= deadline:
                    break
                tail = bytes(self.buf[-64:]).decode("utf-8", "replace")
                quiet = now - self.last_output
                if self._unread() and PROMPT.search(tail) and quiet >= QUIET_SECONDS:
                    return "waiting for input"
                self.cond.wait(min(deadline - now, QUIET_SECONDS / 3))
        code = self.returncode
        return "running" if code is None else "exited with code %d" % code

    def _unread(self):
        return self.dropped + len(self.buf) > self.read_pos

    def take(self):
        with self.cond:
            start = self.read_pos - self.dropped
            note = ""
            if start < 0:
                note = "[... %d earlier bytes were dropped (maxProcessOutputBytes)]\n" % -start
                start = 0
            data = bytes(self.buf[start:])
            self.read_pos = self.dropped + len(self.buf)
        return note + data.decode("utf-8", "replace")


class Processes:
    def __init__(self, files):
        self.files = files
        self.cfg = files.cfg
        self.sessions = {}
        self.lock = threading.Lock()

    def check_command(self, command):
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        blocked = {b.lower() for b in self.cfg["blockedCommands"]}
        for segment in SEGMENT_SPLIT.split(command):
            words = segment.strip().split()
            while words and ("=" in words[0] and not words[0].startswith("=")):
                words.pop(0)  # skip VAR=value prefixes
            if words:
                name = os.path.basename(words[0].strip("'\"(){}")).lower()
                if name.endswith(".exe"):
                    name = name[:-4]
                if name in blocked:
                    raise ToolError("command %r is blocked by blockedCommands" % name)

    def shell_argv(self, command, shell=None):
        shell = shell or self.cfg["defaultShell"]
        if not shell:
            shell = "powershell.exe" if WINDOWS else os.environ.get("SHELL") or "/bin/sh"
        base = os.path.basename(shell).lower()
        if base in ("cmd", "cmd.exe"):
            return [shell, "/d", "/s", "/c", command]
        if base.startswith(("powershell", "pwsh")):
            return [shell, "-NoProfile", "-Command", command]
        return [shell, "-c", command]

    def start_process(self, command, timeout_ms=5000, cwd=None, shell=None):
        self.files.writable()
        self.check_command(command)
        workdir = self.files.resolve(cwd) if cwd else (self.files.roots[0] if self.files.roots else os.path.expanduser("~"))
        if not os.path.isdir(workdir):
            raise ToolError("working directory does not exist: %s" % workdir)
        with self.lock:
            self._evict()
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
            try:
                proc = subprocess.Popen(self.shell_argv(command, shell), cwd=workdir, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **extra)
            except OSError as exc:
                raise ToolError("cannot start process: %s" % exc)
            session = Session(proc, command, self.cfg["maxProcessOutputBytes"])
            self.sessions[proc.pid] = session
        status = session.wait_output(timeout_ms)
        return "Process started with PID %d in %s\nStatus: %s\n\n%s" % (session.pid, workdir, status, session.take())

    def _evict(self):
        if len(self.sessions) < self.cfg["maxSessions"]:
            return
        finished = sorted((s.started, pid) for pid, s in self.sessions.items() if s.returncode is not None)
        if not finished:
            raise ToolError("too many running sessions (maxSessions=%d); terminate one first" % self.cfg["maxSessions"])
        del self.sessions[finished[0][1]]

    def _session(self, pid):
        session = self.sessions.get(pid)
        if session is None:
            raise ToolError("no session with PID %s; use list_sessions" % pid)
        return session

    def interact_with_process(self, pid, input, timeout_ms=8000):
        self.files.writable()
        session = self._session(pid)
        if session.returncode is not None:
            raise ToolError("process %d already exited with code %d" % (pid, session.returncode))
        data = input if input.endswith("\n") else input + "\n"
        try:
            session.proc.stdin.write(data.encode("utf-8"))
            session.proc.stdin.flush()
        except (OSError, ValueError):
            raise ToolError("cannot write to process %d (stdin closed)" % pid)
        session.last_output = time.monotonic()
        status = session.wait_output(timeout_ms)
        return "Status: %s\n\n%s" % (status, session.take())

    def read_process_output(self, pid, timeout_ms=5000, offset=0, length=None):
        session = self._session(pid)
        if offset:  # absolute line or tail view of the retained output; does not move the read position
            code = session.returncode
            with session.cond:
                lines = bytes(session.buf).decode("utf-8", "replace").splitlines(True)
            limit = self.cfg["fileReadLineLimit"] if length is None else max(1, min(length, self.cfg["fileReadLineLimit"]))
            part = lines[offset:][:limit] if offset < 0 else lines[offset:offset + limit]
            start = max(len(lines) + offset, 0) if offset < 0 else offset
            return "Status: %s\n[lines %d-%d of %d retained]\n%s" % (
                "running" if code is None else "exited with code %d" % code, start + 1, start + len(part),
                len(lines), "".join(part))
        if not session._unread():
            status = session.wait_output(timeout_ms)
        else:
            code = session.returncode
            status = "running" if code is None else "exited with code %d" % code
        return "Status: %s\n\n%s" % (status, session.take())

    def force_terminate(self, pid):
        self.files.writable()
        session = self._session(pid)
        if session.returncode is None:
            _kill_tree(session.proc)
        return "Process %d terminated (exit code %s)" % (pid, session.returncode)

    def list_sessions(self):
        if not self.sessions:
            return "No sessions."
        rows = []
        for pid, s in sorted(self.sessions.items()):
            code = s.returncode
            rows.append("PID %d | %s | %ds | %s%s" % (
                pid, "running" if code is None else "exited %d" % code, time.time() - s.started,
                s.command[:120], " | unread output" if s._unread() else ""))
        return "\n".join(rows)

    def close(self):
        for session in list(self.sessions.values()):
            if session.returncode is None:
                _kill_tree(session.proc)

    # -- system processes ---------------------------------------------------
    def list_processes(self, filter=None):
        argv = ["tasklist", "/FO", "CSV", "/NH"] if WINDOWS else ["ps", "-eo", "pid,ppid,user,pcpu,pmem,etime,args"]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=15, errors="replace").stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError("cannot list processes: %s" % exc)
        lines = out.splitlines()
        head, rows = (lines[:1], lines[1:]) if not WINDOWS else ([], lines)
        if filter:
            rows = [r for r in rows if filter.lower() in r.lower()]
        more = len(rows) - 500
        rows = rows[:500] + (["... %d more; use filter" % more] if more > 0 else [])
        return "\n".join(head + rows)

    def kill_process(self, pid, force=False):
        self.files.writable()
        if pid <= 1 or pid == os.getpid():
            raise ToolError("refusing to kill PID %d" % pid)
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid)] + (["/F"] if force else []), check=True,
                               capture_output=True, timeout=15)
            else:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolError("cannot kill PID %d: %s" % (pid, exc))
        return "Sent %s to PID %d" % ("SIGKILL" if force else "SIGTERM", pid)


def _kill_tree(proc):
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                pass
            # The shell can exit before a child that ignores TERM; its session group still belongs to this command.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(5)
    except (OSError, subprocess.SubprocessError):
        pass
