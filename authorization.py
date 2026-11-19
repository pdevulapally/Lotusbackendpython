"""Shared, transactionally checked resource authorization. No billing integration."""

import base64
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal, TypeVar
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Request
from google.api_core.exceptions import (
    Aborted,
    AlreadyExists,
    FailedPrecondition,
    GoogleAPICallError,
)
from pydantic import BaseModel, SecretStr, ValidationError

from auth import CurrentUser
from authorization_models import (
    AuthorizationPolicy,
    LinkGrant,
    MembershipRecord,
    ResourcePermissions,
    ResourceRecord,
    TeamRecord,
    UserGrant,
)
from config import Settings
from firestore_access import FirestoreAccessStore, FirestoreSession, document_segment
from observability import logger

Model = TypeVar("Model", bound=BaseModel)
Result = TypeVar("Result")


def identity_key(uid: str) -> str:
    if not isinstance(uid, str) or not uid:
        raise ValueError("A verified user identity is required.")
    return base64.urlsafe_b64encode(uid.encode("utf-8")).decode("ascii").rstrip("=")


def denied() -> HTTPException:
    # The same response conceals whether another user's resource exists.
    return HTTPException(404, "This resource isn't available to you.")


def record(model: type[Model], value: dict) -> Model:
    return model.model_validate_json(json.dumps(value, allow_nan=False))


class AuthorizationService:
    def __init__(self, store: FirestoreAccessStore, settings: Settings, principal_uid: str | None):
        self.store = store
        self.collections = settings.authorization_collections
        self.policy_id = settings.authorization_policy_id
        self.token_bytes = settings.authorization_link_token_bytes
        self.max_parent_depth = settings.authorization_max_parent_depth
        self.max_batch_resources = settings.authorization_max_batch_resources
        self._uid = principal_uid

    def _signed_in(self) -> str:
        if self._uid is None:
            raise HTTPException(
                401, "Please sign in to continue.", headers={"WWW-Authenticate": "Bearer"}
            )
        identity_key(self._uid)
        return self._uid

    @staticmethod
    def _expiry(expires_at: datetime | None) -> None:
        if expires_at is not None and (
            expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or expires_at <= datetime.now(UTC)
        ):
            raise HTTPException(
                422, "Choose a future expiry time with a timezone, or explicitly no expiry."
            )

    def _run(
        self,
        operation: Callable[[FirestoreSession], Result],
        *,
        reconcile: Callable[[FirestoreSession], Result] | None = None,
    ) -> Result:
        try:
            return self.store.run(operation)
        except Aborted, AlreadyExists, FailedPrecondition:
            if reconcile is not None:
                return self._run(reconcile)
            raise HTTPException(
                409, "The resource changed during your request. Please refresh and try again."
            ) from None
        except (
            GoogleAPICallError,
            ValidationError,
            ValueError,
            TypeError,
            AttributeError,
        ) as error:
            logger.error(
                "authorization_unavailable", extra={"exception_type": type(error).__name__}
            )
            raise HTTPException(
                503, "We couldn't check access to this resource. Please try again later."
            ) from None

    def _path(self, resource_id: str) -> tuple[str, ...]:
        try:
            return self.collections.resources, document_segment(resource_id)
        except ValueError, UnicodeError:
            raise denied() from None

    def _policy(self, tx: FirestoreSession, kind: str) -> ResourcePermissions:
        value = tx.get((self.collections.policies, self.policy_id))
        if value is None:
            raise ValueError("Authorization policy is not provisioned.")
        policy = record(AuthorizationPolicy, value)
        if kind not in policy.resources:
            raise denied()
        return policy.resources[kind]

    def _resource(
        self, tx: FirestoreSession, kind: str, resource_id: str, parent_id: str | None
    ) -> ResourceRecord:
        value = tx.get(self._path(resource_id))
        if value is None:
            raise denied()
        resource = record(ResourceRecord, value)
        if resource.kind != kind or (parent_id is not None and resource.parent_id != parent_id):
            raise denied()
        self._validate_ancestry(tx, resource, resource_id)
        return resource

    def _validate_ancestry(self, tx: FirestoreSession, resource: ResourceRecord, resource_id: str):
        seen = {resource_id}
        ancestor_id = resource.parent_id
        while ancestor_id is not None:
            if ancestor_id in seen or len(seen) > self.max_parent_depth:
                raise ValueError("Invalid resource ancestry.")
            seen.add(ancestor_id)
            ancestor_value = tx.get(self._path(ancestor_id))
            if ancestor_value is None:
                raise denied()
            ancestor = record(ResourceRecord, ancestor_value)
            if ancestor.team_id != resource.team_id:
                raise denied()
            if resource.team_id is None and ancestor.owner_uid != resource.owner_uid:
                raise denied()
            ancestor_id = ancestor.parent_id

    def _member(self, tx: FirestoreSession, team_id: str, uid: str) -> MembershipRecord | None:
        value = tx.get(
            (
                self.collections.teams,
                document_segment(team_id),
                self.collections.members,
                identity_key(uid),
            )
        )
        if value is None:
            return None
        member = record(MembershipRecord, value)
        if member.uid != uid or not member.active:
            return None
        return member

    def _sharing_team(self, tx: FirestoreSession, resource: ResourceRecord) -> str:
        if resource.team_id is None:
            raise denied()
        value = tx.get((self.collections.teams, document_segment(resource.team_id)))
        if value is None or not record(TeamRecord, value).sharing_enabled:
            raise denied()
        if self._member(tx, resource.team_id, resource.owner_uid) is None:
            raise denied()
        return resource.team_id

    def _authorize(
        self,
        tx: FirestoreSession,
        kind: str,
        resource_id: str,
        permission: str,
        uid: str | None,
        *,
        operation: Literal["read", "update", "delete"],
        link_token: SecretStr | None,
        parent_id: str | None,
    ) -> ResourceRecord:
        resource = self._resource(tx, kind, resource_id, parent_id)
        policy = self._policy(tx, kind)
        self._check_action(resource, policy, permission, operation)
        if uid == resource.owner_uid:
            return resource
        if permission not in policy.shareable_permissions:
            raise denied()
        team_id = self._sharing_team(tx, resource)
        now = datetime.now(UTC)
        if uid is not None:
            member = self._member(tx, team_id, uid)
            if member is not None and permission in member.permissions:
                if permission in resource.team_permissions:
                    return resource
                value = tx.get(
                    (*self._path(resource_id), self.collections.grants, identity_key(uid))
                )
                if value is not None:
                    grant = record(UserGrant, value)
                    if (
                        grant.uid == uid
                        and grant.issued_by_uid == resource.owner_uid
                        and grant.access_epoch == resource.access_epoch
                        and grant.team_id == team_id
                        and permission in grant.permissions
                        and grant.permissions <= policy.shareable_permissions
                        and (grant.expires_at is None or now < grant.expires_at)
                    ):
                        return resource
        # Public and link grants never authorize writes, including signed-in visitors.
        if operation == "read":
            if permission in resource.public_permissions:
                return resource
            if link_token is not None:
                digest = sha256(link_token.get_secret_value().encode("utf-8")).hexdigest()
                value = tx.get((self.collections.links, digest))
                if value is not None:
                    link = record(LinkGrant, value)
                    if (
                        link.resource_id == resource_id
                        and link.resource_kind == kind
                        and link.access_epoch == resource.access_epoch
                        and link.issued_by_uid == resource.owner_uid
                        and link.team_id == team_id
                        and not link.revoked
                        and permission in link.permissions
                        and link.permissions <= policy.link_read_permissions
                        and (link.expires_at is None or now < link.expires_at)
                    ):
                        return resource
        raise denied()

    @staticmethod
    def _check_action(
        resource: ResourceRecord,
        policy: ResourcePermissions,
        permission: str,
        operation: Literal["read", "update", "delete"],
    ):
        if (
            not resource.team_permissions <= policy.shareable_permissions
            or not resource.public_permissions <= policy.public_read_permissions
        ):
            raise ValueError("Stored sharing permissions are invalid.")
        if permission not in getattr(policy, f"{operation}_permissions"):
            raise denied()

    def with_owned_resource(
        self,
        kind: str,
        resource_id: str,
        permission: str,
        action: Literal["read", "update"],
        callback: Callable[[FirestoreSession, ResourceRecord], Result],
        *,
        also_require: tuple[str, ...] = (),
    ) -> Result:
        uid = self._signed_in()

        def operation(tx: FirestoreSession):
            resource, policy = self._owner(tx, kind, resource_id, uid)
            for required in (permission, *also_require):
                self._check_action(resource, policy, required, action)
            return callback(tx, resource)

        return self._run(operation)

    def require_update(self, tx: FirestoreSession, resource: ResourceRecord, permission: str):
        """Check a child operation selected from state inside the owner's transaction."""
        self._check_action(resource, self._policy(tx, resource.kind), permission, "update")

    def read_owned(self, kind: str, resource_id: str, permission: str) -> dict:
        return self.with_owned_resource(
            kind, resource_id, permission, "read", lambda tx, resource: resource.data
        )

    def list_owned(self, kind: str, permission: str, limit: int | None, after: str | None):
        uid = self._signed_in()
        size = self.max_batch_resources if limit is None else limit
        if not 1 <= size <= self.max_batch_resources:
            raise HTTPException(422, "Choose a page size within the configured limit.")
        if after is not None:
            try:
                document_segment(after)
            except ValueError:
                raise HTTPException(422, "Invalid page cursor.") from None

        def operation(tx: FirestoreSession):
            policy = self._policy(tx, kind)
            if permission not in policy.read_permissions:
                raise denied()
            rows = tx.owned_page(self.collections.resources, uid, kind, size + 1, after)
            items: dict[str, dict] = {}
            for resource_id, value in rows[:size]:
                resource = record(ResourceRecord, value)
                if resource.owner_uid != uid or resource.kind != kind:
                    raise denied()
                self._validate_ancestry(tx, resource, resource_id)
                self._check_action(resource, policy, permission, "read")
                items[resource_id] = resource.data
            return items, rows[size - 1][0] if len(rows) > size else None

        return self._run(operation)

    def read(
        self,
        kind: str,
        resource_id: str,
        permission: str,
        *,
        link_token: SecretStr | None = None,
        parent_id: str | None = None,
    ) -> dict:
        return self._run(
            lambda tx: (
                self._authorize(
                    tx,
                    kind,
                    resource_id,
                    permission,
                    self._uid,
                    operation="read",
                    link_token=link_token,
                    parent_id=parent_id,
                ).data
            )
        )

    def update_data(
        self,
        kind: str,
        resource_id: str,
        permission: str,
        data: dict,
        *,
        parent_id: str | None = None,
    ) -> None:
        uid = self._signed_in()

        def operation(tx: FirestoreSession):
            resource = self._authorize(
                tx,
                kind,
                resource_id,
                permission,
                uid,
                operation="update",
                link_token=None,
                parent_id=parent_id,
            )
            values = resource.model_dump(mode="json")
            values["data"] = data
            replacement = record(ResourceRecord, values)
            tx.put(self._path(resource_id), replacement.model_dump(mode="json"), exists=True)

        self._run(operation)

    def read_many(
        self, kind: str, resource_ids: list[str], permission: str, *, parent_id: str | None = None
    ) -> dict[str, dict]:
        uid = self._signed_in()
        if len(resource_ids) > self.max_batch_resources:
            raise HTTPException(422, "Please request fewer resources at once.")
        if not resource_ids:
            return {}

        def operation(tx: FirestoreSession):
            return {
                resource_id: self._authorize(
                    tx,
                    kind,
                    resource_id,
                    permission,
                    uid,
                    operation="read",
                    link_token=None,
                    parent_id=parent_id,
                ).data
                for resource_id in resource_ids
            }

        return self._run(operation)

    def delete(
        self, kind: str, resource_id: str, permission: str, *, parent_id: str | None = None
    ) -> None:
        uid = self._signed_in()

        def operation(tx: FirestoreSession):
            self._authorize(
                tx,
                kind,
                resource_id,
                permission,
                uid,
                operation="delete",
                link_token=None,
                parent_id=parent_id,
            )
            tx.delete(self._path(resource_id))

        self._run(operation)

    def _owner(
        self, tx: FirestoreSession, kind: str, resource_id: str, uid: str
    ) -> tuple[ResourceRecord, ResourcePermissions]:
        resource = self._resource(tx, kind, resource_id, None)
        if resource.owner_uid != uid:
            raise denied()
        return resource, self._policy(tx, kind)

    def set_sharing(
        self,
        kind: str,
        resource_id: str,
        *,
        team_permissions: set[str],
        public_permissions: set[str],
    ) -> None:
        uid = self._signed_in()

        def operation(tx: FirestoreSession):
            resource, policy = self._owner(tx, kind, resource_id, uid)
            if (
                not team_permissions <= policy.shareable_permissions
                or not public_permissions <= policy.public_read_permissions
            ):
                raise denied()
            if team_permissions or public_permissions:
                self._sharing_team(tx, resource)
            values = resource.model_dump(mode="json")
            values.update(
                team_permissions=sorted(team_permissions),
                public_permissions=sorted(public_permissions),
            )
            tx.put(self._path(resource_id), values, exists=True)

        self._run(operation)

    def grant_user(
        self,
        kind: str,
        resource_id: str,
        recipient_uid: str,
        *,
        permissions: set[str],
        expires_at: datetime | None,
    ) -> None:
        uid = self._signed_in()
        self._expiry(expires_at)

        def operation(tx: FirestoreSession):
            resource, policy = self._owner(tx, kind, resource_id, uid)
            team_id = self._sharing_team(tx, resource)
            member = self._member(tx, team_id, recipient_uid)
            if member is None or not permissions <= (
                member.permissions & policy.shareable_permissions
            ):
                raise denied()
            grant = UserGrant.model_validate_json(
                json.dumps(
                    {
                        "uid": recipient_uid,
                        "issued_by_uid": uid,
                        "team_id": team_id,
                        "access_epoch": resource.access_epoch,
                        "permissions": sorted(permissions),
                        "expires_at": expires_at.isoformat() if expires_at is not None else None,
                    }
                )
            )
            path = (*self._path(resource_id), self.collections.grants, identity_key(recipient_uid))
            exists = tx.get(path) is not None
            tx.put(path, grant.model_dump(mode="json"), exists=exists)

        self._run(operation)

    def revoke_user(self, kind: str, resource_id: str, recipient_uid: str) -> None:
        uid = self._signed_in()

        def operation(tx: FirestoreSession):
            self._owner(tx, kind, resource_id, uid)
            path = (*self._path(resource_id), self.collections.grants, identity_key(recipient_uid))
            if tx.get(path) is not None:
                tx.delete(path)

        self._run(operation)

    def create_link(
        self, kind: str, resource_id: str, *, permissions: set[str], expires_at: datetime | None
    ) -> SecretStr:
        uid = self._signed_in()
        self._expiry(expires_at)
        token = secrets.token_urlsafe(self.token_bytes)
        digest = sha256(token.encode("ascii")).hexdigest()

        def operation(tx: FirestoreSession):
            resource, policy = self._owner(tx, kind, resource_id, uid)
            team_id = self._sharing_team(tx, resource)
            if not permissions or not permissions <= policy.link_read_permissions:
                raise denied()
            link = record(
                LinkGrant,
                {
                    "resource_id": resource_id,
                    "resource_kind": kind,
                    "issued_by_uid": uid,
                    "team_id": team_id,
                    "permissions": sorted(permissions),
                    "access_epoch": resource.access_epoch,
                    "expires_at": expires_at.isoformat() if expires_at is not None else None,
                    "revoked": False,
                },
            )
            tx.put((self.collections.links, digest), link.model_dump(mode="json"), exists=False)

        self._run(operation)
        return SecretStr(token)

    def revoke_link(self, kind: str, resource_id: str, token: SecretStr) -> None:
        digest = sha256(token.get_secret_value().encode("utf-8")).hexdigest()
        self.revoke_link_id(kind, resource_id, digest)

    def revoke_link_id(self, kind: str, resource_id: str, link_id: str) -> None:
        uid = self._signed_in()

        def operation(tx: FirestoreSession):
            self._owner(tx, kind, resource_id, uid)
            try:
                document_segment(link_id)
                digest_bytes = bytes.fromhex(link_id)
                if len(digest_bytes) != sha256().digest_size or digest_bytes.hex() != link_id:
                    raise ValueError("Invalid link identity")
            except ValueError, UnicodeError:
                raise denied() from None
            path = (self.collections.links, link_id)
            value = tx.get(path)
            if value is None:
                return
            link = record(LinkGrant, value)
            if link.resource_id != resource_id or link.resource_kind != kind:
                raise denied()
            values = link.model_dump(mode="json")
            values["revoked"] = True
            tx.put(path, values, exists=True)

        self._run(operation)

    def make_private(self, kind: str, resource_id: str) -> None:
        uid = self._signed_in()

        def operation(tx: FirestoreSession):
            resource, _ = self._owner(tx, kind, resource_id, uid)
            values = resource.model_dump(mode="json")
            values.update(access_epoch=uuid4().hex, team_permissions=[], public_permissions=[])
            tx.put(self._path(resource_id), values, exists=True)

        self._run(operation)

    def create_private(
        self,
        kind: str,
        permission: str,
        data: dict,
        *,
        team_id: str | None,
        operation_id: UUID | None = None,
    ) -> str:
        uid = self._signed_in()
        resource_id = uuid4().hex
        scope = json.dumps([uid, kind, permission, team_id, str(operation_id)]).encode()
        receipt_path = (
            self.collections.policies,
            self.policy_id,
            "creations",
            sha256(scope).hexdigest(),
        )
        fingerprint = sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()

        def operation(tx: FirestoreSession, *, lookup_only=False):
            policy = self._policy(tx, kind)
            if permission not in policy.create_permissions:
                raise denied()
            identity_key(uid)
            if team_id is not None:
                team = tx.get((self.collections.teams, document_segment(team_id)))
                if team is None:
                    raise denied()
                record(TeamRecord, team)
                member = self._member(tx, team_id, uid)
                if member is None or permission not in member.permissions:
                    raise denied()
            if operation_id and (prior := tx.get(receipt_path)) is not None:
                existing = self._resource(tx, kind, prior["id"], None)
                if existing.owner_uid != uid or existing.access_epoch != prior["epoch"]:
                    raise denied()
                if prior["payload"] != fingerprint:
                    raise HTTPException(409, "This operation already has different content.")
                return prior["id"]
            if lookup_only:
                raise HTTPException(409, "The project changed. Retry this operation.")
            resource = record(
                ResourceRecord,
                {
                    "kind": kind,
                    "owner_uid": uid,
                    "team_id": team_id,
                    "parent_id": None,
                    "access_epoch": uuid4().hex,
                    "data": data,
                    "team_permissions": [],
                    "public_permissions": [],
                },
            )
            tx.put(self._path(resource_id), resource.model_dump(mode="json"), exists=False)
            if operation_id:
                tx.put(
                    receipt_path,
                    {"id": resource_id, "payload": fingerprint, "epoch": resource.access_epoch},
                    exists=False,
                )
            return resource_id

        return self._run(
            operation,
            reconcile=(lambda tx: operation(tx, lookup_only=True)) if operation_id else None,
        )


def get_authorization(request: Request, user: CurrentUser) -> AuthorizationService:
    return AuthorizationService(
        request.app.state.resource_database, request.app.state.settings, user["uid"]
    )


Authorization = Annotated[AuthorizationService, Depends(get_authorization)]


@dataclass(frozen=True)
class RequireResource:
    kind: str
    permission: str
    id_parameter: str
    parent_parameter: str | None = None

    def __call__(self, request: Request, user: CurrentUser) -> dict:
        resource_id = request.path_params[self.id_parameter]
        parent_id = (
            request.path_params[self.parent_parameter]
            if self.parent_parameter is not None
            else None
        )
        return get_authorization(request, user).read(
            self.kind, resource_id, self.permission, parent_id=parent_id
        )
