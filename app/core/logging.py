"""Logging setup.

Modules across the app call `logger.exception(...)` on failures they deliberately
swallow — a mail server that will not answer, an OpenAI outage, a reply a
platform refused. Until this existed none of that was configured, so those calls
landed on stderr at the root logger's default level with no timestamp we
controlled and no way to raise or lower verbosity. The decision to log a failure
rather than leak it to the caller only pays off if the log is somewhere.

JSON when LOG_FORMAT=json, so a log shipper can parse it; plain text otherwise,
because reading JSON in a terminal is miserable.
"""

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything passed as extra={...}, minus the attributes every record has.
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime", "message", "taskName",
}


def configure(level: str = "INFO", fmt: str = "text") -> None:
    """Install a single stdout handler on the root logger.

    Replaces any existing handlers rather than adding to them, so calling this
    twice — the API and the worker share it — cannot double every line.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Uvicorn installs its own handlers; let them propagate to ours instead so
    # request logs and application logs come out in one format.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

    # SQLAlchemy logs every statement at INFO when echoing; it is off by
    # default, but keep it from becoming the loudest thing here if enabled.
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")
