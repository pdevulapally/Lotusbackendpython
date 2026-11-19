"""Bounded, ordered conversation history stored in the existing run transaction."""

from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class Reply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    kind: Literal["answer", "clarify"]
    text: str = Field(min_length=1)


def respond(client, plan, events):
    roles = {"question": "user", "correction": "user", "reply": "user", "clarify": "assistant"}
    messages = [
        {"role": roles[e["kind"]], "content": e["text"]} for e in events if e["kind"] in roles
    ]
    result = client.messages.parse(
        model=plan.model_id,
        max_tokens=plan.max_output_tokens,
        system="Answer using the supplied conversation only. Ask for clarification when needed. "
        "Follow the user's corrections. You cannot inspect or change their project.",
        messages=messages,
        output_format=Reply,
    )
    if result.stop_reason != "end_turn" or result.parsed_output is None:
        raise ValueError("No complete conversation decision.")
    return Reply.model_validate(result.parsed_output), result.usage.model_dump(
        mode="json", exclude_none=True
    )


READY = "conversation_ready"  # Older workspace dispatchers must never claim these jobs.

EventKind = Literal[
    "question",
    "correction",
    "reply",
    "clarify",
    "answer",
    "stop_requested",
    "stopped",
    "blocked_budget",
    "uncertain",
]


class ConversationEvent(TypedDict):
    sequence: int
    kind: EventKind
    text: NotRequired[str]


class ConversationView(TypedDict):
    session: str
    status: Literal[
        "queued",
        "running",
        "clarification",
        "completed",
        "stopping",
        "stopped",
        "blocked_budget",
        "interrupted",
        "uncertain",
    ]
    cursor: int
    events: list[ConversationEvent]


def append_event(job, kind: EventKind, text: str | None = None):
    events = job["events"]
    event: ConversationEvent = {"sequence": len(events) + 1, "kind": kind}
    if text is not None:
        event["text"] = text
    events.append(event)


def view(job, after=0) -> ConversationView:
    status = job["status"]
    if status == READY:
        status = (
            "stopping"
            if job.get("stop_requested")
            else ("running" if job["phase"] == "thinking" else "queued")
        )
    return {
        "session": job["session"],
        "status": status,
        "cursor": len(job["events"]),
        "events": job["events"][after:],
    }
