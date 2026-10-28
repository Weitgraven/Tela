"""Unix socket server for tela-agent.

Protocol: the client connects, writes one JSON object terminated by a newline,
and receives one JSON object terminated by a newline:

    -> {"action": "system.status", "params": {}}
    <- {"ok": true, "result": {...}}
    <- {"ok": false, "error": "message"}
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import logging
import os
import pwd
import socket
import struct
from pathlib import Path
from typing import Any

from . import (
    actions,  # noqa: F401  (registers actions)
    registry,
)
from .registry import ActionError

log = logging.getLogger("tela-agent")

MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def allowed_uids(panel_user: str, extra: str | None) -> set[int]:
    uids = {0}
    try:
        uids.add(pwd.getpwnam(panel_user).pw_uid)
    except KeyError:
        log.warning("panel user %r does not exist; only root may connect", panel_user)
    for part in (extra or "").split(","):
        if part.strip().isdigit():
            uids.add(int(part.strip()))
    return uids


def dispatch(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {"ok": False, "error": "request must be a JSON object"}
    name = request.get("action")
    params = request.get("params", {})
    if not isinstance(name, str) or not isinstance(params, dict):
        return {"ok": False, "error": "invalid request"}
    handler = registry.get(name)
    if handler is None:
        return {"ok": False, "error": f"unknown action {name!r}"}
    try:
        return {"ok": True, "result": handler(params)}
    except ActionError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        log.exception("action %s failed", name)
        return {"ok": False, "error": "internal error"}


class AgentServer:
    def __init__(self, socket_path: Path, group: str, uids: set[int]) -> None:
        self.socket_path = socket_path
        self.group = group
        self.uids = uids

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            uid = peer_uid(sock)
            if uid not in self.uids:
                log.warning("rejected connection from uid %s", uid)
                await self._reply(writer, {"ok": False, "error": "permission denied"})
                return
            try:
                line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=REQUEST_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                await self._reply(writer, {"ok": False, "error": "bad request"})
                return
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                await self._reply(writer, {"ok": False, "error": "invalid JSON"})
                return
            name = request.get("action") if isinstance(request, dict) else None
            response = await asyncio.to_thread(dispatch, request)
            log.info("uid=%s action=%s ok=%s", uid, name, response["ok"])
            await self._reply(writer, response)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        await writer.drain()

    def _prepare_socket_path(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    def _secure_socket(self) -> None:
        gid = -1
        try:
            gid = grp.getgrnam(self.group).gr_gid
        except KeyError:
            log.warning("group %r not found; socket stays owner-only", self.group)
        if os.geteuid() == 0 and gid != -1:
            os.chown(self.socket_path, 0, gid)
            os.chown(self.socket_path.parent, 0, gid)
            os.chmod(self.socket_path.parent, 0o750)  # noqa: S103  (group = panel user)
        os.chmod(self.socket_path, 0o660 if gid != -1 else 0o600)  # noqa: S103

    async def serve(self) -> None:
        self._prepare_socket_path()
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(
                self.handle, path=str(self.socket_path), limit=MAX_REQUEST_BYTES
            )
        finally:
            os.umask(old_umask)
        self._secure_socket()
        log.info(
            "listening on %s (%d actions: %s)",
            self.socket_path,
            len(registry.names()),
            ", ".join(registry.names()),
        )
        async with server:
            await server.serve_forever()
