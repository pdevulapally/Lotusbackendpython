from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StringConstraints

from authorization import Authorization
from config import Settings


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class Project(BaseModel):
    id: str
    name: str


class ProjectPage(BaseModel):
    projects: list[Project]
    next_cursor: str | None


def project_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects")
    kind = settings.project_resource_kind
    read = settings.project_read_permission

    @router.post("", status_code=201)
    def create_project(body: ProjectInput, access: Authorization) -> Project:
        if len(body.name.encode("utf-8")) > settings.project_name_max_bytes:
            raise HTTPException(422, "Please choose a shorter project name.")
        resource_id = access.create_private(
            kind, settings.project_create_permission, body.model_dump(), team_id=None
        )
        return Project(id=resource_id, name=body.name)

    @router.get("/{project_id}")
    def get_project(project_id: str, access: Authorization) -> Project:
        data = access.read_owned(kind, project_id, read)
        return Project(id=project_id, name=data["name"])

    @router.get("")
    def list_projects(
        access: Authorization,
        limit: Annotated[int | None, Query(gt=0)] = None,
        after: str | None = None,
    ) -> ProjectPage:
        records, cursor = access.list_owned(kind, read, limit, after)
        return ProjectPage(
            projects=[Project(id=key, name=data["name"]) for key, data in records.items()],
            next_cursor=cursor,
        )

    return router
