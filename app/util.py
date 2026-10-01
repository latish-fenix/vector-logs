"""Small helpers: time and ids."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_ms(ms: int) -> str:
    return iso(datetime.fromtimestamp(ms / 1000, timezone.utc))


def new_id() -> str:
    return uuid.uuid4().hex
