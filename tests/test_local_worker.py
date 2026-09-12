import http.client
import json
import os
import threading
import unittest
from unittest.mock import patch


class LocalWorkerTests(unittest.TestCase):
    def setUp(self):
        from local_worker_server import WorkerHandler, WorkerServer
        self.environment = patch.dict(os.environ, {"SUPER_BOT_REMOTE_TOKEN": "test-only-token"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server = WorkerServer(("127.0.0.1", 0), WorkerHandler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, method, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        try:
            connection.request(method, "/", body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_health_and_unauthorized_post(self):
        status, data = self.request("GET")
        self.assertEqual((status, data["protocol"]), (200, 2))
        self.assertEqual(self.request("POST", body="{}")[0], 403)

    def test_oversized_body_is_rejected_before_read(self):
        from local_worker_server import MAX_BODY_BYTES
        status, _ = self.request("POST", headers={"X-Super-Bot-Token": "test-only-token", "Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)

    def test_authenticated_request_and_invalid_framing(self):
        headers = {"X-Super-Bot-Token": "test-only-token"}
        with patch("local_worker_server.main_handler", return_value={"statusCode": 200, "body": '{"ok":true}'}) as handler:
            self.assertEqual(self.request("POST", headers=headers, body='{"token":"test-only-token"}')[0], 200)
            handler.assert_called_once()
        headers["Content-Length"] = "-1"
        self.assertEqual(self.request("POST", headers=headers)[0], 400)


if __name__ == "__main__":
    unittest.main()
