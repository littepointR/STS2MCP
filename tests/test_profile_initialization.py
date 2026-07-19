import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_server_module():
    module_name = "sts2_mcp_profile_server_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProfileInitializationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server_module()

    async def test_initialize_profile_resolves_pending_epochs_until_main_menu_ready(self):
        server = self.server
        states = [
            {
                "state_type": "menu",
                "menu_screen": "main",
                "blocked_options": [
                    {
                        "name": "timeline",
                        "reason": "manual_epoch_reveal_required",
                        "pending_epoch_ids": ["NEOW_EPOCH"],
                    }
                ],
            },
            {"state_type": "menu", "menu_screen": "timeline", "options": ["advance"]},
            {"state_type": "menu", "menu_screen": "main", "options": ["singleplayer"]},
        ]
        post_bodies = []

        async def fake_get(params=None):
            self.assertEqual(params, {"format": "json"})
            if not states:
                raise AssertionError("initialize_profile() polled too many times")
            return json.dumps(states.pop(0))

        async def fake_post(body):
            post_bodies.append(body)
            return json.dumps({"status": "ok", "phase": "advanced"})

        async def fake_sleep(_seconds):
            return None

        with (
            mock.patch.object(server, "_get", fake_get),
            mock.patch.object(server, "_post", fake_post),
            mock.patch.object(server.anyio, "sleep", fake_sleep),
        ):
            result = json.loads(await server.initialize_profile())

        self.assertEqual(result["status"], "ok")
        self.assertIs(result["ready"], True)
        self.assertEqual(result["state"]["options"], ["singleplayer"])
        self.assertEqual(
            post_bodies,
            [
                {"action": "resolve_pending_epochs"},
                {"action": "resolve_pending_epochs"},
            ],
        )

    async def test_initialize_profile_is_idempotent_when_ready(self):
        server = self.server
        post_bodies = []

        async def fake_get(params=None):
            self.assertEqual(params, {"format": "json"})
            return json.dumps(
                {"state_type": "menu", "menu_screen": "main", "options": ["singleplayer"]}
            )

        async def fake_post(body):
            post_bodies.append(body)
            return json.dumps({"status": "ok"})

        with (
            mock.patch.object(server, "_get", fake_get),
            mock.patch.object(server, "_post", fake_post),
        ):
            result = json.loads(await server.initialize_profile())

        self.assertEqual(result["status"], "ok")
        self.assertIs(result["ready"], True)
        self.assertEqual(post_bodies, [])

    async def test_initialize_profile_returns_action_error(self):
        server = self.server
        pending_state = {
            "state_type": "menu",
            "menu_screen": "main",
            "blocked_options": [
                {
                    "name": "timeline",
                    "reason": "manual_epoch_reveal_required",
                    "pending_epoch_ids": ["NEOW_EPOCH"],
                }
            ],
        }

        async def fake_get(params=None):
            self.assertEqual(params, {"format": "json"})
            return json.dumps(pending_state)

        async def fake_post(body):
            self.assertEqual(body, {"action": "resolve_pending_epochs"})
            return json.dumps({"status": "error", "error": "Timeline button unavailable"})

        with (
            mock.patch.object(server, "_get", fake_get),
            mock.patch.object(server, "_post", fake_post),
        ):
            result = json.loads(await server.initialize_profile())

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["action_result"]["error"], "Timeline button unavailable")
        self.assertEqual(result["last_state"], pending_state)

    async def test_initialize_profile_returns_structured_http_error(self):
        server = self.server

        async def fake_get(params=None):
            self.assertEqual(params, {"format": "json"})
            raise server.httpx.ConnectError("connection refused")

        with mock.patch.object(server, "_get", fake_get):
            result = json.loads(await server.initialize_profile())

        self.assertEqual(result["status"], "error")
        self.assertIn("Cannot connect to STS2_MCP mod", result["error"])
        self.assertIn("connection refused", result["detail"])

    async def test_initialize_profile_enforces_wall_clock_timeout(self):
        server = self.server

        class ImmediateTimeout:
            def __enter__(self):
                raise TimeoutError

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

        async def fake_get(params=None):
            self.assertEqual(params, {"format": "json"})
            return json.dumps(
                {"state_type": "menu", "menu_screen": "main", "options": []}
            )

        with (
            mock.patch.object(server, "_get", fake_get),
            mock.patch.object(
                server.anyio,
                "fail_after",
                return_value=ImmediateTimeout(),
            ),
        ):
            result = json.loads(await server.initialize_profile())

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["timeout_seconds"],
            server.PROFILE_INITIALIZATION_TIMEOUT_SECONDS,
        )


class StaticProfileInitializationContractTest(unittest.TestCase):
    def test_unattended_profile_initialization_contract_is_exposed(self):
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8-sig")
        mod = (ROOT / "McpMod.cs").read_text(encoding="utf-8-sig")
        state_builder = (ROOT / "McpMod.StateBuilder.cs").read_text(encoding="utf-8-sig")
        server = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")

        self.assertIn("ExecuteResolvePendingEpochs", actions)
        self.assertIn('"resolve_pending_epochs"', mod)
        self.assertIn('["next_action"] = "initialize_profile"', state_builder)
        self.assertIn("async def initialize_profile", server)


    def test_timeline_automation_preserves_native_inspect_and_queue_ordering(self):
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8-sig")

        self.assertIn("Waiting for epoch inspect animation to finish", actions)
        self.assertNotIn("inspectScreen.Close();", actions)
        self.assertNotIn(
            'GetInstanceFieldValue(timelineScreen, "_isUiVisible")',
            actions,
        )
        self.assertNotIn("IsTimelineScreenBusy(timelineScreen)", actions)
        self.assertNotIn("private static bool IsTimelineScreenBusy", actions)

    def test_resolver_returns_to_main_menu_before_advancing_when_complete(self):
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8-sig")
        resolver = actions[
            actions.index("ExecuteResolvePendingEpochs") : actions.index(
                "ExecuteAction", actions.index("ExecuteResolvePendingEpochs")
            )
        ]

        self.assertLess(
            resolver.index("if (pendingEpochIds.Count == 0)"),
            resolver.index('ExecuteMenuSelect("advance")'),
        )

    def test_timeline_advance_reveals_obtained_epoch_slot_via_native_click(self):
        actions = (ROOT / "McpMod.Actions.cs").read_text(encoding="utf-8-sig")

        self.assertIn(
            "TryRevealPendingTimelineEpoch(timelineScreen, unrevealedEpochs)",
            actions,
        )
        self.assertIn("FindAll<NEpochSlot>(timelineScreen)", actions)
        self.assertIn("slot.State == EpochSlotState.Obtained", actions)
        self.assertIn("slot.ForceClick();", actions)
