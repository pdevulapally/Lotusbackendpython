from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from conversation import READY, Reply
from tests.helpers import ServiceTestCase, make_settings
from tests.test_authorization import MemoryStore
from workspace_dispatch import Dispatcher
from workspace_runs import run_key, slot_path


class ConversationTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.settings = make_settings(
            rate_limit_policies={"default": {"requests": 200, "window_seconds": 60}},
            pre_auth_rate_limit={"requests": 500, "window_seconds": 60},
        )
        self.app_ = self.app(settings=self.settings)
        self.enterContext(self.services())
        self.client = self.enterContext(TestClient(self.app_, client=("198.51.100.1", 1000)))
        self.account = SimpleNamespace(disabled=False, tokens_valid_after_timestamp=0)
        self.enterContext(patch("workspace_dispatch.auth.get_user", return_value=self.account))
        self.store = MemoryStore()
        self.install_policy(self.settings, self.store)
        self.app_.state.resource_database = self.store
        self.body = {"name": "Private", "operation_id": str(uuid4())}
        self.base = "/api/v1/projects"
        self.project = self.create().json()["id"]
        self.root = f"{self.base}/{self.project}"
        self.session = uuid4()
        self.url = f"{self.root}/sessions/{self.session}/run"
        self.path = self.settings.ai_runs_collection, run_key(self.project, self.session.hex)
        self.model = Mock()
        self.tools = Mock()
        self.legacy_model = Mock()
        self.config = SimpleNamespace(lease_seconds=20, context_max_bytes=16384, max_steps=5)

    def create(self, **kw):
        return self.client.post(self.base, json=self.body, **kw)

    def ask(self, text="Explain a lock", **kw):
        return self.client.put(self.url + "?mode=conversation", json={"question": text}, **kw)

    def tick(self, admitted=True):
        # Construct a fresh dispatcher on each step: only the store survives restarts.
        return Dispatcher(
            self.app_,
            self.config,
            self.tools,
            self.legacy_model,
            admitted_conversation_model=self.model if admitted else None,
        ).tick()

    def result(self, kind, text):
        return SimpleNamespace(
            stop_reason="end_turn",
            parsed_output=Reply(kind=kind, text=text),
            usage=Mock(model_dump=Mock(return_value={"output_tokens": 1})),
        )

    def tearDown(self):
        self.assertEqual(self.tools.mock_calls, [])
        self.assertEqual(self.legacy_model.mock_calls, [])
        super().tearDown()

    def test_project_receipts_and_conflicting_concurrent_commit(self):
        self.assertEqual(self.create().json()["id"], self.project)
        self.assertEqual(
            self.client.post(self.base, json={**self.body, "name": "Other"}).status_code, 409
        )
        self.assertNotEqual(
            self.create(headers={"X-Test-Verified-User": "bob"}).json()["id"], self.project
        )
        self.body["operation_id"] = str(uuid4())
        winners = []
        self.store.before_commit = lambda: winners.append(self.create().json()["id"])
        concurrent = self.create()
        concurrent.raise_for_status()
        self.assertEqual(concurrent.json()["id"], winners[0])
        self.assertEqual(self.create().json()["id"], winners[0])
        self.assertEqual(len(self.client.get(self.base).json()["projects"]), 2)

    def test_clarification_replay_order_and_reconnect(self):
        self.ask().raise_for_status()
        self.ask().raise_for_status()
        self.assertEqual(self.ask("different").status_code, 409)
        self.model.messages.parse.side_effect = [
            self.result("clarify", "Which lock?"),
            self.result("answer", "A mutex."),
        ]
        self.tick()
        first = self.client.get(self.root + "/conversation").json()
        self.assertEqual(first["status"], "clarification")
        target = self.url + "/messages/" + str(uuid4())
        self.assertEqual(
            self.client.put(target + "?reply_to=1", json={"question": "Threads"}).status_code, 409
        )
        for _ in range(2):
            self.client.put(target + "?reply_to=2", json={"question": "Threads"}).raise_for_status()
        self.assertEqual(
            self.client.put(target + "?reply_to=2", json={"question": "Changed"}).status_code, 409
        )
        self.tick()
        result = self.client.get(self.url + "?after=2").json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual([e["kind"] for e in result["events"]], ["reply", "answer"])
        self.assertEqual(
            self.model.messages.parse.call_args.kwargs["messages"],
            [
                {"role": "user", "content": "Explain a lock"},
                {"role": "assistant", "content": "Which lock?"},
                {"role": "user", "content": "Threads"},
            ],
        )
        self.assertFalse(self.tick())
        self.assertEqual(self.client.get(self.url + "?after=4").json()["events"], [])

    def test_stop_and_correction_during_decision(self):
        self.ask().raise_for_status()

        def corrected(**_):
            self.client.put(
                self.url + "/messages/" + str(uuid4()), json={"question": "Use Python"}
            ).raise_for_status()
            return self.result("answer", "stale answer")

        self.model.messages.parse.side_effect = corrected
        self.tick()
        self.assertEqual(self.store.documents[self.path]["status"], READY)

        def stopped(**_):
            for _ in range(2):
                self.assertEqual(self.client.put(self.url + "/stop").json()["status"], "stopping")
            return self.result("answer", "late answer")

        self.model.messages.parse.side_effect = stopped
        self.tick()
        final = self.client.get(self.url).json()
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(
            [e["kind"] for e in final["events"]],
            ["question", "correction", "stop_requested", "stopped"],
        )
        self.assertFalse(self.tick())

    def test_no_workspace_even_with_existing_binding_and_no_paid_fallback(self):
        self.store.change(
            slot_path(self.settings, self.project),
            {"generation": 1, "active": None, "sandbox": "existing"},
        )
        self.ask().raise_for_status()
        self.tick(admitted=False)
        self.assertEqual(self.client.get(self.url).json()["status"], "blocked_budget")
        self.model.messages.parse.assert_not_called()

    def test_crash_and_takeover_do_not_repeat_provider_effect(self):
        self.ask().raise_for_status()

        def lost(**_):
            self.store.documents[self.path]["available"] = 0
            self.tick()
            return self.result("answer", "stale")

        self.model.messages.parse.side_effect = lost
        self.tick()
        self.assertEqual(self.client.get(self.url).json()["status"], "uncertain")
        self.model.messages.parse.assert_called_once()

    def test_cross_account_denial_has_no_private_effect(self):
        self.ask().raise_for_status()
        before = deepcopy(self.store.documents)
        bob = {"X-Test-Verified-User": "bob"}
        for response in [
            self.ask(headers=bob),
            self.client.get(self.url, headers=bob),
            self.client.get(self.root + "/conversation", headers=bob),
            self.client.put(self.url + "/stop", headers=bob),
            self.client.put(
                self.url + "/messages/" + str(uuid4()), headers=bob, json={"question": "Change it"}
            ),
        ]:
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("Explain a lock", response.text)
        self.assertEqual(before, self.store.documents)
        self.account.tokens_valid_after_timestamp = 2000
        self.tick()
        self.model.messages.parse.assert_not_called()
        self.assertEqual(self.client.get(self.url).json()["status"], "interrupted")

    def test_permission_epoch_cannot_publish_even_after_model_returns(self):
        self.ask().raise_for_status()

        def revoke(**_):
            resource = self.store.documents[
                self.settings.authorization_collections.resources, self.project
            ]
            resource["access_epoch"] = "new-generation"
            return self.result("answer", "should not be stored")

        self.model.messages.parse.side_effect = revoke
        self.tick()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertNotIn("should not be stored", str(self.store.documents[self.path]))
