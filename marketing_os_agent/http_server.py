from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


logger = logging.getLogger(__name__)


class AgentHttpServer:
    def __init__(self, host: str, port: int, health: Callable[[], dict[str, object]], ready: Callable[[], dict[str, object]], slack_handler: Callable[[bytes, dict[str, str]], tuple[int, dict[str, object]]]) -> None:
        self.host = host
        self.port = port
        self.health = health
        self.ready = ready
        self.slack_handler = slack_handler
        handler = self._build_handler()
        self.server = ThreadingHTTPServer((host, port), handler)

    def serve_forever(self) -> None:
        logger.info("http_server_started", extra={"host": self.host, "port": self.port})
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()
        logger.info("http_server_stopped")

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self._send(200, outer.health())
                    return
                if self.path == "/readyz":
                    payload = outer.ready()
                    self._send(200 if payload.get("ok") else 503, payload)
                    return
                self._send(404, {"ok": False, "error": "not_found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self.path == "/webhooks/slack":
                    headers = {key.lower(): value for key, value in self.headers.items()}
                    status, payload = outer.slack_handler(body, headers)
                    self._send(status, payload)
                    return
                if self.path == "/webhooks/notion":
                    logger.info("notion_webhook_received", extra={"bytes": len(body)})
                    self._send(202, {"ok": True, "message": "Notion native webhook received; polling remains authoritative."})
                    return
                self._send(404, {"ok": False, "error": "not_found"})

            def log_message(self, fmt: str, *args: object) -> None:
                logger.info("http_access", extra={"client": self.client_address[0], "request": fmt % args})

            def _send(self, status: int, payload: dict[str, object]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler

