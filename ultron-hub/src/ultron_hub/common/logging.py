"""Logging setup: one line per event to stderr (journald) + rotating file.

stdout is never used here: it is reserved for the JSON result.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "ultron_hub"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(
    logs_dir: Path,
    level: str = "INFO",
    filename: str = "ultron-hub.log",
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure the package logger. Safe to call several times."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(level.upper())
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_dir / filename, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("file logging disabled: %s", exc)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Child logger of the package logger (e.g. get_logger('executor'))."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
