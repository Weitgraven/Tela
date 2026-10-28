"""Read-only system information."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from ..registry import action

# Services the dashboard reports on. Keys are display names, values are systemd units.
SERVICES = {
    "Web server": "lsws",
    "Database": "mariadb",
    "Panel": "tela-panel",
    "Agent": "tela-agent",
}


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _uptime_seconds() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def _memory() -> dict[str, int]:
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                info[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return {"total": 0, "used": 0}
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return {"total": total, "used": max(total - available, 0)}


def _service_state(unit: str) -> str:
    if not shutil.which("systemctl"):
        return "unknown"
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    state = out.stdout.strip()
    return state or "unknown"


@action("system.status")
def status(_params: dict[str, Any]) -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()
    return {
        "hostname": socket.gethostname(),
        "os": _os_name(),
        "kernel": platform.release(),
        "uptime": _uptime_seconds(),
        "cpus": os.cpu_count() or 1,
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "memory": _memory(),
        "disk": {"total": disk.total, "used": disk.used},
        "services": [
            {"name": name, "unit": unit, "state": _service_state(unit)}
            for name, unit in SERVICES.items()
        ],
    }


@action("agent.ping")
def ping(_params: dict[str, Any]) -> str:
    return "pong"
