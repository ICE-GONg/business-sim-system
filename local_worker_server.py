"""Small HTTP wrapper that turns this Mac into a Super Bot worker."""
from __future__ import annotations

import json
import hmac
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scf_worker import main_handler

MAX_BODY_BYTES = 8 * 1024 * 1024
_COMPUTE_LOCK = threading.Lock()


class WorkerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._connections = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class WorkerHandler(BaseHTTPRequestHandler):
    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        self._reply(200, {"ok": True, "protocol": 2, "service": "business-sim-super-bot"})

    def do_POST(self) -> None:
        expected = os.environ.get("SUPER_BOT_REMOTE_TOKEN", "")
        if not expected:
            self._reply(503, {"ok": False, "error": "worker authentication is not configured"})
            return
        if not hmac.compare_digest(self.headers.get("X-Super-Bot-Token", ""), expected):
            self._reply(403, {"ok": False, "error": "forbidden"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("invalid request framing")
            length = int(lengths[0])
            if length <= 0:
                raise ValueError("empty request")
        except ValueError:
            self._reply(400, {"ok": False, "error": "invalid content length"})
            return
        if length > MAX_BODY_BYTES:
            self._reply(413, {"ok": False, "error": "request too large"})
            return
        if not _COMPUTE_LOCK.acquire(blocking=False):
            self._reply(503, {"ok": False, "error": "worker busy; retry after current bot completes"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                self._reply(400, {"ok": False, "error": "incomplete request"})
                return
            result = main_handler({"body": raw.decode("utf-8")}, None)
            self._reply(int(result.get("statusCode", 200)), json.loads(result["body"]))
        except (UnicodeError, ValueError):
            self._reply(400, {"ok": False, "error": "invalid request body"})
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        finally:
            _COMPUTE_LOCK.release()

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.command} request", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("SUPER_BOT_LOCAL_PORT", "8765"))
    print(f"Super Bot worker listening on 127.0.0.1:{port}", flush=True)
    WorkerServer(("127.0.0.1", port), WorkerHandler).serve_forever()
