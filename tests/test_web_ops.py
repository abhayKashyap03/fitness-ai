"""Ops surface: background jobs, diagnostics, db tools, and the CSRF guard.

No network: every job exercised here is either deterministic or fails fast on
absent credentials — which is itself the behaviour worth pinning (a dead source
must be reported, never silently swallowed).
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi", reason="web UI needs the optional [web] extra")

from fastapi.testclient import TestClient

from coach.config import Settings
from coach.web.app import create_app
from coach.web.jobs import JobBusy, JobRunner


@pytest.fixture
def settings(db_path):
    return Settings(
        db_path=db_path,
        user_id=1,
        home_tz="America/New_York",
        units="metric",
        log_level="INFO",
    )


@pytest.fixture
def client(migrated_conn, settings, acknowledged):
    """``acknowledged`` pre-accepts the §8.6 disclaimer — see conftest."""
    migrated_conn.commit()
    return TestClient(create_app(settings))


def _await_job(client, job_id, timeout=10.0):
    """Poll until the job leaves 'running' (jobs are threads, not requests)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


# ---- JobRunner ---------------------------------------------------------------


def test_job_runner_records_success_and_lines():
    runner = JobRunner()
    job = runner.submit("t", lambda emit: emit("hello"))
    for _ in range(200):
        if runner.get(job.id).status != "running":
            break
        time.sleep(0.01)
    done = runner.get(job.id)
    assert done.status == "ok"
    assert done.lines == ["hello"]
    assert done.error is None


def test_job_runner_records_failure_instead_of_swallowing_it():
    runner = JobRunner()

    def boom(emit):
        raise ValueError("kaboom")

    job = runner.submit("t", boom)
    for _ in range(200):
        if runner.get(job.id).status != "running":
            break
        time.sleep(0.01)
    done = runner.get(job.id)
    assert done.status == "failed"
    assert "kaboom" in done.error


def test_job_runner_is_single_flight_per_name():
    """Two concurrent syncs would double-ingest and race the normalizer."""
    runner = JobRunner()
    gate = __import__("threading").Event()
    runner.submit("sync", lambda emit: gate.wait(5))
    try:
        with pytest.raises(JobBusy):
            runner.submit("sync", lambda emit: None)
    finally:
        gate.set()


def test_job_runner_allows_different_names_concurrently():
    runner = JobRunner()
    gate = __import__("threading").Event()
    runner.submit("sync", lambda emit: gate.wait(5))
    try:
        other = runner.submit("normalize", lambda emit: None)
        assert other.name == "normalize"
    finally:
        gate.set()


# ---- job routes --------------------------------------------------------------


def test_normalize_job_runs_to_completion(client):
    started = client.post("/api/jobs/normalize", json={}).json()
    done = _await_job(client, started["id"])
    assert done["status"] == "ok"
    assert any("incremental" in ln for ln in done["lines"])


def test_rebuild_job_reports_the_rebuild_mode(client):
    started = client.post("/api/jobs/normalize", json={"rebuild": True}).json()
    done = _await_job(client, started["id"])
    assert done["status"] == "ok"
    assert any("rebuild" in ln for ln in done["lines"])


def test_sync_job_completes_with_all_sources_skipped(client):
    """No credentials configured: sync must skip cleanly, not fail."""
    started = client.post("/api/jobs/sync", json={}).json()
    done = _await_job(client, started["id"])
    assert done["status"] == "ok"
    assert any("skipped" in ln for ln in done["lines"])


def test_ingest_job_fails_loudly_without_credentials(client):
    started = client.post("/api/jobs/ingest/whoop", json={}).json()
    done = _await_job(client, started["id"])
    assert done["status"] == "failed"
    assert "WHOOP" in done["error"]


def test_ingest_rejects_an_unknown_source(client):
    assert client.post("/api/jobs/ingest/garmin", json={}).status_code == 404


def test_second_run_of_a_live_job_is_refused(client):
    runner = client.app.state.jobs
    gate = __import__("threading").Event()
    runner.submit("normalize", lambda emit: gate.wait(5))
    try:
        assert client.post("/api/jobs/normalize", json={}).status_code == 409
    finally:
        gate.set()


def test_unknown_job_id_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_jobs_listing(client):
    client.post("/api/jobs/normalize", json={})
    assert client.get("/api/jobs").json()["jobs"]


# ---- CSRF guard --------------------------------------------------------------


def test_cross_origin_state_change_is_refused(client):
    """The server is unauthenticated; a foreign page must not drive it."""
    r = client.post("/api/jobs/normalize", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_same_origin_state_change_is_allowed(client):
    r = client.post(
        "/api/jobs/normalize",
        json={},
        headers={"Origin": "http://testserver", "Host": "testserver"},
    )
    assert r.status_code == 200


def test_read_only_routes_are_not_origin_guarded(client):
    assert client.get("/api/doctor", headers={"Origin": "http://evil.example"}).status_code == 200


# ---- diagnostics + db tools --------------------------------------------------


def test_doctor_reports_problems_without_credentials(client):
    body = client.get("/api/doctor").json()
    assert body["ok"] is False
    keys = {c["key"] for c in body["checks"]}
    assert {"schema", "whoop", "mfp", "llm"} <= keys
    # schema is migrated in this fixture, so that check must pass
    schema = next(c for c in body["checks"] if c["key"] == "schema")
    assert schema["status"] == "ok"


def test_doctor_never_exposes_a_secret(db_path, migrated_conn, acknowledged):
    migrated_conn.commit()
    cfg = Settings(
        db_path=db_path,
        user_id=1,
        home_tz="America/New_York",
        units="metric",
        log_level="INFO",
        google_api_key="SUPER-SECRET-VALUE",
    )
    acknowledged()  # §8.6 gate — see conftest; this test is about secret leakage
    body = TestClient(create_app(cfg)).get("/api/doctor").json()
    assert "SUPER-SECRET-VALUE" not in str(body)
    llm = next(c for c in body["checks"] if c["key"] == "llm")
    assert llm["status"] == "ok"  # configured, without revealing the key


def test_doctor_page_renders(client):
    assert client.get("/doctor").status_code == 200


def test_ops_page_renders(client):
    assert client.get("/ops").status_code == 200


def test_db_verify_reports_integrity(client):
    body = client.get("/api/db/verify").json()
    assert body["integrity"] == "ok"
    assert body["fk_violations"] == 0
    assert body["counts"]


def test_db_backup_writes_a_file(client, tmp_path):
    body = client.post("/api/db/backup").json()
    from pathlib import Path

    assert Path(body["path"]).exists()
    assert body["bytes"] > 0


def test_eval_hrv_is_deterministic_and_honest(client):
    body = client.get("/api/eval/hrv").json()
    # empty DB -> can't judge either way; must say so, not guess
    assert body["hrv_days"] == 0
    assert body["verdict"] == "insufficient"
    assert body["rationale"]
