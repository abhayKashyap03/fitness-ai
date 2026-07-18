"""Travel-proof time helpers (CLAUDE.md §2.6).

Every instant is a UTC ISO-8601 string; the local day it belongs to
(``day_key``) is derived from that instant PLUS the local UTC offset — never
from a naive local clock. Day-boundary bugs are an expected failure class here,
so this logic is small, pure, and heavily tested.

WHOOP supplies a UTC *offset* (e.g. ``-05:00``), not an IANA zone name. Per
CLAUDE.md §2.6 (D1) the offset is stored in its own ``utc_offset`` column and
``tz_name`` stays strictly IANA (NULL when unknown) — never overloaded with an
offset. ``day_key`` is derived from instant + offset and is exact regardless.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone


def parse_instant(iso: str) -> datetime:
    """Parse an ISO-8601 instant into an aware UTC datetime.

    Accepts a trailing ``Z`` and fractional seconds.
    """
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_offset(offset: str | None) -> timezone:
    """Parse a WHOOP offset string (``-05:00`` / ``+01:00`` / ``Z``) to a tzinfo."""
    if not offset or offset in {"Z", "+00:00", "-00:00"}:
        return UTC
    sign = 1 if offset[0] == "+" else -1
    hh, mm = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def to_utc_iso(dt: datetime) -> str:
    """Serialize an aware datetime to a UTC ISO-8601 string with ``+00:00``."""
    return dt.astimezone(UTC).isoformat()


def day_key(instant_iso: str, offset: str | None) -> str:
    """The local physiological-day date (``YYYY-MM-DD``) for a UTC instant.

    The instant is shifted into the local offset before the date is taken, so a
    22:00 EST workout that is 03:00 UTC the next day still belongs to the local
    day it was performed.
    """
    dt = parse_instant(instant_iso).astimezone(parse_offset(offset))
    return dt.date().isoformat()


def normalize_offset(offset: str | None) -> str | None:
    """Canonicalize a UTC-offset string for the ``utc_offset`` column.

    ``Z`` becomes ``+00:00``; a missing offset stays ``None`` (absence is
    absence, §2.7). Never returns an IANA name — that column is ``tz_name``.
    """
    if not offset:
        return None
    if offset == "Z":
        return "+00:00"
    return offset
