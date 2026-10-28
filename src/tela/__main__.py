"""Entry point.

python -m tela              run the panel web server
python -m tela <command>    run a CLI command (see `python -m tela --help`)
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1:
        from .cli import main as cli_main

        sys.exit(cli_main(sys.argv[1:]))

    import uvicorn

    from .app import create_app
    from .config import load

    config = load()
    ssl_args = {}
    if config.tls_enabled:
        ssl_args = {"ssl_certfile": str(config.tls_cert), "ssl_keyfile": str(config.tls_key)}
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        proxy_headers=False,
        server_header=False,
        log_level="info",
        **ssl_args,
    )


if __name__ == "__main__":
    main()
