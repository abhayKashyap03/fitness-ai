"""Environment/config/data sanity as DATA (the `doctor` report).

Extracted from the CLI so every consumer renders the same diagnosis instead of
reimplementing it — the same seam pattern as :mod:`coach.services.sync`.

**Read-only by contract.** Diagnosing a broken install must never change it: in
particular this never creates the ``schema_version`` table as a side effect of
looking at an unmigrated file, and it never prints or returns a secret value
(§8.4) — only whether one is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConfigError, Settings
from ..store import db

OK = "ok"
WARN = "warn"
PROBLEM = "problem"


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    status: str  # OK | WARN | PROBLEM
    detail: str


@dataclass(frozen=True)
class RawCount:
    source: str
    rows: int
    last_ingest: str | None  # None = never


@dataclass(frozen=True)
class DoctorReport:
    db_path: str
    checks: list[Check] = field(default_factory=list)
    raw_counts: list[RawCount] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return sum(1 for c in self.checks if c.status == PROBLEM)

    @property
    def ok(self) -> bool:
        return self.problems == 0


RAW_SOURCES = ("whoop_api", "healthkit", "myfitnesspal")


def doctor_report(settings: Settings) -> DoctorReport:
    """Diagnose the install. Never mutates anything, never exposes a secret."""
    checks: list[Check] = []
    raw_counts: list[RawCount] = []

    if not settings.db_path.exists():
        checks.append(
            Check("schema", "schema", PROBLEM, "database missing — run `coach db init`")
        )
    else:
        conn = db.connect(settings.db_path)
        try:
            # Read-only: current_version() would CREATE schema_version, which
            # would be a side effect of merely looking.
            has_schema = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone()
                is not None
            )
            if has_schema:
                version = db.current_version(conn)
                pending = db.pending_migrations(conn)
            else:
                version = 0
                pending = db.discover_migrations()

            if pending:
                checks.append(
                    Check(
                        "schema",
                        "schema",
                        PROBLEM,
                        f"v{version} — {len(pending)} migration(s) pending; run `coach db init`",
                    )
                )
            else:
                checks.append(Check("schema", "schema", OK, f"v{version} (up to date)"))
                for source in RAW_SOURCES:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, MAX(ingested_at) AS last "
                        "FROM raw_events WHERE source=?",
                        (source,),
                    ).fetchone()
                    raw_counts.append(
                        RawCount(source=source, rows=row["n"], last_ingest=row["last"])
                    )
        finally:
            conn.close()

    checks.append(_whoop_check(settings))

    export = Path("apple_health_export/export.xml")
    checks.append(
        Check(
            "hk_export",
            "apple health export",
            OK,
            "present" if export.exists() else "not found (optional)",
        )
    )

    checks.append(_mfp_check(settings))
    checks.append(_llm_check(settings))
    return DoctorReport(db_path=str(settings.db_path), checks=checks, raw_counts=raw_counts)


def _whoop_check(settings: Settings) -> Check:
    from ..adapters.whoop.auth import TokenStore
    from ..paths import whoop_token_path

    try:
        settings.require_whoop()
    except ConfigError:
        return Check(
            "whoop", "whoop", PROBLEM, "not configured (WHOOP_CLIENT_ID/SECRET in .env)"
        )
    store = TokenStore(whoop_token_path(settings.user_id))
    tokens = store.load() if store.exists() else None
    if tokens is None:
        return Check("whoop", "whoop", PROBLEM, "credentials ok, token MISSING — run `coach auth whoop`")
    if tokens.is_expired():
        return Check(
            "whoop",
            "whoop",
            WARN,
            f"token expired {tokens.expires_at.isoformat()} (auto-refreshes on use)",
        )
    return Check("whoop", "whoop", OK, f"token valid until {tokens.expires_at.isoformat()}")


def _mfp_check(settings: Settings) -> Check:
    try:
        settings.require_mfp()
    except ConfigError:
        return Check(
            "mfp",
            "myfitnesspal",
            WARN,
            "no session cookie (MFP_SESSION_COOKIE) — food/weight sync will skip",
        )
    return Check("mfp", "myfitnesspal", OK, "session cookie configured")


def _llm_check(settings: Settings) -> Check:
    """Reports only whether a key is configured — never the key itself (§8.4)."""
    from ..coach.llm import build_provider

    try:
        settings.require_llm()
        provider = build_provider(
            settings.llm_provider, settings.llm_api_key, model=settings.coach_model
        )
    except ConfigError as exc:
        return Check("llm", "coach llm", PROBLEM, f"not configured — {exc}")
    return Check("llm", "coach llm", OK, f"{provider.name}/{provider.model} (key configured)")
