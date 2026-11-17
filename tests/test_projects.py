from uuid import uuid4

from fastapi.testclient import TestClient

from tests.helpers import ServiceTestCase, make_settings
from tests.test_authorization import MemoryStore


class ProjectTests(ServiceTestCase):
    def test_private_project_workflow(self):
        settings = make_settings(
            authorization_max_batch_resources=2,
            rate_limit_policies={"default": {"requests": 100, "window_seconds": 60}},
        )
        app = self.app(settings=settings)
        store = MemoryStore()
        store.change(
            (settings.authorization_collections.policies, settings.authorization_policy_id),
            {
                "resources": {
                    settings.project_resource_kind: {
                        "create_permissions": [settings.project_create_permission],
                        "read_permissions": [settings.project_read_permission],
                        "update_permissions": [settings.project_session_write_permission],
                        "delete_permissions": [],
                        "shareable_permissions": [],
                        "link_read_permissions": [],
                        "public_read_permissions": [],
                    }
                },
            },
        )
        url = "/api/v1/projects"
        bob = {"X-Test-Verified-User": "bob"}
        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            app.state.resource_database = store
            for invalid in ({"name": " "}, {"name": "é" * 33}, {"name": "x", "owner_uid": "bob"}):
                self.assertEqual(client.post(url, json=invalid).status_code, 422)
            created = []
            for title in ("one", "two", "three"):
                response = client.post(url, json={"name": f" {title} "})
                self.assertEqual(response.status_code, 201)
                project = response.json()
                self.assertEqual(project["name"], title)
                created.append(project)
            private_url = f"{url}/{created[0]['id']}"
            self.assertEqual(client.get(private_url).json(), created[0])
            foreign = client.get(private_url, headers=bob)
            missing = client.get(f"{url}/missing", headers=bob)
            self.assertEqual((foreign.status_code, missing.status_code), (404, 404))
            self.assertEqual(foreign.json()["error"], missing.json()["error"])
            self.assertEqual(
                client.get(url + "?owner_uid=alice", headers=bob).json(),
                {
                    "projects": [],
                    "next_cursor": None,
                },
            )
            client.post(url, headers=bob, json={"name": "Bob only"}).raise_for_status()
            first = client.get(url).json()
            self.assertEqual(len(first["projects"]), 2)
            second = client.get(url, params={"after": first["next_cursor"], "limit": 1}).json()
            self.assertIsNone(second["next_cursor"])
            self.assertCountEqual(first["projects"] + second["projects"], created)
            for params in ({"limit": 3}, {"limit": 0}, {"after": "bad/path"}):
                self.assertEqual(client.get(url, params=params).status_code, 422)

            session_id = str(uuid4())
            session = f"{private_url}/sessions/{session_id}"
            question = {"question": "Where does sign-in happen?"}
            expected = {"id": session_id, **question}
            for _ in range(2):
                response = client.put(session, json=question)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), expected)
            self.assertEqual(client.put(session, json={"question": "Different?"}).status_code, 409)
            self.assertEqual(client.get(session).json(), expected)
            self.assertEqual(client.get(session, headers=bob).status_code, 404)
            self.assertEqual(client.put(session, headers=bob, json=question).status_code, 404)
            other_project = f"{url}/{created[1]['id']}/sessions/{session_id}"
            self.assertEqual(client.get(other_project).status_code, 404)
            self.assertEqual(client.put(session, json={"question": " "}).status_code, 422)
            self.assertEqual(client.put(session, json={"question": "a" * 129}).status_code, 422)
            for question_text in (
                "valid\x00",
                " padded " + " " * 128,
                "start\u202eend",
                "text\x1b",
            ):
                self.assertEqual(
                    client.put(session, json={"question": question_text}).status_code, 422
                )
            text = "Explain <strong>O'Reilly & Co</strong> and SELECT 'hello'."
            safe_url = f"{private_url}/sessions/{uuid4()}"
            for result in (client.put(safe_url, json={"question": text}), client.get(safe_url)):
                self.assertEqual(result.json()["question"], text)
                self.assertNotIn(b"<strong>", result.content)
                self.assertEqual(result.headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("default-src 'none'", result.headers["Content-Security-Policy"])

    def test_signed_out_requests_are_denied(self):
        with (
            self.services(),
            TestClient(self.app(verified=False), client=("198.51.100.1", 1000)) as client,
        ):
            self.assertEqual(client.get("/api/v1/projects").status_code, 401)
            self.assertEqual(client.get("/api/v1/projects/id").status_code, 401)
            self.assertEqual(
                client.post("/api/v1/projects", json={"name": "test"}).status_code, 401
            )
