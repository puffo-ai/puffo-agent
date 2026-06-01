"""In-memory ring buffer of recent log records, polled by the UI.

Per-agent log files don't exist (only ``runtime.json`` heartbeats), so
a stdlib ``logging.Handler`` attached to the root logger captures the
daemon's stream into a deque the QTimer-driven log view drains.
"""
from __future__ import annotations

import logging
from collections import deque
from threading import Lock
from typing import Deque


class LogRingHandler(logging.Handler):
    def __init__(self, maxlen: int = 500) -> None:
        super().__init__()
        self._buf: Deque[str] = deque(maxlen=maxlen)
        self._lock = Lock()
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lock:
            self._buf.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._buf)


def install_log_buffer(maxlen: int = 500) -> LogRingHandler:
    handler = LogRingHandler(maxlen=maxlen)
    logging.getLogger().addHandler(handler)
    return handler
