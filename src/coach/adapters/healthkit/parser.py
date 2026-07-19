"""Streaming parser for an Apple Health `export.xml` (T5.2).

The export is ~1 GB / ~1.7M records, so we **never load it into memory**:
``iterparse`` + element clearing keeps usage flat. Only nutrition + body
records are yielded; everything else (Apple Watch / iPhone / WHOOP, workouts,
HR, steps, sleep) is skipped. Malformed records are logged and skipped, never
fatal.

Metadata is read from each ``Record``'s ``<MetadataEntry>`` children BEFORE the
element is cleared — clearing a child on its own end event blanks it (a real bug
found during recon; see docs/healthkit-export-notes.md).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import is_wanted_type

log = logging.getLogger("coach.healthkit.parser")

# Top-level containers to clear so the tree doesn't grow (Records are ~99%).
_CLEARABLE = frozenset(
    {
        "Record",
        "Workout",
        "ActivitySummary",
        "Correlation",
        "Me",
        "Audiogram",
        "VisionPrescription",
        "ClinicalRecord",
    }
)


@dataclass(frozen=True)
class HKRecord:
    """One Apple Health ``<Record>`` we care about.

    ``value`` is parsed to float when possible, else None (malformed/blank).
    ``metadata`` maps ``MetadataEntry`` key -> value (e.g. ``HKTimeZone``,
    ``meal``, ``HKFoodType``, ``HKExternalUUID``).
    """

    type: str
    source_name: str
    unit: str | None
    value: float | None
    start_date: str | None
    end_date: str | None
    creation_date: str | None
    metadata: dict[str, str]


def _to_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _open_export(source: str | Path | IO[bytes]) -> tuple[IO[bytes], zipfile.ZipFile | None]:
    """Return a binary stream over ``export.xml``.

    Accepts a path to ``.xml`` or ``.zip`` (Apple's ``export.zip`` nests
    ``.../export.xml``), or an already-open binary file object. Returns the
    stream plus the owning ZipFile (to close later) or None.
    """
    if hasattr(source, "read"):
        return source, None
    path = Path(source)  # type: ignore[arg-type]
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        names = [
            n
            for n in zf.namelist()
            if n.endswith("export.xml") and not n.endswith("export_cda.xml")
        ]
        if not names:
            zf.close()
            raise ValueError(f"No export.xml found inside {path}")
        return zf.open(names[0]), zf
    return open(path, "rb"), None


def iter_records(
    source: str | Path | IO[bytes],
    *,
    wanted: Callable[[str], bool] = is_wanted_type,
) -> Iterator[HKRecord]:
    """Stream ``HKRecord``s (nutrition + body) from an Apple Health export.

    ``wanted`` filters by record ``type``; defaults to dietary + body types.
    """
    stream, zf = _open_export(source)
    try:
        context = ET.iterparse(stream, events=("end",))
        cleared = 0
        root: ET.Element | None = None
        for _event, elem in context:
            if root is None:
                root = elem
            tag = elem.tag
            if tag == "Record":
                rtype = elem.get("type")
                if rtype and wanted(rtype):
                    rec = _build_record(elem, rtype)
                    if rec is not None:
                        yield rec
            if tag in _CLEARABLE:
                elem.clear()
                cleared += 1
                # periodically drop finished siblings to keep memory flat
                if root is not None and cleared % 20000 == 0:
                    root.clear()
    finally:
        stream.close()
        if zf is not None:
            zf.close()


def _build_record(elem: ET.Element, rtype: str) -> HKRecord | None:
    try:
        metadata = {
            m.get("key"): m.get("value", "") for m in elem.findall("MetadataEntry") if m.get("key")
        }
        return HKRecord(
            type=rtype,
            source_name=elem.get("sourceName") or "",
            unit=elem.get("unit"),
            value=_to_float(elem.get("value")),
            start_date=elem.get("startDate"),
            end_date=elem.get("endDate"),
            creation_date=elem.get("creationDate"),
            metadata=metadata,  # type: ignore[arg-type]
        )
    except Exception as exc:  # never let one bad record kill the import
        log.warning("skipping malformed HealthKit record (%s): %s", rtype, exc)
        return None
