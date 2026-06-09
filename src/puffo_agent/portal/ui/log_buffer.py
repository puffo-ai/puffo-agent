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
        # Monotonic count of every line ever emitted. The view diffs on
        # this (not on buffer length) so it keeps tailing once the ring
        # fills — otherwise length stays pinned and the view freezes.
        self._total = 0
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
            self._total += 1

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._buf)

    def counter(self) -> int:
        with self._lock:
            return self._total


def install_log_buffer(maxlen: int = 500) -> LogRingHandler:
    handler = LogRingHandler(maxlen=maxlen)
    logging.getLogger().addHandler(handler)
    return handler
