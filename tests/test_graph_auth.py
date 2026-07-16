from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from core import db
from core.graph.auth import DEFAULT_GRAPH_CLIENT_ID, GraphAuth


class FakeSerializableCache:
    def __init__(self) -> None:
        self.data = {"accounts": []}
        self.has_state_changed = False

    def deserialize(self, serialized: str) -> None:
        self.data = json.loads(serialized)
        self.has_state_changed = False

    def serialize(self) -> str:
        self.has_state_changed = False
        return json.dumps(self.data)


class FakePublicClientApplication:
    interactive_calls: list[dict] = []

    def __init__(self, *, client_id, authority, token_cache) -> None:
        self.client_id = client_id
        self.authority = authority
        self.cache = token_cache

    def get_accounts(self):
        return list(self.cache.data["accounts"])

    def initiate_device_flow(self, scopes):
        return {
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
        }

    def acquire_token_by_device_flow(self, flow):
        del flow
        return self._complete_login()

    def acquire_token_interactive(self, scopes, timeout=None):
        self.interactive_calls.append({"scopes": scopes, "timeout": timeout})
        return self._complete_login()

    def _complete_login(self):
        self.cache.data = {
            "accounts": [
                {
                    "username": "person@example.com",
                    "name": "Person",
                    "home_account_id": "home-1",
                }
            ],
            "access_token": "test-token",
        }
        self.cache.has_state_changed = True
        return {"access_token": "test-token"}

    def acquire_token_silent(self, scopes, account):
        del scopes, account
        token = self.cache.data.get("access_token")
        return {"access_token": token} if token else None


class GraphAuthCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        FakePublicClientApplication.interactive_calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.old_workspace = db.WORKSPACE_DIR
        self.old_db_path = db.DB_PATH
        db.WORKSPACE_DIR = Path(self.tmp.name) / "workspace"
        db.DB_PATH = db.WORKSPACE_DIR / "state.db"
        db.init_db()

    def tearDown(self) -> None:
        db.WORKSPACE_DIR = self.old_workspace
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _auth(self) -> GraphAuth:
        return GraphAuth(
            app_factory=FakePublicClientApplication,
            cache_factory=FakeSerializableCache,
        )

    def test_device_flow_persists_cache_with_0600_and_next_instance_reads_it(self) -> None:
        auth = self._auth()
        auth.set_client_id("00000000-0000-0000-0000-123456789abc")
        flow = auth.start_device_flow()

        account = auth.complete_device_flow(flow)

        cache_path = db.WORKSPACE_DIR / "graph_token_cache.json"
        self.assertTrue(cache_path.exists())
        self.assertEqual(0o600, stat.S_IMODE(cache_path.stat().st_mode))
        self.assertEqual("person@example.com", account["username"])

        reloaded = self._auth()
        self.assertEqual("test-token", reloaded.get_access_token())
        self.assertEqual("person@example.com", reloaded.account_info()["username"])

    def test_default_client_id_is_configured_and_only_exposes_tail(self) -> None:
        auth = self._auth()

        self.assertEqual(DEFAULT_GRAPH_CLIENT_ID, auth.client_id())
        self.assertEqual(
            {"configured": True, "using_default": True, "client_id_tail": "b173"},
            auth.client_id_info(),
        )

    def test_custom_client_id_overrides_default_and_can_be_cleared(self) -> None:
        auth = self._auth()
        auth.set_client_id("client-id-secret-1234")

        info = auth.client_id_info()

        self.assertEqual(
            {"configured": True, "using_default": False, "client_id_tail": "1234"},
            info,
        )
        self.assertNotIn("client-id-secret", str(info))

        auth.clear_client_id()

        self.assertEqual(DEFAULT_GRAPH_CLIENT_ID, auth.client_id())
        self.assertTrue(auth.client_id_info()["using_default"])

    def test_interactive_flow_uses_300_second_timeout_and_persists_cache(self) -> None:
        auth = self._auth()

        account = auth.complete_interactive_flow()

        self.assertEqual("person@example.com", account["username"])
        self.assertEqual(
            [{"scopes": ["Calendars.ReadWrite", "User.Read"], "timeout": 300}],
            FakePublicClientApplication.interactive_calls,
        )
        reloaded = self._auth()
        self.assertEqual("test-token", reloaded.get_access_token())

    def test_logout_removes_token_cache(self) -> None:
        auth = self._auth()
        auth.set_client_id("client-1234")
        auth.complete_device_flow(auth.start_device_flow())

        auth.logout()

        self.assertFalse((db.WORKSPACE_DIR / "graph_token_cache.json").exists())


if __name__ == "__main__":
    unittest.main()
