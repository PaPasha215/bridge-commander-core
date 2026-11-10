import unittest

from helpers import Sandbox
from bridge_commander.tools import tool_specs


class ExternalActions(unittest.TestCase):
    def test_network_capable_tools_declare_open_world(self):
        specs = {row["name"]: row["annotations"] for row in tool_specs()}
        for tool in ("read_file", "start_process", "interact_with_process"):
            self.assertTrue(specs[tool]["openWorldHint"])
        self.assertFalse(specs["get_config"]["openWorldHint"])
