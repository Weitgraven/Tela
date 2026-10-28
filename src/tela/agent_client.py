"""Client for talking to tela-agent over its Unix socket."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class AgentError(Exception):
    pass


class AgentUnavailable(AgentError):
    pass


class AgentClient:
    def __init__(self, socket_path: Path, timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        payload = json.dumps({"action": action, "params": params or {}}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.socket_path))
                sock.sendall(payload)
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if chunk.endswith(b"\n"):
                        break
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise AgentUnavailable("tela-agent is not running") from exc
        except OSError as exc:
            raise AgentUnavailable(f"cannot reach tela-agent: {exc}") from exc

        try:
            response = json.loads(b"".join(chunks))
        except json.JSONDecodeError as exc:
            raise AgentError("invalid response from tela-agent") from exc
        if not response.get("ok"):
            raise AgentError(str(response.get("error", "unknown error")))
        return response.get("result")
