import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from bridge_commander.config import validate  # noqa: E402
from bridge_commander.tools import Toolbox  # noqa: E402


class Sandbox(unittest.TestCase):
    """A temp dir as the only allowed directory, auditing off."""

    extra = {}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="bc-test-"))
        self.root = os.path.join(self.tmp, "allowed")
        os.mkdir(self.root)
        self.cfg = validate(dict({"allowedDirectories": [self.root], "auditLog": None}, **self.extra))
        self.box = Toolbox(self.cfg)

    def tearDown(self):
        self.box.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def call(self, tool, **args):
        content, is_error = self.box.call(tool, args)
        return content[0]["text"] if content and content[0]["type"] == "text" else content, is_error

    def ok(self, tool, **args):
        text, is_error = self.call(tool, **args)
        self.assertFalse(is_error, text)
        return text

    def err(self, tool, **args):
        text, is_error = self.call(tool, **args)
        self.assertTrue(is_error, text)
        return text


def worker_command(config_file):
    return [sys.executable, "-c", "import sys; sys.path.insert(0, %r); from bridge_commander.cli import main; "
            "main(['--config', %r, 'worker'])" % (SRC, config_file)]


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)
    return path
