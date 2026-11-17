from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient

from authorization import Authorization, RequireResource
from tests.helpers import ServiceTestCase, make_settings
from tests.test_authorization import MemoryStore


class AuthorizationRouteTests(ServiceTestCase):
    def test_route_dependencies_bind_identity_and_do_not_trust_request_owner_fields(self):
        settings = make_settings(
            rate_limit_policies={"default": {"requests": 100, "window_seconds": 60}}
        )
        app = self.app(settings=settings)
        store = MemoryStore()
        c = settings.authorization_collections
        store.change(
            (c.policies, settings.authorization_policy_id),
            {
                "resources": {
                    "asset": {
                        "read_permissions": ["inspect"],
                        "update_permissions": ["change"],
                        "delete_permissions": ["remove"],
                        "create_permissions": ["make"],
                        "shareable_permissions": [],
                        "public_read_permissions": [],
                        "link_read_permissions": [],
                    }
                }
            },
        )
        for resource_id, owner in (("a", "alice"), ("b", "bob")):
            store.change(
                (c.resources, resource_id),
                {
                    "kind": "asset",
                    "owner_uid": owner,
                    "team_id": None,
                    "parent_id": None,
                    "access_epoch": "test-epoch",
                    "data": {"text": f"private-{owner}"},
                    "team_permissions": [],
                    "public_permissions": [],
                },
            )

        @app.get("/test-resources/{resource_id}")
        def read_resource(
            resource: Annotated[dict, Depends(RequireResource("asset", "inspect", "resource_id"))],
        ):
            return resource

        @app.patch("/test-resources/{resource_id}")
        def update_resource(resource_id: str, data: dict, access: Authorization):
            access.update_data("asset", resource_id, "change", data)
            return {"updated": True}

        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            app.state.resource_database = store
            response = client.get("/test-resources/a")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.verified_calls), 1)
            response = client.get(
                "/test-resources/b?uid=bob&owner_uid=bob", headers={"X-User-ID": "bob"}
            )
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("private-bob", response.text)
            response = client.patch("/test-resources/b", json={"uid": "bob", "owner_uid": "alice"})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(store.documents[(c.resources, "b")]["data"], {"text": "private-bob"})
            response = client.get("/test-resources/b", headers={"X-Test-Verified-User": "bob"})
            self.assertEqual(response.status_code, 200)
