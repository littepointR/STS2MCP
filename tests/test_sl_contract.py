import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_server_module():
    module_name = "sts2_mcp_server_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SlContractTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server_module()

    async def test_sl_saves_to_menu_then_uses_menu_continue(self):
        server = self.server
        calls = []
        states = [
            {"state_type": "menu", "menu_screen": "main", "options": ["continue"]},
            {"state_type": "map"},
        ]

        async def fake_post(body, timeout=10):
            calls.append(("_post", body, timeout))
            if body == {"action": "save_and_quit_to_menu"}:
                return json.dumps({"status": "ok", "message": "Saved and quit to menu"})
            if body == {"action": "menu_select", "option": "continue"}:
                return json.dumps({"status": "ok", "message": "Selected continue"})
            raise AssertionError(f"unexpected POST body: {body!r}")

        async def fake_get(params=None):
            calls.append(("_get", params))
            if not states:
                raise AssertionError("sl() polled game state too many times")
            return json.dumps(states.pop(0))

        async def fake_sleep(_seconds):
            calls.append(("sleep", _seconds))

        with (
            mock.patch.object(server, "_post", fake_post),
            mock.patch.object(server, "_get", fake_get),
            mock.patch.object(server.asyncio, "sleep", fake_sleep),
        ):
            result = json.loads(await server.sl())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [call for call in calls if call[0] == "_post"],
            [
                ("_post", {"action": "save_and_quit_to_menu"}, 10),
                ("_post", {"action": "menu_select", "option": "continue"}, 10),
            ],
        )
        self.assertNotIn(("_post", {"action": "sl"}, 75), calls)

    async def test_sl_waits_past_transient_unknown_and_combat_loading_state(self):
        server = self.server
        calls = []
        states = [
            {"state_type": "menu", "menu_screen": "main", "options": ["continue"]},
            {"state_type": "unknown", "run": {"floor": 32}},
            {
                "state_type": "boss",
                "battle": {"turn": "player", "is_play_phase": False},
                "player": {"hand": [], "energy": 0},
            },
            {
                "state_type": "boss",
                "battle": {"turn": "player", "is_play_phase": True},
                "player": {"hand": [{"index": 0}], "energy": 3},
            },
        ]

        async def fake_post(body, timeout=10):
            calls.append(("_post", body, timeout))
            if body == {"action": "save_and_quit_to_menu"}:
                return json.dumps({"status": "ok", "message": "Saved and quit to menu"})
            if body == {"action": "menu_select", "option": "continue"}:
                return json.dumps({"status": "ok", "message": "Selected continue"})
            raise AssertionError(f"unexpected POST body: {body!r}")

        async def fake_get(params=None):
            calls.append(("_get", params))
            if not states:
                raise AssertionError("sl() polled game state too many times")
            return json.dumps(states.pop(0))

        async def fake_sleep(_seconds):
            calls.append(("sleep", _seconds))

        with (
            mock.patch.object(server, "_post", fake_post),
            mock.patch.object(server, "_get", fake_get),
            mock.patch.object(server.asyncio, "sleep", fake_sleep),
        ):
            result = json.loads(await server.sl())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["state"]["state_type"], "boss")
        self.assertTrue(result["state"]["battle"]["is_play_phase"])
        self.assertGreaterEqual(len([call for call in calls if call[0] == "_get"]), 4)

    async def test_sl_returns_error_when_continue_is_not_available(self):
        server = self.server

        async def fake_post(body, timeout=10):
            if body == {"action": "save_and_quit_to_menu"}:
                return json.dumps({"status": "ok"})
            raise AssertionError(f"unexpected POST body: {body!r}")

        async def fake_get(params=None):
            return json.dumps({"state_type": "menu", "menu_screen": "main", "options": ["singleplayer"]})

        async def fake_sleep(_seconds):
            return None

        with (
            mock.patch.object(server, "_post", fake_post),
            mock.patch.object(server, "_get", fake_get),
            mock.patch.object(server.asyncio, "sleep", fake_sleep),
        ):
            result = json.loads(await server.sl())

        self.assertEqual(result["status"], "error")
        self.assertIn("continue", result["error"])


class StaticContractTest(unittest.TestCase):
    def test_csharp_exposes_only_save_and_quit_to_menu_not_monolithic_sl(self):
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8-sig")
        mod = (ROOT / "McpMod.cs").read_text(encoding="utf-8-sig")

        self.assertNotIn("ExecuteSlAsync", actions + mod)
        self.assertIn('"save_and_quit_to_menu"', actions + mod)
        self.assertIn("No singleplayer run in progress", actions)
        self.assertIn("NPauseMenu", actions)
        self.assertIn("NTopBarPauseButton", actions)
        self.assertIn("Save", actions)
        self.assertNotIn('"sl"', actions + mod)

    def test_delete_profile_is_not_exposed_as_mcp_tool(self):
        server = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        readme = (ROOT / "mcp" / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("async def delete_profile", server)
        self.assertNotIn("delete_profile", readme)

    def test_timeline_menu_entry_remains_selectable_with_pending_epochs(self):
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8-sig")
        state_builder = (ROOT / "McpMod.StateBuilder.cs").read_text(encoding="utf-8-sig")
        server = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        docs = "\n".join(
            [
                (ROOT / "docs" / "raw-simplified.md").read_text(encoding="utf-8"),
                (ROOT / "docs" / "raw-full.md").read_text(encoding="utf-8"),
            ]
        )

        self.assertNotIn("TimelineUnlocksNeedManualReveal", actions)
        self.assertNotIn('normalizedMainMenuOption == "timeline"', actions)
        self.assertIn('"timeline" => "_timelineButton"', actions)
        self.assertIn("timelineOptionVisible", state_builder)
        self.assertIn('["enabled"] = true', state_builder)
        self.assertIn("blocked_options are advisory metadata", server)
        self.assertIn('`menu_select("timeline")` still opens Timeline', docs)
        self.assertNotIn("instead of opening Timeline", docs)


if __name__ == "__main__":
    unittest.main()
