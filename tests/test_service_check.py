from unittest.mock import patch

from pydantic import ValidationError

from config import IntegrationSettings
from scripts.check_services import main
from tests.helpers import ServiceTestCase, make_settings


def integration_settings(**changes):
    values = {
        "target_environment": "review-environment",
        "api_origin": "https://testserver",
        "client_ip": "198.51.100.17",
        "id_token": "test-token",
        "expected_uid": "verified-user",
    }
    values.update(changes)
    return IntegrationSettings(**values)


class ServiceCheckTests(ServiceTestCase):
    def test_selected_environment_and_origin_are_used(self):
        settings = make_settings(api_environment="review-environment", require_https=True)
        selected = integration_settings()
        with (
            self.services(),
            patch("scripts.check_services.load_settings", return_value=settings),
            patch(
                "scripts.check_services.IntegrationSettings.model_validate", return_value=selected
            ),
            patch("auth.auth.verify_id_token") as verify,
            patch("builtins.print"),
            patch("scripts.check_services.check_authorization_policy"),
        ):
            from firebase_admin.auth import InvalidIdTokenError

            verify.side_effect = [{"uid": "verified-user"}, InvalidIdTokenError("invalid")]
            main()
            self.assertEqual(verify.call_count, 2)

    def test_environment_mismatch_stops_before_any_service_connection(self):
        with (
            patch("scripts.check_services.load_settings", return_value=make_settings()),
            patch(
                "scripts.check_services.IntegrationSettings.model_validate",
                return_value=integration_settings(),
            ),
            patch("scripts.check_services.create_app") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "INTEGRATION_TARGET_ENVIRONMENT"):
                main()
            create.assert_not_called()

    def test_origin_must_match_an_allowed_host(self):
        settings = make_settings(api_environment="review-environment")
        with (
            patch("scripts.check_services.load_settings", return_value=settings),
            patch(
                "scripts.check_services.IntegrationSettings.model_validate",
                return_value=integration_settings(api_origin="https://wrong-host.example"),
            ),
            patch("scripts.check_services.create_app") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "allowed host"):
                main()
            create.assert_not_called()

    def test_integration_settings_reject_invalid_input(self):
        for changes in (
            {"id_token": ""},
            {"api_origin": "https://testserver/path"},
            {"api_origin": "https://user:password@testserver"},
            {"client_ip": "not-an-ip"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                integration_settings(**changes)
