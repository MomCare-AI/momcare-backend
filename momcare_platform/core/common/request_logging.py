"""JSON log formatting and per-request correlation context.

Two ``ContextVar``s carry per-request identity into every log line emitted
anywhere in the call stack (views, services, Django internals) without
threading it through every function signature. Both are set/reset by
middleware in ``middleware.py`` — this module only defines the shape.
"""

from __future__ import annotations

import datetime
from contextvars import ContextVar

from pythonjsonlogger.json import JsonFormatter as BaseJsonFormatter

request_id_ctx: ContextVar[str | None] = ContextVar("request_id_ctx", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id_ctx", default=None)

# Standard LogRecord attributes — anything NOT in this set that survives
# JsonFormatter's own field merging is a caller-supplied `extra={...}` kwarg.
_RECORD_ATTRS = frozenset(
    {
        "args", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
        "name", "pathname", "process", "processName", "relativeCreated",
        "stack_info", "thread", "threadName", "taskName", "asctime",
    }
)


class JsonFormatter(BaseJsonFormatter):
    """Renders every log record as one JSON line with a fixed field order.

    Order: timestamp, level, request_id, user_id, message, logger, func,
    line, then any caller-supplied ``extra={...}`` fields, then
    exc_info/stack_info when present. ``request_id``/``user_id`` are omitted
    when no request is in flight (e.g. a management command) or the request
    is unauthenticated.
    """

    def add_fields(self, log_data, record, message_dict):
        super().add_fields(log_data, record, message_dict)

        extras = {k: v for k, v in log_data.items() if k not in _RECORD_ATTRS}
        message = log_data.get("message", record.getMessage())
        exc_info = log_data.get("exc_info")
        stack_info = log_data.get("stack_info")

        log_data.clear()

        dt = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
        log_data["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
        log_data["level"] = record.levelname

        request_id = request_id_ctx.get()
        if request_id is not None:
            log_data["request_id"] = request_id

        user_id = user_id_ctx.get()
        if user_id is not None:
            log_data["user_id"] = user_id

        log_data["message"] = message
        log_data["logger"] = record.name
        log_data["func"] = record.funcName
        log_data["line"] = record.lineno

        log_data.update(extras)

        if exc_info:
            log_data["exc_info"] = exc_info
        if stack_info:
            log_data["stack_info"] = stack_info
