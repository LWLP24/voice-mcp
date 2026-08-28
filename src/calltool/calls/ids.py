from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_ulid(value: int) -> str:
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(encoded)


def new_id(prefix: str) -> str:
    """Return a sortable ULID-style identifier with a domain prefix."""
    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    return f"{prefix}_{_encode_ulid((timestamp_ms << 80) | randomness)}"
