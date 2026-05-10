"""RFC 8785 canonical JSON serialization."""

from __future__ import annotations

import json


def canonicalize(value: object) -> bytes:
    buf = bytearray()
    _write(buf, value)
    return bytes(buf)


def canonicalize_for_signing(value: object) -> bytes:
    if isinstance(value, dict):
        stripped = {k: v for k, v in value.items() if k not in ("signature", "self_signature")}
        return canonicalize(stripped)
    return canonicalize(value)


def _write(buf: bytearray, value: object) -> None:
    if value is None:
        buf.extend(b"null")
    elif isinstance(value, bool):
        buf.extend(b"true" if value else b"false")
    elif isinstance(value, int):
        buf.extend(str(value).encode())
    elif isinstance(value, float):
        buf.extend(_format_float(value).encode())
    elif isinstance(value, str):
        _write_string(buf, value)
    elif isinstance(value, list):
        buf.extend(b"[")
        for i, item in enumerate(value):
            if i > 0:
                buf.extend(b",")
            _write(buf, item)
        buf.extend(b"]")
    elif isinstance(value, dict):
        keys = sorted(value.keys(), key=_utf16_sort_key)
        buf.extend(b"{")
        for i, key in enumerate(keys):
            if i > 0:
                buf.extend(b",")
            _write_string(buf, key)
            buf.extend(b":")
            _write(buf, value[key])
        buf.extend(b"}")


def _utf16_sort_key(s: str) -> list[int]:
    return list(s.encode("utf-16-le"))


def _write_string(buf: bytearray, s: str) -> None:
    buf.extend(b'"')
    for ch in s:
        cp = ord(ch)
        if ch == '"':
            buf.extend(b'\\"')
        elif ch == '\\':
            buf.extend(b'\\\\')
        elif ch == '\b':
            buf.extend(b'\\b')
        elif ch == '\f':
            buf.extend(b'\\f')
        elif ch == '\n':
            buf.extend(b'\\n')
        elif ch == '\r':
            buf.extend(b'\\r')
        elif ch == '\t':
            buf.extend(b'\\t')
        elif cp < 0x20:
            buf.extend(f"\\u{cp:04x}".encode())
        else:
            buf.extend(ch.encode("utf-8"))
    buf.extend(b'"')


def _format_float(f: float) -> str:
    if f == int(f) and not (f == 0 and str(f).startswith("-")):
        return str(int(f))
    return repr(f)
