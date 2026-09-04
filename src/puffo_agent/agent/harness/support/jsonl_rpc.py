"""Small transport mechanics shared by persistent JSONL Drivers.

Protocol identity stays in each Driver: request ids, frame shapes, blank-line
policy, response errors, and dispatch are deliberately not modeled here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Hashable, MutableMapping
from typing import Any, TypeVar


RequestId = TypeVar("RequestId", bound=Hashable)


class RpcRequestTimeout(TimeoutError):
    """A registered request received no response before its deadline."""


class RpcFrameTooLarge(ValueError):
    """The stream reader cannot resynchronize after an oversized frame."""


def fail_pending_requests(
    pending: MutableMapping[RequestId, asyncio.Future[Any]], message: str
) -> None:
    """Fail and forget every request owned by a closing transport."""
    for future in pending.values():
        if not future.done():
            future.set_exception(RuntimeError(message))
    pending.clear()


async def await_rpc_response(
    pending: MutableMapping[RequestId, asyncio.Future[Any]],
    request_id: RequestId,
    *,
    send: Awaitable[None],
    timeout_seconds: float,
) -> Any:
    """Register before writing, then always remove the request on exit."""
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    try:
        await send
        try:
            return await asyncio.wait_for(future, timeout_seconds)
        except asyncio.TimeoutError:
            raise RpcRequestTimeout from None
    finally:
        pending.pop(request_id, None)


async def write_json_line(
    stdin: Any,
    lock: asyncio.Lock,
    frame: dict[str, Any],
) -> None:
    """Write one compact UTF-8 JSON object without interleaving writers."""
    encoded = (
        json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    )
    async with lock:
        stdin.write(encoded)
        await stdin.drain()


async def read_json_line(stdout: Any) -> bytes:
    """Read one framed record and name asyncio's limit failure."""
    try:
        return await stdout.readline()
    except ValueError:
        raise RpcFrameTooLarge from None


def decode_json_object(record: bytes) -> dict[str, Any]:
    """Decode a JSON record; callers retain their own framing policy."""
    frame = json.loads(record)
    if not isinstance(frame, dict):
        raise TypeError("JSONL protocol frame is not an object")
    return frame
