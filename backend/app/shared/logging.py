"""One structured log format for every component and worker (#72).

Each line is a JSON object with ``time``, ``level``, ``component`` and
``message`` (``component`` is the process's unless the line passes its own
through ``extra``), plus any trace fields passed through ``extra``: ``correlationId``,
``eventId``, ``eventType``, ``scope`` and ``runId``. Following one
``correlationId`` across components is how the walking skeleton is checked.
"""

import json
import logging
from datetime import UTC, datetime

TRACE_FIELDS = ("correlationId", "eventId", "eventType", "scope", "runId")


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            # in-process calls (C05 -> C09/C10) share one process, so a line
            # may name its own component through ``extra={"component": ...}``
            "component": getattr(record, "component", None) or self._component,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in TRACE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                line[field] = str(value)
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line)


def configure_logging(component: str, level: int = logging.INFO) -> None:
    """Send JSON log lines for ``component`` (for example ``C07``) to stderr."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(component))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("pika").setLevel(logging.WARNING)


def trace(envelope) -> dict[str, str | None]:
    """``extra`` fields that tie a log line to an event envelope."""
    return {
        "correlationId": str(envelope.correlation_id),
        "eventId": str(envelope.event_id),
        "eventType": envelope.event_type,
        "scope": envelope.scope,
        "runId": str(envelope.run_id) if envelope.run_id else None,
    }
