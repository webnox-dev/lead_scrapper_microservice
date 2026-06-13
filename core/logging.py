"""Structured logging configuration."""

import logging
import sys
from typing import Any

import structlog
from structlog.processors import JSONRenderer


def configure_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.ExtraAdder(),
    ]

    if debug:
        # Console output for development
        structlog.configure(
            processors=shared_processors + [
                structlog.dev.ConsoleRenderer(colors=True)
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                logging.DEBUG if debug else logging.INFO
            ),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        # JSON output for production
        structlog.configure(
            processors=shared_processors + [
                structlog.processors.dict_tracebacks,
                JSONRenderer()
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )

    # Configure stdlib logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if debug else logging.INFO,
    )

    # Suppress verbose third-party loggers to keep output clean and legible
    for verbose_logger in (
        "sqlalchemy",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "httpcore",
        "httpx",
        "asyncio",
        "playwright",
        "urllib3",
    ):
        logging.getLogger(verbose_logger).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)
