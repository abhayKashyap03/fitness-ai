"""Sync orchestration — degradation contract, no network, no credentials.

The whole point of extracting this from the CLI: one dead source must never
cost you the others, and the outcome must be inspectable DATA (so a scheduler
or API can consume it) rather than printed text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coach.adapters.myfitnesspal.auth import MfpAuthError
from coach.adapters.whoop.auth import ReauthRequired
from coach.config import Settings
from coach.services.sync import run_sync


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "c.db",
        user_id=1,
        home_tz="America/New_York",
        units="metric",
        log_level="INFO",
    )


def _by_name(result):
    return {s.name: s for s in result.sources}


def _boom_whoop(_s):
    raise AssertionError("client must not be built when the source is skipped")


def _boom_mfp(_s):
    raise AssertionError("client must not be built when the source is skipped")


def test_unconfigured_sources_are_skipped_not_fatal(migrated_conn, settings):
    # no whoop creds, no mfp cookie -> both skipped, normalize still runs
    res = run_sync(
        migrated_conn,
        settings,
        whoop_client=_boom_whoop,
        mfp_client=_boom_mfp,
        today="2026-06-15",
    )
    got = _by_name(res)
    assert got["whoop"].skipped == "not configured"
    assert got["mfp"].skipped == "not configured"
    assert got["whoop"].counts == {} and got["mfp"].counts == {}
    # normalize ran regardless — the daily driver still refreshes canonical
    assert "recovery" in res.normalized


def test_auth_failure_in_one_source_does_not_kill_the_other(migrated_conn, settings, monkeypatch):
    configured = Settings(
        **{
            **settings.__dict__,
            "whoop_client_id": "id",
            "whoop_client_secret": "sec",
            "mfp_session_cookie": "ck",
        }
    )
    # both have a prior-ingest watermark so they attempt a real ingest
    monkeypatch.setattr(
        "coach.services.sync.whoop_auto_since_by_type",
        lambda _c: {"recovery": "2026-06-01", "workout": "2026-06-01"},
    )
    monkeypatch.setattr("coach.services.sync.mfp_auto_since", lambda _c: "2026-06-01")

    def whoop_dies(_s):
        raise ReauthRequired("token expired")

    captured = {}

    def mfp_ok(_s):
        captured["built"] = True
        return object()

    monkeypatch.setattr(
        "coach.services.sync.ingest_mfp",
        lambda *a, **k: {
            "diary": {"inserted": 2, "skipped": 0},
            "weight": {"inserted": 1, "skipped": 0},
            "days": 2,
        },
    )
    res = run_sync(
        migrated_conn,
        configured,
        whoop_client=whoop_dies,
        mfp_client=mfp_ok,
        today="2026-06-15",
    )
    got = _by_name(res)
    assert "auth needed" in got["whoop"].skipped  # WHOOP degraded...
    assert got["mfp"].skipped is None  # ...MFP still ran
    assert got["mfp"].counts["diary"]["inserted"] == 2
    assert captured["built"]


def test_mfp_auth_error_is_caught_too(migrated_conn, settings, monkeypatch):
    configured = Settings(**{**settings.__dict__, "mfp_session_cookie": "ck"})
    monkeypatch.setattr("coach.services.sync.mfp_auto_since", lambda _c: "2026-06-01")

    def mfp_dies(_s):
        raise MfpAuthError("cookie expired")

    res = run_sync(
        migrated_conn,
        configured,
        whoop_client=_boom_whoop,
        mfp_client=mfp_dies,
        today="2026-06-15",
    )
    assert "auth needed" in _by_name(res)["mfp"].skipped


def test_healthkit_only_runs_when_a_path_is_given(migrated_conn, settings):
    res = run_sync(
        migrated_conn,
        settings,
        whoop_client=_boom_whoop,
        mfp_client=_boom_mfp,
        today="2026-06-15",
    )
    assert "healthkit" not in _by_name(res)  # not part of the daily path

    res2 = run_sync(
        migrated_conn,
        settings,
        whoop_client=_boom_whoop,
        mfp_client=_boom_mfp,
        today="2026-06-15",
        hk_file=Path("/nope/missing.xml"),
    )
    assert "export not found" in _by_name(res2)["healthkit"].skipped


# ---- cron-readiness: does a failure actually look like a failure? -----------


def test_an_unconfigured_source_is_not_a_failure(migrated_conn, settings):
    """A source you don't use must not fail the nightly job forever."""
    res = run_sync(
        migrated_conn,
        settings,
        whoop_client=_boom_whoop,
        mfp_client=_boom_mfp,
        today="2026-05-01",
    )
    assert [s.skipped for s in res.sources] == ["not configured", "not configured"]
    assert res.ok is True
    assert res.attention == []


def test_a_dead_credential_is_a_failure(migrated_conn, settings, monkeypatch):
    """The bug this field exists for.

    Before ``needs_attention``, "not configured" and "auth needed" were the same
    skipped string and the same exit code — so a WHOOP token revoked last
    Tuesday would ingest nothing every night and cron would report success every
    night. Silence has to mean "nothing to do", never "it stopped working".
    """
    from coach.services import sync as S

    monkeypatch.setattr(Settings, "require_whoop", lambda self: None, raising=False)
    monkeypatch.setattr(S, "whoop_auto_since_by_type", lambda conn: {"recovery": "2026-04-01"})

    def _revoked(_s):
        raise ReauthRequired("refresh token rejected")

    res = run_sync(
        migrated_conn,
        settings,
        whoop_client=_revoked,
        mfp_client=_boom_mfp,
        today="2026-05-01",
    )
    whoop = _by_name(res)["whoop"]
    assert whoop.needs_attention is True
    assert "auth needed" in whoop.skipped
    assert res.ok is False
    assert [s.name for s in res.attention] == ["whoop"]


def test_a_never_ingested_source_needs_attention(migrated_conn, settings, monkeypatch):
    """It is configured and doing nothing — which cron would otherwise call fine."""
    from coach.services import sync as S

    monkeypatch.setattr(Settings, "require_whoop", lambda self: None, raising=False)
    monkeypatch.setattr(S, "whoop_auto_since_by_type", lambda conn: {})

    res = run_sync(
        migrated_conn,
        settings,
        whoop_client=_boom_whoop,
        mfp_client=_boom_mfp,
        today="2026-05-01",
    )
    assert _by_name(res)["whoop"].needs_attention is True
    assert res.ok is False
