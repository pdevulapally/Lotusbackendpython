"""Owner-bound run submission and durable per-project writer coordination."""

import json
import time
from hashlib import sha256
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from limits import RateLimitItemPerSecond
from pydantic import UUID4

from auth import CurrentUser
from authorization import Authorization, denied, record
from billing import effective_plan
from conversation import READY, append_event, view
from observability import AgentJSONResponse
from project_sessions import SavedSession, SessionInput, validated_question
from rate_limits import consume_limit

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/sessions/{session_id}/run",
    default_response_class=AgentJSONResponse,
)


def run_key(project, session):
    return sha256(f"{project}/{session}".encode()).hexdigest()


def slot_path(settings, project):
    return settings.ai_runs_collection, "project-" + sha256(project.encode()).hexdigest()


def owned_run(job, project):
    if not job or job["uid"] != project.owner_uid or job["epoch"] != project.access_epoch:
        raise denied()
    return job


def public_state(job, after=0):
    if job.get("mode") == "conversation":
        return view(job, after)
    result = {key: job[key] for key in ("status", "answer", "review")}
    if result["status"] == "queued" and job.get("started") is not None:
        result["status"] = "running"
    if result["status"] == "interrupted" and job["pending_write"]:
        result["status"] = "awaiting_workspace_receipt"
    if job.get("stop_requested") and job["status"] == "queued":
        result["status"] = "stopping"
    result["messages"] = job.get("messages", [])
    return result


def change_run(
    project_id, session_id, request, access, *, message_id=None, question=None, reply_to=None
):
    settings = request.app.state.settings
    key = run_key(project_id, session_id.hex)
    path = (settings.ai_runs_collection, key)

    def change(tx, project):
        job = owned_run(tx.get(path), project)
        messages = job.get("messages", [])
        if message_id is not None:
            if job.get("mode") == "conversation":
                access.require_update(tx, project, settings.project_session_write_permission)
            for message in messages:
                if message["id"] == message_id.hex:
                    if message["text"] != question or message.get("reply_to") != reply_to:
                        raise HTTPException(409, "This message ID already has different text.")
                    return public_state(job)
        elif job["status"] == "stopped" or (
            job.get("mode") == "conversation" and job.get("stop_requested")
        ):
            return public_state(job)
        slot = tx.get(slot_path(settings, project_id))
        if not slot or slot["active"] != key:
            raise HTTPException(409, "This run has finished.")
        if message_id is None:
            job["stop_requested"] = True
            if job.get("mode") == "conversation":
                append_event(job, "stop_requested")
                if job["phase"] != "thinking":
                    job.update(status="stopped", claim=None)
                    append_event(job, "stopped")
                    tx.put(slot_path(settings, project_id), {**slot, "active": None}, exists=True)
            if job["status"] == "interrupted" and job["pending_write"]:
                job.update(
                    status="queued", phase="recover", available=time.time(), receipt_attempts=0
                )
        else:
            if job["status"] not in {"queued", READY, "clarification"} or job.get("stop_requested"):
                raise HTTPException(409, "This run is no longer accepting changes.")
            waiting = job["status"] == "clarification"
            expected_reply = len(job["events"]) if waiting else None
            if reply_to != expected_reply:
                raise HTTPException(409, "This clarification is no longer current.")
            if job.get("mode") == "conversation":
                append_event(job, "reply" if waiting else "correction", question)
                if waiting:
                    job.update(
                        status=READY, phase="decide", available=time.time(), submitted=time.time()
                    )
            job["messages"] = [
                *messages,
                {
                    "id": message_id.hex,
                    "text": question,
                    "after": len(job["history"]),
                    **({"reply_to": reply_to} if reply_to is not None else {}),
                },
            ]
            if len(json.dumps(job).encode()) > settings.worker_context_max_bytes:
                raise HTTPException(422, "This run has reached its conversation size limit.")
        tx.put(path, job, exists=True)
        return public_state(job)

    return access.with_owned_resource(
        settings.project_resource_kind,
        project_id,
        settings.project_run_permission,
        "update",
        change,
    )


@router.put("/messages/{message_id}", status_code=202)
def steer(
    project_id: str,
    session_id: UUID4,
    message_id: UUID4,
    body: SessionInput,
    request: Request,
    access: Authorization,
    reply_to: Annotated[int | None, Query(gt=0)] = None,
):
    question = body.message(request.app.state.settings.session_question_max_bytes)
    return change_run(
        project_id,
        session_id,
        request,
        access,
        message_id=message_id,
        question=question,
        reply_to=reply_to,
    )


@router.put("/stop", status_code=202)
def stop(project_id: str, session_id: UUID4, request: Request, access: Authorization):
    return change_run(project_id, session_id, request, access)


@router.put("", status_code=202)
def submit(
    project_id: str,
    session_id: UUID4,
    request: Request,
    access: Authorization,
    user: CurrentUser,
    mode: Literal["read_only", "edit", "conversation"] | None = None,
    body: SessionInput | None = None,
):
    settings = request.app.state.settings
    key = run_key(project_id, session_id.hex)
    path = (settings.ai_runs_collection, key)
    slot = slot_path(settings, project_id)
    supplied = body.message(settings.session_question_max_bytes) if body else None
    if (mode == "conversation") != (supplied is not None):
        raise HTTPException(422, "Supply a question with conversation mode.")

    def enqueue(tx, project):
        session_path = (
            settings.authorization_collections.resources,
            project_id,
            settings.project_sessions_collection,
            session_id.hex,
        )
        value = tx.get(session_path)
        if value is not None:
            saved = record(SavedSession, value)
            if saved.owner_uid != project.owner_uid:
                raise denied()
            if supplied is not None and saved.question != supplied:
                raise HTTPException(409, "This session already has a different question.")
            requested = saved.question
        elif supplied is not None:
            requested = supplied
        else:
            raise denied()
        old = tx.get(path)
        if old is not None:
            owned_run(old, project)
            if mode is not None and mode != old.get("mode", "edit"):
                raise HTTPException(409, "A submitted run's access mode cannot be changed.")
            if old["status"] == "interrupted" and old["pending_write"]:
                writer = tx.get(slot)
                if (
                    not writer
                    or writer["active"] != key
                    or writer["generation"] != old["generation"]
                ):
                    raise HTTPException(409, "This run no longer holds the project.")
                old.update(
                    status="queued", phase="recover", available=time.time(), receipt_attempts=0
                )
                tx.put(path, old, exists=True)
            return public_state(old)
        if project.team_id is not None:
            raise HTTPException(403, "Team run allowances are not configured.")
        current = tx.get(slot)
        if current and current["active"] is not None:
            raise HTTPException(409, "A run is already using this project.")
        try:
            question = validated_question(
                requested,
                settings.session_question_max_bytes,
            )
        except ValueError:
            raise HTTPException(
                422, "This saved message must be replaced in a new session."
            ) from None
        plan = effective_plan(tx, settings, "individual", project.owner_uid)
        quota = settings.model_plans[plan].request_limit
        consume_limit(
            request.app,
            RateLimitItemPerSecond(quota.requests, quota.window_seconds),
            "workspace-runs",
            project.owner_uid,
        )
        generation = current["generation"] + 1 if current else 1
        job = {
            "project": project_id,
            "uid": project.owner_uid,
            "epoch": project.access_epoch,
            "session": session_id.hex,
            "plan": plan,
            "model": settings.model_plans[plan].model_id,
            "usage": [],
            "question": question,
            "mode": mode or "edit",
            "generation": generation,
            "status": "queued",
            "submitted": time.time(),
            "available": time.time(),
            "claim": None,
            "step": 0,
            "action": {"action": "begin"},
            "history": [],
            "phase": "tool",
            "pending_write": False,
            "answer": None,
            "review": None,
        }
        latest = (current or {}).get("latest_conversation")
        if mode == "conversation":
            signed_in_at = user.get("auth_time")
            if type(signed_in_at) is not int or signed_in_at <= 0:
                raise HTTPException(401, "Sign in again before starting a conversation.")
            job.update(phase="decide", action=None, status=READY, events=[], auth_time=signed_in_at)
            append_event(job, "question", question)
            if len(json.dumps(job).encode()) > settings.worker_context_max_bytes:
                raise HTTPException(422, "The question exceeds the conversation budget.")
            latest = session_id.hex
            if value is None:
                tx.put(
                    session_path,
                    {"owner_uid": project.owner_uid, "question": question},
                    exists=False,
                )
        tx.put(
            slot,
            {
                "active": key,
                "generation": generation,
                "sandbox": current["sandbox"] if current else None,
                **({"latest_conversation": latest} if latest else {}),
            },
            exists=current is not None,
        )
        tx.put(path, job, exists=False)
        return public_state(job)

    return access.with_owned_resource(
        settings.project_resource_kind,
        project_id,
        settings.project_run_permission,
        "update",
        enqueue,
        also_require=(settings.project_session_write_permission,) if mode == "conversation" else (),
    )


@router.get("")
def status(
    project_id: str,
    session_id: UUID4,
    request: Request,
    access: Authorization,
    after: Annotated[int, Query(ge=0)] = 0,
):
    settings = request.app.state.settings

    def read(tx, project):
        job = tx.get((settings.ai_runs_collection, run_key(project_id, session_id.hex)))
        return public_state(owned_run(job, project), after)

    return access.with_owned_resource(
        settings.project_resource_kind, project_id, settings.project_read_permission, "read", read
    )
