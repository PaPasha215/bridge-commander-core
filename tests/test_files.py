import os
import sys
import unittest

from helpers import Sandbox, validate

from bridge_commander.config import ConfigError


class FileTools(Sandbox):
    def test_write_read_append(self):
        self.ok("write_file", path=self.p("a", "b.txt"), content="one\ntwo\n")
        self.ok("write_file", path=self.p("a", "b.txt"), content="three\n", mode="append")
        self.assertEqual(self.ok("read_file", path=self.p("a", "b.txt")), "one\ntwo\nthree\n")
        self.assertEqual(self.ok("read_file", path=self.p("a", "b.txt"), offset=1, length=1),
                         "[lines 2-2 of 3]\ntwo\n")
        self.assertIn("three", self.ok("read_file", path=self.p("a", "b.txt"), offset=-1))
        self.assertIn("past the end", self.ok("read_file", path=self.p("a", "b.txt"), offset=10))

    def test_relative_path_is_inside_first_root(self):
        self.ok("write_file", path="rel.txt", content="x")
        self.assertTrue(os.path.exists(self.p("rel.txt")))

    def test_outside_and_symlink_escape_denied(self):
        self.assertIn("outside allowed", self.err("read_file", path=os.path.join(self.tmp, "x")))
        self.assertIn("outside allowed", self.err("read_file", path=self.p("..", "x")))
        os.symlink(self.tmp, self.p("link"))
        self.assertIn("outside allowed", self.err("write_file", path=self.p("link", "evil.txt"), content="x"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_line_limit(self):
        self.cfg["fileReadLineLimit"] = 2
        self.ok("write_file", path=self.p("n.txt"), content="1\n2\n3\n4\n")
        self.assertEqual(self.ok("read_file", path=self.p("n.txt")), "[lines 1-2 of 4]\n1\n2\n")

    def test_binary_and_image(self):
        with open(self.p("bin"), "wb") as f:
            f.write(b"\x00\x01\x02")
        self.assertIn("binary", self.err("read_file", path=self.p("bin")))
        with open(self.p("pic.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        content, is_error = self.box.call("read_file", {"path": self.p("pic.png")})
        self.assertFalse(is_error)
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["mimeType"], "image/png")

    def test_special_files_not_read(self):
        os.mkfifo(self.p("pipe"))
        self.assertIn("not a regular file", self.err("read_file", path=self.p("pipe")))
        self.assertIn("0 matching", self.ok("search_code", path=self.root, pattern="x"))

    def test_edit_block(self):
        path = self.p("e.py")
        self.ok("write_file", path=path, content="a = 1\nb = 2\na = 1\n")
        self.assertIn("found 2 occurrences", self.err("edit_block", file_path=path, old_string="a = 1", new_string="a = 3"))
        self.ok("edit_block", file_path=path, old_string="a = 1", new_string="a = 3", expected_replacements=2)
        self.ok("edit_block", file_path=path, old_string="b = 2", new_string="b = 5")
        self.assertEqual(self.ok("read_file", path=path), "a = 3\nb = 5\na = 3\n")
        miss = self.err("edit_block", file_path=path, old_string="b = 6", new_string="x")
        self.assertIn("Closest text at lines 2-2", miss)
        self.assertIn("b = {-6-}{+5+}", miss)

    def test_edit_block_crlf(self):
        path = self.p("w.txt")
        with open(path, "wb") as f:
            f.write(b"one\r\ntwo\r\n")
        self.ok("edit_block", file_path=path, old_string="one\ntwo", new_string="uno\ndos")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"uno\r\ndos\r\n")

    def test_write_keeps_mode(self):
        path = self.p("x.sh")
        self.ok("write_file", path=path, content="echo 1\n")
        os.chmod(path, 0o750)
        self.ok("write_file", path=path, content="echo 2\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o750)

    def test_directory_move_info(self):
        self.ok("create_directory", path=self.p("d", "e"))
        self.ok("write_file", path=self.p("d", "e", "f.txt"), content="hi\n")
        listing = self.ok("list_directory", path=self.p("d"))
        self.assertIn("[DIR] e/", listing)
        self.assertIn("[FILE] e/f.txt (3 B)", listing)
        self.assertNotIn("f.txt", self.ok("list_directory", path=self.p("d"), depth=1))
        self.ok("move_file", source=self.p("d", "e", "f.txt"), destination=self.p("g.txt"))
        self.assertIn("destination already exists",
                      self.err("move_file", source=self.p("g.txt"), destination=self.p("g.txt")))
        info = self.ok("get_file_info", path=self.p("g.txt"))
        self.assertIn("type: file", info)
        self.assertIn("lineCount: 1", info)

    def test_read_multiple(self):
        self.ok("write_file", path=self.p("1.txt"), content="first")
        text = self.ok("read_multiple_files", paths=[self.p("1.txt"), self.p("missing.txt")])
        self.assertIn("first", text)
        self.assertIn("ERROR: file not found", text)

    def test_search(self):
        self.ok("write_file", path=self.p("src", "main.py"), content="def hello():\n    return 'Hi'\n")
        self.ok("write_file", path=self.p("src", "util.js"), content="hello();\n")
        self.ok("write_file", path=self.p(".hidden", "secret.py"), content="hello\n")
        self.ok("write_file", path=self.p("node_modules", "x.py"), content="hello\n")
        names = self.ok("search_files", path=self.root, pattern="*.py")
        self.assertIn("main.py", names)
        self.assertNotIn("secret.py", names)
        self.assertIn("secret.py", self.ok("search_files", path=self.root, pattern="secret", include_hidden=True))
        code = self.ok("search_code", path=self.root, pattern=r"hel+o\(")
        self.assertIn("main.py:1:def hello", code)
        self.assertIn("util.js:1:", code)
        self.assertNotIn("node_modules", code)
        self.assertIn("1 matching", self.ok("search_code", path=self.root, pattern="hello(", literal=True,
                                             file_pattern="*.js"))
        self.assertIn("main.py:2:", self.ok("search_code", path=self.root, pattern="HI", ignore_case=True))
        self.assertIn("invalid regular expression", self.err("search_code", path=self.root, pattern="("))
        ctx = self.ok("search_code", path=self.p("src", "main.py"), pattern="return", context_lines=1)
        self.assertIn("main.py:1-def hello", ctx)

    def test_argument_validation(self):
        self.assertIn("missing argument", self.err("read_file"))
        self.assertIn("unknown argument", self.err("read_file", path="x", bogus=1))
        self.assertIn("must be of type integer", self.err("read_file", path="x", offset="1"))
        self.assertIn("must be one of", self.err("write_file", path="x", content="", mode="w"))
        self.ok("list_directory", path=self.root, depth=None)  # null means omitted
        with self.assertRaises(KeyError):
            self.box.call("no_such_tool", {})


class Documents(Sandbox):
    def test_docx(self):
        import zipfile
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        xml = ('<w:document %s><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Title</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>Привет, </w:t></w:r><w:r><w:t>мир</w:t></w:r></w:p>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
               '</w:body></w:document>') % w
        with zipfile.ZipFile(self.p("d.docx"), "w") as zf:
            zf.writestr("word/document.xml", xml)
        self.assertEqual(self.ok("read_file", path=self.p("d.docx")), "# Title\nПривет, мир\n| a | b |\n")

    def test_entity_declarations_refused(self):
        import zipfile
        with zipfile.ZipFile(self.p("bomb.docx"), "w") as zf:
            zf.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
        self.assertIn("refusing to parse", self.err("read_file", path=self.p("bomb.docx")))

    @unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/sbin/cupsfilter"), "macOS only")
    def test_pdf_macos(self):
        import subprocess
        with open(self.p("p.txt"), "w") as f:
            f.write("Hello PDF\nПривет из PDF\n")
        with open(self.p("p.pdf"), "wb") as f:
            subprocess.run(["/usr/sbin/cupsfilter", "-m", "application/pdf", self.p("p.txt")], stdout=f,
                           stderr=subprocess.DEVNULL, check=True)
        self.assertIn("Привет из PDF", self.ok("read_file", path=self.p("p.pdf")))

    def test_xlsx(self):
        import zipfile
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        with zipfile.ZipFile(self.p("t.xlsx"), "w") as zf:
            zf.writestr("xl/workbook.xml", '<workbook %s xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
                        '2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>' % ns)
            zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                        'package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            zf.writestr("xl/sharedStrings.xml", '<sst %s><si><t>name</t></si><si><t>Иван</t></si></sst>' % ns)
            zf.writestr("xl/worksheets/sheet1.xml", '<worksheet %s><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                        '<c r="C1"><v>42</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData></worksheet>' % ns)
        self.assertEqual(self.ok("read_file", path=self.p("t.xlsx")), "## Sheet: Data\nname\t\t42\nИван\n")
        self.assertIn("no sheet", self.err("read_file", path=self.p("t.xlsx"), sheet="Nope"))
        self.assertIn("Data", self.ok("read_file", path=self.p("t.xlsx"), sheet="0"))

    def test_url(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = "line1\nline2\nстрока3\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            from unittest import mock
            url = "http://127.0.0.1:%d/x" % srv.server_port
            self.assertIn("non-public address 127.0.0.1", self.err("read_file", path=url, is_url=True))
            self.assertIn("non-public", self.err("read_file", path="http://169.254.169.254/latest", is_url=True))
            self.assertIn("non-public", self.err("read_file", path="http://[::ffff:127.0.0.1]:%d/" % srv.server_port,
                                                 is_url=True))
            with mock.patch("bridge_commander.files._check_ip"):  # this test server is necessarily local
                text = self.ok("read_file", path=url, is_url=True, offset=-1)
            self.assertIn("200 text/plain", text)
            self.assertIn("строка3", text)
            self.assertIn("only http", self.err("read_file", path="file:///etc/passwd", is_url=True))
        finally:
            srv.shutdown()
            srv.server_close()


class History(Sandbox):
    def setUp(self):
        super().setUp()
        self.cfg["auditLog"] = os.path.join(self.tmp, "audit.jsonl")

    def test_recent_calls_and_stats(self):
        self.ok("write_file", path=self.p("h.txt"), content="hello")
        self.err("read_file", path=self.p("missing.txt"))
        recent = self.ok("get_recent_tool_calls")
        self.assertIn("write_file", recent)
        self.assertIn("ERROR", recent)
        self.assertIn('"content": "<5 chars>"', recent)
        self.assertIn("result: Wrote 5 bytes", recent)
        self.assertNotIn("get_recent_tool_calls |", self.ok("get_recent_tool_calls"))
        self.assertIn("1 call(s)", self.ok("get_recent_tool_calls", tool_name="read_file"))
        self.assertIn("No recorded", self.ok("get_recent_tool_calls", since="2999"))
        stats = self.ok("get_usage_stats")
        self.assertIn("2 calls", stats)
        self.assertRegex(stats, r"read_file\s+1 calls\s+1 failed")


class ReadOnly(Sandbox):
    extra = {"readOnly": True}

    def test_mutations_refused(self):
        self.assertIn("read-only", self.err("write_file", path=self.p("x"), content="x"))
        self.assertIn("read-only", self.err("start_process", command="echo hi"))
        self.assertIn("read-only", self.err("kill_process", pid=999999))
        self.ok("list_directory", path=self.root)


class RuntimeConfig(Sandbox):
    def test_disabled_by_default(self):
        self.assertIn("disabled", self.err("set_config_value", key="readOnly", value=True))

    def test_enabled(self):
        self.cfg["allowConfigChanges"] = True
        self.assertIn("cannot be changed", self.err("set_config_value", key="nodes", value={}))
        self.assertIn("must be true or false", self.err("set_config_value", key="readOnly", value="yes"))
        self.ok("set_config_value", key="allowedDirectories", value=[self.tmp])
        self.ok("write_file", path=os.path.join(self.tmp, "now-ok.txt"), content="x")
        self.assertIn('"version"', self.ok("get_config"))


class ConfigValidation(unittest.TestCase):
    def test_rejects_bad_config(self):
        for bad in ({"nope": 1}, {"readOnly": "yes"}, {"nodes": {"local": {"command": ["x"]}}},
                    {"nodes": {"a": {"command": "ssh a"}}}, {"http": {"port": "80"}}, {"maxSessions": 0}):
            with self.assertRaises(ConfigError, msg=bad):
                validate(bad)

    def test_defaults(self):
        cfg = validate({})
        self.assertEqual(cfg["allowedDirectories"], ["~"])
        self.assertIn("sudo", cfg["blockedCommands"])
        self.assertEqual(cfg["http"]["host"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
