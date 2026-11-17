from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import fakeredis
from fastapi import Depends
from fastapi.testclient import TestClient
from limits import RateLimitItemPerSecond
from limits.storage import RedisStorage
from limits.strategies import MovingWindowRateLimiter

from auth import CurrentUser
from rate_limits import RateLimit
from tests.helpers import ServiceTestCase, make_settings


class RateLimitTests(ServiceTestCase):
    def app(self, **kwargs):
        app = super().app(**kwargs)
        from main import get_account

        app.get("/account")(get_account)

        @app.get("/future")
        def future(user: CurrentUser):
            return {"uid": user["uid"]}

        @app.get("/agent", dependencies=[Depends(RateLimit("agent"))])
        def agent():
            return {"accepted": True}

        return app

    def storage(self, uri, **options):
        return RedisStorage(uri, connection_pool=self.pool(), **options)

    def test_shared_quota_across_routes_and_user_isolation(self):
        with self.services(), TestClient(self.app(), client=("198.51.100.1", 1000)) as client:
            headers = {"X-Test-Verified-User": "alice"}
            self.assertEqual(client.get("/account", headers=headers).status_code, 200)
            self.assertEqual(client.get("/future", headers=headers).status_code, 200)
            response = client.get("/account?uid=bob", headers=headers)
            self.assertEqual(response.status_code, 429)
            self.assertGreater(int(response.headers["Retry-After"]), 0)
            self.assertEqual(
                client.get("/account", headers={"X-Test-Verified-User": "bob"}).status_code,
                200,
            )
            connection = fakeredis.FakeRedis(server=self.server)
            try:
                keys = connection.keys("test:rate_limits:*")
                self.assertTrue(keys)
                for key in keys:
                    self.assertNotIn(b"alice", key)
                    self.assertGreater(connection.ttl(key), 0)
                    self.assertLessEqual(connection.ttl(key), 60)
            finally:
                connection.close()

    def test_workers_share_counters(self):
        with (
            self.services(),
            TestClient(self.app(), client=("198.51.100.1", 1000)) as first,
            TestClient(self.app(), client=("198.51.100.1", 1000)) as second,
        ):
            headers = {"X-Test-Verified-User": "alice"}
            self.assertEqual(first.get("/account", headers=headers).status_code, 200)
            self.assertEqual(second.get("/account", headers=headers).status_code, 200)
            self.assertEqual(first.get("/account", headers=headers).status_code, 429)

    def test_named_policy_adds_stricter_limit(self):
        with self.services(), TestClient(self.app(), client=("198.51.100.1", 1000)) as client:
            headers = {"X-Test-Verified-User": "alice"}
            self.assertEqual(client.get("/agent", headers=headers).status_code, 200)
            self.assertEqual(client.get("/agent", headers=headers).status_code, 429)

    def test_concurrent_requests_cannot_exceed_quota(self):
        stores = [self.storage("redis://", key_prefix="concurrent") for _ in range(2)]
        try:
            limiters = [MovingWindowRateLimiter(store) for store in stores]
            policy = RateLimitItemPerSecond(10, 60)
            with ThreadPoolExecutor(max_workers=16) as executor:
                results = list(
                    executor.map(
                        lambda index: limiters[index % 2].hit(policy, "default", "alice"),
                        range(64),
                    )
                )
            self.assertEqual(sum(results), 10)
        finally:
            for store in stores:
                store.storage.close()

    def test_window_expiry_restores_capacity(self):
        store = self.storage("redis://", key_prefix="expiry")
        try:
            limiter = MovingWindowRateLimiter(store)
            policy = RateLimitItemPerSecond(1, 60)
            with patch("limits.storage.redis.time.time", return_value=1000):
                self.assertTrue(limiter.hit(policy, "default", "alice"))
                self.assertFalse(limiter.hit(policy, "default", "alice"))
            with patch("limits.storage.redis.time.time", return_value=1061):
                self.assertTrue(limiter.hit(policy, "default", "alice"))
        finally:
            store.storage.close()

    def test_redis_failure_blocks_requests(self):
        with self.services(), TestClient(self.app(), client=("198.51.100.1", 1000)) as client:
            self.server.connected = False
            response = client.get("/account", headers={"X-Test-Verified-User": "alice"})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("unused.invalid", response.text)

    def test_redis_failure_blocks_startup(self):
        with self.services():
            self.server.connected = False
            with self.assertRaisesRegex(RuntimeError, "Cannot connect"):
                with TestClient(self.app(), client=("198.51.100.1", 1000)):
                    pass

    def test_unknown_policy_blocks_startup(self):
        self.settings = make_settings(
            rate_limit_policies={"default": {"requests": 2, "window_seconds": 60}}
        )
        with self.services(), self.assertRaisesRegex(RuntimeError, "agent"):
            with TestClient(self.app(), client=("198.51.100.1", 1000)):
                pass
