from __future__ import annotations

import http.server
import threading
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from raincurve.config import load_global_config, save_global_config
from raincurve.ui.console import rc_error, rc_print, rc_success

RAINCURVE_AUTH_URL = "https://raincurve.dev/cli-auth"
RAINCURVE_TOKEN_URL = "https://raincurve.dev/api/cli/token"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    auth_code: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]

        if code:
            _CallbackHandler.auth_code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Logged in! You can close this tab.</h2></body></html>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing auth code")

    def log_message(self, *args: Any) -> None:
        pass


def login() -> bool:
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://localhost:{port}"

    url = f"{RAINCURVE_AUTH_URL}?redirect_uri={redirect_uri}"
    rc_print(f"Opening browser for login...")
    rc_print(f"If it doesn't open, visit: {url}", style="rc.dim")
    webbrowser.open(url)

    _CallbackHandler.auth_code = None
    server.timeout = 120
    while _CallbackHandler.auth_code is None:
        server.handle_request()

    server.server_close()
    code = _CallbackHandler.auth_code

    if not code:
        rc_error("Login failed — no auth code received.")
        return False

    try:
        resp = httpx.post(RAINCURVE_TOKEN_URL, json={"code": code, "redirect_uri": redirect_uri})
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, Exception) as e:
        rc_error(f"Token exchange failed: {e}")
        return False

    cfg = load_global_config()
    cfg.auth.access_token = data.get("access_token")
    cfg.auth.refresh_token = data.get("refresh_token")
    cfg.auth.expires_at = data.get("expires_at")
    cfg.auth.email = data.get("email")
    save_global_config(cfg)

    rc_success(f"Logged in as {cfg.auth.email}")
    return True
