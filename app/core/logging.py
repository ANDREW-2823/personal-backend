from __future__ import annotations

import logging
import sys

from app.core.config import settings

_SECRET_KEYS = {"token", "api_key", "password", "authorization", "secret"}


class RedactFilter(logging.Filter):
    """Best-effort scrubbing of secrets from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for key in _SECRET_KEYS:
            if key in msg.lower() and "=" in msg:
                # Hide obvious key=value pairs.
                parts = []
                for token in msg.split():
                    if "=" in token and token.split("=", 1)[0].lower().strip("-") in _SECRET_KEYS:
                        parts.append(token.split("=", 1)[0] + "=***")
                    else:
                        parts.append(token)
                msg = " ".join(parts)
        record.msg = msg
        record.args = ()
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s :: %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
