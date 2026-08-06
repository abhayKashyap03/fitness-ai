"""WHOOP BLE discovery classification (ADR-0012 spike).

No radio, no `bleak`: the scan and probe calls need hardware and are not tested.
What IS tested is the judgement layer — how a device gets called a candidate and
how the acceptance gate is worded — because that is where a spike lies to you.

A spike that reports what it hoped for is worse than no spike, so the cases
below are mostly about NOT over-claiming: a WHOOP-ish name is not the protocol,
a failed connection is not an absent service, and a 4.0 is not a 5.0.
"""

from __future__ import annotations

from coach.adapters.whoop_ble.discover import Candidate, ProbeResult

FD4B = "fd4b0001-9acc-4d9a-9b2a-6f5c9a4b1e01"
FD4B_CHAR = "fd4b0002-9acc-4d9a-9b2a-6f5c9a4b1e01"
SIX108 = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"


# ---- scan classification ---------------------------------------------------


def test_a_device_advertising_fd4b_is_a_candidate():
    """The strongest possible pre-connection signal: the strap says which
    protocol family it speaks in the advertisement itself."""
    c = Candidate(address="X", name=None, rssi=-50, service_uuids=[FD4B])
    assert c.advertises_5x_family is True
    assert c.likely_whoop is True
    assert "fd4b" in c.why


def test_the_short_16_bit_form_counts_too():
    """Some stacks surface the advertised service as the 16-bit short UUID."""
    c = Candidate(address="X", name=None, rssi=-50, service_uuids=["fd4b"])
    assert c.advertises_5x_family is True


def test_uuid_case_does_not_matter():
    c = Candidate(address="X", name=None, rssi=-50, service_uuids=[FD4B.upper()])
    assert c.advertises_5x_family is True


def test_a_whoop_ish_name_is_a_candidate_but_not_protocol_evidence():
    """Worth surfacing, worth not over-claiming. A name is a hint; only the
    service family answers ADR-0012's question."""
    c = Candidate(address="X", name="WHOOP 5B2", rssi=-60, service_uuids=[])
    assert c.likely_whoop is True
    assert c.advertises_5x_family is False
    assert "name matches" in c.why


def test_an_unrelated_device_is_not_a_candidate():
    c = Candidate(address="X", name="Someone's AirPods", rssi=-70, service_uuids=["180f"])
    assert c.likely_whoop is False
    assert c.why == "no WHOOP indicators"


def test_an_unnamed_device_does_not_crash_the_name_check():
    """Most BLE advertisements carry no name at all."""
    c = Candidate(address="X", name=None, rssi=None, service_uuids=[])
    assert c.likely_whoop is False


# ---- the acceptance gate ---------------------------------------------------


def test_the_gate_passes_only_on_the_fd4b_family():
    r = ProbeResult(address="X", connected=True, services={FD4B: [FD4B_CHAR]})
    assert r.family_5x == [FD4B]
    assert r.verdict.startswith("GATE PASSED")
    assert "1 characteristic" in r.verdict


def test_connected_but_no_fd4b_is_a_distinct_finding():
    """The interesting negative: the strap is reachable and does NOT speak the
    protocol whoop-vault documented. That points at the MG firmware, which is
    exactly the residual risk ADR-0012 named."""
    r = ProbeResult(address="X", connected=True, services={"180a": ["2a29"]})
    assert r.verdict.startswith("GATE NOT PASSED")
    assert "MG firmware differs" in r.verdict or "differs" in r.verdict


def test_a_failed_connection_is_not_an_absent_service():
    """Collapsing these into 'failed' would waste the spike — they have
    completely different next steps."""
    r = ProbeResult(address="X", connected=False, error="TimeoutError: no response")
    assert r.verdict.startswith("COULD NOT CONNECT")
    assert "TimeoutError" in r.verdict
    assert r.family_5x == []


def test_a_40_strap_is_called_out_specifically():
    """6108 + CRC8 is the 4.0 family. Reporting it as 'no fd4b' would send
    someone debugging firmware when they are holding the wrong device."""
    r = ProbeResult(address="X", connected=True, services={SIX108: []})
    assert "4.0" in r.verdict
    assert r.family_40 == [SIX108]
