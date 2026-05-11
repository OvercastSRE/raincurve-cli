from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass
class CmdResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class RemoteHost:
    def __init__(
        self,
        host: str,
        user: str = "ubuntu",
        key_path: str | None = None,
        port: int = 22,
    ) -> None:
        self.host = host
        self.user = user
        self.key_path = key_path
        self.port = port

    @classmethod
    def from_string(cls, conn: str, key_path: str | None = None, port: int = 22) -> RemoteHost:
        user = "ubuntu"
        host = conn
        if "@" in conn:
            user, host = conn.rsplit("@", 1)
        if ":" in host:
            host, port_str = host.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                pass
        return cls(host=host, user=user, key_path=key_path, port=port)

    @property
    def label(self) -> str:
        return f"{self.user}@{self.host}"

    def _ssh_base(self) -> list[str]:
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "BatchMode=yes",
        ]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _scp_base(self) -> list[str]:
        cmd = [
            "scp",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=15",
        ]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.port != 22:
            cmd.extend(["-P", str(self.port)])
        return cmd

    def test(self) -> CmdResult:
        return self.run("echo rc-ok", timeout=20)

    def run(self, cmd: str, timeout: int = 120) -> CmdResult:
        full = self._ssh_base() + [cmd]
        try:
            proc = subprocess.run(
                full,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return CmdResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", "Command timed out")
        except FileNotFoundError:
            return CmdResult(127, "", "ssh not found — install OpenSSH")

    def run_stream(
        self,
        cmd: str,
        on_line: Callable[[str], None],
        timeout: int = 900,
    ) -> int:
        full = self._ssh_base() + ["-tt", cmd]
        try:
            proc = subprocess.Popen(
                full,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                on_line(line.rstrip("\n\r"))
            proc.wait(timeout=timeout)
            return proc.returncode or 0
        except subprocess.TimeoutExpired:
            proc.kill()
            return 124
        except FileNotFoundError:
            return 127

    def run_interactive(self, cmd: str) -> int:
        """Fully interactive SSH — user's stdin/stdout connected directly."""
        import sys

        full = self._ssh_base() + ["-tt", cmd]
        # Drop BatchMode for interactive use
        full = [a for a in full if a != "BatchMode=yes"]
        try:
            proc = subprocess.run(full, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
            return proc.returncode or 0
        except FileNotFoundError:
            return 127

    def upload(self, local_path: str, remote_path: str) -> CmdResult:
        cmd = self._scp_base() + [local_path, f"{self.user}@{self.host}:{remote_path}"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
            return CmdResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", "Upload timed out")
        except FileNotFoundError:
            return CmdResult(127, "", "scp not found — install OpenSSH")

    def download(self, remote_path: str, local_path: str) -> CmdResult:
        cmd = self._scp_base() + [f"{self.user}@{self.host}:{remote_path}", local_path]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
            return CmdResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", "Download timed out")
        except FileNotFoundError:
            return CmdResult(127, "", "scp not found — install OpenSSH")

    def to_dict(self) -> dict:
        d: dict = {"host": self.host, "user": self.user, "port": self.port}
        if self.key_path:
            d["key_path"] = self.key_path
        return d

    @classmethod
    def from_dict(cls, d: dict) -> RemoteHost:
        return cls(
            host=d["host"],
            user=d.get("user", "ubuntu"),
            key_path=d.get("key_path"),
            port=d.get("port", 22),
        )
