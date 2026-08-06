"""Adapter B — local Bluetooth read of the WHOOP strap (ADR-0012).

The subscription-survival path. WHOOP's cloud API dies with the membership; the
strap does not, and it speaks BLE to anyone who knows the protocol.

**This package is the hardware spike, not the adapter yet.** ADR-0012 gates the
whole thing on one empirical question — does the *user's own MG-variant strap*
expose the `fd4b` service family that whoop-vault and NOOP documented on
Maverick-variant 5.0 hardware — and refuses to let any ingest code ship before
that is answered. So what exists here is exactly enough to answer it:
:func:`scan` and :func:`probe`.

Nothing in this package is imported by the core CLI path. ``bleak`` lives behind
the optional ``[ble]`` extra, and every import of it is deferred into the
function that needs it, so a user who never touches Bluetooth never pays for it
and the test suite runs without a radio.
"""
