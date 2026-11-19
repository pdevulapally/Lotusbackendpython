"""Bounded, single-attempt Firestore transactions for authorization and data writes."""

import time
from collections.abc import Callable
from typing import TypeVar

from google.api_core.exceptions import DeadlineExceeded, GoogleAPICallError
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.services.firestore import FirestoreClient
from google.cloud.firestore_v1.types import Cursor, Document, StructuredQuery, Write

from conversation import READY
from observability import logger

Result = TypeVar("Result")


def document_segment(value: str) -> str:
    # Firestore document/collection ID constraints, not deployment defaults.
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or value in {".", ".."}
        or (value.startswith("__") and value.endswith("__"))
        or len(value.encode("utf-8")) > 1500
    ):
        raise ValueError("Invalid Firestore path segment.")
    return value


class FirestoreSession:
    def __init__(self, client: FirestoreClient, database: str, transaction: bytes, deadline: float):
        self.client = client
        self.database = database
        self.transaction = transaction
        self.deadline = deadline
        self.writes: list[Write] = []

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("Authorization transaction deadline exceeded.")
        return remaining

    def name(self, path: tuple[str, ...]) -> str:
        if not path or len(path) % 2:
            raise ValueError("A document path must contain collection/document pairs.")
        return f"{self.database}/documents/" + "/".join(document_segment(part) for part in path)

    def get(self, path: tuple[str, ...]) -> dict | None:
        if self.writes:
            raise RuntimeError("Read authorization records before queuing any writes.")
        name = self.name(path)
        responses = self.client.batch_get_documents(
            request={
                "database": self.database,
                "documents": [name],
                "transaction": self.transaction,
            },
            retry=None,
            timeout=self.remaining(),
        )
        results = list(responses)
        if len(results) != 1:
            raise RuntimeError("Firestore did not return exactly one document result.")
        result = results[0]
        if result.missing == name:
            return None
        if result.found.name != name:
            raise RuntimeError("Firestore returned an unexpected document identity.")
        # Reuse the locked SDK's encoders; no custom Firestore value serialization.
        # Our records use JSON-compatible values and reject document references.
        return _helpers.decode_dict(result.found.fields, None)

    def put(self, path: tuple[str, ...], record: dict, *, exists: bool) -> None:
        self.writes.append(
            Write(
                update=Document(name=self.name(path), fields=_helpers.encode_dict(record)),
                current_document={"exists": exists},
            )
        )

    def owned_page(
        self, collection: str, uid: str, kind: str, limit: int, after: str | None
    ) -> list[tuple[str, dict]]:
        if self.writes:
            raise RuntimeError("Read resources before queuing any writes.")
        filters = [
            {
                "field_filter": {
                    "field": {"field_path": field},
                    "op": "EQUAL",
                    "value": {"string_value": value},
                }
            }
            for field, value in (("owner_uid", uid), ("kind", kind))
        ]
        query = StructuredQuery(
            from_=[{"collection_id": document_segment(collection)}],
            where={"composite_filter": {"op": "AND", "filters": filters}},
            order_by=[{"field": {"field_path": "__name__"}, "direction": "ASCENDING"}],
            limit=limit,
        )
        if after is not None:
            query.start_at = Cursor(
                values=[{"reference_value": self.name((collection, after))}], before=False
            )
        return self.query_rows(collection, query, limit)

    def next_queued_run(self, collection: str):
        query = StructuredQuery(
            from_=[{"collection_id": document_segment(collection)}],
            where={
                "composite_filter": {
                    "op": "AND",
                    "filters": [
                        {
                            "field_filter": {
                                "field": {"field_path": "status"},
                                "op": "IN",
                                "value": {
                                    "array_value": {
                                        "values": [
                                            {"string_value": "queued"},
                                            {"string_value": READY},
                                        ]
                                    }
                                },
                            }
                        },
                        {
                            "field_filter": {
                                "field": {"field_path": "available"},
                                "op": "LESS_THAN_OR_EQUAL",
                                "value": {"double_value": time.time()},
                            }
                        },
                    ],
                }
            },
            order_by=[{"field": {"field_path": "available"}, "direction": "ASCENDING"}],
            limit=1,
        )
        rows = self.query_rows(collection, query, 1)
        return rows[0] if rows else None

    def query_rows(
        self, collection: str, query: StructuredQuery, limit: int
    ) -> list[tuple[str, dict]]:
        if self.writes:
            raise RuntimeError("Read resources before queuing any writes.")
        parent = f"{self.database}/documents"
        responses = self.client.run_query(
            request={"parent": parent, "structured_query": query, "transaction": self.transaction},
            retry=None,
            timeout=self.remaining(),
        )
        rows = []
        prefix = f"{parent}/{collection}/"
        for response in responses:
            if "document" not in response:
                continue
            doc = response.document
            if not doc.name.startswith(prefix) or len(rows) >= limit:
                raise ValueError("Unexpected Firestore query result.")
            resource_id = document_segment(doc.name[len(prefix) :])
            rows.append((resource_id, _helpers.decode_dict(doc.fields, None)))
        return rows

    def delete(self, path: tuple[str, ...]) -> None:
        self.writes.append(Write(delete=self.name(path), current_document={"exists": True}))


class FirestoreAccessStore:
    def __init__(self, client: FirestoreClient, project: str, database: str, timeout: float):
        self.client = client
        self.database = (
            f"projects/{document_segment(project)}/databases/{document_segment(database)}"
        )
        self.timeout = timeout

    def run(self, operation: Callable[[FirestoreSession], Result]) -> Result:
        deadline = time.monotonic() + self.timeout
        response = self.client.begin_transaction(
            request={"database": self.database, "options": {"read_write": {}}},
            retry=None,
            timeout=self.timeout,
        )
        if not isinstance(response.transaction, bytes) or not response.transaction:
            raise ValueError("Firestore did not return a transaction identity.")
        session = FirestoreSession(self.client, self.database, response.transaction, deadline)
        try:
            result = operation(session)
            self.client.commit(
                request={
                    "database": self.database,
                    "transaction": response.transaction,
                    "writes": session.writes,
                },
                retry=None,
                timeout=session.remaining(),
            )
            return result
        except Exception:
            try:
                self.client.rollback(
                    request={"database": self.database, "transaction": response.transaction},
                    retry=None,
                    timeout=self.timeout,
                )
            except GoogleAPICallError as cleanup_error:
                logger.error(
                    "authorization_rollback_failed",
                    extra={"exception_type": type(cleanup_error).__name__},
                )
            raise

    def close(self) -> None:
        self.client.transport.close()
