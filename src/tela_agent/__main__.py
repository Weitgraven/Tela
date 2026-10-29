"""Entry point: python -m tela_agent"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from .server import AgentServer, allowed_uids

log = logging.getLogger("tela_agent")


async def watch_htaccess(interval: float = 30) -> None:
    from . import settings
    from .actions.websites import HtaccessWatch

    watch = HtaccessWatch()
    while True:
        try:
            if await asyncio.to_thread(watch.check, settings.load()):
                log.info("an .htaccess file changed; reloaded the web server")
        except Exception:  # noqa: BLE001 - keep watching whatever happens
            log.exception("checking .htaccess files failed")
        await asyncio.sleep(interval)


async def run(server: AgentServer) -> None:
    watcher = asyncio.create_task(watch_htaccess())
    try:
        await server.serve()
    finally:
        watcher.cancel()


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
        asyncio.run(run(server))


if __name__ == "__main__":
    main()
