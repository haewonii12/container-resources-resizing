"""JSON logs to stdout with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys

_PATTERNS = [re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
             re.compile(r"(?i)((?:token|api[_-]?key|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{4,}"),
             re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")]
_STD = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime"}


class Redactor:
    def __init__(self):
        self.values: list[str] = []

    def add(self, *values: str) -> None:
        self.values.extend(v for v in values if v and len(v) >= 4 and v not in self.values)

    def __call__(self, text: str) -> str:
        for v in self.values:
            text = text.replace(v, "[REDACTED]")
        for pat in _PATTERNS:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]" + ("@" if m.group(0).endswith("@") else ""), text)
        return text


REDACT = Redactor()


class JsonFormatter(logging.Formatter):
    def __init__(self, run_id: str = ""):
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                   "logger": record.name, "run_id": self.run_id, "msg": record.getMessage()}
        for key, value in vars(record).items():
            if key not in _STD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return REDACT(json.dumps(payload, ensure_ascii=False, default=str))


def setup(run_id: str, level: str = "INFO", stream=None) -> logging.Logger:
    root = logging.getLogger("rightsizing")
    root.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(run_id))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    return root
