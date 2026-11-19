import unittest
from unittest.mock import Mock

from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.types import (
    BatchGetDocumentsResponse,
    BeginTransactionResponse,
    Document,
    RunQueryResponse,
)

from firestore_access import FirestoreAccessStore


class FirestoreAccessTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.begin_transaction.return_value = BeginTransactionResponse(
            transaction=b"test-tx"
        )
        self.store = FirestoreAccessStore(self.client, "test-project", "test-db", 2)

    def test_read_and_write_use_the_same_transaction_and_bounded_no_retry_calls(self):
        name = "projects/test-project/databases/test-db/documents/records/id"
        self.client.batch_get_documents.return_value = [
            BatchGetDocumentsResponse(
                found=Document(name=name, fields=_helpers.encode_dict({"data": "before"}))
            )
        ]

        def operation(session):
            self.assertEqual(session.get(("records", "id")), {"data": "before"})
            session.put(("records", "id"), {"data": "after"}, exists=True)
            return "completed"

        self.assertEqual(self.store.run(operation), "completed")
        read = self.client.batch_get_documents.call_args.kwargs
        commit = self.client.commit.call_args.kwargs
        self.assertEqual(read["request"]["transaction"], b"test-tx")
        self.assertEqual(commit["request"]["transaction"], b"test-tx")
        self.assertTrue(commit["request"]["writes"][0].current_document.exists)
        for call in (
            self.client.begin_transaction.call_args,
            self.client.batch_get_documents.call_args,
            self.client.commit.call_args,
        ):
            self.assertIsNone(call.kwargs["retry"])
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 2)
        self.client.rollback.assert_not_called()

    def test_failed_commit_rolls_back_without_retry_or_success(self):
        self.client.commit.side_effect = Aborted("changed permission record")
        with self.assertRaises(Aborted):
            self.store.run(lambda session: "must not return this")
        self.client.commit.assert_called_once()
        self.client.rollback.assert_called_once()

    def test_owner_query_uses_transaction_scoped_filters_and_exclusive_cursor(self):
        root = self.store.database + "/documents"
        self.client.run_query.return_value = [
            RunQueryResponse(),
            RunQueryResponse(
                document=Document(
                    name=root + "/assets/b", fields=_helpers.encode_dict({"name": "B"})
                )
            ),
        ]
        result = self.store.run(lambda tx: tx.owned_page("assets", "alice", "custom", 3, "a"))
        self.assertEqual(result, [("b", {"name": "B"})])
        call = self.client.run_query.call_args.kwargs
        self.assertIsNone(call["retry"])
        self.assertGreater(call["timeout"], 0)
        request = call["request"]
        self.assertEqual(request["transaction"], b"test-tx")
        query = request["structured_query"]
        self.assertEqual(query.from_[0].collection_id, "assets")
        self.assertEqual(query.order_by[0].field.field_path, "__name__")
        self.assertEqual(query.limit, 3)
        self.assertEqual(query.start_at.values[0].reference_value, root + "/assets/a")
        self.assertFalse(query.start_at.before)
        self.assertEqual(
            {
                f.field_filter.field.field_path: f.field_filter.value.string_value
                for f in query.where.composite_filter.filters
            },
            {"owner_uid": "alice", "kind": "custom"},
        )

    def test_missing_transaction_identity_never_falls_back_to_nontransactional_write(self):
        self.client.begin_transaction.return_value = BeginTransactionResponse(transaction=b"")
        with self.assertRaisesRegex(ValueError, "transaction identity"):
            self.store.run(lambda session: session.put(("records", "id"), {}, exists=False))
        self.client.commit.assert_not_called()

    def test_due_query_includes_only_workspace_and_conversation_ready_jobs(self):
        self.client.run_query.return_value = []
        self.assertIsNone(self.store.run(lambda tx: tx.next_queued_run("jobs")))
        request = self.client.run_query.call_args.kwargs["request"]
        query = request["structured_query"]
        status, due = (item.field_filter for item in query.where.composite_filter.filters)
        self.assertEqual(status.op.name, "IN")
        self.assertEqual(
            [v.string_value for v in status.value.array_value.values],
            [
                "queued",
                "conversation_ready",
            ],
        )
        self.assertEqual(request["transaction"], b"test-tx")
        self.assertEqual(due.field.field_path, "available")
        self.assertEqual(query.limit, 1)

    def test_wrong_returned_document_identity_is_rejected(self):
        self.client.batch_get_documents.return_value = [
            BatchGetDocumentsResponse(found=Document(name="wrong"))
        ]
        with self.assertRaisesRegex(RuntimeError, "unexpected document"):
            self.store.run(lambda session: session.get(("records", "id")))
        self.client.commit.assert_not_called()

    def test_writes_cannot_be_queued_before_authorization_reads(self):
        def operation(session):
            session.put(("records", "id"), {}, exists=False)
            session.get(("records", "id"))

        with self.assertRaisesRegex(RuntimeError, "before queuing"):
            self.store.run(operation)
        self.client.commit.assert_not_called()
