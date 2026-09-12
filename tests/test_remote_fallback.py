from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sim import remote_worker as remote


class RemoteFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-fallback-")
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"SIM_DB_PATH": str(Path(self.temp.name) / "source.db")})
        environment.start()
        self.addCleanup(environment.stop)
        from sim import db

        self.addCleanup(setattr, db, "DB_PATH", db.DB_PATH)
        db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
        db.init_db()
        self.conn = sqlite3.connect(db.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM companies WHERE id>2")
        self.conn.execute("UPDATE companies SET is_bot=1,is_super_bot=1,home_city='广州'")
        self.conn.execute("UPDATE rounds SET status='open',starts_at='start-1'")
        self.conn.commit()
        self.primary, self.fallback = "https://local.test", "https://cloud.test"
        self.calls = []

    def dispatch(self, payload):
        with closing(sqlite3.connect(":memory:")) as worker:
            worker.row_factory = sqlite3.Row
            remote.load_snapshot(worker, remote.decode_snapshot(payload))
            return remote.run_remote_request(worker, payload)

    def test_connection_failure_retries_same_request_on_fallback(self):
        def post(endpoint, token, payload):
            self.calls.append((endpoint, payload["phase"], payload["company_id"], payload["request_id"], payload["snapshot_hash"]))
            if endpoint == self.primary:
                raise remote.RemoteTransportError("connection failed")
            return self.dispatch(payload)

        with patch.object(remote, "_post", post):
            self.assertEqual(remote.remote_submit(self.conn, [self.primary, self.fallback], "token", 1), 2)
        self.assertEqual([row[0] for row in self.calls], [self.primary, self.fallback, self.fallback, self.fallback])
        self.assertEqual(self.calls[0][1:], self.calls[1][1:])
        self.assertFalse(remote.remote_pending(self.conn, 1))

    def test_business_error_never_uses_fallback(self):
        def post(endpoint, token, payload):
            self.calls.append(endpoint)
            return {"ok": False, "error": "business calculation rejected"}

        with patch.object(remote, "_post", post):
            with self.assertRaisesRegex(remote.RemoteWorkerError, "business calculation rejected"):
                remote.remote_submit(self.conn, [self.primary, self.fallback], "token", 1)
        self.assertEqual(self.calls, [self.primary])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_stale_result_never_uses_fallback(self):
        def post(endpoint, token, payload):
            self.calls.append(endpoint)
            result = self.dispatch(payload)
            self.conn.execute("UPDATE companies SET cash=cash+100 WHERE id=1")
            self.conn.commit()
            return result

        with patch.object(remote, "_post", post):
            with self.assertRaisesRegex(remote.RemoteWorkerError, "数据已改变"):
                remote.remote_submit(self.conn, [self.primary, self.fallback], "token", 1)
        self.assertEqual(self.calls, [self.primary])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_partial_saved_job_switches_without_repeating_first_bot(self):
        saved = []

        def post(endpoint, token, payload):
            self.calls.append((endpoint, payload["phase"], payload["company_id"]))
            if endpoint == self.primary and payload["company_id"] == 2:
                saved.append(dict(self.conn.execute("SELECT * FROM decisions WHERE company_id=1").fetchone()))
                raise remote.RemoteTransportError("local worker disconnected")
            if endpoint == self.fallback and payload["company_id"] == 2:
                self.assertEqual(dict(self.conn.execute("SELECT * FROM decisions WHERE company_id=1").fetchone()), saved[0])
            return self.dispatch(payload)

        with patch.object(remote, "_post", post):
            remote.remote_submit(self.conn, [self.primary, self.fallback], "token", 1)
        self.assertEqual(self.calls, [
            (self.primary, "bot", 1), (self.primary, "bot", 2),
            (self.fallback, "bot", 2), (self.fallback, "rebalance", None),
        ])
        self.assertEqual(self.conn.execute("SELECT submitted_at FROM decisions WHERE company_id=1").fetchone()[0], saved[0]["submitted_at"])

    def test_post_classifies_connection_and_busy_but_not_auth_errors(self):
        for error in (
            urllib.error.URLError("connection refused"),
            *[urllib.error.HTTPError(self.primary, code, "unavailable", {}, io.BytesIO())
              for code in (429, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530)],
        ):
            with patch.object(remote.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(remote.RemoteTransportError):
                    remote._post(self.primary, "token", {})
        for code in (400, 403, 500):
            with patch.object(remote.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(self.primary, code, "rejected", {}, io.BytesIO())):
                with self.assertRaises(remote.RemoteWorkerError) as caught:
                    remote._post(self.primary, "token", {})
            self.assertNotIsInstance(caught.exception, remote.RemoteTransportError)

    def test_cloudflare_530_fails_over_and_http_500_does_not(self):
        def open_request(request, timeout):
            self.assertEqual(timeout, 120)
            self.calls.append(request.full_url)
            if request.full_url == self.primary:
                raise urllib.error.HTTPError(self.primary, 530, "tunnel offline", {}, io.BytesIO())
            return io.BytesIO(json.dumps(self.dispatch(json.loads(request.data))).encode())

        with patch.object(remote.urllib.request, "urlopen", open_request):
            self.assertEqual(remote.remote_submit(self.conn, [self.primary, self.fallback], "token", 1), 2)
        self.assertEqual(self.calls, [self.primary, self.fallback, self.fallback, self.fallback])
        self.calls = []

        def business_failure(request, timeout):
            self.calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 500, "calculation rejected", {}, io.BytesIO())

        with patch.object(remote.urllib.request, "urlopen", business_failure):
            with self.assertRaisesRegex(remote.RemoteWorkerError, "HTTP 500"):
                remote.remote_submit(self.conn, [self.primary, self.fallback], "token", 1, replace_existing=True)
        self.assertEqual(self.calls, [self.primary])

    def test_endpoint_formats_preserve_priority_and_single_url(self):
        expected = [self.primary, self.fallback]
        self.assertEqual(remote.resolve_remote_endpoints(self.primary, fallback=self.fallback), expected)
        self.assertEqual(remote.resolve_remote_endpoints(urls=expected), expected)
        self.assertEqual(remote.resolve_remote_endpoints(urls=f" {self.primary}, {self.fallback} "), expected)
        self.assertEqual(remote.resolve_remote_endpoints(urls=json.dumps(expected), fallback=self.fallback), expected)
        self.assertEqual(remote.resolve_remote_endpoints(self.primary), [self.primary])

    def test_health_is_public_get_only_and_handles_offline_fallback(self):
        requests = []

        def get(request, timeout):
            requests.append(request)
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertIsNone(request.get_header("X-super-bot-token"))
            self.assertLessEqual(timeout, 5)
            if request.full_url == self.fallback:
                raise urllib.error.URLError("offline")
            return io.BytesIO(b'{"ok":true,"protocol":2,"service":"business-sim-super-bot"}')

        with patch.object(remote.urllib.request, "urlopen", get):
            statuses = remote.remote_health([self.primary, self.fallback])
        self.assertEqual(len(requests), 2)
        self.assertEqual([row["ok"] for row in statuses], [True, False])
        self.assertFalse(any(self.primary in str(row) or self.fallback in str(row) for row in statuses))
