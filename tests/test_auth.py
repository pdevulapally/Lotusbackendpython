import inspect
from unittest.mock import patch

from fastapi.testclient import TestClient
from firebase_admin import auth
from firebase_admin.exceptions import UnavailableError
from google.auth.exceptions import DefaultCredentialsError, TransportError

from tests.helpers import ServiceTestCase


class AuthenticationTests(ServiceTestCase):
    def test_missing_and_malformed_authorization(self):
        with (
            self.services(),
            TestClient(self.app(verified=False), client=("198.51.100.1", 1000)) as client,
        ):
            with patch("auth.auth.verify_id_token") as verify:
                for headers in (
                    {},
                    {"Authorization": "Basic invalid"},
                    {"Authorization": "Bearer"},
                ):
                    response = client.get("/api/v1/users/account", headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
                    self.assertEqual(response.json()["error"]["code"], "unauthorized")
                verify.assert_not_called()

    def test_verification_runs_once_and_identity_cannot_be_overridden(self):
        app = self.app(verified=False)
        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            with patch(
                "auth.auth.verify_id_token",
                return_value={
                    "uid": "verified-user",
                    "email": "user@example.com",
                    "email_verified": True,
                    "private_claim": "hidden",
                },
            ) as verify:
                response = client.get(
                    "/api/v1/users/account?uid=attacker",
                    headers={"Authorization": "Bearer test-token"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(),
                    {
                        "uid": "verified-user",
                        "email": "user@example.com",
                        "email_verified": True,
                    },
                )
                verify.assert_called_once_with(
                    "test-token", app=app.state.firebase_app, check_revoked=True
                )

    def test_rejected_sessions_and_provider_failures(self):
        errors = (
            (auth.InvalidIdTokenError, 401),
            (auth.ExpiredIdTokenError, 401),
            (auth.RevokedIdTokenError, 401),
            (auth.UserDisabledError, 401),
            (auth.UserNotFoundError, 401),
            (auth.CertificateFetchError, 503),
            (UnavailableError, 503),
            (TransportError, 503),
        )
        with (
            self.services(),
            TestClient(self.app(verified=False), client=("198.51.100.1", 1000)) as client,
        ):
            for error_type, status in errors:
                kwargs = (
                    {"cause": None} if "cause" in inspect.signature(error_type).parameters else {}
                )
                error = error_type("private provider details", **kwargs)
                with (
                    self.subTest(error=error_type),
                    patch("auth.auth.verify_id_token", side_effect=error),
                ):
                    response = client.get(
                        "/api/v1/users/account", headers={"Authorization": "Bearer test-token"}
                    )
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn("private provider details", response.text)

    def test_firebase_app_is_initialized_and_closed_once(self):
        with self.services() as (initialize, cleanup):
            with TestClient(self.app(), client=("198.51.100.1", 1000)):
                self.assertEqual(initialize.call_count, 1)
                self.assertEqual(
                    initialize.call_args.kwargs["options"],
                    {
                        "projectId": "test-project",
                        "httpTimeout": 1,
                    },
                )
                cleanup.assert_not_called()
            cleanup.assert_called_once()

    def test_missing_credentials_reject_startup(self):
        with self.services():
            with patch("auth.credentials.ApplicationDefault") as credentials:
                credentials.return_value.get_credential.side_effect = DefaultCredentialsError(
                    "private"
                )
                with self.assertRaisesRegex(RuntimeError, "GOOGLE_APPLICATION_CREDENTIALS"):
                    with TestClient(self.app()):
                        pass
