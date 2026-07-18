"""Config loading: defaults, validation, fail-loud on missing required vars."""

from __future__ import annotations

import pytest

from coach.config import ConfigError, load_settings


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "COACH_DB_PATH": "/tmp/x/coach.db",
        "COACH_USER_ID": "1",
        "COACH_HOME_TZ": "America/New_York",
        "COACH_UNITS": "metric",
        "COACH_LOG_LEVEL": "INFO",
    }
    env.update(overrides)
    return env


def test_defaults_applied_for_empty_env():
    s = load_settings(env={}, load_dotenv_file=False)
    assert s.user_id == 1
    assert s.units == "metric"
    assert s.home_tz == "America/New_York"
    assert str(s.db_path).endswith("data/coach.db")


def test_bad_user_id_raises_naming_var():
    with pytest.raises(ConfigError, match="COACH_USER_ID"):
        load_settings(env=_base_env(COACH_USER_ID="not-an-int"), load_dotenv_file=False)


def test_bad_units_raises_naming_var():
    with pytest.raises(ConfigError, match="COACH_UNITS"):
        load_settings(env=_base_env(COACH_UNITS="stone"), load_dotenv_file=False)


def test_require_whoop_names_missing_vars():
    s = load_settings(env=_base_env(), load_dotenv_file=False)
    with pytest.raises(ConfigError) as exc:
        s.require_whoop()
    msg = str(exc.value)
    assert "WHOOP_CLIENT_ID" in msg
    assert "WHOOP_CLIENT_SECRET" in msg


def test_require_whoop_passes_when_present():
    s = load_settings(
        env=_base_env(WHOOP_CLIENT_ID="cid", WHOOP_CLIENT_SECRET="secret"),
        load_dotenv_file=False,
    )
    s.require_whoop()  # should not raise


def test_secrets_not_in_repr():
    s = load_settings(
        env=_base_env(WHOOP_CLIENT_ID="cid", WHOOP_CLIENT_SECRET="topsecret"),
        load_dotenv_file=False,
    )
    assert "topsecret" not in repr(s)
    assert "cid" not in repr(s)


def test_relative_db_path_resolved_to_repo_root():
    s = load_settings(env=_base_env(COACH_DB_PATH="./data/x.db"), load_dotenv_file=False)
    assert s.db_path.is_absolute()
