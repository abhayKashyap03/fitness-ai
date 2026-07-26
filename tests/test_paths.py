"""Credential path namespacing — tokens must never collide across users.

`user_id` is on every canonical row as multi-tenancy insurance (§2.4); token
FILES were the one place that ignored it, so a second user authorizing WHOOP
would silently overwrite the first user's refresh token.
"""

from __future__ import annotations

import json

import pytest

from coach import paths


@pytest.fixture
def creds(tmp_path, monkeypatch):
    """Point the credentials dir at a temp location (never touch the real one)."""
    monkeypatch.setattr(paths, "credentials_dir", lambda: tmp_path / ".credentials")
    return tmp_path / ".credentials"


def test_token_paths_are_namespaced_per_user(creds):
    assert paths.whoop_token_path(1) != paths.whoop_token_path(2)
    assert paths.mfp_token_path(1) != paths.mfp_token_path(2)
    assert paths.whoop_token_path(1).parent.name == "u1"
    assert paths.whoop_token_path(2).parent.name == "u2"
    # whoop and mfp stay distinct files within a user
    assert paths.whoop_token_path(1) != paths.mfp_token_path(1)


def test_two_users_tokens_do_not_overwrite_each_other(creds):
    for uid, secret in ((1, "user-one-token"), (2, "user-two-token")):
        p = paths.whoop_token_path(uid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"access_token": secret}))
    assert json.loads(paths.whoop_token_path(1).read_text())["access_token"] == "user-one-token"
    assert json.loads(paths.whoop_token_path(2).read_text())["access_token"] == "user-two-token"


def test_legacy_global_token_is_adopted_not_lost(creds):
    # a pre-namespacing install: token sitting directly in .credentials/
    creds.mkdir(parents=True, exist_ok=True)
    legacy = creds / "whoop_token.json"
    legacy.write_text(json.dumps({"access_token": "legacy"}))

    new = paths.whoop_token_path(1)
    assert new.exists()  # moved into u1/ — the live session keeps working
    assert json.loads(new.read_text())["access_token"] == "legacy"
    assert not legacy.exists()  # moved, not copied (no stale duplicate secret)


def test_existing_namespaced_token_wins_over_legacy(creds):
    new = paths.whoop_token_path(1)
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text(json.dumps({"access_token": "current"}))
    (creds / "whoop_token.json").write_text(json.dumps({"access_token": "stale"}))

    resolved = paths.whoop_token_path(1)
    assert json.loads(resolved.read_text())["access_token"] == "current"
