import json
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from config import ServerSettings, Settings, load_settings
from tests.helpers import make_settings


class ConfigurationTests(unittest.TestCase):
    def test_complete_configuration_loads_from_environment(self):
        values = make_settings().model_dump()
        values["rate_limit_redis_url"] = "redis://unused.invalid:6379/0"
        for key in ("stripe_api_key", "stripe_webhook_secret"):
            values[key] = values[key].get_secret_value()
        environment = {
            key.upper(): json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
            for key, value in values.items()
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = load_settings()
            self.assertEqual(settings.firebase_project_id, "test-project")
            self.assertEqual(settings.rate_limit_policies["default"].requests, 2)
            self.assertEqual(settings.cors_allowed_origins, ["https://frontend.example"])

    def test_configuration_is_required(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValidationError):
            Settings()

    def test_no_wildcards_or_paths_in_origins(self):
        for origin in (
            "*",
            "https://*.example",
            "https://frontend.example/path",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValidationError):
                make_settings(cors_allowed_origins=[origin])

    def test_https_policy_is_explicit_and_independent_of_environment_name(self):
        make_settings(
            api_environment="local-preview",
            require_https=False,
            cors_allowed_origins=["http://configured-host.example:3000"],
        )
        with self.assertRaises(ValidationError):
            make_settings(require_https=True, cors_allowed_origins=["http://localhost:3000"])

    def test_default_policy_and_logging_level_are_configurable(self):
        settings = make_settings(
            rate_limit_policies={"shared-api": {"requests": 17, "window_seconds": 42}},
            rate_limit_default_policy="shared-api",
            log_level="DEBUG",
        )
        self.assertEqual(settings.rate_limit_default_policy, "shared-api")
        self.assertEqual(settings.log_level, "DEBUG")
        with self.assertRaises(ValidationError):
            make_settings(log_level="not-a-log-level")

    def test_policies_are_strict_positive_and_default_is_required(self):
        for policies in (
            {},
            {"default": {"requests": 0, "window_seconds": 60}},
            {"default": {"requests": True, "window_seconds": 60}},
            {"default": {"requests": 2, "window_seconds": -1}},
            {"default": {"requests": 2, "window_seconds": 60, "typo": 1}},
        ):
            with self.subTest(policies=policies), self.assertRaises(ValidationError):
                make_settings(rate_limit_policies=policies)

    def test_redis_options_cannot_override_connection_security(self):
        for url in ("memory://", "redis://", "rediss://host?ssl_cert_reqs=none", "redis://host:0"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                make_settings(rate_limit_redis_url=url)

    def test_error_messages_do_not_expose_configuration_values(self):
        with patch.dict(os.environ, {"RATE_LIMIT_REDIS_URL": "SECRET_VALUE"}, clear=True):
            with self.assertRaises(RuntimeError) as raised:
                load_settings()
            self.assertNotIn("SECRET_VALUE", str(raised.exception))

    def test_emulator_configuration_is_rejected(self):
        with patch.dict(os.environ, {"FIREBASE_AUTH_EMULATOR_HOST": "localhost:9099"}):
            with self.assertRaises(ValidationError):
                make_settings()

    def test_firestore_emulator_and_ambiguous_authorization_collections_are_rejected(self):
        with patch.dict(os.environ, {"FIRESTORE_EMULATOR_HOST": "localhost:8080"}):
            with self.assertRaises(ValidationError):
                make_settings()
        collections = make_settings().authorization_collections.model_dump()
        collections["teams"] = collections["resources"]
        with self.assertRaises(ValidationError):
            make_settings(authorization_collections=collections)
        with self.assertRaises(ValidationError):
            make_settings(authorization_link_token_bytes=1)

    def test_bounded_dependency_timeouts(self):
        with self.assertRaises(ValidationError):
            make_settings(firebase_timeout_seconds=30)
        with self.assertRaises(ValidationError):
            make_settings(rate_limit_timeout_seconds=float("inf"))
        with self.assertRaises(ValidationError):
            make_settings(caddy_upstream_timeout_seconds=30)

    def test_proxy_trust_cannot_include_every_peer(self):
        values = {
            "host": "127.0.0.1",
            "port": 8000,
            "workers": 1,
            "limit_concurrency": 20,
            "keep_alive_seconds": 5,
            "graceful_shutdown_seconds": 30,
        }
        for ips in (["*"], ["0.0.0.0/0"], ["::/0"], ["192.168.1.4/24"]):
            with self.subTest(ips=ips), self.assertRaises(ValidationError):
                ServerSettings(**values, trusted_proxy_ips=ips)
        ServerSettings(**values, trusted_proxy_ips=["127.0.0.1", "::1"])
