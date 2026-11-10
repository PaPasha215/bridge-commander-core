import os
import re
import shlex
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from helpers import Sandbox

PY = sys.executable


def group_members(pgid):
    output = subprocess.check_output(["ps", "-eo", "pid=,pgid=,stat="], text=True, timeout=5)
    return [(int(pid), state) for pid, group, state in (line.split() for line in output.splitlines())
            if int(group) == pgid]


def running_group_members(pgid):
    return [(pid, state) for pid, state in group_members(pgid) if not state.startswith(("Z", "X"))]


class ReadOnlyProcesses(Sandbox):
    extra = {"readOnly": True}

    def test_force_terminate_does_not_signal_a_session_in_read_only_mode(self):
        session = Mock(returncode=None)
        self.box.processes.sessions[987654] = session
        try:
            with patch("bridge_commander.processes._kill_tree") as kill:
                self.assertIn("read-only", self.err("force_terminate", pid=987654))
                kill.assert_not_called()
        finally:
            self.box.processes.sessions.clear()  # mocked session: teardown must not signal a process


class ProcessTools(Sandbox):
    def pid_of(self, text):
        return int(re.search(r"PID (\d+)", text).group(1))

    def assert_group_stopped(self, pgid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not running_group_members(pgid):
                return
            time.sleep(0.02)
        self.assertEqual(running_group_members(pgid), [], "managed group still has a living process")

    def test_quick_command(self):
        text = self.ok("start_process", command="echo hello && pwd", timeout_ms=5000)
        self.assertIn("exited with code 0", text)
        self.assertIn("hello", text)
        self.assertIn(self.root, text)  # default cwd is the first allowed directory

    def test_exit_code_and_cwd(self):
        self.ok("create_directory", path=self.p("sub"))
        text = self.ok("start_process", command="pwd; exit 3", cwd=self.p("sub"), timeout_ms=5000)
        self.assertIn("exited with code 3", text)
        self.assertIn(self.p("sub"), text)
        self.assertIn("outside allowed", self.err("start_process", command="pwd", cwd="/"))

    def test_long_running_and_incremental_output(self):
        cmd = "%s -c \"import time\nfor i in range(3):\n    print(i, flush=True); time.sleep(0.4)\"" % PY
        text = self.ok("start_process", command=cmd, timeout_ms=200)
        self.assertIn("Status: running", text)
        pid = self.pid_of(text)
        self.assertIn(str(pid), self.ok("list_sessions"))
        deadline, seen = time.time() + 10, ""
        while "exited" not in seen and time.time() < deadline:
            seen += self.ok("read_process_output", pid=pid, timeout_ms=1000)
        self.assertIn("exited with code 0", seen)
        self.assertIn("2", seen)

    def test_interactive_repl(self):
        text = self.ok("start_process", command="%s -i -q" % PY, timeout_ms=5000)
        self.assertIn("waiting for input", text)
        pid = self.pid_of(text)
        reply = self.ok("interact_with_process", pid=pid, input="print(6*7)", timeout_ms=5000)
        self.assertIn("42", reply)
        self.ok("interact_with_process", pid=pid, input="exit()", timeout_ms=5000)
        self.assertIn("exited", self.ok("read_process_output", pid=pid, timeout_ms=3000))
        self.assertIn("already exited", self.err("interact_with_process", pid=pid, input="1"))

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_force_terminate_kills_children(self):
        text = self.ok("start_process", command="sleep 30 & sleep 30; echo done", timeout_ms=300)
        pid = self.pid_of(text)
        self.ok("force_terminate", pid=pid)
        self.assertIn("exited", self.ok("list_sessions"))
        self.assert_group_stopped(pid)

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_force_terminate_kills_a_term_resistant_child(self):
        script = ("import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  "print('ready child=%d? ' % os.getpid(),flush=True); time.sleep(30)")
        command = "%s -c %s & wait" % (shlex.quote(PY), shlex.quote(script))
        text = self.ok("start_process", command=command, timeout_ms=5000)
        pid = self.pid_of(text)
        child = int(re.search(r"child=(\d+)", text).group(1))
        try:
            os.kill(child, signal.SIGTERM)  # verify the fixture really ignores TERM before testing force_terminate
            self.assertIn(child, [p for p, _ in running_group_members(pid)])
            self.ok("force_terminate", pid=pid)
            self.assert_group_stopped(pid)
        finally:
            for owned, _ in running_group_members(pid):
                try:
                    os.kill(owned, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipIf(os.name == "nt", "POSIX zombie assertion")
    def test_unreaped_zombie_group_is_not_classified_as_running(self):
        child = subprocess.Popen([PY, "-c", "pass"], start_new_session=True)
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if any(state.startswith("Z") for _, state in group_members(child.pid)):
                    break
                time.sleep(0.02)
            self.assertTrue(any(state.startswith("Z") for _, state in group_members(child.pid)))
            if sys.platform.startswith("linux"):
                # Linux reports an unreaped zombie's group as existing; macOS may return EPERM for that dead PID.
                os.killpg(child.pid, 0)
            self.assertEqual(running_group_members(child.pid), [])
        finally:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    def test_blocked_commands(self):
        for cmd in ("sudo ls", "ls; sudo ls", "echo x | /usr/bin/sudo -S ls", "FOO=1 shutdown -h now",
                    "echo $(reboot)", "(mkfs /dev/x)"):
            self.assertIn("blocked", self.err("start_process", command=cmd), cmd)
        self.ok("start_process", command="echo sudo is just a word", timeout_ms=3000)

    def test_output_cap(self):
        self.cfg["maxProcessOutputBytes"] = 1000
        text = self.ok("start_process", command="%s -c \"print('x'*5000)\"" % PY, timeout_ms=5000)
        self.assertIn("earlier bytes were dropped", text)
        self.assertLess(len(text), 1400)

    def test_unknown_pid_and_list_processes(self):
        self.assertIn("no session", self.err("read_process_output", pid=1234567))
        self.assertIn(str(os.getpid()), self.ok("list_processes", filter="python"))
        self.assertIn("refusing", self.err("kill_process", pid=1))

    def test_kill_process(self):
        text = self.ok("start_process", command="sleep 30", timeout_ms=100)
        pid = self.pid_of(text)
        self.ok("kill_process", pid=pid)
        self.assertIn("exited", self.ok("read_process_output", pid=pid, timeout_ms=3000))


if __name__ == "__main__":
    unittest.main()
