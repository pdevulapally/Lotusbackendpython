import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import fakeredis
from fastapi import Request
from redis import ConnectionPool

from auth import get_current_user
from config import Settings
from main import create_app


def make_settings(**changes) -> Settings:
    values = {
        "api_environment": "development",
        "api_docs_enabled": False,
        "require_https": False,
        "firebase_project_id": "test-project",
        "firebase_timeout_seconds": 1,
        "firestore_database_id": "test-database",
        "firestore_timeout_seconds": 1,
        "authorization_collections": {
            "resources": "test-resources",
            "teams": "test-teams",
            "members": "test-members",
            "grants": "test-grants",
            "links": "test-links",
            "policies": "test-policies",
        },
        "authorization_policy_id": "test-policy",
        "authorization_link_token_bytes": 32,
        "authorization_max_parent_depth": 10,
        "authorization_max_batch_resources": 50,
        "project_resource_kind": "test-project-kind",
        "project_create_permission": "make",
        "project_read_permission": "inspect",
        "project_name_max_bytes": 64,
        "project_sessions_collection": "test-sessions",
        "project_session_write_permission": "start-session",
        "project_run_permission": "run-agent",
        "session_question_max_bytes": 128,
        "worker_context_max_bytes": 16384,
        "model_plans": {
            "entry": {
                "model_id": "test-small",
                "max_output_tokens": 32,
                "request_limit": {"requests": 2, "window_seconds": 60},
                "scope": "individual",
            },
            "plus": {
                "model_id": "test-large",
                "max_output_tokens": 128,
                "request_limit": {"requests": 5, "window_seconds": 60},
                "scope": "individual",
            },
        },
        "free_plan_id": "entry",
        "stripe_price_plans": {"price_test": "plus"},
        "billing_collection": "test-billing",
        "ai_runs_collection": "test-runs",
        "ai_run_timeout_seconds": 20,
        "ai_answer_max_bytes": 1024,
        "stripe_api_key": "test-key",
        "stripe_webhook_secret": "test-secret",
        "stripe_livemode": False,
        "stripe_success_url": "https://frontend.example/success",
        "stripe_cancel_url": "https://frontend.example/cancel",
        "stripe_timeout_seconds": 0.01,
        "stripe_signature_tolerance_seconds": 60,
        "rate_limit_redis_url": "redis://unused.invalid:6379/0",
        "rate_limit_key_prefix": "test:rate_limits",
        "rate_limit_timeout_seconds": 1,
        "rate_limit_policies": {
            "default": {"requests": 2, "window_seconds": 60},
            "agent": {"requests": 1, "window_seconds": 60},
        },
        "rate_limit_default_policy": "default",
        "pre_auth_rate_limit": {"requests": 100, "window_seconds": 60},
        "cors_allowed_origins": ["https://frontend.example"],
        "cors_allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        "cors_allowed_headers": ["Authorization", "Content-Type"],
        "cors_expose_headers": ["X-Request-ID", "Retry-After"],
        "cors_max_age_seconds": 600,
        "allowed_hosts": ["testserver", "127.0.0.1"],
        "request_max_body_bytes": 1048576,
        "request_timeout_seconds": 30,
        "caddy_upstream_timeout_seconds": 35,
        "log_level": "ERROR",
    }
    values.update(changes)
    return Settings(**values)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.server = fakeredis.FakeServer()
        self.settings = make_settings()
        self.verified_calls = []

    def pool(self, *args, **kwargs):
        return ConnectionPool(connection_class=fakeredis.FakeRedisConnection, server=self.server)

    def app(self, *, verified=True, settings=None):
        app = create_app(self.settings if settings is None else settings)

        def verified_user(request: Request):
            self.verified_calls.append(True)
            return {"uid": request.headers.get("X-Test-Verified-User", "alice")}

        if verified:
            app.dependency_overrides[get_current_user] = verified_user
        return app

    @contextmanager
    def services(self):
        with (
            patch("rate_limits.ConnectionPool.from_url", side_effect=self.pool),
            patch("auth.credentials.ApplicationDefault"),
            patch("auth.initialize_app", return_value=Mock()) as initialize,
            patch("auth.delete_app") as cleanup,
            patch("database.FirestoreClient", return_value=Mock()),
        ):
            yield initialize, cleanup
