from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.helpers import ServiceTestCase, make_settings


class DatabaseTests(ServiceTestCase):
    def test_firestore_reuses_firebase_app_and_configured_database(self):
        app = self.app()
        client = Mock()
        with self.services(), patch("database.FirestoreClient", return_value=client) as factory:
            with TestClient(app, client=("198.51.100.1", 1000)):
                factory.assert_called_once_with(
                    credentials=app.state.firebase_app.credential.get_credential()
                )
                self.assertIs(app.state.resource_database.client, client)
                self.assertEqual(
                    app.state.resource_database.database,
                    "projects/test-project/databases/test-database",
                )
                client.transport.close.assert_not_called()
            client.transport.close.assert_called_once_with()

    def test_firestore_initialization_failure_does_not_allow_startup(self):
        with self.services() as (_, cleanup):
            with patch("database.FirestoreClient", side_effect=ValueError("invalid configuration")):
                with self.assertRaises(ValueError), TestClient(self.app()):
                    pass
            cleanup.assert_called_once()

    def test_firestore_configuration_is_required_and_bounded(self):
        with self.assertRaises(ValidationError):
            make_settings(firestore_database_id="")
        with self.assertRaises(ValidationError):
            make_settings(firestore_timeout_seconds=30)
