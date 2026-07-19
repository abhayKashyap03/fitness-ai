"""Apple Health export adapter — the ONLY HealthKit-aware code (§2.5).

Ingests nutrition (MyFitnessPal / Foodnoms) and body composition (smart scale)
from an Apple Health `export.xml`. Everything Apple-Watch/iPhone/WHOOP-sourced is
out of scope (WHOOP is covered by its own API adapter). See
docs/healthkit-export-notes.md for the observed structure this is built against.
"""

DIETARY_PREFIX = "HKQuantityTypeIdentifierDietary"

BODY_TYPES = frozenset(
    {
        "HKQuantityTypeIdentifierBodyMass",
        "HKQuantityTypeIdentifierBodyFatPercentage",
        "HKQuantityTypeIdentifierBodyMassIndex",
        "HKQuantityTypeIdentifierLeanBodyMass",
    }
)


def is_wanted_type(record_type: str) -> bool:
    """True for the dietary + body-composition types we ingest; False otherwise."""
    return record_type.startswith(DIETARY_PREFIX) or record_type in BODY_TYPES
