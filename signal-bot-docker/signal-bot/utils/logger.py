"""utils/logger.py — Structured logger"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
import structlog

Path("logs").mkdir(exist_ok=True)

_logger = logging.getLogger("signal_bot")
if not _logger.handlers:
    _logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    fh = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024,
                              backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    _logger.addHandler(ch)
    _logger.addHandler(fh)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log = structlog.get_logger("signal_bot")
