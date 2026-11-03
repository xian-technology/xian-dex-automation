"""Bounded UTC operational diagnostics without payloads or exception messages."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import traceback
import unicodedata
from datetime import UTC, datetime
from itertools import islice

SERVICE = "dex-automation"
CONTEXT_FIELDS = (
    "pid",
    "event",
    "reason",
    "operation",
    "request_id",
    "tool",
    "rule_id",
    "pair_id",
    "tx_hash",
    "chain_id",
    "retry_seconds",
    "config_generation",
    "attempts",
    "suppressed",
    "error_type",
    "errno",
    "status_code",
    "error_trace",
)
_PRINTF = re.compile(r"%(?:\([^)]+\))?[#0\- +]*([0-9*]*)(?:\.([0-9*]*))?[hlL]?[diouxXeEfFgGcrsa%]")


def safe_scalar(value, limit=256):
    """Do not call arbitrary repr/str methods or stringify unbounded integers."""
    if type(value) is str:
        return value[:limit]
    if type(value) is int:
        return value if value.bit_length() <= 256 else "<large-int>"
    if type(value) is float:
        return value if math.isfinite(value) else "<nonfinite-float>"
    if value is None or type(value) is bool:
        return value
    return f"<{type(value).__name__[:64]}>"


def _escape_character(char):
    if unicodedata.category(char).startswith("C") or char in "\u2028\u2029|":
        code = ord(char)
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return char


def one_line(value, limit=2048):
    text = str(safe_scalar(value, limit))
    text = re.sub(r"https?://[^\s]+", "<endpoint>", text)
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    return "".join(_escape_character(char) for char in escaped)


def _safe_format(message):
    for match in _PRINTF.finditer(message):
        for width in match.groups():
            if width and ("*" in width or len(width) > 3 or int(width) > 256):
                return False
    return True


def _message(record):
    message = str(safe_scalar(record.msg, 2048))
    if not record.args:
        return message
    if not _safe_format(message):
        return "Log format exceeded width limit"
    if type(record.args) is tuple:
        args = tuple(safe_scalar(arg, 128) for arg in record.args[:16])
    elif type(record.args) is dict:
        keys = re.findall(r"%\(([^)]+)\)", message)[:16]
        args = {key: safe_scalar(record.args.get(key), 128) for key in keys}
    else:
        return message
    try:
        return message % args
    except TypeError, ValueError, OverflowError:
        return message + " [invalid log arguments]"


def _errors(exc):
    pending, seen = [exc], set()
    for _ in range(8):
        if not pending:
            return
        error = pending.pop(0)
        if id(error) in seen:
            continue
        seen.add(id(error))
        yield error
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions[:8])
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if cause is not None:
            pending.append(cause)


def _numeric_code(error, key):
    value = getattr(error, key, None)
    if key == "status_code" and value is None:
        value = getattr(getattr(error, "response", None), "status_code", None)
    return value if type(value) is int and value.bit_length() <= 32 else None


def exception_context(exc):
    """Bounded chain/group locations; walk_tb never reads source lines or locals."""
    errors = list(_errors(exc))
    context = {"error_type": ",".join(type(error).__name__[:64] for error in errors)}
    frames = []
    for error in errors:
        for frame, line in islice(traceback.walk_tb(error.__traceback__), 16):
            filename = frame.f_code.co_filename[-256:].replace("\\", "/").rsplit("/", 1)[-1]
            frames.append(f"{filename}:{line}:{frame.f_code.co_name[:64]}")
    if frames:
        context["error_trace"] = " > ".join(frames)[:2048]
    for error in errors:
        for key in ("errno", "status_code"):
            value = _numeric_code(error, key)
            if value is not None:
                context.setdefault(key, value)
    return context


def _context_fields(context):
    fields = []
    for key in CONTEXT_FIELDS:
        if key in context:
            value = safe_scalar(context[key], 2048 if key == "error_trace" else 256)
            text = one_line(value, 2048)
            fields.append(
                f"{key}={text}" if re.fullmatch(r"[A-Za-z0-9_.:/-]+", text) else f'{key}="{text}"'
            )
    return " ".join(fields)


class ServiceFormatter(logging.Formatter):
    def __init__(self, service=SERVICE):
        super().__init__()
        self.service = service

    def format(self, record):
        context = {key: record.__dict__[key] for key in CONTEXT_FIELDS if key in record.__dict__}
        context["pid"] = record.process
        if record.exc_info and record.exc_info[1]:
            context.update(exception_context(record.exc_info[1]))
        stamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        line = (
            f"{stamp.replace('+00:00', 'Z')} | {one_line(record.levelname, 16)} | "
            f"{self.service}/{one_line(record.name, 128)} | {one_line(_message(record), 4096)}"
        )
        fields = _context_fields(context)
        if fields:
            line += " | " + fields
        return line.encode("utf-8")[:8192].decode("utf-8", errors="ignore")


def configure_logging(level="INFO"):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ServiceFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        child = logging.getLogger(name)
        child.handlers = []
        child.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class RetryReporter:
    """Log the first repeating failure, then counts at most once per minute."""

    def __init__(self, logger, clock=None):
        import time

        self.logger = logger
        self.clock = clock or time.monotonic
        self.attempts = 0
        self.suppressed = 0
        self.last_report = float("-inf")

    def failed(self, exc, **context):
        self.attempts += 1
        now = self.clock()
        if now - self.last_report < 60:
            self.suppressed += 1
            return
        self.logger.error(
            "Automation worker failed; retrying active configuration",
            extra={
                **exception_context(exc),
                **context,
                "reason": "worker_task_failed",
                "operation": "run_configuration",
                "retry_seconds": 1,
                "attempts": self.attempts,
                "suppressed": self.suppressed,
            },
        )
        self.last_report = now
        self.suppressed = 0

    def recovered(self):
        if self.attempts:
            self.logger.info(
                "Automation event processing recovered",
                extra={
                    "reason": "worker_recovered",
                    "attempts": self.attempts,
                    "suppressed": self.suppressed,
                },
            )
            self.attempts = self.suppressed = 0
            self.last_report = float("-inf")
