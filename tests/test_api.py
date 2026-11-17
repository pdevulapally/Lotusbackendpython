import asyncio
from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tests.helpers import ServiceTestCase, make_settings


class ApiTests(ServiceTestCase):
    def test_malformed_host_and_size_headers_are_client_errors(self):
        with self.services(), TestClient(self.app(), client=("198.51.100.1", 1000)) as client:
            for host in ("testserver:invalid", "[invalid", "testserver:0"):
                self.assertEqual(
                    client.get("/api/v1/users/account", headers={"Host": host}).status_code,
                    400,
                )
            response = client.get(
                "/api/v1/users/account", headers={"Content-Length": "not-a-number"}
            )
            self.assertEqual(response.status_code, 400)
            response = client.get("/api/v1/users/account", headers={"Content-Length": "9" * 5000})
            self.assertEqual(response.status_code, 413)

    def test_health_does_not_require_a_user_or_consume_their_quota(self):
        app = self.app(verified=False)
        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            with patch("auth.auth.verify_id_token") as verify:
                self.assertEqual(client.get("/health/live").json(), {"status": "live"})
                self.assertEqual(client.get("/health/ready").json(), {"status": "ready"})
                verify.assert_not_called()
                self.server.connected = False
                self.assertEqual(client.get("/health/live").status_code, 200)
                self.assertEqual(client.get("/health/ready").status_code, 503)

    def test_cors_preflight_and_authentication_errors(self):
        with (
            self.services(),
            TestClient(self.app(verified=False), client=("198.51.100.1", 1000)) as client,
        ):
            response = client.options(
                "/api/v1/users/account",
                headers={
                    "Origin": "https://frontend.example",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers["Access-Control-Allow-Origin"], "https://frontend.example"
            )
            self.assertNotIn("Access-Control-Allow-Credentials", response.headers)
            response = client.get(
                "/api/v1/users/account", headers={"Origin": "https://frontend.example"}
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(
                response.headers["Access-Control-Allow-Origin"], "https://frontend.example"
            )
            self.assertEqual(response.json()["request_id"], response.headers["X-Request-ID"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unknown_origins_and_hosts_are_rejected(self):
        with (
            self.services(),
            TestClient(self.app(verified=False), client=("198.51.100.1", 1000)) as client,
        ):
            with patch("auth.auth.verify_id_token") as verify:
                self.assertEqual(
                    client.get(
                        "/api/v1/users/account", headers={"Origin": "https://attacker.example"}
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.get(
                        "/api/v1/users/account", headers={"Host": "attacker.example"}
                    ).status_code,
                    400,
                )
                verify.assert_not_called()

    def test_declared_and_streamed_body_limits(self):
        app = self.app(settings=make_settings(request_max_body_bytes=10))

        @app.post("/body")
        async def body(request: Request):
            return {"length": len(await request.body())}

        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            self.assertEqual(client.post("/body", content=b"x" * 11).status_code, 413)
            self.assertEqual(
                client.post("/body", content=iter([b"x" * 6, b"y" * 5])).status_code, 413
            )
            self.assertEqual(client.post("/body", content=b"x" * 10).json(), {"length": 10})

    def test_timeout_returns_a_safe_error(self):
        app = self.app(
            settings=make_settings(
                request_timeout_seconds=0.02,
                firebase_timeout_seconds=0.01,
                firestore_timeout_seconds=0.01,
                rate_limit_timeout_seconds=0.01,
            )
        )

        @app.get("/slow")
        async def slow():
            await asyncio.sleep(1)

        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            with self.assertLogs("lotusbuild.api", level="ERROR"):
                response = client.get("/slow")
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json()["error"]["code"], "gateway_timeout")

    def test_logs_and_errors_do_not_contain_private_input(self):
        app = self.app()

        @app.get("/crash")
        def crash():
            raise RuntimeError("SECRET_EXCEPTION")

        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            with self.assertLogs("lotusbuild.api", level="INFO") as recorded:
                response = client.get(
                    "/crash?token=SECRET_QUERY",
                    headers={
                        "Authorization": "Bearer SECRET_TOKEN",
                        "X-Request-ID": "UNTRUSTED_ID",
                    },
                )
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("SECRET", response.text)
            self.assertNotEqual(response.headers["X-Request-ID"], "UNTRUSTED_ID")
            from observability import JsonFormatter

            logs = "\n".join(JsonFormatter().format(record) for record in recorded.records)
            self.assertNotIn("SECRET", logs)
            self.assertNotIn("UNTRUSTED_ID", logs)
            self.assertIn(response.headers["X-Request-ID"], logs)

    def test_validation_errors_do_not_echo_input(self):
        app = self.app()

        class Input(BaseModel):
            count: int

        @app.post("/validate")
        def validate(data: Input):
            return {"count": data.count}

        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            response = client.post("/validate", json={"count": "SECRET_INPUT"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("SECRET_INPUT", response.text)

    def test_pre_auth_limit_cannot_be_bypassed_by_forwarded_headers(self):
        app = self.app(
            verified=False,
            settings=make_settings(pre_auth_rate_limit={"requests": 1, "window_seconds": 60}),
        )
        with self.services(), TestClient(app, client=("198.51.100.1", 1000)) as client:
            with patch("auth.auth.verify_id_token") as verify:
                self.assertEqual(
                    client.get(
                        "/api/v1/users/account", headers={"X-Forwarded-For": "1.1.1.1"}
                    ).status_code,
                    401,
                )
                response = client.get(
                    "/api/v1/users/account",
                    headers={
                        "X-Forwarded-For": "8.8.8.8",
                        "Authorization": "Bearer anything",
                    },
                )
                self.assertEqual(response.status_code, 429)
                verify.assert_not_called()
                self.assertEqual(client.get("/health/live").status_code, 200)

    def test_only_trusted_caddy_peers_can_supply_client_addresses(self):
        settings = make_settings(pre_auth_rate_limit={"requests": 1, "window_seconds": 60})
        app = ProxyHeadersMiddleware(
            self.app(verified=False, settings=settings), trusted_hosts=["127.0.0.1"]
        )
        with self.services(), TestClient(app, client=("127.0.0.1", 1000)) as client:
            for ip in ("198.51.100.1", "198.51.100.2"):
                self.assertEqual(
                    client.get(
                        "/api/v1/users/account", headers={"X-Forwarded-For": ip}
                    ).status_code,
                    401,
                )

        other = ProxyHeadersMiddleware(
            self.app(verified=False, settings=settings), trusted_hosts=["127.0.0.1"]
        )
        with self.services(), TestClient(other, client=("198.51.100.3", 1000)) as client:
            self.assertEqual(
                client.get(
                    "/api/v1/users/account", headers={"X-Forwarded-For": "198.51.100.4"}
                ).status_code,
                401,
            )
            self.assertEqual(
                client.get(
                    "/api/v1/users/account", headers={"X-Forwarded-For": "198.51.100.5"}
                ).status_code,
                429,
            )

    def test_redis_failure_blocks_before_firebase(self):
        with (
            self.services(),
            TestClient(self.app(verified=False), client=("198.51.100.1", 1000)) as client,
        ):
            self.server.connected = False
            with patch("auth.auth.verify_id_token") as verify:
                response = client.get(
                    "/api/v1/users/account", headers={"Authorization": "Bearer token"}
                )
                self.assertEqual(response.status_code, 503)
                verify.assert_not_called()

    def test_production_requires_https_but_allows_private_health_probes(self):
        settings = make_settings(api_environment="production", require_https=True)
        with (
            self.services(),
            TestClient(self.app(settings=settings), client=("198.51.100.1", 1000)) as client,
        ):
            self.assertEqual(client.get("/api/v1/users/account").status_code, 400)
            self.assertEqual(client.get("/health/live").status_code, 200)
            self.assertEqual(client.get("https://testserver/api/v1/users/account").status_code, 200)

    def test_docs_are_explicitly_configured_and_old_route_is_removed(self):
        with self.services(), TestClient(self.app(), client=("198.51.100.1", 1000)) as client:
            self.assertEqual(client.get("/docs").status_code, 404)
            self.assertEqual(client.get("/api/v1/users/me").status_code, 404)
            self.assertEqual(client.get("/").status_code, 404)

    def test_server_uses_explicit_proxy_and_resource_settings(self):
        from config import ServerSettings
        from serve import main

        settings = ServerSettings(
            host="127.0.0.1",
            port=8000,
            workers=2,
            limit_concurrency=100,
            keep_alive_seconds=5,
            graceful_shutdown_seconds=30,
            trusted_proxy_ips=["127.0.0.1"],
        )
        with (
            patch("serve.load_settings"),
            patch("serve.ServerSettings.model_validate", return_value=settings),
        ):
            with patch("serve.uvicorn.run") as run:
                main()
                self.assertEqual(run.call_args.kwargs["forwarded_allow_ips"], "127.0.0.1")
                self.assertFalse(run.call_args.kwargs["access_log"])
                self.assertEqual(run.call_args.kwargs["limit_concurrency"], 100)
