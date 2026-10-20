"""Placeholder job ingest loop.

v1 ships with data/sample_jobs.json seeded on API startup.
Later: pull UK sponsor-linked listings + NO/FI feeds into SQLite.
"""

from __future__ import annotations

import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("guide.worker")


def main() -> None:
    logger.info("guide-worker idle stub — job ingest not implemented yet")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
