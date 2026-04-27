from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SlContractTests(unittest.TestCase):
    def test_python_mcp_exposes_blocking_sl_tool(self):
        server = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")

        self.assertIn("async def sl() -> str:", server)
        self.assertIn('return await _post({"action": "sl"}, timeout=75)', server)
        self.assertIn("Save and quit to the main menu, continue the run", server)

    def test_csharp_sl_action_uses_async_http_path_and_main_thread_steps(self):
        core = (ROOT / "McpMod.cs").read_text(encoding="utf-8")
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8")

        self.assertIn('action == "sl"', core)
        self.assertIn("ExecuteSlAsync()", core)
        self.assertNotIn('"sl" => ExecuteSl', actions)
        self.assertIn("RunOnMainThread", actions)
        self.assertIn("NMainMenuContinueButton", actions)
        self.assertIn("NPauseMenu", actions)
        self.assertIn("NTopBarPauseButton", actions)


if __name__ == "__main__":
    unittest.main()
