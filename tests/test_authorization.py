import copy
import json
import unittest
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from google.api_core.exceptions import Aborted, ServiceUnavailable
from pydantic import SecretStr

from authorization import AuthorizationService, identity_key
from tests.helpers import make_settings


class MemorySession:
    def __init__(self, documents):
        self.documents = copy.deepcopy(documents)
        self.writes = []

    def get(self, path):
        if self.writes:
            raise AssertionError("Reads must precede writes")
        return copy.deepcopy(self.documents.get(path))

    def put(self, path, data, *, exists):
        if (path in self.documents) != exists:
            raise Aborted("Existence changed")
        self.writes.append((path, copy.deepcopy(data)))

    def next_queued_run(self, collection):
        import time

        rows = [
            (path[1], doc)
            for path, doc in self.documents.items()
            if len(path) == 2
            and path[0] == collection
            and doc.get("status") == "queued"
            and doc["available"] <= time.time()
        ]
        return min(rows, key=lambda row: row[1]["available"]) if rows else None

    def owned_page(self, collection, uid, kind, limit, after):
        return [
            (path[1], doc)
            for path, doc in sorted(self.documents.items())
            if len(path) == 2
            and path[0] == collection
            and doc["owner_uid"] == uid
            and doc["kind"] == kind
            and (after is None or path[1] > after)
        ][:limit]

    def delete(self, path):
        self.writes.append((path, None))


class MemoryStore:
    """Test-only snapshot/commit simulator; production uses Firestore transactions."""

    def __init__(self):
        self.documents = {}
        self.version = 0
        self.before_commit = None

    def change(self, path, value):
        if value is None:
            self.documents.pop(path, None)
        else:
            self.documents[path] = copy.deepcopy(value)
        self.version += 1

    def run(self, operation):
        version = self.version
        session = MemorySession(self.documents)
        result = operation(session)
        if self.before_commit is not None:
            callback, self.before_commit = self.before_commit, None
            callback()
        if version != self.version:
            raise Aborted("A read dependency changed")
        for path, data in session.writes:
            self.change(path, data)
        return result


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()
        self.c = self.settings.authorization_collections
        self.store = MemoryStore()
        permissions = {
            "read_permissions": ["inspect"],
            "update_permissions": ["change"],
            "delete_permissions": ["remove"],
            "create_permissions": ["make"],
            "shareable_permissions": ["inspect", "change", "remove"],
            "public_read_permissions": ["inspect"],
            "link_read_permissions": ["inspect"],
        }
        self.store.change(
            (self.c.policies, self.settings.authorization_policy_id),
            {"resources": {"asset": permissions, "other-kind": permissions}},
        )
        self.store.change((self.c.resources, "a"), self.resource("alice", "t1", "private-a"))
        self.store.change((self.c.resources, "b"), self.resource("bob", "t2", "private-b"))
        for team in ("t1", "t2"):
            self.store.change((self.c.teams, team), {"sharing_enabled": True})
        self.member("t1", "alice")
        self.member("t1", "teammate")
        self.member("t2", "bob")
        self.member("t2", "mallory")

    def resource(self, owner, team, text, **changes):
        value = {
            "kind": "asset",
            "owner_uid": owner,
            "team_id": team,
            "parent_id": None,
            "access_epoch": "test-generation",
            "data": {"text": text},
            "team_permissions": [],
            "public_permissions": [],
        }
        value.update(changes)
        return value

    def member(self, team, uid, *, active=True, permissions=None):
        self.store.change(
            (self.c.teams, team, self.c.members, identity_key(uid)),
            {
                "uid": uid,
                "active": active,
                "permissions": ["inspect", "change", "remove", "make"]
                if permissions is None
                else permissions,
            },
        )

    def access(self, uid):
        return AuthorizationService(self.store, self.settings, uid)

    def denied(self, callback, status=404):
        with self.assertRaises(HTTPException) as error:
            callback()
        self.assertEqual(error.exception.status_code, status)

    def grant(self, permissions=None):
        self.access("alice").grant_user(
            "asset",
            "a",
            "teammate",
            permissions={"inspect"} if permissions is None else permissions,
            expires_at=None,
        )

    def test_owner_and_other_user_isolation(self):
        self.assertEqual(self.access("alice").read("asset", "a", "inspect"), {"text": "private-a"})
        self.denied(lambda: self.access("alice").read("asset", "b", "inspect"))
        self.denied(lambda: self.access("bob").read("asset", "a", "inspect"))
        self.denied(lambda: self.access("alice").read("other-kind", "a", "inspect"))
        self.denied(lambda: self.access("alice").read("asset", "missing", "inspect"))
        self.denied(lambda: self.access("alice").read("asset", "../b", "inspect"))

    def test_membership_does_not_grant_resource_access_by_itself(self):
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))
        self.grant()
        self.assertEqual(
            self.access("teammate").read("asset", "a", "inspect"), {"text": "private-a"}
        )
        self.denied(
            lambda: self.access("teammate").update_data("asset", "a", "change", {"text": "no"})
        )

    def test_invitee_must_first_be_an_active_team_member(self):
        self.denied(
            lambda: self.access("alice").grant_user(
                "asset", "a", "outsider", permissions={"inspect"}, expires_at=None
            )
        )
        self.member("t1", "outsider", active=False)
        self.denied(
            lambda: self.access("alice").grant_user(
                "asset", "a", "outsider", permissions={"inspect"}, expires_at=None
            )
        )
        self.member("t1", "outsider", active=True)
        self.access("alice").grant_user(
            "asset", "a", "outsider", permissions={"inspect"}, expires_at=None
        )
        self.assertEqual(self.access("outsider").read("asset", "a", "inspect")["text"], "private-a")

    def test_member_and_grant_revocation_take_effect_without_cache(self):
        self.grant()
        self.member("t1", "teammate", active=False)
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))
        self.member("t1", "teammate", permissions=[])
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))
        self.member("t1", "teammate")
        self.access("alice").revoke_user("asset", "a", "teammate")
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))

    def test_only_owner_manages_sharing_even_when_teammate_can_edit(self):
        self.grant({"inspect", "change"})
        self.denied(
            lambda: self.access("teammate").set_sharing(
                "asset", "a", team_permissions={"inspect"}, public_permissions=set()
            )
        )
        self.denied(
            lambda: self.access("teammate").grant_user(
                "asset", "a", "mallory", permissions={"inspect"}, expires_at=None
            )
        )
        self.denied(
            lambda: self.access("teammate").create_link(
                "asset", "a", permissions={"inspect"}, expires_at=None
            )
        )

    def test_private_resources_without_team_cannot_be_shared(self):
        self.store.change((self.c.resources, "personal"), self.resource("alice", None, "personal"))
        self.denied(
            lambda: self.access("alice").set_sharing(
                "asset", "personal", team_permissions=set(), public_permissions={"inspect"}
            )
        )
        self.assertEqual(
            self.access("alice").read("asset", "personal", "inspect")["text"], "personal"
        )

    def test_missing_or_disabled_capability_denies_all_shared_access(self):
        self.grant()
        self.access("alice").set_sharing(
            "asset", "a", team_permissions={"inspect"}, public_permissions={"inspect"}
        )
        token = self.access("alice").create_link(
            "asset", "a", permissions={"inspect"}, expires_at=None
        )
        for value in ({"sharing_enabled": False}, None):
            self.store.change((self.c.teams, "t1"), value)
            self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))
            self.denied(lambda: self.access(None).read("asset", "a", "inspect", link_token=token))
            self.assertEqual(
                self.access("alice").read("asset", "a", "inspect")["text"], "private-a"
            )
        self.access("alice").set_sharing(
            "asset", "a", team_permissions=set(), public_permissions=set()
        )
        self.access("alice").revoke_user("asset", "a", "teammate")
        self.access("alice").revoke_link("asset", "a", token)

    def test_team_permissions_are_limited_by_member_permissions(self):
        self.access("alice").set_sharing(
            "asset", "a", team_permissions={"inspect", "change"}, public_permissions=set()
        )
        self.member("t1", "teammate", permissions=["inspect"])
        self.assertEqual(self.access("teammate").read("asset", "a", "inspect")["text"], "private-a")
        self.denied(
            lambda: self.access("teammate").update_data("asset", "a", "change", {"text": "no"})
        )
        self.denied(lambda: self.access("mallory").read("asset", "a", "inspect"))

    def test_public_and_link_access_never_authorize_writes(self):
        self.access("alice").set_sharing(
            "asset", "a", team_permissions=set(), public_permissions={"inspect"}
        )
        self.assertEqual(self.access(None).read("asset", "a", "inspect")["text"], "private-a")
        self.denied(lambda: self.access(None).update_data("asset", "a", "change", {}), 401)
        self.denied(lambda: self.access("mallory").update_data("asset", "a", "change", {}))
        self.denied(lambda: self.access("mallory").delete("asset", "a", "remove"))
        self.denied(
            lambda: self.access("alice").set_sharing(
                "asset", "a", team_permissions=set(), public_permissions={"change"}
            )
        )

    def test_links_are_resource_bound_hashed_and_revocable(self):
        token = self.access("alice").create_link(
            "asset", "a", permissions={"inspect"}, expires_at=None
        )
        self.assertNotIn(token.get_secret_value(), json.dumps(list(self.store.documents.values())))
        self.assertEqual(
            self.access(None).read("asset", "a", "inspect", link_token=token)["text"], "private-a"
        )
        self.denied(lambda: self.access(None).read("asset", "b", "inspect", link_token=token))
        self.denied(
            lambda: self.access(None).read(
                "asset", "a", "inspect", link_token=SecretStr("wrong-token")
            )
        )
        self.access("alice").revoke_link("asset", "a", token)
        self.denied(lambda: self.access(None).read("asset", "a", "inspect", link_token=token))

    def test_expiry_and_invalid_expiry(self):
        future = datetime.now(UTC) + timedelta(hours=1)
        self.access("alice").grant_user(
            "asset", "a", "teammate", permissions={"inspect"}, expires_at=future
        )
        path = (self.c.resources, "a", self.c.grants, identity_key("teammate"))
        grant = self.store.documents[path]
        grant["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.store.change(path, grant)
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))
        self.denied(
            lambda: self.access("alice").create_link(
                "asset",
                "a",
                permissions={"inspect"},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
            422,
        )

    def test_payload_changes_cannot_change_access_metadata(self):
        self.grant({"inspect", "change"})
        self.access("teammate").update_data(
            "asset",
            "a",
            "change",
            {"owner_uid": "teammate", "team_id": "t2", "public_permissions": ["inspect"]},
        )
        stored = self.store.documents[(self.c.resources, "a")]
        self.assertEqual(stored["owner_uid"], "alice")
        self.assertEqual(stored["team_id"], "t1")
        self.assertEqual(stored["public_permissions"], [])
        self.denied(lambda: self.access("mallory").read("asset", "a", "inspect"))

    def test_read_permission_cannot_be_used_for_a_mutation(self):
        self.denied(lambda: self.access("alice").update_data("asset", "a", "inspect", {}))
        self.denied(lambda: self.access("alice").delete("asset", "a", "inspect"))
        self.assertIn((self.c.resources, "a"), self.store.documents)

    def test_revocation_between_check_and_write_aborts_without_mutating(self):
        self.grant({"inspect", "change"})
        grant_path = (self.c.resources, "a", self.c.grants, identity_key("teammate"))
        self.store.before_commit = lambda: self.store.change(grant_path, None)
        self.denied(
            lambda: self.access("teammate").update_data(
                "asset", "a", "change", {"text": "forbidden"}
            ),
            409,
        )
        self.assertEqual(
            self.store.documents[(self.c.resources, "a")]["data"], {"text": "private-a"}
        )

    def test_parent_binding_and_deleted_ancestors(self):
        self.store.change(
            (self.c.resources, "child"), self.resource("alice", "t1", "child", parent_id="a")
        )
        self.denied(lambda: self.access("alice").read("asset", "child", "inspect", parent_id="b"))
        self.assertEqual(
            self.access("alice").read("asset", "child", "inspect", parent_id="a")["text"], "child"
        )
        self.store.change((self.c.resources, "a"), None)
        self.denied(lambda: self.access("alice").read("asset", "child", "inspect"))

    def test_access_epoch_change_invalidates_previous_grants_and_links(self):
        self.grant()
        token = self.access("alice").create_link(
            "asset", "a", permissions={"inspect"}, expires_at=None
        )
        resource = self.store.documents[(self.c.resources, "a")]
        resource["access_epoch"] = "new-generation"
        self.store.change((self.c.resources, "a"), resource)
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))
        self.denied(lambda: self.access(None).read("asset", "a", "inspect", link_token=token))

    def test_new_resources_are_private_and_take_owner_from_bound_identity(self):
        resource_id = self.access("alice").create_private(
            "asset", "make", {"owner_uid": "mallory"}, team_id=None
        )
        stored = self.store.documents[(self.c.resources, resource_id)]
        self.assertEqual(stored["owner_uid"], "alice")
        self.assertEqual(stored["team_permissions"], [])
        self.assertEqual(stored["public_permissions"], [])
        self.denied(lambda: self.access("mallory").read("asset", resource_id, "inspect"))

    def test_missing_or_malformed_policy_fails_closed(self):
        path = (self.c.policies, self.settings.authorization_policy_id)
        self.store.change(path, None)
        with self.assertLogs("lotusbuild.api", level="ERROR"):
            self.denied(lambda: self.access("alice").read("asset", "a", "inspect"), 503)

    def test_store_failure_is_not_allowed_or_reported_as_success(self):
        def fail(operation):
            raise ServiceUnavailable("private provider message")

        self.store.run = fail
        with self.assertLogs("lotusbuild.api", level="ERROR"):
            self.denied(lambda: self.access("alice").read("asset", "a", "inspect"), 503)

    def test_batch_reads_never_return_partial_or_other_users_data(self):
        self.assertEqual(
            self.access("alice").read_many("asset", ["a"], "inspect"), {"a": {"text": "private-a"}}
        )
        self.denied(lambda: self.access("alice").read_many("asset", ["a", "b"], "inspect"))
        self.denied(lambda: self.access("alice").read_many("asset", ["a"] * 51, "inspect"), 422)

    def test_expired_links_and_removed_owner_membership_are_denied(self):
        token = self.access("alice").create_link(
            "asset", "a", permissions={"inspect"}, expires_at=None
        )
        from hashlib import sha256

        path = (self.c.links, sha256(token.get_secret_value().encode()).hexdigest())
        link = self.store.documents[path]
        link["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.store.change(path, link)
        self.denied(lambda: self.access(None).read("asset", "a", "inspect", link_token=token))
        self.grant()
        self.member("t1", "alice", active=False)
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))

    def test_unknown_permission_and_wildcard_policy_do_not_grant_access(self):
        self.denied(lambda: self.access("alice").read("asset", "a", "invented-action"))
        path = (self.c.policies, self.settings.authorization_policy_id)
        policy = self.store.documents[path]
        policy["resources"]["asset"]["read_permissions"] = ["*"]
        self.store.change(path, policy)
        with self.assertLogs("lotusbuild.api", level="ERROR"):
            self.denied(lambda: self.access("alice").read("asset", "a", "inspect"), 503)

    def test_parent_cycles_fail_closed(self):
        resource = self.store.documents[(self.c.resources, "a")]
        resource["parent_id"] = "a"
        self.store.change((self.c.resources, "a"), resource)
        with self.assertLogs("lotusbuild.api", level="ERROR"):
            self.denied(lambda: self.access("alice").read("asset", "a", "inspect"), 503)

    def test_make_private_revokes_all_existing_access_paths(self):
        self.grant({"inspect", "change"})
        token = self.access("alice").create_link(
            "asset", "a", permissions={"inspect"}, expires_at=None
        )
        self.access("alice").set_sharing(
            "asset", "a", team_permissions={"inspect"}, public_permissions={"inspect"}
        )
        self.denied(lambda: self.access("teammate").make_private("asset", "a"))
        self.access("alice").make_private("asset", "a")
        self.denied(lambda: self.access(None).read("asset", "a", "inspect", link_token=token))
        self.denied(lambda: self.access(None).read("asset", "a", "inspect"))
        self.denied(lambda: self.access("teammate").read("asset", "a", "inspect"))
        self.assertEqual(self.access("alice").read("asset", "a", "inspect")["text"], "private-a")

    def test_owner_can_revoke_a_link_using_its_id_without_retaining_the_secret(self):
        from hashlib import sha256

        token = self.access("alice").create_link(
            "asset", "a", permissions={"inspect"}, expires_at=None
        )
        link_id = sha256(token.get_secret_value().encode()).hexdigest()
        self.denied(lambda: self.access("teammate").revoke_link_id("asset", "a", link_id))
        self.access("alice").revoke_link_id("asset", "a", link_id)
        self.denied(lambda: self.access(None).read("asset", "a", "inspect", link_token=token))
