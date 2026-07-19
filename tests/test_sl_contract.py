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

    async def test_sl_reloads_autosave_and_waits_for_combat(self):
        server = self.server
        calls = []
        states = [
            {"state_type": "monster", "battle": {"turn": "player", "is_play_phase": True}},
            {"state_type": "loading"},
            {"state_type": "monster", "battle": {"turn": "player", "is_play_phase": False}},
            {"state_type": "monster", "battle": {"turn": "player", "is_play_phase": True}},
        ]

        async def fake_post(body):
            calls.append(("_post", body))
            if body == {"action": "restart_combat"}:
                return json.dumps({"status": "ok", "message": "Reloaded combat autosave"})
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
            mock.patch.object(server.anyio, "sleep", fake_sleep),
        ):
            result = json.loads(await server.sl())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["state"]["state_type"], "monster")
        self.assertIs(result["state"]["battle"]["is_play_phase"], True)
        self.assertEqual(
            [call for call in calls if call[0] == "_post"],
            [
                ("_post", {"action": "restart_combat"}),
            ],
        )

    async def test_sl_returns_error_when_combat_restart_fails(self):
        server = self.server

        async def fake_post(body):
            if body == {"action": "restart_combat"}:
                return json.dumps({"status": "error", "error": "Autosave unavailable"})
            raise AssertionError(f"unexpected POST body: {body!r}")

        async def fake_get(params=None):
            return json.dumps(
                {"state_type": "monster", "battle": {"turn": "player", "is_play_phase": True}}
            )

        with (
            mock.patch.object(server, "_post", fake_post),
            mock.patch.object(server, "_get", fake_get),
        ):
            result = json.loads(await server.sl())

        self.assertEqual(result["status"], "error")
        self.assertIn("Autosave", result["error"])

    async def test_sl_rejects_non_combat_state_before_restarting(self):
        server = self.server
        post_bodies = []

        async def fake_post(body):
            post_bodies.append(body)
            return json.dumps({"status": "ok"})

        async def fake_get(params=None):
            return json.dumps({"state_type": "map"})

        with (
            mock.patch.object(server, "_post", fake_post),
            mock.patch.object(server, "_get", fake_get),
        ):
            result = json.loads(await server.sl())

        self.assertEqual(result["status"], "error")
        self.assertIn("combat", result["error"].lower())
        self.assertEqual(post_bodies, [])


class StaticContractTest(unittest.TestCase):
    def test_csharp_exposes_native_combat_restart_action(self):
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8-sig")
        mod = (ROOT / "McpMod.cs").read_text(encoding="utf-8-sig")

        self.assertIn('"restart_combat"', actions + mod)
        self.assertIn("LoadRunSave", actions)
        self.assertIn("SetUpSavedSingleplayer", actions)
        self.assertIn("LoadRun", actions)
        self.assertNotIn("NPauseMenu", actions)

    def test_delete_profile_is_not_exposed_as_mcp_tool(self):
        server = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        readme = (ROOT / "mcp" / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("async def delete_profile", server)
        self.assertNotIn("delete_profile", readme)


class BridgeFailureContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server_module()

    def test_empty_transport_errors_keep_exception_type_and_diagnostic_context(self):
        for error in (
            self.server.httpx.ReadError(""),
            self.server.httpx.ReadTimeout(""),
            TimeoutError(),
        ):
            rendered = self.server._handle_error(error)
            self.assertIn(type(error).__name__, rendered)
            self.assertRegex(rendered, r"detail=\S+")

    def test_csharp_main_thread_wait_has_a_bound_and_busy_guard(self):
        source = (ROOT / "McpMod.cs").read_text(encoding="utf-8-sig")

        self.assertIn("MainThreadRequestTimeoutException", source)
        self.assertIn("MainThreadRequestBusyException", source)
        self.assertIn("MainThreadRequestTimeoutMilliseconds", source)
        self.assertIn("RunOnMainThreadAndWait", source)
        self.assertIn(
            "RunOnMainThreadAndWaitAsync(ExecuteRestartCombatAsync)",
            source,
        )
        self.assertNotRegex(
            source,
            r"RunOnMainThread\([^\n]+\)\.GetAwaiter\(\)\.GetResult\(\)",
        )
