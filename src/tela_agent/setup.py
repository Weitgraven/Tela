"""Installer hook: python -m tela_agent.setup

Brings the OpenLiteSpeed configuration in line with Tela's site state. Safe to
run any number of times. The installer restarts the web server afterwards.
"""

from __future__ import annotations

import sys

from . import ols, settings
from .actions import dns
from .actions.websites import load_sites


def main() -> int:
    paths = settings.load()
    if not paths.httpd_config.exists():
        print(
            f"error: {paths.httpd_config} not found; is OpenLiteSpeed installed?", file=sys.stderr
        )
        return 1
    for required in (paths.default_cert, paths.default_key):
        if not required.exists():
            print(f"error: {required} is missing", file=sys.stderr)
            return 1
    sites = load_sites(paths)
    written = ols.write(sites, paths)
    print(f"web server config updated for {len(sites)} site(s), {len(written)} file(s) written")

    pdns = dns.powerdns(paths)
    if pdns.available:
        zones = dns._render_local(paths, dns.load_settings(paths))
        pdns.write(zones)
        pdns.reload()
        print(f"DNS server config updated for {len(zones)} zone(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
