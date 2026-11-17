import unicodedata

from fastapi import APIRouter, HTTPException
from pydantic import UUID4, BaseModel, ConfigDict, StrictStr

from authorization import Authorization, denied, record
from authorization_models import Record, ResourceRecord
from config import Settings
from firestore_access import FirestoreSession
from observability import AgentJSONResponse


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: StrictStr

    def message(self, max_bytes: int) -> str:
        try:
            return validated_question(self.question, max_bytes)
        except UnicodeEncodeError:
            raise HTTPException(422, "Enter valid Unicode text.") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None


def validated_question(text: str, max_bytes: int) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Enter a message.")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Please shorten your question.")
    # Preserve source code and natural language, including RTL scripts and emoji.
    # Explicit direction overrides and invisible control bytes can disguise instructions.
    if any(
        (unicodedata.category(c) == "Cc" and c not in "\t\r\n")
        or unicodedata.bidirectional(c)
        in {"LRO", "RLO", "LRE", "RLE", "PDF", "LRI", "RLI", "FSI", "PDI"}
        for c in text
    ):
        raise ValueError("Remove hidden control characters from your message.")
    return text.strip()


class SavedSession(Record):
    owner_uid: str
    question: str


class ProjectSession(BaseModel):
    id: UUID4
    question: str


def session_router(settings: Settings) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/sessions", default_response_class=AgentJSONResponse
    )

    def session_path(project_id: str, session_id: UUID4):
        return (
            settings.authorization_collections.resources,
            project_id,
            settings.project_sessions_collection,
            session_id.hex,
        )

    @router.put("/{session_id}")
    def save_session(
        project_id: str, session_id: UUID4, body: SessionInput, access: Authorization
    ) -> ProjectSession:
        question = body.message(settings.session_question_max_bytes)
        path = session_path(project_id, session_id)

        def save(tx: FirestoreSession, project: ResourceRecord):
            contents = SavedSession(owner_uid=project.owner_uid, question=question)
            existing = tx.get(path)
            if existing is None:
                tx.put(path, contents.model_dump(), exists=False)
            else:
                saved = record(SavedSession, existing)
                if saved.owner_uid != project.owner_uid:
                    raise denied()
                if saved.question != question:
                    raise HTTPException(409, "This session already has a different question.")
            return ProjectSession(id=session_id, question=question)

        return access.with_owned_resource(
            settings.project_resource_kind,
            project_id,
            settings.project_session_write_permission,
            "update",
            save,
        )

    @router.get("/{session_id}")
    def reopen_session(project_id: str, session_id: UUID4, access: Authorization) -> ProjectSession:
        def read(tx: FirestoreSession, project: ResourceRecord):
            value = tx.get(session_path(project_id, session_id))
            if value is None:
                raise denied()
            saved = record(SavedSession, value)
            if saved.owner_uid != project.owner_uid:
                raise denied()
            return ProjectSession(id=session_id, question=saved.question)

        return access.with_owned_resource(
            settings.project_resource_kind,
            project_id,
            settings.project_read_permission,
            "read",
            read,
        )

    return router
