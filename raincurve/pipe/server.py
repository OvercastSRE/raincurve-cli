from __future__ import annotations

import hashlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import openai

from .domains import get_env_wiring
from .mock_agent import MockAgent
from .models import InterceptedRequest, MockResponse
from .state import StateStore

log = logging.getLogger(__name__)

PIPE_PORT = 19877


class ResponseCache:
    """LRU-ish cache for GET responses to avoid redundant LLM calls."""

    def __init__(self, max_size: int = 256) -> None:
        self._cache: dict[str, MockResponse] = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    @staticmethod
    def _key(request: InterceptedRequest) -> str:
        body_hash = hashlib.md5((request.body or "").encode()).hexdigest()[:8]
        return f"{request.api}:{request.method}:{request.path}:{body_hash}"

    def get(self, request: InterceptedRequest) -> MockResponse | None:
        if request.method not in ("GET", "HEAD"):
            return None
        with self._lock:
            return self._cache.get(self._key(request))

    def put(self, request: InterceptedRequest, response: MockResponse) -> None:
        if request.method not in ("GET", "HEAD"):
            return
        with self._lock:
            if len(self._cache) >= self._max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            self._cache[self._key(request)] = response


class _PipeHandler(BaseHTTPRequestHandler):
    server: PipeServer  # type: ignore[assignment]

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/", 1)
        if not parts or not parts[0]:
            self._send_json(404, {"error": "No API prefix in path"})
            return

        api = parts[0]
        api_path = "/" + parts[1] if len(parts) > 1 else "/"
        if parsed.query:
            api_path += "?" + parsed.query

        content_length = int(self.headers.get("Content-Length", 0))
        body = (
            self.rfile.read(content_length).decode("utf-8")
            if content_length > 0
            else None
        )

        request = InterceptedRequest(
            api=api,
            method=self.command,
            path=api_path,
            headers={k: v for k, v in self.headers.items()},
            body=body,
        )

        cached = self.server.cache.get(request)
        if cached is not None:
            self._send_json(cached.status, cached.body, cached.headers)
            return

        try:
            response = self.server.mock_agent.generate_response(
                request, self.server.state
            )
        except Exception as exc:
            log.exception("Pipe mock generation failed")
            self._send_json(
                502, {"error": {"message": f"Mock generation error: {exc}"}}
            )
            return

        self.server.cache.put(request, response)
        self._send_json(response.status, response.body, response.headers)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle

    def _send_json(
        self,
        status: int,
        body: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if isinstance(body, bytes):
            payload = body
        elif isinstance(body, str):
            payload = body.encode()
        else:
            payload = json.dumps(body).encode()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("Pipe: %s", format % args)


class PipeServer(ThreadingHTTPServer):
    """LLM-backed mock API server.

    Listens on a single port. Requests are routed by the first path segment:
    ``/stripe/v1/customers`` → api="stripe", path="/v1/customers".
    GET responses are cached to avoid redundant LLM calls.
    """

    def __init__(
        self,
        client: openai.OpenAI,
        model: str = "openai/gpt-5.4-nano",
        port: int = PIPE_PORT,
    ) -> None:
        super().__init__(("0.0.0.0", port), _PipeHandler)
        self.port = self.server_address[1]
        self.state = StateStore()
        self.mock_agent = MockAgent(client=client, model=model)
        self.cache = ResponseCache()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        log.info("Pipe server listening on port %d", self.port)

    def stop(self) -> None:
        self.shutdown()
        if self._thread:
            self._thread.join(timeout=5)

    def base_url(self, host: str = "host.docker.internal") -> str:
        return f"http://{host}:{self.port}"

    def env_wiring_for(
        self,
        detected_apis: list[str],
        host: str = "host.docker.internal",
    ) -> dict[str, str]:
        base = self.base_url(host)
        wiring: dict[str, str] = {}
        for api in detected_apis:
            wiring.update(get_env_wiring(api, base, self.port))
        return wiring
