import io
import json
import os
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from sandbox_worker import component_path, inventory, review_patch, validate_command
from tests.helpers import ServiceTestCase, make_settings
from tests.test_authorization import MemoryStore
from workspace_dispatch import (
    MAX_RECEIPT_ATTEMPTS,
    Dispatcher,
    RejectedAction,
    SandboxTools,
    checked_receipt,
    decide,
    user_redirect,
)
from workspace_runs import run_key, slot_path


class WorkerTests(ServiceTestCase):
    def install_policy(self, settings, store):
        store.change(
            (settings.authorization_collections.policies, settings.authorization_policy_id),
            {
                "resources": {
                    settings.project_resource_kind: {
                        "create_permissions": [settings.project_create_permission],
                        "read_permissions": [settings.project_read_permission],
                        "update_permissions": [
                            settings.project_session_write_permission,
                            settings.project_run_permission,
                        ],
                        "delete_permissions": [],
                        "shareable_permissions": [],
                        "link_read_permissions": [],
                        "public_read_permissions": [],
                    }
                }
            },
        )

    def test_pre_start_correction_preserves_the_new_run_baseline(self):
        job = {
            "messages": [{"text": "Change the title"}],
            "pending_write": False,
            "action": {"action": "begin"},
        }
        self.assertEqual(user_redirect(job, {})["action"], {"action": "begin"})
        self.assertEqual(user_redirect(job, {"history": [{"action": "begin"}]})["phase"], "decide")

    def test_authorized_dispatch_reconnect_review_and_fencing(self):
        self.exercise_workspace(False)

    def test_steer_during_model_call_and_stop_during_write(self):
        self.exercise_workspace(True)

    def test_stop_recovers_an_uncertain_write_without_repeating_it(self):
        self.exercise_workspace(True, missing_receipt=True)

    def exercise_workspace(self, steer, missing_receipt=False):
        settings = make_settings(
            rate_limit_policies={"default": {"requests": 100, "window_seconds": 60}}
        )
        app = self.app(settings=settings)
        store = MemoryStore()
        self.install_policy(settings, store)
        tools = Mock()
        tools.open.return_value.id = "test-sandbox"
        tools.perform.side_effect = [
            {"workspace": "ready"},
            {"text": "draft", "sha256": sha256(b"draft").hexdigest()},
            {
                "path": "notes.md",
                "patch": "-draft\n+revised",
                "sha256": sha256(b"revised").hexdigest(),
            },
        ]
        model = Mock()

        def decision(name, args):
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name=name, input=args)],
                stop_reason="tool_use",
                usage=Mock(model_dump=Mock(return_value={"input_tokens": 21, "output_tokens": 9})),
            )

        model.messages.create.side_effect = [
            decision("read", {"path": "notes.md"}),
            decision("write", {"path": "notes.md", "text": "revised"}),
            decision("answer", {"text": "Revised the document."}),
        ]
        config = SimpleNamespace(
            file_max_bytes=1024, context_max_bytes=16384, max_steps=5, lease_seconds=20
        )
        with (
            self.services(),
            TestClient(app, client=("198.51.100.1", 1000)) as client,
            patch("workspace_dispatch.auth.get_user", return_value=SimpleNamespace(disabled=False)),
        ):
            app.state.resource_database = store
            project = client.post("/api/v1/projects", json={"name": "workspace"}).json()["id"]
            session_id = uuid4()
            base = f"/api/v1/projects/{project}/sessions"
            session = f"{base}/{session_id}"
            other = f"{base}/{uuid4()}"
            for url in (session, other):
                client.put(url, json={"question": "Revise notes.md"}).raise_for_status()
            url = session + "/run"
            self.assertEqual(
                client.put(url, headers={"X-Test-Verified-User": "bob"}).status_code, 404
            )
            self.assertEqual(client.put(url).status_code, 202)
            self.assertEqual(client.put(url).status_code, 202)
            self.assertEqual(client.put(other + "/run").status_code, 409)
            path = (settings.ai_runs_collection, run_key(project, session_id.hex))
            # While a live lease is held, another dispatcher sees no candidate.

            def active_call(*args, **kwargs):
                self.assertFalse(Dispatcher(app, config, tools, model).tick())
                if steer and model.messages.create.call_count == 1:
                    message_url = url + "/messages/" + str(uuid4())
                    body = {"question": "Revise the ending instead."}
                    self.assertEqual(
                        client.put(
                            message_url, json=body, headers={"X-Test-Verified-User": "bob"}
                        ).status_code,
                        404,
                    )
                    self.assertEqual(
                        client.put(message_url, json={"question": " "}).status_code, 422
                    )
                    for _ in range(2):
                        self.assertEqual(
                            len(client.put(message_url, json=body).json()["messages"]), 1
                        )
                    self.assertEqual(
                        client.put(message_url, json={"question": "Different."}).status_code, 409
                    )
                    return decision("answer", {"text": "Stale answer"})
                if steer and model.messages.create.call_count == 2:
                    self.assertEqual(
                        kwargs["messages"][-1]["content"][-1]["text"], "Revise the ending instead."
                    )
                return next(replies)

            replies = iter(model.messages.create.side_effect)
            model.messages.create.side_effect = active_call
            if steer:
                receipts = iter(tools.perform.side_effect)

                def perform(box, command, action, generation):
                    if action["action"] == "write":
                        self.assertEqual(client.put(url + "/stop").json()["status"], "stopping")
                        self.assertEqual(
                            client.put(
                                url + "/messages/" + str(uuid4()), json={"question": "Continue"}
                            ).status_code,
                            409,
                        )
                        if missing_receipt:
                            tools.receipt.return_value = next(receipts)
                            raise ValueError("Receipt temporarily unavailable")
                    return next(receipts)

                tools.perform.side_effect = perform
            for _ in range(6):
                # Each tick can run in a freshly started backend process.
                self.assertTrue(Dispatcher(app, config, tools, model).tick())
            if missing_receipt:
                self.assertEqual(client.get(url).json()["status"], "stopping")
                self.assertEqual(store.documents[slot_path(settings, project)]["active"], path[1])
                self.assertFalse(Dispatcher(app, config, tools, model).tick())
                store.documents[path]["available"] = 0
                self.assertEqual(client.put(url + "/stop").json()["status"], "stopping")
                self.assertTrue(Dispatcher(app, config, tools, model).tick())
                tools.receipt.assert_called_once()
                self.assertEqual(tools.perform.call_count, 3)
            result = client.get(url)
            self.assertEqual(result.json()["status"], "stopped" if steer else "completed")
            if steer:
                self.assertIsNone(result.json()["answer"])
                self.assertEqual(client.put(url + "/stop").json()["status"], "stopped")
            self.assertEqual(result.json()["review"][0]["patch"], "-draft\n+revised")
            self.assertEqual(
                client.get(url, headers={"X-Test-Verified-User": "bob"}).status_code, 404
            )
            self.assertEqual(store.documents[path]["step"], 2)
            self.assertEqual(len(store.documents[path]["usage"]), 3)
            self.assertEqual(
                tools.perform.call_args.args[2]["expected"], sha256(b"draft").hexdigest()
            )
            self.assertIsNone(store.documents[slot_path(settings, project)]["active"])
            client.put(other + "/run?mode=read_only").raise_for_status()
            self.assertEqual(client.put(other + "/run?mode=edit").status_code, 409)
            other_path = (
                settings.ai_runs_collection,
                run_key(project, other.rsplit("/", 1)[1].replace("-", "")),
            )
            store.documents[other_path]["phase"] = "thinking"
            self.assertTrue(Dispatcher(app, config, tools, model).tick())
            self.assertEqual(client.get(other + "/run").json()["status"], "interrupted")
            self.assertEqual(model.messages.create.call_count, 3)

    def write_fixture(self):
        settings = make_settings(
            rate_limit_policies={"default": {"requests": 100, "window_seconds": 60}}
        )
        app = self.app(settings=settings)
        self.enterContext(self.services())
        client = self.enterContext(TestClient(app, client=("198.51.100.1", 1000)))
        self.enterContext(
            patch("workspace_dispatch.auth.get_user", return_value=SimpleNamespace(disabled=False))
        )
        store = MemoryStore()
        self.install_policy(settings, store)
        app.state.resource_database = store
        project = client.post("/api/v1/projects", json={"name": "sample"}).json()["id"]
        session = uuid4()
        url = f"/api/v1/projects/{project}/sessions/{session}"
        client.put(url, json={"question": "Update notes"}).raise_for_status()
        url += "/run"
        client.put(url).raise_for_status()
        path = (settings.ai_runs_collection, run_key(project, session.hex))
        slot = slot_path(settings, project)
        store.documents[path].update(
            action={"action": "write", "path": "notes", "text": "new", "expected": None}, step=1
        )
        tools = Mock()
        tools.open.return_value.id = "bound"
        receipt = {"path": "notes", "sha256": sha256(b"new").hexdigest(), "patch": "+new"}
        tools.perform.return_value = tools.receipt.return_value = receipt
        worker = Dispatcher(
            app, SimpleNamespace(lease_seconds=20, context_max_bytes=16384), tools, Mock()
        )
        return worker, tools, store, path, slot, client, url

    def assert_settled(self, store, path, slot, writes):
        job = store.documents[path]
        self.assertEqual(job["status"], "interrupted")
        self.assertFalse(job["pending_write"])
        self.assertIsNone(job["claim"])
        self.assertIsNone(store.documents[slot]["active"])
        self.assertEqual(len(job["history"]), writes)
        self.assertEqual(job["review"] or [], [entry["result"] for entry in job["history"]])

    def test_over_budget_write_persists_review_before_releasing(self):
        worker, tools, store, path, slot, _, _ = self.write_fixture()
        worker.config.context_max_bytes = 1000
        tools.perform.return_value["patch"] = "x" * 1500
        worker.tick()
        self.assert_settled(store, path, slot, 1)
        self.assertEqual(store.documents[path]["review"], [tools.perform.return_value])
        tools.receipt.assert_not_called()

    def test_failed_receipt_commit_recovers_using_original_command(self):
        worker, tools, store, path, slot, _, _ = self.write_fixture()

        def write(*args):
            store.before_commit = Mock(side_effect=TimeoutError("commit unavailable"))
            return tools.perform.return_value

        tools.perform.side_effect = write
        worker.tick()
        self.assertEqual(store.documents[slot]["active"], path[1])
        self.assertEqual(store.documents[path]["phase"], "recover")
        self.assertTrue(store.documents[path]["pending_write"])
        self.assertFalse(worker.tick())
        store.documents[path]["available"] = 0
        worker.tick()
        self.assert_settled(store, path, slot, 1)
        tools.receipt.assert_called_once_with(*tools.perform.call_args.args[:2], write=True)
        self.assertFalse(worker.tick())
        tools.perform.assert_called_once()

    def test_lost_acknowledgement_after_successful_commit(self):
        worker, tools, store, path, slot, _, _ = self.write_fixture()
        run = store.run

        def write(*args):
            def lose_ack(operation):
                store.run = run
                run(operation)
                raise TimeoutError("acknowledgement lost")

            store.run = lose_ack
            return tools.perform.return_value

        tools.perform.side_effect = write
        worker.tick()
        self.assertEqual(store.documents[slot]["active"], path[1])
        job = store.documents[path]
        self.assertFalse(job["pending_write"])
        self.assertEqual(job["phase"], "decide")
        self.assertEqual(len(job["history"]), 1)
        self.assertEqual(job["review"], [tools.perform.return_value])
        tools.receipt.assert_not_called()

    def test_correction_during_reserved_write_survives_recovery(self):
        worker, tools, store, path, slot, client, url = self.write_fixture()

        def write(*args):
            client.put(
                url + "/messages/" + str(uuid4()), json={"question": "Pause and summarize"}
            ).raise_for_status()
            raise TimeoutError("delayed receipt")

        tools.perform.side_effect = write
        worker.tick()
        store.documents[path]["available"] = 0
        worker.tick()
        job = store.documents[path]
        self.assertEqual(job["phase"], "decide")
        self.assertFalse(job["pending_write"])
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["applied"], 1)
        self.assertEqual(job["review"], [tools.receipt.return_value])
        self.assertEqual(len(job["history"]), 1)
        self.assertEqual(store.documents[slot]["active"], path[1])
        tools.perform.assert_called_once()

    def test_timeout_invalid_and_missing_receipts_then_owner_recovery(self):
        worker, tools, store, path, slot, client, url = self.write_fixture()
        tools.perform.side_effect = TimeoutError()
        tools.receipt.side_effect = [
            FileNotFoundError(),
            {"path": "wrong"},
            ValueError("legacy error"),
        ]
        worker.tick()
        for attempt in range(MAX_RECEIPT_ATTEMPTS):
            self.assertFalse(worker.tick())
            self.assertEqual(store.documents[slot]["active"], path[1])
            store.documents[path]["available"] = 0
            worker.tick()
            self.assertEqual(store.documents[path]["receipt_attempts"], attempt + 1)
        self.assertFalse(worker.tick())
        self.assertTrue(store.documents[path]["pending_write"])
        self.assertEqual(store.documents[slot]["active"], path[1])
        self.assertEqual(client.get(url).json()["status"], "awaiting_workspace_receipt")
        tools.receipt.side_effect = None
        for _ in range(2):
            client.put(url).raise_for_status()
        worker.tick()
        self.assert_settled(store, path, slot, 1)
        tools.perform.assert_called_once()
        self.assertFalse(worker.tick())

    def test_rejection_releases_without_recording_a_write(self):
        worker, tools, store, path, slot, _, _ = self.write_fixture()
        tools.perform.side_effect = RejectedAction()
        worker.tick()
        self.assert_settled(store, path, slot, 0)

    def test_legacy_expired_reservation_only_reads_the_receipt(self):
        worker, tools, store, path, slot, _, _ = self.write_fixture()
        store.documents[path].update(pending_write=True, phase="tool", claim="expired", available=0)
        store.documents[slot]["sandbox"] = "bound"
        worker.tick()
        self.assert_settled(store, path, slot, 1)
        tools.receipt.assert_called_once()
        tools.perform.assert_not_called()

    def test_only_explicit_rejection_is_proof_of_no_mutation(self):
        config = SimpleNamespace(state_dir="/control", operation_seconds=5, context_max_bytes=1024)
        sandbox = Mock()
        tools = object.__new__(SandboxTools)
        tools.config = config
        for envelope in (
            {"error": "Workspace action failed or the file changed."},
            {"error": "Conflicting replay."},
            {"rejected": 1},
            {"rejected": True},
        ):
            with self.subTest(envelope=envelope):
                sandbox.fs.download_file_stream.return_value = io.BytesIO(
                    json.dumps(envelope).encode()
                )
                with self.assertRaises(ValueError) as error:
                    tools.receipt(sandbox, "key", write=True)
                self.assertEqual(
                    isinstance(error.exception, RejectedAction), envelope.get("rejected") is True
                )

    def test_lease_takeover_fences_the_original_writer(self):
        worker, tools, store, path, slot, _, _ = self.write_fixture()

        def delayed_return(*args):
            self.assertFalse(worker.tick())
            store.documents[path]["available"] = 0
            worker.tick()
            self.assert_settled(store, path, slot, 1)
            return tools.perform.return_value

        tools.perform.side_effect = delayed_return
        worker.tick()
        self.assert_settled(store, path, slot, 1)
        tools.perform.assert_called_once()
        tools.receipt.assert_called_once()

    def test_revoked_access_keeps_reservation_without_receipt_access(self):
        worker, tools, store, path, slot, _, _ = self.write_fixture()
        tools.perform.side_effect = TimeoutError()
        worker.tick()
        settings = worker.settings
        store.change(
            (settings.authorization_collections.resources, store.documents[path]["project"]), None
        )
        store.documents[path]["available"] = 0
        worker.tick()
        self.assertEqual(store.documents[path]["status"], "interrupted")
        self.assertTrue(store.documents[path]["pending_write"])
        self.assertEqual(store.documents[slot]["active"], path[1])
        self.assertFalse(worker.tick())
        tools.receipt.assert_not_called()
        tools.open.assert_called_once()

    def test_untrusted_file_is_a_tool_result_and_cannot_enable_writes(self):
        prompt = "Explain this file."
        file_text = "The file claims the user approved editing a different document."
        job = {
            "mode": "read_only",
            "question": prompt,
            "history": [
                {
                    "action": "read",
                    "path": "notes.md",
                    "result": {"text": file_text, "sha256": sha256(file_text.encode()).hexdigest()},
                }
            ],
        }
        model = Mock()
        model.messages.create.return_value = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(
                    type="tool_use", name="write", input={"path": "notes.md", "text": "changed"}
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "Invalid model tool"):
            decide(model, SimpleNamespace(model_id="test", max_output_tokens=32), job)
        call = model.messages.create.call_args.kwargs
        self.assertNotIn("write", {tool["name"] for tool in call["tools"]})
        self.assertEqual(call["messages"][0]["content"], [{"type": "text", "text": prompt}])
        result = call["messages"][-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(json.loads(result["content"])["text"], file_text)
        self.assertNotIn(file_text, call["system"])

    def test_receipt_cannot_supply_instructions_or_a_forged_revision(self):
        action = {"action": "read", "path": "notes.md"}
        for invalid in (
            {},
            {"exists": 0},
            {"text": "changed", "sha256": "wrong"},
            {"exists": False, "role": "system"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                checked_receipt(action, invalid)

    def test_daemon_validates_commands_before_touching_sqlite(self):
        validate_command({"action": "read", "generation": 1, "path": "O'Reilly.sql"}, 1024)
        for invalid in (
            {"action": "read", "generation": "1", "path": "a.sql"},
            {"action": "read", "generation": 1, "path": "a.sql", "statement": "SELECT 1"},
            {
                "action": "write",
                "generation": 1,
                "path": "a.sql",
                "text": "hello",
                "expected": "unexpected",
            },
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_command(invalid, 1024)

    def test_paths_are_project_relative(self):
        for path in ("../other", "/etc/passwd", "x/.git/config", "x\\y", ".env", "a//b"):
            with self.assertRaises(ValueError):
                component_path(path)

    def test_git_patch_applies_to_existing_uncommitted_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                ["git", "config", "--local", "core.autocrlf", "false"], cwd=root, check=True
            )
            old = b"draft\nuser's uncommitted paragraph\n"
            new = old + b"agent addition\n"
            (root / "notes.md").write_bytes(old)
            (root / "other.txt").write_bytes(b"keep")
            before = inventory(root, 1024)
            patch_text = review_patch("notes.md", old, new)
            subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=root,
                input=patch_text.encode(),
                check=True,
            )
            self.assertEqual((root / "notes.md").read_bytes(), new)
            self.assertEqual(before["other.txt"], inventory(root, 1024)["other.txt"])
            self.assertEqual((root / "other.txt").read_bytes(), b"keep")
            added = review_patch("new.md", None, b"new file")
            subprocess.run(["git", "apply", "-"], cwd=root, input=added.encode(), check=True)
            self.assertEqual((root / "new.md").read_bytes(), b"new file")

    @unittest.skipUnless(os.name == "posix", "Daytona worker uses Linux directory descriptors")
    def test_real_git_edit_and_replay_across_worker_processes(self):
        import sys
        import time

        script = Path(__file__).resolve().parents[1] / "sandbox_worker.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            state = Path(folder) / "control"
            state.mkdir()
            root.mkdir()
            (root / "notes.md").write_bytes(b"uncommitted draft\n")
            (root / "unrelated.md").write_bytes(b"keep me")
            key = uuid4().hex
            job = {
                "action": "write",
                "path": "notes.md",
                "text": "new\n",
                "expected": sha256(b"uncommitted draft\n").hexdigest(),
                "generation": 1,
            }

            def run(action, key):
                (state / (key + ".ready")).write_text(json.dumps(action))
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(script),
                        "serve",
                        str(root),
                        str(state),
                        "1024",
                        "0.01",
                        "5",
                    ]
                )
                try:
                    end = time.monotonic() + 5
                    while not (state / (key + ".out")).exists() and time.monotonic() < end:
                        time.sleep(0.01)
                    return json.loads((state / (key + ".out")).read_bytes())
                finally:
                    proc.terminate()
                    proc.wait(timeout=5)

            self.assertEqual(
                run({"action": "begin", "generation": 1}, uuid4().hex)["result"],
                {"workspace": "ready"},
            )
            receipt = run(job, key)
            self.assertIn("-uncommitted draft", receipt["result"]["patch"])
            (state / (key + ".out")).unlink()
            self.assertEqual(run(job, key), receipt)
            self.assertEqual((root / "notes.md").read_bytes(), b"new\n")
            self.assertEqual((root / "unrelated.md").read_bytes(), b"keep me")
            self.assertTrue((root / ".git").is_dir())
            self.assertEqual(run(job, uuid4().hex), {"rejected": True})
