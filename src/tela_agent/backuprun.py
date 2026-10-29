"""Run backups outside the agent.

python -m tela_agent.backuprun scheduled    # from tela-backup.timer
python -m tela_agent.backuprun job <id>     # started by the agent (systemd-run)
"""

from __future__ import annotations

import os
import sys

from . import (
    actions,  # noqa: F401  (registers every kind of job)
    backups,
    settings,
)


def main(argv: list[str]) -> int:
    os.umask(0o077)
    paths = settings.load()
    if argv[:1] == ["scheduled"]:
        for job_id in backups.run_scheduled(paths):
            job = backups.load_job(paths, job_id)
            print(f"{job_id}: {job['status']} {job['message']}")
        return 0
    if len(argv) == 2 and argv[0] == "job":
        job = backups.execute(paths, argv[1])
        print(f"{job['id']}: {job['status']} {job['message']}")
        return 0 if job["status"] in ("ok", "partial") else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
