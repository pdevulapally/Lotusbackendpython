from fastapi.testclient import TestClient

from tests.helpers import ServiceTestCase, make_settings


class EnvironmentPolicyTests(ServiceTestCase):
    def test_cors_permissions_and_cache_lifetime_use_configuration(self):
        settings = make_settings(
            cors_allowed_methods=["GET"],
            cors_allowed_headers=["Authorization", "X-Client-Tag"],
            cors_expose_headers=["X-Request-ID"],
            cors_max_age_seconds=17,
        )
        with (
            self.services(),
            TestClient(self.app(settings=settings), client=("198.51.100.1", 1000)) as client,
        ):
            headers = {
                "Origin": "https://frontend.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Client-Tag",
            }
            response = client.options("/api/v1/users/account", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Access-Control-Max-Age"], "17")
            self.assertEqual(response.headers["Access-Control-Allow-Methods"], "GET")
            headers["Access-Control-Request-Method"] = "POST"
            self.assertEqual(
                client.options("/api/v1/users/account", headers=headers).status_code, 400
            )
            headers["Access-Control-Request-Method"] = "GET"
            headers["Access-Control-Request-Headers"] = "X-Not-Allowed"
            self.assertEqual(
                client.options("/api/v1/users/account", headers=headers).status_code, 400
            )
            response = client.get(
                "/api/v1/users/account", headers={"Origin": "https://frontend.example"}
            )
            self.assertEqual(response.headers["Access-Control-Expose-Headers"], "X-Request-ID")

    def test_routes_enforce_the_configured_default_policy_name(self):
        settings = make_settings(
            rate_limit_default_policy="shared-api",
            rate_limit_policies={"shared-api": {"requests": 1, "window_seconds": 60}},
        )
        with (
            self.services(),
            TestClient(self.app(settings=settings), client=("198.51.100.1", 1000)) as client,
        ):
            self.assertEqual(client.get("/api/v1/users/account").status_code, 200)
            self.assertEqual(client.get("/api/v1/users/account").status_code, 429)

    def test_environment_label_does_not_choose_https_behaviour(self):
        settings = make_settings(api_environment="custom-private-environment", require_https=False)
        with (
            self.services(),
            TestClient(self.app(settings=settings), client=("198.51.100.1", 1000)) as client,
        ):
            self.assertEqual(client.get("/api/v1/users/account").status_code, 200)

        settings = make_settings(api_environment="custom-public-environment", require_https=True)
        with (
            self.services(),
            TestClient(self.app(settings=settings), client=("198.51.100.2", 1000)) as client,
        ):
            self.assertEqual(client.get("/api/v1/users/account").status_code, 400)
