"""Bound edits and compressed-document expansion without allocating oversized fixtures."""
from pathlib import Path
import sys
import time
import zipfile

from helpers import Sandbox
from bridge_commander.files import ToolError, _bounded_stdout, _col


class DocumentLimits(Sandbox):
    def archive(self, name, parts):
        path = self.p(name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, text in parts.items():
                archive.writestr(member, text)
        return path

    def workbook(self, sheet, shared=None, padding=""):
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        parts = {
            "xl/workbook.xml": '<workbook %s xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" r:id="rId1"/></sheets></workbook>' % ns,
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '<worksheet %s><sheetData>%s</sheetData>%s</worksheet>' % (ns, sheet, padding),
        }
        if shared is not None:
            parts["xl/sharedStrings.xml"] = '<sst %s><si><t>%s</t></si></sst>' % (ns, shared)
        return self.archive("limits.xlsx", parts)

    def test_edit_refuses_oversized_input_without_changing_it(self):
        self.cfg["maxFileSizeBytes"] = 16
        path = Path(self.p("large.txt"))
        original = b"old" + b"x" * 14
        path.write_bytes(original)
        self.assertIn("maxFileSizeBytes", self.err("edit_block", file_path=str(path), old_string="old", new_string="new"))
        self.assertEqual(path.read_bytes(), original)

    def test_edit_checks_projected_utf8_bytes_before_expanding(self):
        self.cfg["maxFileSizeBytes"] = 16
        path = Path(self.p("expand.txt"))
        path.write_bytes(b"a" * 8)
        self.assertIn("maxFileSizeBytes", self.err("edit_block", file_path=str(path), old_string="a",
                                                 new_string="€€", expected_replacements=8))
        self.assertEqual(path.read_bytes(), b"a" * 8)

    def test_xlsx_column_is_bounded_before_dense_row_allocation(self):
        self.assertEqual(_col("XFD1"), 16383)
        for ref in ("XFE1", "ZZZZZZZZ1", "bad", "A0"):
            with self.subTest(ref=ref), self.assertRaises(ToolError):
                _col(ref)
        path = self.workbook('<row><c r="XFE1"><v>1</v></c></row>')
        self.assertIn("column", self.err("read_file", path=path))

    def test_xlsx_shared_strings_cannot_amplify_output_past_limit(self):
        self.cfg["maxFileSizeBytes"] = 2048
        row = '<row>' + ''.join('<c r="%s1" t="s"><v>0</v></c>' % c for c in "ABC") + '</row>'
        path = self.workbook(row, shared="x" * 1000)
        self.assertLess(Path(path).stat().st_size, self.cfg["maxFileSizeBytes"])
        self.assertIn("extracted text", self.err("read_file", path=path))

    def test_xlsx_aggregate_xml_is_bounded_even_when_each_part_fits(self):
        self.cfg["maxFileSizeBytes"] = 2048
        path = self.workbook('<row><c r="A1" t="s"><v>0</v></c></row>', shared=" " * 6000, padding=" " * 6000)
        self.assertLess(Path(path).stat().st_size, self.cfg["maxFileSizeBytes"])
        self.assertIn("uncompressed XML", self.err("read_file", path=path))

    def test_xlsx_budget_counts_actual_parses_regardless_of_extension_or_repeat(self):
        self.cfg["maxFileSizeBytes"] = 2048
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        for targets in (("worksheets/one.bin", "worksheets/two.bin"), ("worksheets/same.xml", "worksheets/same.xml")):
            with self.subTest(targets=targets):
                parts = {
                    "xl/workbook.xml": '<workbook %s xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>%s</sheets></workbook>' % (
                        ns, ''.join('<sheet name="S%d" r:id="rId%d"/>' % (i, i) for i in range(2))),
                    "xl/_rels/workbook.xml.rels": '<Relationships>%s</Relationships>' % ''.join(
                        '<Relationship Id="rId%d" Target="%s"/>' % (i, target) for i, target in enumerate(targets)),
                }
                for target in targets:
                    parts["xl/" + target] = '<worksheet %s><sheetData><row><c r="A1"><v>1</v></c></row></sheetData>%s</worksheet>' % (ns, ' ' * 6000)
                path = self.archive("parsed-parts.xlsx", parts)
                self.assertLess(Path(path).stat().st_size, self.cfg["maxFileSizeBytes"])
                self.assertIn("uncompressed XML", self.err("read_file", path=path))

    def test_utf16_xml_cannot_bypass_entity_refusal(self):
        xml = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "CANARY">]>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body></w:document>').encode("utf-16")
        path = self.archive("entity.docx", {"word/document.xml": xml})
        self.assertIn("DTD", self.err("read_file", path=path))

    def test_pdf_converter_output_and_runtime_are_bounded(self):
        argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 1025)"]
        with self.assertRaisesRegex(ToolError, "maxFileSizeBytes"):
            _bounded_stdout(argv, 1024, timeout=2)
        started = time.monotonic()
        with self.assertRaisesRegex(ToolError, "timed out"):
            _bounded_stdout([sys.executable, "-c", "import time; time.sleep(5)"], 1024, timeout=0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(_bounded_stdout([sys.executable, "-c", "print('ok')"], 1024, timeout=2), b"ok\n")
