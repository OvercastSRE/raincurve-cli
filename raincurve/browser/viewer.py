"""
Live browser viewer — streams FRAME and STEP lines from the browser
container to a local web page via polling.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import webbrowser
from typing import Any

import docker

_VIEWER_PORT = 19876
_instance: BrowserViewer | None = None

_HTML_PAGE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>raincurve — live sandbox view</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0f1117;
    color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  header {
    background: #161922;
    border-bottom: 1px solid #252830;
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  header h1 {
    font-size: 14px;
    font-weight: 600;
    color: #94a3b8;
  }
  #status {
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 10px;
  }
  #status.connected { color: #4ade80; background: #14532d33; }
  #status.disconnected { color: #f87171; background: #7f1d1d33; }
  #step-bar {
    background: #131620;
    border-bottom: 1px solid #252830;
    padding: 8px 20px;
    font-size: 12px;
    color: #999;
    flex-shrink: 0;
    min-height: 32px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  #step-action {
    color: #94a3b8;
    font-weight: 600;
  }
  #step-thought {
    color: #555d6e;
    font-style: italic;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  #viewer {
    flex: 1;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    overflow: auto;
    padding: 8px;
  }
  #screen {
    width: 100%;
    height: auto;
    border: 1px solid #252830;
    border-radius: 6px;
    object-fit: contain;
  }
  #placeholder {
    color: #3a3f4b;
    font-size: 14px;
  }
</style>
</head>
<body>
  <header>
    <h1>raincurve sandbox</h1>
    <span id="status" class="disconnected">connecting</span>
  </header>
  <div id="step-bar">
    <span id="step-action">waiting for scenario...</span>
    <span id="step-thought"></span>
  </div>
  <div id="viewer">
    <span id="placeholder">waiting for first frame...</span>
    <img id="screen" style="display:none" />
  </div>
  <script>
    const status = document.getElementById('status');
    const screen = document.getElementById('screen');
    const placeholder = document.getElementById('placeholder');
    const stepAction = document.getElementById('step-action');
    const stepThought = document.getElementById('step-thought');

    let lastSeq = -1;

    async function poll() {
      try {
        const r = await fetch('/poll?seq=' + lastSeq);
        if (!r.ok) { throw new Error(r.status); }
        const msg = await r.json();

        status.textContent = 'live';
        status.className = 'connected';

        if (msg.seq !== undefined) lastSeq = msg.seq;

        if (msg.frame) {
          screen.src = 'data:image/png;base64,' + msg.frame;
          screen.style.display = 'block';
          placeholder.style.display = 'none';
        }
        if (msg.step) {
          const s = msg.step;
          const action = s.action || '?';
          const step = s.step || 0;
          stepAction.textContent = 'step ' + step + ': ' + action;
          stepThought.textContent = s.thought || '';
          if (s.verdict) {
            stepAction.textContent = s.verdict + ': ' + (s.summary || '');
            stepThought.textContent = '';
          }
        }
        if (msg.meta) {
          stepAction.textContent = msg.meta.kind + ': ' + msg.meta.value;
        }
      } catch (e) {
        status.textContent = 'disconnected';
        status.className = 'disconnected';
      }
      setTimeout(poll, 300);
    }
    poll();
  </script>
</body>
</html>
"""


class _ViewerHandler(http.server.BaseHTTPRequestHandler):
    viewer: BrowserViewer | None = None

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "":
            self._serve_html()
        elif self.path.startswith("/poll"):
            self._serve_poll()
        else:
            self.send_error(404)

    def _serve_html(self) -> None:
        data = _HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_poll(self) -> None:
        seq = -1
        if "seq=" in self.path:
            try:
                seq = int(self.path.split("seq=")[1].split("&")[0])
            except ValueError:
                pass

        v = _ViewerHandler.viewer
        if not v:
            self._json_response({"seq": 0})
            return

        with v._lock:
            if v._seq <= seq:
                self._json_response({"seq": v._seq})
                return
            resp: dict[str, Any] = {"seq": v._seq}
            if v._latest_frame:
                resp["frame"] = v._latest_frame
            if v._latest_step:
                resp["step"] = v._latest_step
            if v._latest_meta:
                resp["meta"] = v._latest_meta

        self._json_response(resp)

    def _json_response(self, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class BrowserViewer:

    def __init__(self, container_name: str) -> None:
        global _instance
        self._container_name = container_name
        self._lock = threading.Lock()
        self._seq = 0
        self._latest_frame: str | None = None
        self._latest_step: dict | None = None
        self._latest_meta: dict | None = None
        self._stop_event = threading.Event()
        self._log_thread: threading.Thread | None = None

        # Reuse existing server or start new one
        if _instance and _instance._server_thread and _instance._server_thread.is_alive():
            _instance._stop_log_tail()
            self._server = _instance._server
            self._server_thread = _instance._server_thread
            self._opened = True
        else:
            self._server = None
            self._server_thread: threading.Thread | None = None
            self._opened = False

        _ViewerHandler.viewer = self
        _instance = self

    def start(self) -> None:
        if not self._server:
            self._start_server()
        self._start_log_tail()
        if not self._opened:
            webbrowser.open(f"http://localhost:{_VIEWER_PORT}")
            self._opened = True

    def stop(self) -> None:
        self._stop_log_tail()

    def _stop_log_tail(self) -> None:
        self._stop_event.set()
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=3)

    def _start_server(self) -> None:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", _VIEWER_PORT), _ViewerHandler
        )
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server = server
        self._server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._server_thread.start()

    def _start_log_tail(self) -> None:
        self._stop_event.clear()
        self._log_thread = threading.Thread(target=self._tail_logs, daemon=True)
        self._log_thread.start()

    def _tail_logs(self) -> None:
        try:
            client = docker.from_env()
            container = client.containers.get(self._container_name)
        except Exception:
            return

        buffer = ""
        try:
            for chunk in container.logs(stream=True, follow=True, tail=5):
                if self._stop_event.is_set():
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line:
                        continue
                    self._process_line(line)
        except Exception:
            pass

    def _process_line(self, line: str) -> None:
        with self._lock:
            if line.startswith("FRAME:"):
                self._latest_frame = line[6:]
                self._seq += 1
            elif line.startswith("STEP:"):
                try:
                    self._latest_step = json.loads(line[5:])
                    self._seq += 1
                except Exception:
                    pass
            elif ":" in line:
                kind, _, value = line.partition(":")
                if kind in ("BOOT", "READY", "URL", "WARN", "ERROR"):
                    self._latest_meta = {"kind": kind, "value": value}
                    self._seq += 1
