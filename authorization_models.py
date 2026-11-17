from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

Identifier = Annotated[str, Field(strict=True, min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ResourcePermissions(Record):
    read_permissions: frozenset[Identifier]
    update_permissions: frozenset[Identifier]
    delete_permissions: frozenset[Identifier]
    create_permissions: frozenset[Identifier]
    shareable_permissions: frozenset[Identifier]
    public_read_permissions: frozenset[Identifier]
    link_read_permissions: frozenset[Identifier]

    @property
    def owner_permissions(self) -> frozenset[str]:
        return (
            self.read_permissions
            | self.update_permissions
            | self.delete_permissions
            | self.create_permissions
        )

    @model_validator(mode="after")
    def validate_permissions(self):
        if "*" in self.owner_permissions:
            raise ValueError("Permissions must be explicit; wildcards are not supported.")
        if sum(
            len(values)
            for values in (
                self.read_permissions,
                self.update_permissions,
                self.delete_permissions,
                self.create_permissions,
            )
        ) != len(self.owner_permissions):
            raise ValueError("Each permission must describe exactly one resource operation.")
        if not self.shareable_permissions <= self.owner_permissions:
            raise ValueError("Shared permissions must be valid resource permissions.")
        if not (self.public_read_permissions | self.link_read_permissions) <= (
            self.shareable_permissions & self.read_permissions
        ):
            raise ValueError("Public/link read permissions must be shareable permissions.")
        return self


class AuthorizationPolicy(Record):
    resources: dict[Identifier, ResourcePermissions]


class ResourceRecord(Record):
    kind: Identifier
    owner_uid: Identifier
    team_id: Identifier | None
    parent_id: Identifier | None
    access_epoch: Identifier
    data: dict[str, JsonValue]
    team_permissions: frozenset[Identifier]
    public_permissions: frozenset[Identifier]


class TeamRecord(Record):
    sharing_enabled: bool


class MembershipRecord(Record):
    uid: Identifier
    active: bool
    permissions: frozenset[Identifier]


class UserGrant(Record):
    uid: Identifier
    issued_by_uid: Identifier
    team_id: Identifier
    access_epoch: Identifier
    permissions: frozenset[Identifier]
    expires_at: AwareDatetime | None


class LinkGrant(Record):
    resource_id: Identifier
    resource_kind: Identifier
    issued_by_uid: Identifier
    team_id: Identifier
    access_epoch: Identifier
    permissions: frozenset[Identifier]
    expires_at: AwareDatetime | None
    revoked: bool
