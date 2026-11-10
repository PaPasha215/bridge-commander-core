import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bridge_commander.cli import init_config


class FirstRun(unittest.TestCase):
    def test_explicit_shared_folder_starts_read_only_and_config_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            shared = Path(tmp) / "shared"
            init_config(config, False, [str(shared)])
            data = json.loads(config.read_text())
            self.assertEqual(data["allowedDirectories"], [str(shared.resolve())])
            self.assertTrue(data["readOnly"])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            init_config(config, True, [str(shared)], allow_writes=True)
            self.assertFalse(json.loads(config.read_text())["readOnly"])

    def test_no_config_remote_refuses_before_network_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, BRIDGE_COMMANDER_CONFIG=str(Path(tmp) / "missing.json"))
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            result = subprocess.run([sys.executable, "-m", "bridge_commander", "remote", "--cloud", "https://invalid.example"],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("create an explicit sharing config", result.stderr)
            self.assertFalse((Path(tmp) / ".config/bridge-commander/device.json").exists())
