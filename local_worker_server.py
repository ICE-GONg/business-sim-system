"""Small HTTP wrapper that turns this Mac into a Super Bot worker."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scf_worker import main_handler


class WorkerHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"ok": True, "service": "business-sim-super-bot"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        result = main_handler({"body": raw.decode("utf-8")}, None)
        body = result["body"].encode("utf-8")
        self.send_response(int(result.get("statusCode", 200)))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(fmt % args, flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("SUPER_BOT_LOCAL_PORT", "8765"))
    print(f"Super Bot worker listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), WorkerHandler).serve_forever()
