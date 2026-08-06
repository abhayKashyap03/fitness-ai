"""Record a live heart-rate session from the strap and store it raw.

Subscribes to the standard SIG Heart Rate Measurement characteristic (0x2A37),
collects notifications for a bounded window, and writes the session **verbatim**
to ``raw_events`` (§2.1). Nothing is computed on the way in; the normalizer
derives HRV and heart rate from the stored bytes, so improving that math later
means re-deriving history rather than re-recording it.

Two deliberate shapes:

* **One raw event per session, not per beat.** A beat is not independently
  meaningful and 1 Hz for ten minutes would be 600 rows describing one act of
  measurement. The session is the fact.
* **Frames are kept as decoded samples plus their original hex.** The hex is
  the true raw (§2.1 — never clean on the way in); the decoded form is a
  convenience that a rebuild could regenerate from it if the parser improves.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ...store.raw import insert_raw_event
from .heartrate import HeartRateMeasurement, parse_measurement

HR_MEASUREMENT_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"


@dataclass
class Session:
    started_at: str
    samples: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def beats(self) -> int:
        return len(self.samples)

    @property
    def rr_count(self) -> int:
        return sum(len(s.get("rr_ms") or []) for s in self.samples)


async def _record(address: str, seconds: float, connect_timeout: float) -> Session:
    from bleak import BleakClient

    session = Session(started_at=datetime.now(UTC).isoformat())
    done = asyncio.Event()

    def _on_notify(_sender, data: bytearray) -> None:
        raw = bytes(data)
        at = datetime.now(UTC).isoformat()
        try:
            m: HeartRateMeasurement = parse_measurement(raw)
        except ValueError as exc:
            # Recorded, never dropped silently. A run that quietly discarded a
            # third of its frames would produce a confident HRV number from a
            # sample nobody knew was thinned.
            session.errors.append(f"{at}: {exc} (hex={raw.hex()})")
            return
        session.samples.append(
            {
                "at": at,
                "hex": raw.hex(),  # the true raw (§2.1)
                "bpm": m.bpm,
                "rr_ms": [round(v, 3) for v in m.rr_intervals_ms],
                "contact": m.sensor_contact,
            }
        )

    async with BleakClient(address, timeout=connect_timeout) as client:
        await client.start_notify(HR_MEASUREMENT_CHAR, _on_notify)
        try:
            await asyncio.wait_for(done.wait(), timeout=seconds)
        except TimeoutError:
            pass  # the window elapsing IS the stop condition
        finally:
            # A disconnect race on the way out is not a failure — the samples
            # are already collected and the window is over.
            with contextlib.suppress(Exception):
                await client.stop_notify(HR_MEASUREMENT_CHAR)
    return session


def record_session(
    address: str, *, seconds: float = 300.0, connect_timeout: float = 20.0
) -> Session:
    """Collect heart-rate notifications for ``seconds``. Requires the radio."""
    return asyncio.run(_record(address, seconds, connect_timeout))


def store_session(
    conn: sqlite3.Connection,
    session: Session,
    *,
    utc_offset: str | None = None,
    user_id: int = 1,
) -> tuple[str, bool]:
    """Write one recorded session to ``raw_events``. Returns ``(row_id, inserted)``.

    ``external_id`` is the session start instant, which makes re-storing the
    same session idempotent while allowing many sessions per day.
    """
    payload = {
        "started_at": session.started_at,
        "ended_at": datetime.now(UTC).isoformat(),
        "utc_offset": utc_offset,
        "characteristic": HR_MEASUREMENT_CHAR,
        "samples": session.samples,
        # Kept in the payload rather than logged away: how much was dropped is
        # part of what this measurement is worth.
        "parse_errors": session.errors,
    }
    return insert_raw_event(
        conn,
        source="whoop_ble",
        record_type="hr_session",
        payload=payload,
        external_id=session.started_at,
        recorded_at=session.started_at,
        user_id=user_id,
    )
