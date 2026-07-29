"""Per-record-type incremental watermarks (auto_since_by_type).

Regression: the MIN-across-types watermark meant a QUIET type (no workouts
logged for days) pinned every other type's window open, so each sync re-fetched
days already stored — forever.
"""

from __future__ import annotations

from coach.adapters.whoop.ingest import auto_since, auto_since_by_type


def _raw(conn, record_type: str, recorded_at: str, ext: str) -> None:
    conn.execute(
        "INSERT INTO raw_events (id, user_id, source, record_type, external_id, "
        "payload, payload_hash, ingested_at, recorded_at) "
        "VALUES (?, 1, 'whoop_api', ?, ?, '{}', ?, '2026-07-27T00:00:00+00:00', ?)",
        (f"raw:{ext}", record_type, ext, ext, recorded_at),
    )


def test_each_type_resumes_from_its_own_watermark(migrated_conn):
    # recovery is current; workout is 4 days stale because none were logged
    _raw(migrated_conn, "recovery", "2026-07-27T13:00:00Z", "r1")
    _raw(migrated_conn, "sleep", "2026-07-27T12:00:00Z", "s1")
    _raw(migrated_conn, "workout", "2026-07-23T23:00:00Z", "w1")
    migrated_conn.commit()

    by_type = auto_since_by_type(migrated_conn, overlap_days=2)
    assert by_type["recovery"].startswith("2026-07-25")
    assert by_type["sleep"].startswith("2026-07-25")
    assert by_type["workout"].startswith("2026-07-21")

    # the old behaviour: ONE window, dragged back by the quiet type
    assert auto_since(migrated_conn, overlap_days=2).startswith("2026-07-21")


def test_empty_db_yields_no_watermarks(migrated_conn):
    assert auto_since_by_type(migrated_conn) == {}


def test_types_without_a_timestamp_are_omitted(migrated_conn):
    """body_measurement is dateless — it must not fabricate a window."""
    _raw(migrated_conn, "recovery", "2026-07-27T13:00:00Z", "r1")
    migrated_conn.execute(
        "INSERT INTO raw_events (id, user_id, source, record_type, external_id, "
        "payload, payload_hash, ingested_at, recorded_at) "
        "VALUES ('raw:b1', 1, 'whoop_api', 'body_measurement', NULL, '{}', 'b1', "
        "'2026-07-27T00:00:00+00:00', NULL)"
    )
    migrated_conn.commit()
    by_type = auto_since_by_type(migrated_conn)
    assert "body_measurement" not in by_type
    assert "recovery" in by_type


def test_ingest_uses_each_types_own_window(migrated_conn):
    """The mapping must reach the client per endpoint, not one window for all."""
    from coach.adapters.whoop.ingest import ingest_whoop

    seen: dict[str, str] = {}

    class FakeClient:
        def get_recovery(self, since, until=None):
            seen["recovery"] = since
            return []

        def get_cycles(self, since, until=None):
            seen["cycle"] = since
            return []

        def get_sleep(self, since, until=None):
            seen["sleep"] = since
            return []

        def get_workouts(self, since, until=None):
            seen["workout"] = since
            return []

        def get_body_measurement(self):
            return {"ok": True}

    ingest_whoop(
        migrated_conn,
        FakeClient(),
        since={
            "recovery": "2026-07-25",
            "cycle": "2026-07-25",
            "sleep": "2026-07-25",
            "workout": "2026-07-21",
        },
    )
    assert seen["recovery"] == "2026-07-25"
    assert seen["workout"] == "2026-07-21"


def test_a_bare_string_still_applies_one_window_to_all(migrated_conn):
    """Explicit backfill (`--since`) must keep working unchanged."""
    from coach.adapters.whoop.ingest import ingest_whoop

    seen: dict[str, str] = {}

    class FakeClient:
        def get_recovery(self, since, until=None):
            seen["recovery"] = since
            return []

        def get_cycles(self, since, until=None):
            seen["cycle"] = since
            return []

        def get_sleep(self, since, until=None):
            seen["sleep"] = since
            return []

        def get_workouts(self, since, until=None):
            seen["workout"] = since
            return []

        def get_body_measurement(self):
            return {"ok": True}

    ingest_whoop(migrated_conn, FakeClient(), since="2026-01-01")
    assert set(seen.values()) == {"2026-01-01"}


def test_a_type_missing_from_the_mapping_falls_back_to_the_earliest(migrated_conn):
    """A brand-new record type must be fetched, never silently skipped."""
    from coach.adapters.whoop.ingest import ingest_whoop

    seen: dict[str, str] = {}

    class FakeClient:
        def get_recovery(self, since, until=None):
            seen["recovery"] = since
            return []

        def get_cycles(self, since, until=None):
            seen["cycle"] = since
            return []

        def get_sleep(self, since, until=None):
            seen["sleep"] = since
            return []

        def get_workouts(self, since, until=None):
            seen["workout"] = since
            return []

        def get_body_measurement(self):
            return {"ok": True}

    ingest_whoop(migrated_conn, FakeClient(), since={"recovery": "2026-07-25"})
    assert seen["workout"] == "2026-07-25"  # earliest known, not skipped
