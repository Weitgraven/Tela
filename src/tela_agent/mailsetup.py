"""Installer hook: python -m tela_agent.mailsetup

Writes the Postfix, Dovecot and Rspamd configuration from Tela's mail state.
The installer restarts the services afterwards.
"""

from __future__ import annotations

import sys

from . import settings
from .actions.mail import setup_server
from .registry import ActionError


def main() -> int:
    try:
        print(setup_server(settings.load()))
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
