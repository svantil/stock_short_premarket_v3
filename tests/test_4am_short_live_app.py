"""Offline API checks: explicit controls, origin protection, and safe errors."""

import unittest

from fastapi.testclient import TestClient

from four_am_short.live.app import COVER_CONFIRMATION, create_app


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.running = False

    async def open(self):
        self.calls.append("open")

    async def close(self):
        self.calls.append("close")

    async def start(self):
        if self.failure:
            raise self.failure
        self.calls.append("start")
        self.running = True

    async def stop_entries(self):
        self.calls.append("stop_entries")

    async def cover_all(self):
        self.calls.append("cover_all")

    async def start_backtest(self):
        self.calls.append("start_backtest")

    def snapshot(self):
        return {"strategy_name": "4am short", "mode": "monitor", "running": self.running, "trades": []}


class LiveAppTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.client = TestClient(create_app(engine=self.engine), base_url="http://127.0.0.1:8003")
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))

    def headers(self):
        token = self.client.get("/api/session").json()["control_token"]
        return {"X-Control-Token": token, "Origin": "http://127.0.0.1:8003"}

    def test_page_reading_does_not_start_engine(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("4am short", response.text)
        self.assertIn("monthlyRows", response.text)
        self.assertEqual(self.client.get("/api/state").json()["strategy_name"], "4am short")
        self.assertEqual(self.engine.calls, ["open"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_static_assets_load(self):
        for name in ["app.js", "styles.css"]:
            with self.subTest(name=name):
                response = self.client.get(f"/static/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertGreater(len(response.content), 100)

    def test_every_mutation_requires_token(self):
        for path in ["start", "stop", "cover", "backtest"]:
            with self.subTest(path=path):
                response = self.client.post(f"/api/{path}", json={"confirmation": COVER_CONFIRMATION})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.engine.calls, ["open"])

    def test_valid_controls_and_returned_state(self):
        headers = self.headers()
        self.assertTrue(self.client.post("/api/start", headers=headers).json()["running"])
        self.assertEqual(self.client.post("/api/stop", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/backtest", headers=headers).status_code, 200)
        self.assertEqual(self.engine.calls, ["open", "start", "stop_entries", "start_backtest"])

    def test_cover_requires_exact_phrase(self):
        headers = self.headers()
        for phrase in ["", "COVER", "cover 4am short"]:
            self.assertEqual(self.client.post("/api/cover", headers=headers, json={"confirmation": phrase}).status_code, 400)
        self.assertNotIn("cover_all", self.engine.calls)
        self.assertEqual(self.client.post("/api/cover", headers=headers, json={"confirmation": COVER_CONFIRMATION}).status_code, 200)
        self.assertEqual(self.engine.calls[-1], "cover_all")

    def test_cross_origin_cannot_read_token_or_mutate(self):
        headers = self.headers()
        for origin in ["https://example.com", "http://127.0.0.1:9000", "null", "http://[invalid"]:
            with self.subTest(origin=origin):
                headers["Origin"] = origin
                self.assertEqual(self.client.get("/api/session", headers=headers).status_code, 403)
                self.assertEqual(self.client.post("/api/start", headers=headers).status_code, 403)
        self.assertEqual(self.engine.calls, ["open"])

    def test_fetch_metadata_blocks_cross_site(self):
        headers = {**self.headers(), "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.client.get("/api/session", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/start", headers=headers).status_code, 403)

    def test_dns_rebinding_host_is_rejected(self):
        for path in ["/", "/api/session", "/api/state"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers={"Host": "attacker.example:8003"}).status_code, 400)

    def test_malformed_hosts_are_rejected(self):
        for host in ["user@127.0.0.1:8003", "127.0.0.1:badport", "127.0.0.1/path", "[invalid", "127.0.0.1:8003#fragment"]:
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/session", headers={"Host": host}).status_code, 400)

    def test_loopback_hosts_are_accepted(self):
        for host in ["localhost:8003", "127.0.0.1:8003", "[::1]:8003"]:
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/state", headers={"Host": host}).status_code, 200)

    def test_invalid_token_is_rejected(self):
        response = self.client.post("/api/start", headers={"X-Control-Token": "bad-token"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("start", self.engine.calls)

    def test_exception_details_do_not_leak_secrets(self):
        for exception, status in [(RuntimeError("SECRET_KEY=abcdef"), 409), (OSError("PASSWORD=abcdef"), 503)]:
            with self.subTest(exception=type(exception).__name__):
                self.engine.failure = exception
                response = self.client.post("/api/start", headers=self.headers())
                self.assertEqual(response.status_code, status)
                self.assertNotIn("abcdef", response.text)
                self.assertIn("activity log", response.json()["detail"])

    def test_token_is_different_for_each_app_instance(self):
        with TestClient(create_app(engine=FakeEngine()), base_url="http://localhost") as other:
            self.assertNotEqual(self.client.get("/api/session").json(), other.get("/api/session").json())

    def test_lifespan_closes_engine(self):
        engine = FakeEngine()
        with TestClient(create_app(engine=engine), base_url="http://localhost"):
            self.assertEqual(engine.calls, ["open"])
        self.assertEqual(engine.calls, ["open", "close"])


if __name__ == "__main__":
    unittest.main()
