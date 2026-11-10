import os
import re
import time
import unittest

from helpers import Sandbox


class BackgroundSearch(Sandbox):
    def sid(self, text):
        return re.search(r"Search (\w+):", text).group(1)

    def test_files_and_paging(self):
        for i in range(250):
            self.ok("write_file", path=self.p("d%d" % (i % 5), "f%03d.txt" % i), content="needle %d\n" % i)
        text = self.ok("start_search", path=self.root, pattern="*.txt", timeout_ms=5000)
        sid = self.sid(text)
        self.assertIn("finished | 250 match(es)", text)
        self.assertIn("showing results 1-100 of 250", text)
        self.assertIn("offset=100", text)
        self.assertIn("results 201-250 of 250", self.ok("get_more_search_results", search_id=sid, offset=200))
        self.assertIn("results 241-250", self.ok("get_more_search_results", search_id=sid, offset=-10))
        self.assertIn(sid, self.ok("list_searches"))

    def test_content_search_and_stop(self):
        self.ok("write_file", path=self.p("a.py"), content="x = 1\nNeedle = 2\n")
        text = self.ok("start_search", path=self.root, pattern="needle", search_type="content", timeout_ms=5000)
        self.assertIn("a.py:2:Needle = 2", text)  # case-insensitive by default
        self.assertIn("0 matching", self.ok("start_search", path=self.root, pattern="needle", search_type="content",
                                            ignore_case=False, timeout_ms=5000))
        self.assertIn("invalid regular expression",
                      self.err("start_search", path=self.root, pattern="(", search_type="content"))
        sid = self.sid(text)
        self.assertIn("finished", self.ok("stop_search", search_id=sid))
        self.assertIn("no search", self.err("get_more_search_results", search_id="nope"))
        self.assertIn("outside allowed", self.err("start_search", path="/", pattern="x"))

    def test_max_results(self):
        for i in range(30):
            self.ok("write_file", path=self.p("m%02d.log" % i), content="")
        text = self.ok("start_search", path=self.root, pattern=".log", max_results=10, timeout_ms=5000)
        self.assertIn("stopped at max_results | 10 match(es)", text)

    def test_content_search_cannot_read_symlinks_outside_allowed_root(self):
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w") as f:
            f.write("needle OUTSIDE_CANARY\n")
        os.symlink(outside, self.p("escape.txt"))
        self.ok("write_file", path=self.p("inside.txt"), content="needle INSIDE_CANARY\n")
        os.symlink(self.p("inside.txt"), self.p("inside-link.txt"))
        self.cfg["readOnly"] = True
        self.assertIn("outside allowed", self.err("read_file", path=self.p("escape.txt")))
        for tool, extra in (("search_code", {}), ("start_search", {"search_type": "content"})):
            with self.subTest(tool=tool):
                result = self.ok(tool, path=self.root, pattern="needle", literal=True, timeout_ms=5000, **extra)
                self.assertIn("INSIDE_CANARY", result)
                self.assertNotIn("OUTSIDE_CANARY", result)
                self.assertNotIn("escape.txt:", result)


class ProcessOutputPaging(Sandbox):
    def test_tail_without_consuming(self):
        text = self.ok("start_process", command="for i in 1 2 3 4 5; do echo line$i; done", timeout_ms=5000)
        pid = int(re.search(r"PID (\d+)", text).group(1))
        tail = self.ok("read_process_output", pid=pid, offset=-2)
        self.assertIn("lines 4-5 of 5", tail)
        self.assertTrue(tail.rstrip().endswith("line5"))
        self.assertIn("line2\nline3\n", self.ok("read_process_output", pid=pid, offset=1, length=2))


if __name__ == "__main__":
    unittest.main()
