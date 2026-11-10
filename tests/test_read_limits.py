from helpers import Sandbox


class ReadLimits(Sandbox):
    def test_plain_text_and_multiple_reads_respect_memory_budget(self):
        self.cfg["maxFileSizeBytes"] = 1024
        with open(self.p("too-large.txt"), "w") as f:
            f.write("x" * 1025)
        self.assertIn("larger than maxFileSizeBytes", self.err("read_file", path=self.p("too-large.txt")))
        for name, content in (("one.txt", "A" * 700), ("two.txt", "B" * 700)):
            with open(self.p(name), "w") as f:
                f.write(content)
        result = self.ok("read_multiple_files", paths=[self.p("one.txt"), self.p("two.txt")])
        self.assertIn("A" * 700, result)
        self.assertNotIn("B" * 700, result)
        self.assertIn("combined output stopped", result)
