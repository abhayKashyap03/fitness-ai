"""Scan for and probe a WHOOP strap over BLE — ADR-0012's acceptance gate.

Two questions, in order:

1. **Is the strap visible?** (:func:`scan`) A WHOOP advertises a recognisable
   name, and on 5.x it advertises the `fd4b` service family in the advertisement
   data itself — which means the gate can often be answered without connecting
   at all.
2. **Does it speak the protocol we can read?** (:func:`probe`) Connect and
   enumerate GATT. ADR-0012's residual risk is that the user's MG variant
   differs from the r52 "Maverick" firmware whoop-vault was built against, and
   the honest way to find out is to list what the strap actually exposes rather
   than to assume.

Every classification here is **evidence-based and explicitly uncertain where the
evidence is** (§2.7). A device that looks like a WHOOP is reported as a
candidate, not as a WHOOP; a probe that finds no `fd4b` service says so plainly
rather than falling back to a guess. The point of a spike is to learn the truth,
and a spike that reports what it hoped for is worse than no spike.

Nothing here writes to the database. Ingest is deliberately not built until the
gate passes (ADR-0012 §4).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

# The 5.0 / MG service family. NOOP documents the split explicitly:
#   61080001-… + CRC8         -> WHOOP 4.0
#   fd4b0001-… + CRC16-Modbus -> WHOOP 5.0 / MG
# whoop-vault's characteristics live at fd4b0002-0007 under this service.
SERVICE_5X_PREFIX = "fd4b0001"
SERVICE_40_PREFIX = "61080001"

# Short-form 16-bit UUID as it appears in advertisement data on some stacks.
ADV_SHORT_5X = "fd4b"

_NAME_HINTS = ("whoop", "wh-", "strap")


@dataclass(frozen=True)
class Candidate:
    """A nearby BLE device that might be the strap.

    ``address`` is a MAC on Linux/Windows and an opaque CoreBluetooth UUID on
    macOS — the platform decides, and it is only ever passed straight back to
    :func:`probe`, never parsed.
    """

    address: str
    name: str | None
    rssi: int | None
    service_uuids: list[str] = field(default_factory=list)

    @property
    def advertises_5x_family(self) -> bool:
        """True if the advertisement itself carries the 5.0/MG service family.

        When this is true the ADR-0012 gate is already substantially answered
        without connecting — the strap is telling us which protocol it speaks.
        """
        return any(
            u.lower().startswith(SERVICE_5X_PREFIX) or u.lower() == ADV_SHORT_5X
            for u in self.service_uuids
        )

    @property
    def name_looks_like_whoop(self) -> bool:
        return bool(self.name) and any(h in (self.name or "").lower() for h in _NAME_HINTS)

    @property
    def likely_whoop(self) -> bool:
        return self.advertises_5x_family or self.name_looks_like_whoop

    @property
    def why(self) -> str:
        """Why this device is being shown — so the operator can judge it too."""
        reasons = []
        if self.advertises_5x_family:
            reasons.append("advertises the fd4b (5.0/MG) service family")
        if self.name_looks_like_whoop:
            reasons.append(f"name matches {self.name!r}")
        return "; ".join(reasons) or "no WHOOP indicators"


async def _scan(seconds: float) -> list[Candidate]:
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    out: list[Candidate] = []
    for device, adv in found.values():
        out.append(
            Candidate(
                address=device.address,
                name=adv.local_name or device.name,
                rssi=adv.rssi,
                service_uuids=list(adv.service_uuids or []),
            )
        )
    # Strongest signal first: the strap on your wrist should outrank a
    # neighbour's headphones, and the operator reads the top of the list.
    out.sort(key=lambda c: (not c.likely_whoop, -(c.rssi if c.rssi is not None else -999)))
    return out


def scan(seconds: float = 8.0) -> list[Candidate]:
    """Every nearby BLE device, most-likely-WHOOP first.

    Returns *everything*, not only matches. A scan that silently filters is
    useless for the case that actually matters — "I don't see my strap" — where
    the useful information is what you DID see.
    """
    return asyncio.run(_scan(seconds))


@dataclass(frozen=True)
class ProbeResult:
    address: str
    connected: bool
    services: dict[str, list[str]] = field(default_factory=dict)  # service uuid -> char uuids
    error: str | None = None

    @property
    def family_5x(self) -> list[str]:
        return [u for u in self.services if u.lower().startswith(SERVICE_5X_PREFIX)]

    @property
    def family_40(self) -> list[str]:
        return [u for u in self.services if u.lower().startswith(SERVICE_40_PREFIX)]

    @property
    def verdict(self) -> str:
        """ADR-0012's acceptance gate, stated in one line.

        Deliberately distinguishes "connected and the protocol is absent" from
        "could not connect". They are different findings with different next
        steps, and collapsing them into "failed" would waste the spike.
        """
        if not self.connected:
            return f"COULD NOT CONNECT — {self.error or 'unknown error'}"
        if self.family_5x:
            n = sum(len(self.services[u]) for u in self.family_5x)
            return f"GATE PASSED — fd4b (5.0/MG) service family present, {n} characteristic(s)"
        if self.family_40:
            return "UNEXPECTED — this looks like a WHOOP 4.0 (6108 family), not a 5.0/MG"
        return (
            "GATE NOT PASSED — connected, but no fd4b service. Either this is not "
            "the strap, or this MG firmware differs from the r52 Maverick build "
            "whoop-vault was written against."
        )


async def _probe(address: str, timeout: float) -> ProbeResult:
    from bleak import BleakClient

    try:
        async with BleakClient(address, timeout=timeout) as client:
            services: dict[str, list[str]] = {}
            for service in client.services:
                services[service.uuid] = [c.uuid for c in service.characteristics]
            return ProbeResult(address=address, connected=True, services=services)
    except Exception as exc:  # bleak raises a wide family; all of them are "no"
        # Reported, never swallowed. "Probe found nothing" and "probe could not
        # run" must not look the same to the person reading the output.
        return ProbeResult(address=address, connected=False, error=f"{type(exc).__name__}: {exc}")


def probe(address: str, timeout: float = 20.0) -> ProbeResult:
    """Connect and enumerate GATT. This is the acceptance test in ADR-0012 §4.

    Read-only: it connects, lists services and characteristics, and disconnects.
    It sends no commands to the strap. Pairing may prompt on the host, and on
    macOS the terminal application needs Bluetooth permission.
    """
    return asyncio.run(_probe(address, timeout))
