"""Atomic gzip snapshots and cancellation-safe cross-loop cache guards."""

import asyncio
from contextlib import asynccontextmanager
import gzip
import json
import os
from pathlib import Path
import tempfile


def write_gzip_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as raw:
            temporary = Path(raw.name)
            with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
                compressed.write(json.dumps(payload, allow_nan=False).encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@asynccontextmanager
async def async_guard(lock):
    # Waiting must not occupy executor threads that a producer needs for DNS
    # or disk writes. A cancelled waiter never owns an outstanding acquire.
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.025)
    try:
        yield
    finally:
        lock.release()
