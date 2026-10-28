"""Entry point: python -m tela_agent"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from .server import AgentServer, allowed_uids


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("TELA_AGENT_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
    )
    socket_path = Path(os.environ.get("TELA_AGENT_SOCKET", "/run/tela/agent.sock"))
    panel_user = os.environ.get("TELA_PANEL_USER", "tela")
    uids = allowed_uids(panel_user, os.environ.get("TELA_AGENT_ALLOW_UIDS"))
    server = AgentServer(socket_path, group=panel_user, uids=uids)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
